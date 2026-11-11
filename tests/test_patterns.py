import pytest

from app.agent import patterns
from app.agent.validate import validate
from app.db import run_query
from app.entitlements import MOCK_USERS
from tests.conftest import golden_expected, golden_sql, rows_equal
from tests.helpers import ask, make_llm, make_orch

Q8 = "What drove the year-on-year change in management-fee revenue for the Global Equity Alpha Fund?"
Q10 = "Which three funds need attention today, and why?"


@pytest.mark.parametrize("q,expected", [
    (Q10, "fund_attention_ranking"),
    ("Which funds should we prioritise today", "fund_attention_ranking"),
    ("Give me a management summary of the funds", "fund_attention_ranking"),
    (Q8, "yoy_revenue_decomposition"),
    ("why did fee income change versus last year for EM Equity", "yoy_revenue_decomposition"),
    ("top 3 funds by AUM", None),
    ("which funds have the worst cash position", None),
    ("funds with the most fails", None),
    ("which fund needs attention on cash only", None),
    ("show revenue for each fund", None),
    ("List all active funds", None),
])
def test_trigger_rules(q, expected):
    assert patterns.match_pattern(q) == expected


def test_hallucinated_hint_alone_never_fires():
    assert patterns.match_pattern("List all active funds", "fund_attention_ranking") is None
    assert patterns.match_pattern("show revenue by fund", "yoy_revenue_decomposition") is None


def test_top_n_parsing_and_clamping():
    assert patterns.parse_top_n("which three funds need attention") == 3
    assert patterns.parse_top_n("which 5 funds need attention") == 5
    assert patterns.parse_top_n("which funds need attention") == 3
    assert patterns.parse_top_n("which 9 funds need attention") == 3  # out of range digit ignored


def test_top_n_is_clamped_in_the_generated_sql(date_ctx, cat_overlay):
    assert patterns.build_attention(date_ctx, cat_overlay.glossary, 99).sql.rstrip().endswith("LIMIT 6")
    assert patterns.build_attention(date_ctx, cat_overlay.glossary, 0).sql.rstrip().endswith("LIMIT 1")


def test_q10_sql_uses_glossary_predicates_weights_and_has_no_leftovers(date_ctx, cat_overlay):
    g = cat_overlay.glossary
    sql = patterns.build_attention(date_ctx, g).sql
    for k in ("failed_settlement", "aged_break", "tight_cash", "stale_price", "sla_risk", "nav_under_review"):
        assert g[k]["predicate"] in sql, k
    for k, w in {"fails": 3, "aged_breaks": 2, "low_cash": 2, "pricing_exceptions": 2, "sla_risk": 1, "outflow": 2}.items():
        assert g["attention_weights"][k] == w
    assert "{" not in sql and "}" not in sql
    assert "<= -100000000" in sql and "settlement_date = '2026-09-04'" in sql
    assert validate(sql, cat_overlay, MOCK_USERS["priya"]).tables_used


@pytest.mark.parametrize("which", ["db_base", "db_overlay"])
def test_pattern_outputs_match_golden_in_both_dbs(which, request, cat_overlay):
    from app.metadata import compute_date_context
    db = request.getfixturevalue(which)
    dc = compute_date_context(db)
    user = MOCK_USERS["priya"]
    q10 = run_query(db, validate(patterns.build_attention(dc, cat_overlay.glossary).sql, cat_overlay, user).sql)
    assert [(r[0], r[1]) for r in q10.rows] == [("MS EMEA Fixed Income Fund", 16), ("MS US Core Bond Fund", 13),
                                                ("MS Emerging Markets Equity Fund", 6)]
    comp = {r[0]: dict(zip(q10.columns, r)) for r in q10.rows}
    assert comp["MS EMEA Fixed Income Fund"]["failed_settlements"] == 2 and comp["MS EMEA Fixed Income Fund"]["aged_breaks"] == 2
    assert comp["MS EMEA Fixed Income Fund"]["low_cash"] == 1 and comp["MS EMEA Fixed Income Fund"]["outflow"] == 1
    assert comp["MS Emerging Markets Equity Fund"]["nav_under_review"] == 1
    q8 = run_query(db, validate(patterns.build_yoy(dc, ["F001"]).sql, cat_overlay, user).sql)
    row = dict(zip(q8.columns, q8.rows[0]))
    assert (row["rev_current"], row["rev_prior"], row["rev_yoy_pct"], row["avg_aum_yoy_pct"]) == (3137500, 2762500, 13.6, 13.6)
    assert (row["fee_rate_prior_pct"], row["fee_rate_current_pct"], row["aum_effect"], row["rate_effect"]) == (0.75, 0.75, 375000, 0)


def test_patterns_run_without_calling_the_sql_stage(db_overlay):
    llm = make_llm(["DROP TABLE dim_fund"])
    orch = make_orch(db_overlay, llm)
    r10 = ask(orch, Q10)
    assert r10.route == "pattern:fund_attention_ranking" and r10.error is None
    assert rows_equal([x[:2] for x in r10.rows], golden_expected()["Q10"]["rows"])
    assert {s.table for s in r10.sources} == {"dim_fund", "fact_settlement", "fact_recon_break", "fact_cash_balance",
                                              "fact_pricing_exception", "fact_sla", "fact_nav", "fact_flows"}
    assert all(s.why and s.synthetic == "no" for s in r10.sources)
    r8 = ask(orch, Q8)
    assert r8.route == "pattern:yoy_revenue_decomposition"
    row = dict(zip(r8.columns, r8.rows[0]))
    assert row["rev_current"] == 3137500 and row["residual"] == 0 and row["rate_effect"] == 0 and row["aum_effect"] == 375000
    assert "fact_revenue" in {s.table for s in r8.sources} and "2026-08" in r8.sql and "2025-08" in r8.sql
    assert llm.counts.get("generate", 0) == 0 and llm.counts.get("resolve", 0) == 0


def test_pattern_still_works_when_the_model_is_down(db_overlay):
    from app.llm import LLMUnavailable

    def down(system, user, i):
        return LLMUnavailable("down")
    llm = make_llm(["x"], extra={"understand": down, "narrate": down})
    r = ask(make_orch(db_overlay, llm), Q10)
    assert r.route == "pattern:fund_attention_ranking" and [x[1] for x in r.rows] == [16, 13, 6]
    assert "Narrative unavailable: llm_unavailable" in r.warnings


def test_restricted_user_patterns_are_scoped(db_overlay):
    orch = make_orch(db_overlay, make_llm(["x"]))
    r = ask(orch, Q10, user="analyst_restricted")
    assert [(x[0], x[1]) for x in r.rows] == [("MS EMEA Fixed Income Fund", 16), ("MS Emerging Markets Equity Fund", 6),
                                              ("MS Sustainable Global Credit Fund", 0)]
    r8 = ask(orch, "What drove the YoY change in management fee revenue?", user="analyst_restricted")
    assert sorted(x[0] for x in r8.rows) == ["F002", "F005", "F006"]
    denied = ask(orch, Q8, user="analyst_restricted")
    assert denied.error.code == "fund_access_denied" and denied.rows == []
