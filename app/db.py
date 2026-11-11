"""SQLite builder (CSV + synthetic overlay) and the read-only query executor."""
from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("msim.db")

SCHEMA_VERSION = "2"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT = re.compile(r"^-?\d+$")

# Single source of truth for SQL functions: used by the validator AND the SQLite authorizer.
ALLOWED_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "date", "datetime", "dense_rank", "group_concat",
    "glob", "ifnull", "iif", "instr", "julianday", "lag", "lead", "length", "like", "lower",
    "ltrim", "max", "min", "nullif", "printf", "rank", "replace", "round", "row_number",
    "rtrim", "strftime", "substr", "substring", "sum", "total", "trim", "upper",
    "likely", "unlikely",
})
HIDDEN_TABLES = frozenset({"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema",
                           "sqlite_sequence", "_build_info"})

NATURAL_KEYS = {
    "fact_nav": [("fund_id", "nav_date")],
    "fact_cash_balance": [("fund_id", "balance_date")],
    "fact_revenue": [("rev_id",), ("fund_id", "month")],
    "fact_flows": [("flow_id",), ("fund_id", "month")],
    "fact_recon_break": [("break_id",)],
    "dim_date": [("date",)],
    "fact_flow_detail": [("flow_id",)],
    "fact_sla_history": [("hist_id",)],
    "dim_fx_rate": [("rate_date", "ccy")],
}


class LoaderError(Exception):
    pass


class QueryError(Exception):
    """Sanitised SQL execution error (safe to show to the repair stage)."""


class QueryDenied(QueryError):
    """The SQLite authorizer blocked the statement (security category)."""


class QueryTimeout(QueryError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool = False
    elapsed_ms: float = 0.0


@dataclass
class DictRow:
    table: str
    column: str
    type: str
    key: str
    description: str


def read_dictionary(path: Path) -> list[DictRow]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [DictRow(r["table"], r["column"], r["type"].strip().upper(), (r.get("key") or "").strip(),
                        r.get("description") or "") for r in csv.DictReader(fh)]


def load_dictionaries(data_dir: Path, include_supplement: bool):
    """Return (base_rows, supp_rows)."""
    base = read_dictionary(data_dir / "data_dictionary.csv")
    supp: list[DictRow] = []
    p = data_dir / "supplementary" / "data_dictionary_supplementary.csv"
    if include_supplement and p.exists():
        supp = read_dictionary(p)
    return base, supp


def _convert(v: str, typ: str, table: str, col: str):
    if v == "" or v is None:
        return None
    if typ == "NUMBER":
        return int(v) if _INT.match(v) else float(v)
    if typ == "DATE" and not _ISO.match(v):
        raise LoaderError(f"{table}.{col}: non-ISO date value")
    return v


def _sqlite_type(t: str) -> str:
    return "NUMERIC" if t == "NUMBER" else "TEXT"


def fingerprint(data_dir: Path, include_supplement: bool) -> str:
    h = hashlib.sha256()
    files = sorted(data_dir.glob("*.csv"))
    if include_supplement:
        files += sorted((data_dir / "supplementary").glob("*.csv"))
    for p in files:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    h.update(f"{include_supplement}|{SCHEMA_VERSION}".encode())
    return h.hexdigest()


def build_database(data_dir, db_path, include_supplement: bool = True) -> dict:
    """Build the DB atomically. Returns {table: row_count}."""
    data_dir, db_path = Path(data_dir), Path(db_path)
    base, supp = load_dictionaries(data_dir, include_supplement)
    base_cols: dict[str, list[DictRow]] = {}
    for r in base:
        base_cols.setdefault(r.table, []).append(r)
    overlay_cols: dict[str, list[DictRow]] = {}
    new_tables: dict[str, list[DictRow]] = {}
    for r in supp:
        if r.table in base_cols:
            if any(b.column == r.column for b in base_cols[r.table]):
                raise LoaderError(f"{r.table}.{r.column} already in base dictionary")
            overlay_cols.setdefault(r.table, []).append(r)
        else:
            new_tables.setdefault(r.table, []).append(r)

    tmp = Path(str(db_path) + ".tmp")
    if tmp.exists():
        tmp.unlink()
    counts: dict[str, int] = {}
    skipped_rows: dict[str, int] = {}
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        plan: list[tuple[str, list[DictRow]]] = []
        for t, cols in base_cols.items():
            plan.append((t, cols + overlay_cols.get(t, [])))
        for t, cols in new_tables.items():
            plan.append((t, cols))
        for t, cols in plan:
            pks = [c.column for c in cols if c.key == "PK"]
            defs = []
            for c in cols:
                d = f'"{c.column}" {_sqlite_type(c.type)}'
                if c.column == "is_synthetic":
                    d += " NOT NULL DEFAULT 0"
                elif len(pks) == 1 and c.column == pks[0]:
                    d += " PRIMARY KEY"
                defs.append(d)
            con.execute(f'CREATE TABLE "{t}" ({", ".join(defs)})')

        for t, cols in plan:
            colnames = [c.column for c in cols]
            types = {c.column: c.type for c in cols}
            ph = ",".join("?" * len(colnames))
            ins = f'INSERT INTO "{t}" ({",".join(chr(34) + c + chr(34) for c in colnames)}) VALUES ({ph})'
            inserted = 0
            if t in base_cols:
                src = data_dir / f"{t}.csv"
                with src.open(newline="", encoding="utf-8-sig") as fh:
                    rd = csv.DictReader(fh)
                    bnames = {c.column for c in base_cols[t]}
                    if set(rd.fieldnames or []) != bnames:
                        raise LoaderError(f"{t}: CSV header does not match data dictionary")
                    n_csv = 0
                    for row in rd:
                        n_csv += 1
                        vals = [(_convert(row[c], types[c], t, c) if c in bnames else
                                 (0 if c == "is_synthetic" else None)) for c in colnames]
                        con.execute(ins, vals)
                        inserted += 1
                if inserted != n_csv:
                    raise LoaderError(f"{t}: loaded {inserted} of {n_csv} rows")
            counts[t] = inserted

        if include_supplement:
            for t, cols in plan:
                sp = data_dir / "supplementary" / f"{t}.csv"
                if not sp.exists():
                    continue
                colnames = [c.column for c in cols]
                types = {c.column: c.type for c in cols}
                declared = set(colnames)
                required = declared - {"is_synthetic"} if t in new_tables else \
                    {c.column for c in base_cols[t]}
                ins = f'INSERT INTO "{t}" ({",".join(chr(34) + c + chr(34) for c in colnames)}) VALUES ({",".join("?" * len(colnames))})'
                keysets = [tuple(k) for k in NATURAL_KEYS.get(t, [])]
                seen = [set(con.execute(f'SELECT {",".join(k)} FROM "{t}"').fetchall()) for k in keysets]
                seen_file = [set() for _ in keysets]
                skipped = n_csv = 0
                with sp.open(newline="", encoding="utf-8-sig") as fh:
                    rd = csv.DictReader(fh)
                    hdr = set(rd.fieldnames or [])
                    if not required <= hdr or not hdr <= declared:
                        raise LoaderError(f"{t}: supplementary header mismatch")
                    for row in rd:
                        n_csv += 1
                        vals = [(_convert(row.get(c, ""), types[c], t, c) if c != "is_synthetic" else 1)
                                for c in colnames]
                        kv = [tuple(vals[colnames.index(c)] for c in k) for k in keysets]
                        if any(k in sf for k, sf in zip(kv, seen_file)):
                            raise LoaderError(f"{t}: duplicate natural key inside supplementary file")
                        for k, sf in zip(kv, seen_file):
                            sf.add(k)
                        if any(k in s for k, s in zip(kv, seen)):
                            skipped += 1
                            continue
                        con.execute(ins, vals)
                        counts[t] += 1
                if skipped:
                    log.info("overlay %s: skipped %d rows with existing keys", t, skipped)
                skipped_rows[t] = skipped
        for t, cols in plan:
            for c in cols:
                if c.column in ("fund_id",) or c.type == "DATE" and c.column != "inception_date":
                    con.execute(f'CREATE INDEX "ix_{t}_{c.column}" ON "{t}" ("{c.column}")')
        con.execute("CREATE TABLE _build_info (fingerprint TEXT, use_supplement INTEGER)")
        con.execute("INSERT INTO _build_info VALUES (?,?)",
                    (fingerprint(data_dir, include_supplement), 1 if include_supplement else 0))
        for t, _ in plan:
            actual = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            counts[t] = actual
        con.commit()
    except BaseException:
        con.close()
        if tmp.exists():
            tmp.unlink()
        raise
    con.close()
    try:
        os.replace(tmp, db_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    log.info("built %s: %s", db_path.name, counts)
    build_database.last_skipped = skipped_rows  # type: ignore[attr-defined]
    return counts


def ensure_database(data_dir, db_path, include_supplement: bool = True) -> bool:
    """Rebuild when missing or stale. Returns True if (re)built."""
    data_dir, db_path = Path(data_dir), Path(db_path)
    want = fingerprint(data_dir, include_supplement)
    if db_path.exists():
        try:
            con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            try:
                row = con.execute("SELECT fingerprint FROM _build_info").fetchone()
            finally:
                con.close()
            if row and row[0] == want:
                return False
        except sqlite3.Error:
            pass
    db_path.parent.mkdir(parents=True, exist_ok=True)
    build_database(data_dir, db_path, include_supplement)
    return True


def table_row_counts(db_path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name <> '_build_info' ORDER BY name")]
        return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


def _authorizer(record: set | None):
    ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
    allowed_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                       sqlite3.SQLITE_RECURSIVE}

    def cb(action, arg1, arg2, dbname, source):
        if action not in allowed_actions:
            return deny
        if action == sqlite3.SQLITE_READ and (arg1 or "").lower() in HIDDEN_TABLES:
            return deny
        if action == sqlite3.SQLITE_FUNCTION:
            name = (arg2 or "").lower()
            if record is not None:
                record.add(name)
            if name not in ALLOWED_FUNCTIONS:
                return deny
        return ok
    return cb


def connect_ro(db_path, record_functions: set | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only=ON")
    con.set_authorizer(_authorizer(record_functions))
    return con


def _sanitise(msg: str) -> str:
    msg = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/[\w./-]+\.db", "<path>", msg)
    return msg[:300]


def run_query(db_path, sql: str, row_cap: int = 500, timeout_s: float = 5.0,
              record_functions: set | None = None) -> QueryResult:
    con = connect_ro(db_path, record_functions)
    deadline = time.monotonic() + timeout_s
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    t0 = time.monotonic()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(row_cap + 1)
    except sqlite3.DatabaseError as e:
        m = str(e)
        if "not authorized" in m or "authoriz" in m.lower() or "prohibited" in m:
            raise QueryDenied("statement not authorized") from None
        if "interrupted" in m:
            raise QueryTimeout("Query exceeded the time limit; simplify or add filters.") from None
        raise QueryError(_sanitise(m)) from None
    finally:
        con.close()
    truncated = len(fetched) > row_cap
    rows = [[(round(v, 6) if isinstance(v, float) else v) for v in r] for r in fetched[:row_cap]]
    return QueryResult(cols, rows, truncated, (time.monotonic() - t0) * 1000)
