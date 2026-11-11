import pytest
import sqlglot
from sqlglot import exp

from app.agent.validate import validate
from app.db import run_query
from app.entitlements import (MOCK_USERS, PLACEHOLDER, EntitlementEnforcementError, UnknownUser, get_user,
                              sanitize_question, verify_scoped)
from tests.conftest import golden_sql

R = MOCK_USERS["analyst_restricted"]
ALLOWED = {"F002", "F005", "F006"}


def run(db, cat, sql, user=R):
    v = validate(sql, cat, user)
    return run_query(db, v.sql)


def test_mock_users():
    assert R.fund_ids == frozenset(ALLOWED)
    assert MOCK_USERS["priya"].fund_ids is None and MOCK_USERS["david"].fund_ids is None
    with pytest.raises(UnknownUser):
        get_user("mallory")


def test_restricted_user_sees_only_permitted_funds_in_golden_queries(db_overlay, cat_overlay):
    q1 = run(db_overlay, cat_overlay, golden_sql("Q1"))
    assert sorted(r[0] for r in q1.rows) == ["F002", "F005", "F006"]
    q3 = run(db_overlay, cat_overlay, golden_sql("Q3"))
    assert sorted(r[1] for r in q3.rows) == ["German Bund 2.3% 2034", "Meridian Corp 5.1% 2030"]
    q5 = run(db_overlay, cat_overlay, golden_sql("Q5"))
    assert [r[0] for r in q5.rows] == ["BRK002", "BRK004"]
    q10 = run(db_overlay, cat_overlay, golden_sql("Q10"))
    assert q10.rows == [["MS EMEA Fixed Income Fund", 16], ["MS Emerging Markets Equity Fund", 6],
                        ["MS Sustainable Global Credit Fund", 0]]


def test_aggregates_cover_only_permitted_funds(db_overlay, cat_overlay):
    r = run(db_overlay, cat_overlay, "SELECT SUM(net_flow) FROM fact_flows WHERE month = '2026-09'")
    assert r.rows == [[-110000000 - 135000000 + 75000000]]
    full = run(db_overlay, cat_overlay, "SELECT SUM(net_flow) FROM fact_flows WHERE month = '2026-09'", MOCK_USERS["priya"])
    assert full.rows == [[10000000 + 20000000 + 110000000 - 110000000 - 135000000 + 75000000]]


def test_subquery_cte_union_selfjoin_leaks_are_closed(db_overlay, cat_overlay):
    perm = run(db_overlay, cat_overlay, "SELECT COUNT(*) FROM fact_settlement").rows[0][0]
    direct = run_query(db_overlay, "SELECT COUNT(*) FROM fact_settlement WHERE fund_id IN ('F002','F005','F006')").rows[0][0]
    assert perm == direct and 0 < perm < 632
    sc = run(db_overlay, cat_overlay, "SELECT (SELECT COUNT(*) FROM fact_settlement) AS n").rows[0][0]
    assert sc == direct
    r = run(db_overlay, cat_overlay, "SELECT fund_id FROM dim_fund WHERE fund_id IN (SELECT fund_id FROM fact_nav)")
    assert sorted(x[0] for x in r.rows) == sorted(ALLOWED)
    r = run(db_overlay, cat_overlay, "WITH x AS (SELECT fund_id FROM fact_nav) SELECT DISTINCT fund_id FROM x")
    assert {x[0] for x in r.rows} == ALLOWED
    r = run(db_overlay, cat_overlay, "SELECT fund_id FROM fact_nav UNION SELECT fund_id FROM fact_cash_balance")
    assert {x[0] for x in r.rows} == ALLOWED
    r = run(db_overlay, cat_overlay, "SELECT DISTINCT a.fund_id FROM fact_nav a JOIN fact_nav b ON a.fund_id = b.fund_id")
    assert {x[0] for x in r.rows} == ALLOWED
    assert run(db_overlay, cat_overlay, "SELECT * FROM fact_nav WHERE fund_id = 'F001'").rows == []


@pytest.mark.parametrize("sql", ["SELECT * FROM FACT_NAV", 'SELECT * FROM "Fact_Nav"', "SELECT * FROM Fact_Flows f",
                                 "SELECT * FROM [FACT_NAV]", "SELECT * FROM `fact_nav`"])
def test_case_and_quoting_variants_are_scoped(sql, db_overlay, cat_overlay):
    r = run(db_overlay, cat_overlay, sql)
    assert r.rows and {row[r.columns.index("fund_id")] for row in r.rows} <= ALLOWED


def test_new_fund_tables_are_scoped(db_overlay, cat_overlay):
    for t in ("fact_flow_detail", "fact_sla_history"):
        r = run(db_overlay, cat_overlay, f"SELECT DISTINCT fund_id FROM {t}")
        assert {x[0] for x in r.rows} == ALLOWED


def test_unwrapped_scoped_table_fails_closed(cat_overlay):
    fs = cat_overlay.fund_scoped
    for sql in ("SELECT * FROM fact_nav", "SELECT * FROM FACT_NAV"):
        with pytest.raises(EntitlementEnforcementError):
            verify_scoped(sqlglot.parse_one(sql, read="sqlite"), fs)
    # a hand-written lookalike subquery without the internal mark is rejected too
    fake = sqlglot.parse_one("SELECT * FROM (SELECT * FROM fact_nav WHERE fund_id IN ('F002')) AS n", read="sqlite")
    with pytest.raises(EntitlementEnforcementError):
        verify_scoped(fake, fs)


def test_sanitize_replaces_forbidden_funds_only(aliases):
    s = sanitize_question("show cash for F001 and EM Equity", R, aliases)
    assert PLACEHOLDER in s.text and "F001" not in s.text and "EM Equity" in s.text
    assert [f[1] for f in s.forbidden] == ["F001"] and [p.id for p in s.permitted] == ["F005"]
    assert s.denial_text == "You do not have access to fund F001."
    s2 = sanitize_question("cash for Global Equity Alpha", R, aliases)
    assert s2.forbidden[0][1] == "F001" and "Global Equity" not in s2.text
    s3 = sanitize_question("cash for F001", MOCK_USERS["priya"], aliases)
    assert not s3.forbidden and s3.text == "cash for F001"
