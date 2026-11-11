"""Stage 6 helpers: KPI builder, number-grounding check, currency/partial-month warnings, chart recipes."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import exp

from ..db import QueryError, run_query
from ..models import KPI

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_MONTH_RE = rf"(?:{MONTHS})[a-z]*\.?"


# ---- KPIs --------------------------------------------------------------------------------------
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt(v, fmt: str) -> str:
    if v is None:
        return "-"
    if not _is_num(v):
        return str(v)
    if fmt == "integer":
        return f"{round(v):,}"
    if fmt == "decimal1":
        return f"{v:,.1f}"
    if fmt == "decimal2":
        return f"{v:,.2f}"
    if fmt == "percent1":
        return f"{v:,.1f}%"
    if fmt == "percent2":
        return f"{v:,.2f}%"
    if fmt == "currency_m":
        return f"{v / 1e6:,.2f}m"
    if fmt == "currency_bn":
        return f"{v / 1e9:,.2f}bn"
    return f"{v:,}" if isinstance(v, int) else f"{v:,.2f}"


def _readable(col: str) -> str:
    return col.replace("_", " ").strip().capitalize()


def build_kpis(specs, columns: list[str], rows: list[list]) -> list[KPI]:
    out: list[KPI] = []
    low = {c.lower(): i for i, c in enumerate(columns)}
    ccy_i = low.get("ccy")
    for s in specs or []:
        try:
            if s.op == "count_rows":
                v = len(rows)
            else:
                i = low.get((s.column or "").lower())
                if i is None:
                    continue
                if s.op == "cell":
                    if not 0 <= s.row < len(rows):
                        continue
                    v = rows[s.row][i]
                else:
                    vals = [r[i] for r in rows if _is_num(r[i])]
                    if not vals:
                        continue
                    v = {"sum": sum, "max": max, "min": min}[s.op](vals)
        except (KeyError, IndexError, TypeError):
            continue
        fmt = s.format if s.format in ("integer", "decimal1", "decimal2", "percent1", "percent2",
                                       "currency_m", "currency_bn", "text") else "text"
        unit = rows[s.row][ccy_i] if ccy_i is not None and s.op == "cell" and 0 <= s.row < len(rows) else None
        out.append(KPI(label=(s.label or _readable(s.column))[:60], value=v, display=_fmt(v, fmt), unit=unit))
    if out:
        return out[:4]
    return fallback_kpis(columns, rows)


def fallback_kpis(columns, rows) -> list[KPI]:
    if len(rows) == 1:
        res = []
        for c, v in zip(columns, rows[0]):
            if _is_num(v) and len(res) < 4:
                res.append(KPI(label=_readable(c), value=v,
                               display=_fmt(v, "decimal1" if isinstance(v, float) else "integer")))
        if res:
            return res
    return [KPI(label="Rows returned", value=len(rows), display=str(len(rows)))]


def deterministic_narrative(row_count: int, truncated: bool) -> str:
    if row_count == 0:
        return "The query returned no rows."
    return f"The query returned {row_count} row{'s' if row_count != 1 else ''}."


# ---- grounding ------------------------------------------------------------------------------------
_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|bn|billion|million|thousand|m|k|b)?(?![A-Za-z])", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "b": 1e9, "billion": 1e9}


def _strip_non_claims(text: str) -> str:
    t = re.sub(r"\b\d{4}-\d{2}(?:-\d{2})?\b", " ", text)
    t = re.sub(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_RE}(?:,?\s+\d{{4}})?", " ", t, flags=re.I)
    t = re.sub(rf"\b{_MONTH_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?", " ", t, flags=re.I)
    t = re.sub(rf"\b{_MONTH_RE}\s+\d{{4}}\b", " ", t, flags=re.I)
    t = re.sub(r"\b[A-Za-z]+\d+[A-Za-z0-9]*\b", " ", t)
    return t


def _tokens(text: str):
    for m in _NUM.finditer(text):
        raw = m.group(0).strip()
        mm = re.match(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*(%|[A-Za-z]*)$", raw)
        if not mm:
            continue
        num_s, unit = mm.group(1), mm.group(2).lower()
        dec = len(num_s.split(".")[1]) if "." in num_s else 0
        yield raw, float(num_s.replace(",", "")), unit, dec


def check_grounding(narrative: str, columns, rows, kpis=(), extra_numbers=()) -> list[str]:
    text = narrative or ""
    cells_num: list[float] = []
    texts: list[str] = []
    for r in rows:
        for v in r:
            if _is_num(v):
                cells_num.append(float(v))
            elif isinstance(v, str) and v:
                texts.append(v)
    for k in kpis:
        if _is_num(k.value):
            cells_num.append(float(k.value))
    cells_num.append(float(len(rows)))
    cells_num.extend(float(x) for x in extra_numbers)
    for t in texts:
        for _, n, _, _ in _tokens(t):
            cells_num.append(n)
    for t in sorted(set(texts), key=len, reverse=True):
        text = re.sub(re.escape(t), " ", text, flags=re.I)
    text = _strip_non_claims(text)
    bad: list[str] = []
    for raw, n, unit, dec in _tokens(text):
        mult = _MULT.get(unit, 1.0)
        tol = 0.5 * 10 ** (-dec)
        ok = False
        for v in cells_num:
            cands = [(n, tol), (n * mult, tol * mult)]
            if unit == "%":
                cands.append((n / 100.0, tol / 100.0))
            for x, tl in cands:
                for vv in (v, abs(v), v * 100 if unit == "%" else v):
                    if abs(abs(x) - abs(vv)) <= tl + 1e-9:
                        ok = True
                        break
                if ok:
                    break
            if ok:
                break
        if not ok:
            msg = f"Narrative mentions {raw} which is not in the query result."
            if msg not in bad:
                bad.append(msg)
    return bad


# ---- currency / partial-month warnings ---------------------------------------------------------------
PLAIN_SUM = "Amounts in different currencies are added without FX conversion; treat totals as indicative."


def _money_names(catalog) -> set[str]:
    return {c for _, c in catalog.money_columns}


def currency_warnings(validated, catalog, db_path, user, as_of: str, row_cap=500) -> list[str]:
    tree = validated.unscoped_tree
    if tree is None:
        return []
    tables = set(validated.tables_used)
    if "dim_fx_rate" in tables:
        return [f"USD-equivalent at synthetic FX rates as of {as_of}."]
    money = _money_names(catalog)
    for agg in list(tree.find_all(exp.Sum)) + list(tree.find_all(exp.Avg)):
        cols = [c for c in agg.find_all(exp.Column) if c.name.lower() in money]
        if not cols:
            continue
        sel = agg.find_ancestor(exp.Select)
        if sel is None:
            continue
        grp = sel.args.get("group")
        if grp and any(isinstance(g, exp.Column) and g.name.lower() == "ccy" for g in grp.expressions):
            continue
        try:
            ccy_alias = None
            for t in sel.find_all(exp.Table):
                if t.find_ancestor(exp.Select) is not sel and t.find_ancestor(exp.Select) is not None:
                    continue
                name = t.name.lower()
                if name in catalog.tables and "ccy" in catalog.tables[name].columns:
                    ccy_alias = t.alias or t.name
                    break
            if ccy_alias is None:
                continue
            probe = sel.copy()
            probe.set("expressions", [exp.alias_(exp.Count(this=exp.Distinct(
                expressions=[exp.column("ccy", table=ccy_alias)])), "n")])
            for k in ("group", "having", "order", "limit", "qualify", "distinct"):
                probe.set(k, None)
            from .validate import validate
            v = validate(probe.sql(dialect="sqlite"), catalog, user, row_cap)
            res = run_query(db_path, v.sql, row_cap)
            if res.rows and res.rows[0][0] and res.rows[0][0] > 1:
                return [PLAIN_SUM]
        except Exception:  # best effort: no warning when the probe cannot be built
            continue
    return []


def partial_month_warning(validated, date_ctx) -> list[str]:
    if not date_ctx.current_month_partial or validated.unscoped_tree is None:
        return []
    tabs = set(validated.tables_used)
    if not tabs & {"fact_flows", "fact_revenue", "fact_flow_detail"}:
        return []
    lits = {l.name for l in validated.unscoped_tree.find_all(exp.Literal) if l.is_string}
    if date_ctx.current_month in lits or any(s.startswith(date_ctx.current_month) for s in lits):
        return [f"{date_ctx.current_month} is month-to-date; comparisons with full months are not like-for-like."]
    return []


# ---- charts ------------------------------------------------------------------------------------------------
def _is_date_like(v) -> bool:
    return isinstance(v, str) and re.match(r"^\d{4}-\d{2}(-\d{2})?$", v) is not None


def chart_for(columns, rows, title: str = "") -> dict | None:
    if len(rows) < 2:
        return None
    n = len(columns)
    num_idx = [i for i in range(n) if all(_is_num(r[i]) or r[i] is None for r in rows) and
               any(_is_num(r[i]) for r in rows) and columns[i].lower() not in ("ccy",)]
    for i in range(n):
        if all(_is_date_like(r[i]) for r in rows) and len(rows) >= 3:
            ys = [j for j in num_idx if j != i][:3]
            if ys:
                return {"type": "line", "title": title, "x_label": columns[i], "y_label": "",
                        "x": [r[i] for r in rows],
                        "series": [{"name": columns[j], "values": [r[j] for r in rows]} for j in ys]}
    label_idx = [i for i in range(n) if all(isinstance(r[i], str) for r in rows) and columns[i].lower() != "ccy"]
    if label_idx and len(num_idx) >= 2:
        cur = [j for j in num_idx if re.search(r"(mtd|current|cur|this)", columns[j], re.I)]
        pri = [j for j in num_idx if re.search(r"(prior|last|previous|prev)", columns[j], re.I)]
        if cur and pri:
            a, b = cur[0], pri[0]
            return {"type": "grouped_bar", "title": title, "x_label": columns[label_idx[0]], "y_label": "",
                    "x": [r[label_idx[0]] for r in rows],
                    "series": [{"name": columns[a], "values": [r[a] for r in rows]},
                               {"name": columns[b], "values": [r[b] for r in rows]}]}
    return None


def settlement_rate_chart(db_path, catalog, user, date_ctx, row_cap=500) -> dict | None:
    from .validate import validate
    sql = ("SELECT settlement_date AS d, ROUND(100.0 * SUM(status = 'Settled') / COUNT(*), 1) AS rate_pct "
           f"FROM fact_settlement WHERE settlement_date BETWEEN '{date_ctx.four_week_start}' AND '{date_ctx.as_of}' "
           "GROUP BY settlement_date ORDER BY settlement_date")
    try:
        v = validate(sql, catalog, user, row_cap)
        res = run_query(db_path, v.sql, row_cap)
    except Exception:
        return None
    if len(res.rows) < 3:
        return None
    return {"type": "line", "title": "Daily settlement rate (%)", "x_label": "settlement date", "y_label": "% settled",
            "x": [r[0] for r in res.rows], "series": [{"name": "rate_pct", "values": [r[1] for r in res.rows]}]}
