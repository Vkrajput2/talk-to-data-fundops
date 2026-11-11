"""Registered, model-independent patterns for Q8 (YoY revenue decomposition) and Q10 (attention ranking)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

REV_WORD = re.compile(r"(revenue|fee income|management fee|mgmt fee|\bfees\b)", re.I)
CMP_WORD = re.compile(r"(\byoy\b|year[- ]over[- ]year|year[- ]on[- ]year|vs\.? (the )?(same month )?last year|"
                      r"compared (to|with) last year|prior year|\bdrove\b|\bdriver|\bdriven\b|\bwhy\b)", re.I)
FUND_WORD = re.compile(r"\bfunds?\b", re.I)
ATT_WORD = re.compile(r"(needs? attention|\battention\b|management summary|prioriti[sz]e)", re.I)
ATT_EXCLUDE = re.compile(r"\b(by|with|having)\s+(aum|nav|cash|flows?|revenue|breaks?|fails?|sla)\b|\bonly\b", re.I)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


@dataclass
class BuiltQuery:
    name: str
    sql: str
    sources: list[dict]
    title: str
    post: Callable | None = None
    warnings: list[str] = field(default_factory=list)


def parse_top_n(q: str, default: int = 3) -> int:
    m = re.search(r"\b([1-6])\b", q)
    if m:
        return int(m.group(1))
    for w, n in NUMBER_WORDS.items():
        if re.search(rf"\b{w}\b", q, re.I):
            return n
    return default


def match_pattern(question: str, hint: str = "none") -> str | None:
    q = question.lower()
    att = bool(FUND_WORD.search(q) and ATT_WORD.search(q) and not ATT_EXCLUDE.search(q))
    rev = bool(REV_WORD.search(q) and CMP_WORD.search(q))
    if att:
        return "fund_attention_ranking"
    if rev:
        return "yoy_revenue_decomposition"
    return None


def _check_month(m: str) -> str:
    if not re.match(r"^\d{4}-\d{2}$", m):
        raise ValueError("bad month")
    return m


def _check_date(d: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        raise ValueError("bad date")
    return d


def build_yoy(date_ctx, fund_ids: list[str]) -> BuiltQuery:
    cur, pri = _check_month(date_ctx.revenue_month), _check_month(date_ctx.revenue_prior_year_month)
    for f in fund_ids:
        if not re.match(r"^F\d{3}$", f):
            raise ValueError("bad fund id")
    where_f = f" AND f.fund_id IN ({', '.join(repr(f) for f in fund_ids)})" if fund_ids else ""
    def s(col, m):
        return f"SUM(CASE WHEN r.month = '{m}' THEN r.{col} END)"
    sql = f"""SELECT f.fund_id, f.fund_name,
  {s('mgmt_fee_revenue', cur)} AS rev_current,
  {s('mgmt_fee_revenue', pri)} AS rev_prior,
  ROUND(100.0 * ({s('mgmt_fee_revenue', cur)} - {s('mgmt_fee_revenue', pri)}) / {s('mgmt_fee_revenue', pri)}, 1) AS rev_yoy_pct,
  ROUND(100.0 * ({s('avg_aum', cur)} - {s('avg_aum', pri)}) / {s('avg_aum', pri)}, 1) AS avg_aum_yoy_pct,
  ROUND({s('fee_rate_pct', pri)}, 4) AS fee_rate_prior_pct,
  ROUND({s('fee_rate_pct', cur)}, 4) AS fee_rate_current_pct,
  ROUND(({s('avg_aum', cur)} - {s('avg_aum', pri)}) * {s('fee_rate_pct', pri)} / 1200.0, 0) AS aum_effect,
  ROUND(({s('fee_rate_pct', cur)} - {s('fee_rate_pct', pri)}) * {s('avg_aum', cur)} / 1200.0, 0) AS rate_effect,
  r.ccy AS ccy
FROM fact_revenue r JOIN dim_fund f ON f.fund_id = r.fund_id
WHERE r.month IN ('{cur}', '{pri}'){where_f}
GROUP BY f.fund_id, f.fund_name, r.ccy
HAVING COUNT(DISTINCT r.month) = 2
ORDER BY f.fund_name"""
    sources = [
        dict(table="fact_revenue", why="Monthly management-fee revenue, average AUM and fee rate for both periods",
             columns=["month", "mgmt_fee_revenue", "avg_aum", "fee_rate_pct"], join_keys=["fact_revenue.fund_id = dim_fund.fund_id"],
             synthetic="no"),
        dict(table="dim_fund", why="Fund names for the revenue rows", columns=["fund_id", "fund_name"],
             join_keys=["fact_revenue.fund_id = dim_fund.fund_id"], synthetic="no"),
    ]

    def post(columns, rows):
        warns = []
        ix = {c: i for i, c in enumerate(columns)}
        if not all(k in ix for k in ("rev_current", "rev_prior", "aum_effect", "rate_effect")):
            return columns, rows, warns
        cols = columns + ["residual"]
        out = []
        for r in rows:
            res = (r[ix["rev_current"]] - r[ix["rev_prior"]]) - r[ix["aum_effect"]] - r[ix["rate_effect"]]
            out.append(list(r) + [round(res, 2)])
            if abs(res) > 0.01 * abs(r[ix["rev_prior"]]):
                warns.append("The AUM/rate decomposition leaves a residual above 1% of prior revenue "
                             "(residual column, computed from the query result).")
        return cols, out, list(dict.fromkeys(warns))

    return BuiltQuery("yoy_revenue_decomposition", sql, sources,
                      f"Year-on-year management-fee revenue drivers ({cur} vs {pri})", post)


def build_attention(date_ctx, glossary: dict, top_n: int = 3) -> BuiltQuery:
    w = glossary["attention_weights"]
    g = lambda k: glossary[k]["predicate"]
    snap = date_ctx.snapshot_dates
    y = _check_date(date_ctx.yesterday)
    cash_d, nav_d = _check_date(snap["fact_cash_balance"]), _check_date(snap["fact_nav"])
    cm = _check_month(date_ctx.current_month)
    thr = int(glossary["outflow_threshold"])
    top_n = max(1, min(int(top_n), 6))
    sql = f"""WITH fails AS (SELECT fund_id, COUNT(*) c FROM fact_settlement WHERE settlement_date = '{y}' AND {g('failed_settlement')} GROUP BY fund_id),
 aged AS (SELECT fund_id, COUNT(*) c FROM fact_recon_break WHERE {g('aged_break')} GROUP BY fund_id),
 lowcash AS (SELECT fund_id, 1 c FROM fact_cash_balance WHERE balance_date = '{cash_d}' AND {g('tight_cash')}),
 px AS (SELECT fund_id, COUNT(*) c FROM fact_pricing_exception WHERE {g('stale_price')} GROUP BY fund_id),
 slar AS (SELECT fund_id, 1 c FROM fact_sla WHERE {g('sla_risk')} GROUP BY fund_id),
 navr AS (SELECT fund_id, 1 c FROM fact_nav WHERE nav_date = '{nav_d}' AND {g('nav_under_review')}),
 outfl AS (SELECT fund_id, 1 c FROM fact_flows WHERE month = '{cm}' AND net_flow <= {thr})
SELECT f.fund_name,
  {w['fails']} * COALESCE(fails.c, 0) + {w['aged_breaks']} * COALESCE(aged.c, 0) + {w['low_cash']} * COALESCE(lowcash.c, 0)
  + {w['pricing_exceptions']} * COALESCE(px.c, 0) + {w['sla_risk']} * COALESCE(slar.c, 0)
  + {w['nav_under_review']} * COALESCE(navr.c, 0) + {w['outflow']} * COALESCE(outfl.c, 0) AS attention_score,
  COALESCE(fails.c, 0) AS failed_settlements, COALESCE(aged.c, 0) AS aged_breaks, COALESCE(lowcash.c, 0) AS low_cash,
  COALESCE(px.c, 0) AS stale_prices, COALESCE(slar.c, 0) AS sla_risk, COALESCE(navr.c, 0) AS nav_under_review,
  COALESCE(outfl.c, 0) AS outflow
FROM dim_fund f
LEFT JOIN fails ON f.fund_id = fails.fund_id LEFT JOIN aged ON f.fund_id = aged.fund_id
LEFT JOIN lowcash ON f.fund_id = lowcash.fund_id LEFT JOIN px ON f.fund_id = px.fund_id
LEFT JOIN slar ON f.fund_id = slar.fund_id LEFT JOIN navr ON f.fund_id = navr.fund_id
LEFT JOIN outfl ON f.fund_id = outfl.fund_id
ORDER BY attention_score DESC, f.fund_name
LIMIT {top_n}"""
    fk = lambda t: f"{t}.fund_id = dim_fund.fund_id"
    mk = lambda t, why, cols: dict(table=t, why=why, columns=cols, join_keys=[fk(t)], synthetic="no")
    sources = [
        dict(table="dim_fund", why="Fund names; the base of the ranking", columns=["fund_id", "fund_name"], join_keys=[], synthetic="no"),
        mk("fact_settlement", "Failed settlements on the latest settlement day (weight 3 each)", ["fund_id", "settlement_date", "status"]),
        mk("fact_recon_break", "Open reconciliation breaks older than 5 days (weight 2 each)", ["fund_id", "status", "age_days"]),
        mk("fact_cash_balance", "Cash below 2% of AUM (weight 2)", ["fund_id", "balance_date", "cash_pct_aum"]),
        mk("fact_pricing_exception", "Open stale or missing prices of 2+ days (weight 2 each)", ["fund_id", "status", "stale_days"]),
        mk("fact_sla", "Pending SLA processes at 80%+ of target (weight 1)", ["fund_id", "status", "pct_of_sla"]),
        mk("fact_nav", "NAV status Under Review (weight 2)", ["fund_id", "nav_date", "nav_status"]),
        mk("fact_flows", "Month-to-date net outflow of 100m or more (weight 2)", ["fund_id", "month", "net_flow"]),
    ]
    return BuiltQuery("fund_attention_ranking", sql, sources, f"Top {top_n} funds needing attention (scored)")
