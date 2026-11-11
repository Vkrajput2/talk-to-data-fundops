"""Forbidden-fund handling: one well-defined `fund_access_denied` result, and no forbidden name reaches any prompt."""
from tests.conftest import golden_sql
from tests.helpers import ask, make_llm, make_orch

FORBIDDEN_NAMES = ("F001", "F003", "F004", "Global Equity Alpha", "Asia Multi-Asset", "US Core Bond")


def prompts_text(llm):
    return "\n".join(f"{c[1]}\n{c[2]}" for c in llm.calls)


def test_forbidden_fund_returns_defined_denial_with_zero_llm_calls(db_overlay):
    llm = make_llm([golden_sql("Q1")])
    r = ask(make_orch(db_overlay, llm), "cash for F001", user="analyst_restricted")
    assert r.error.code == "fund_access_denied" and r.error.message == "You do not have access to fund F001."
    assert r.rows == [] and r.columns == [] and r.sql == "" and r.sources == [] and r.row_count == 0
    assert r.route == "denied" and r.warnings == ["You do not have access to fund F001."]
    assert "no query was run" in r.narrative
    assert llm.calls == []  # no stage, in particular no SQL stage, ran


def test_denial_uses_the_text_the_user_typed(db_overlay):
    r = ask(make_orch(db_overlay, make_llm(["x"])), "cash for Global Equity Alpha", user="analyst_restricted")
    assert r.error.message == "You do not have access to fund Global Equity Alpha."


def test_permitted_and_forbidden_mix_continues_with_permitted_and_warns(db_overlay):
    sql = "SELECT fund_id, available_balance FROM fact_cash_balance WHERE fund_id IN ('F001','F005') AND balance_date = '2026-09-04'"
    llm = make_llm([sql])
    r = ask(make_orch(db_overlay, llm), "show cash for F001 and EM Equity", user="analyst_restricted")
    assert r.error is None and [x[0] for x in r.rows] == ["F005"]  # F001 filtered out by the entitlement rewrite
    assert "You do not have access to fund F001." in r.warnings
    text = prompts_text(llm)
    assert "[a fund you cannot access]" in text and "EM Equity" in text
    for bad in FORBIDDEN_NAMES:
        assert bad not in text.replace(sql, ""), bad


def test_prompts_for_restricted_user_never_name_other_funds(db_overlay):
    llm = make_llm([golden_sql("Q1")], understand={"time_refs": ["today"]})
    r = ask(make_orch(db_overlay, llm), "List all active funds with region, strategy and latest AUM", user="analyst_restricted")
    assert sorted(x[0] for x in r.rows) == ["F002", "F005", "F006"]
    text = prompts_text(llm)
    gen = [c[2] for c in llm.calls if c[0] == "generate"][0]
    # catalog and rules carry no fund names; examples naming other funds are filtered out for this user
    for bad in ("Global Equity Alpha", "Asia Multi-Asset", "US Core Bond", "F001", "F003", "F004"):
        assert bad not in gen, bad
    assert "narrate" in [c[0] for c in llm.calls]
    assert "Global Equity Alpha" not in text.split("Rows (")[0]


def test_unrestricted_user_may_name_any_fund(db_overlay):
    sql = "SELECT fund_id FROM dim_fund WHERE fund_id = 'F001'"
    r = ask(make_orch(db_overlay, make_llm([sql])), "show fund F001", user="priya")
    assert r.error is None and r.rows == [["F001"]]


def test_restricted_user_cannot_get_other_funds_by_writing_the_filter_in_sql(db_overlay):
    llm = make_llm(["SELECT fund_id FROM dim_fund WHERE fund_id = 'F001'"])
    r = ask(make_orch(db_overlay, llm), "list a fund", user="analyst_restricted")
    assert r.error is None and r.rows == [] and r.row_count == 0
