"""Catalog, join graph, glossary, alias index and date context."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .db import DictRow, load_dictionaries

GLOSSARY_PATH = Path(__file__).with_name("glossary.json")


def load_glossary() -> dict:
    return json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))


@dataclass
class Column:
    name: str
    type: str
    key: str
    description: str


@dataclass
class Table:
    name: str
    columns: dict[str, Column]
    role: str = ""
    pk: str | None = None


@dataclass(frozen=True)
class JoinEdge:
    left_table: str
    left_col: str
    right_table: str
    right_col: str
    kind: str = "fk"
    note: str = ""

    def on_clause(self) -> str:
        return f"{self.left_table}.{self.left_col} = {self.right_table}.{self.right_col}"


class Catalog:
    def __init__(self, base: list[DictRow], supp: list[DictRow], glossary: dict | None = None):
        self.glossary = glossary or load_glossary()
        roles = self.glossary["table_roles"]
        self.tables: dict[str, Table] = {}
        for r in base + supp:
            t = self.tables.setdefault(r.table, Table(r.table, {}, roles.get(r.table, "")))
            t.columns[r.column] = Column(r.column, r.type, r.key, r.description)
            if r.key == "PK":
                t.pk = r.column
        base_tables = {r.table for r in base}
        self.synthetic_tables: dict[str, str] = {}
        for r in supp:
            if r.table not in base_tables:
                self.synthetic_tables[r.table] = "full"
            elif r.table not in self.synthetic_tables:
                self.synthetic_tables[r.table] = "partial"
        self.fund_scoped = {n for n, t in self.tables.items() if "fund_id" in t.columns}
        self.money_columns = {(t, c) for t, c in [
            ("fact_transaction", "gross_amount"), ("fact_recon_break", "break_amount"),
            ("fact_cash_balance", "available_balance"), ("fact_cash_balance", "total_net_assets"),
            ("fact_nav", "total_net_assets"), ("fact_flows", "net_flow"), ("fact_flows", "subscriptions"),
            ("fact_flows", "redemptions"), ("fact_revenue", "mgmt_fee_revenue"), ("fact_revenue", "avg_aum"),
            ("fact_flow_detail", "amount")] if t in self.tables}
        self.edges: list[JoinEdge] = []
        for t in self.tables.values():
            for c in t.columns.values():
                m = re.match(r"FK\s+(\w+)", c.key)
                if m and m.group(1) in self.tables and self.tables[m.group(1)].pk:
                    self.edges.append(JoinEdge(t.name, c.name, m.group(1), self.tables[m.group(1)].pk))
        # dictionary marks the FK with no target: broker_id is the counterparty key
        self.edges.append(JoinEdge("fact_settlement", "broker_id", "dim_counterparty", "cp_id", "inferred",
                                   "dictionary marks FK without a target"))
        if "dim_fx_rate" in self.tables:
            self.edges.append(JoinEdge("dim_fx_rate", "ccy", "fact_*", "ccy", "fx",
                                       "join on ccy and rate_date = AS_OF date"))
        self.non_joins = [
            ("fact_recon_break.custodian", "a custodian name string, not a key (matches cp_name only for some rows)"),
            ("dim_date.month", "integer month; fact_flows.month and fact_revenue.month are 'YYYY-MM' text"),
            ("security_name / broker_name on facts", "denormalised display columns, not join keys"),
        ]

    # --- helpers -------------------------------------------------------------------------
    @property
    def mapping_schema(self) -> dict:
        return {t.name: {c: ("REAL" if col.type == "NUMBER" else "TEXT") for c, col in t.columns.items()}
                for t in self.tables.values()}

    def edges_between(self, names) -> list[JoinEdge]:
        s = set(names)
        return [e for e in self.edges if e.kind != "fx" and e.left_table in s and e.right_table in s]

    def key_columns(self, table: str) -> list[str]:
        t = self.tables[table]
        out = [t.pk] if t.pk else []
        for e in self.edges:
            if e.kind == "fx":
                continue
            if e.left_table == table and e.left_col not in out:
                out.append(e.left_col)
            if e.right_table == table and e.right_col not in out:
                out.append(e.right_col)
        return out

    def describe(self, names=None, compact: bool = False) -> str:
        names = list(names) if names else list(self.tables)
        lines = []
        if compact:
            for n in names:
                t = self.tables[n]
                lines.append(f"- {n}: {t.role}. Columns: " + ", ".join(c for c in t.columns if c != "is_synthetic"))
            return "\n".join(lines)
        for n in names:
            t = self.tables[n]
            syn = " [synthetic]" if n in self.synthetic_tables else ""
            cols = ", ".join(f"{c.name} {c.type.lower()}{' [synthetic]' if c.name == 'is_synthetic' else ''}"
                             for c in t.columns.values())
            lines.append(f"- {n}{syn}: {t.role}. Columns: {cols}")
        return "\n".join(lines)

    def join_text(self) -> str:
        out = [f"{e.left_table} JOIN {e.right_table} ON {e.on_clause()}" for e in self.edges if e.kind != "fx"]
        out.append("dim_fx_rate fx JOIN <fact> t ON fx.ccy = t.ccy AND fx.rate_date = '<as_of>'"
                   if "dim_fx_rate" in self.tables else "")
        out += [f"DO NOT JOIN on {a}: {b}" for a, b in self.non_joins]
        return "\n".join(x for x in out if x)


def build_catalog(data_dir, include_supplement=True) -> Catalog:
    base, supp = load_dictionaries(Path(data_dir), include_supplement)
    return Catalog(base, supp)


# ---------------------------------------------------------------------------------------------
@dataclass
class Entity:
    type: str
    id: str
    name: str
    matched: str


def _fund_variants(fid: str, name: str, g: dict) -> set[str]:
    n = name
    pre, suf = g["fund_name_normalisation"]["strip_prefix"], g["fund_name_normalisation"]["strip_suffix"]
    if n.startswith(pre):
        n = n[len(pre):]
    if n.endswith(suf):
        n = n[: -len(suf)]
    v = {name.lower(), n.lower(), fid.lower()}
    if "emerging markets" in n.lower():
        v.add(re.sub("emerging markets", "EM", n, flags=re.I).lower())
    return v


class AliasIndex:
    def __init__(self, db_path, glossary: dict):
        con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        self.entries: list[tuple[str, Entity, bool]] = []  # (alias, entity, case_sensitive)
        try:
            funds = con.execute("SELECT fund_id, fund_name FROM dim_fund").fetchall()
            for fid, name in funds:
                for a in _fund_variants(fid, name, glossary):
                    self.entries.append((a, Entity("fund", fid, name, a), False))
            cps = con.execute("SELECT cp_id, cp_name, cp_type FROM dim_counterparty").fetchall()
            first = {}
            for cid, name, typ in cps:
                first.setdefault(name.split()[0].lower(), []).append(cid)
            for cid, name, typ in cps:
                etype = "custodian" if typ.lower() == "custodian" else "broker"
                self.entries.append((name.lower(), Entity(etype, cid, name, name), False))
                self.entries.append((cid.lower(), Entity(etype, cid, name, cid), False))
                w = name.split()[0].lower()
                if len(first[w]) == 1:
                    self.entries.append((w, Entity(etype, cid, name, w), False))
            codes = con.execute("SELECT DISTINCT fail_reason_code, fail_reason_text FROM fact_settlement "
                                "WHERE fail_reason_code IS NOT NULL").fetchall()
            for code, text in codes:
                self.entries.append((code, Entity("fail_code", code, text, code), True))
            for alias, code in glossary.get("extra_aliases", {}).items():
                txt = next((t for c, t in codes if c == code), code)
                self.entries.append((alias.lower(), Entity("fail_code", code, txt, alias), False))
            for (p,) in con.execute("SELECT DISTINCT process FROM fact_sla"):
                self.entries.append((p.lower(), Entity("process", p, p, p), False))
            for sid, name in con.execute("SELECT security_id, security_name FROM dim_security"):
                self.entries.append((name.lower(), Entity("security", sid, name, name), False))
        finally:
            con.close()
        self.entries.sort(key=lambda e: -len(e[0]))

    def find(self, text: str) -> list[tuple[Entity, int, int]]:
        """Return (entity, start, end) hits, longest alias first, no overlaps."""
        low = text.lower()
        taken: list[tuple[int, int]] = []
        hits = []
        for alias, ent, cs in self.entries:
            hay = text if cs else low
            for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", hay):
                s, e = m.span()
                if any(s < b and a < e for a, b in taken):
                    continue
                taken.append((s, e))
                hits.append((Entity(ent.type, ent.id, ent.name, text[s:e]), s, e))
        return sorted(hits, key=lambda h: h[1])


# ---------------------------------------------------------------------------------------------
@dataclass
class DateContext:
    as_of: str
    yesterday: str
    current_month: str
    previous_month: str
    current_month_partial: bool
    four_week_start: str
    revenue_month: str
    revenue_prior_year_month: str
    snapshot_dates: dict[str, str] = field(default_factory=dict)

    @property
    def anchor(self) -> str:
        return self.as_of

    @property
    def seven_days_start(self) -> str:
        return (dt.date.fromisoformat(self.as_of) - dt.timedelta(days=6)).isoformat()

    def prompt_block(self) -> str:
        return (f"as_of (today) = {self.as_of}\nyesterday (latest settlement day) = {self.yesterday}\n"
                f"this month = {self.current_month} (month-to-date: {self.current_month_partial}); last month = {self.previous_month}\n"
                f"last 4 weeks = {self.four_week_start} to {self.as_of}; last 7 days = {self.seven_days_start} to {self.as_of}\n"
                f"revenue comparison months = {self.revenue_month} vs {self.revenue_prior_year_month}\n"
                f"latest snapshot dates: " + ", ".join(f"{k}={v}" for k, v in self.snapshot_dates.items()))


def _shift_month(m: str, delta: int) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    idx = y * 12 + (mo - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def compute_date_context(db_path, as_of_override: str | None = None, yesterday_override: str | None = None
                         ) -> DateContext:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        def mx(sql, *a):
            return con.execute(sql, a).fetchone()[0]
        if as_of_override:
            as_of = as_of_override
        else:
            as_of = max(mx("SELECT MAX(settlement_date) FROM fact_settlement"),
                        mx("SELECT MAX(nav_date) FROM fact_nav"),
                        mx("SELECT MAX(balance_date) FROM fact_cash_balance"))
        snaps = {
            "fact_nav": mx("SELECT MAX(nav_date) FROM fact_nav WHERE nav_date <= ?", as_of),
            "fact_cash_balance": mx("SELECT MAX(balance_date) FROM fact_cash_balance WHERE balance_date <= ?", as_of),
            "fact_settlement": mx("SELECT MAX(settlement_date) FROM fact_settlement WHERE settlement_date <= ?", as_of),
            "fact_sla": mx("SELECT MAX(as_of_date) FROM fact_sla WHERE as_of_date <= ?", as_of),
            "fact_pricing_exception": mx("SELECT MAX(as_of_date) FROM fact_pricing_exception WHERE as_of_date <= ?", as_of),
        }
        yesterday = yesterday_override or snaps["fact_settlement"]
        cur = as_of[:7]
        last_day = (dt.date.fromisoformat(as_of) + dt.timedelta(days=1)).month != dt.date.fromisoformat(as_of).month
        months = {r[0] for r in con.execute("SELECT DISTINCT month FROM fact_revenue")}
        cands = sorted(m for m in months if m < cur and _shift_month(m, -12) in months)
        rev = cands[-1] if cands else _shift_month(cur, -1)
        return DateContext(
            as_of=as_of, yesterday=yesterday, current_month=cur, previous_month=_shift_month(cur, -1),
            current_month_partial=not last_day,
            four_week_start=(dt.date.fromisoformat(as_of) - dt.timedelta(days=27)).isoformat(),
            revenue_month=rev, revenue_prior_year_month=_shift_month(rev, -12), snapshot_dates=snaps)
    finally:
        con.close()
