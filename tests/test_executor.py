import sqlite3

import pytest

from app.agent.validate import validate
from app.db import ALLOWED_FUNCTIONS, QueryDenied, QueryError, QueryTimeout, connect_ro, run_query
from app.entitlements import MOCK_USERS
from app.agent.retrieval import ExampleBank, substitute
import json
from tests.conftest import GOLDEN, GOLDEN_IDS, golden_sql


@pytest.mark.parametrize("sql", [
    "INSERT INTO dim_fund (fund_id) VALUES ('X')", "DELETE FROM dim_fund", "DROP TABLE dim_fund",
    "ATTACH DATABASE ':memory:' AS z", "PRAGMA writable_schema=1", "SELECT * FROM sqlite_master",
    "SELECT * FROM _build_info", "SELECT load_extension('x')", "SELECT randomblob(8)",
])
def test_authorizer_blocks_everything_but_safe_reads(sql, db_overlay):
    with pytest.raises(QueryDenied):
        run_query(db_overlay, sql)


def test_connection_is_read_only(db_overlay):
    con = connect_ro(db_overlay)
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE dim_fund SET region = 'x'")
    con.close()
    assert run_query(db_overlay, "SELECT COUNT(*) FROM dim_fund").rows == [[6]]


def test_timeout_aborts_cross_join(db_overlay):
    sql = ("SELECT COUNT(*) FROM fact_transaction a, fact_transaction b, fact_transaction c, fact_settlement d")
    with pytest.raises(QueryTimeout):
        run_query(db_overlay, sql, timeout_s=0.3)


def test_row_cap_truncation_flag(db_overlay):
    r = run_query(db_overlay, "SELECT * FROM fact_transaction", row_cap=100)
    assert len(r.rows) == 100 and r.truncated is True
    r = run_query(db_overlay, "SELECT * FROM dim_fund", row_cap=6)
    assert len(r.rows) == 6 and r.truncated is False


def test_float_noise_is_rounded(db_overlay):
    assert run_query(db_overlay, "SELECT 0.35000000000000003").rows == [[0.35]]


def test_sqlite_errors_are_sanitised(db_overlay):
    with pytest.raises(QueryError) as e:
        run_query(db_overlay, "SELECT nope FROM dim_fund")
    assert "no such column" in str(e.value) and "\\" not in str(e.value)


@pytest.mark.parametrize("sql,expect", [
    ("SELECT COUNT(*) FROM dim_fund WHERE fund_name GLOB 'MS*'", 6),
    ("SELECT SUBSTR(fund_name, 1, 2) FROM dim_fund WHERE fund_id = 'F001'", "MS"),
    ("SELECT strftime('%Y-%m', MAX(nav_date)) FROM fact_nav", "2026-09"),
    ("SELECT COALESCE(NULL, 7)", 7), ("SELECT ROUND(2.567, 1)", 2.6),
])
def test_allowed_functions_execute(sql, expect, db_overlay):
    assert run_query(db_overlay, sql).rows[0][0] == expect


def test_helios_like_count_is_nonzero(db_overlay):
    assert run_query(db_overlay, "SELECT COUNT(*) FROM fact_settlement WHERE broker_name LIKE '%Helios%'").rows[0][0] > 0


def test_function_closure_over_all_reference_bank_and_pattern_sql(db_overlay, cat_overlay, date_ctx):
    from app.agent import patterns
    seen: set[str] = set()
    sqls = [golden_sql(q) for q in GOLDEN_IDS]
    sqls += [substitute(e.sql, date_ctx) for e in ExampleBank().examples]
    novel = GOLDEN / "novel_questions.json"
    if novel.exists():
        sqls += [n["sql"] for n in json.loads(novel.read_text())]
    sqls.append(patterns.build_attention(date_ctx, cat_overlay.glossary).sql)
    sqls.append(patterns.build_yoy(date_ctx, ["F001"]).sql)
    assert len(sqls) >= 40
    for sql in sqls:
        v = validate(sql, cat_overlay, MOCK_USERS["priya"])
        run_query(db_overlay, v.sql, record_functions=seen)
    assert seen and seen <= ALLOWED_FUNCTIONS, sorted(seen - ALLOWED_FUNCTIONS)
