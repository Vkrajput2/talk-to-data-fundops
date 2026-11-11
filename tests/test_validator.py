import pytest

from app.agent.validate import ValidationError, validate
from app.entitlements import MOCK_USERS
from tests.conftest import GOLDEN_IDS, golden_sql

PRIYA = MOCK_USERS["priya"]


def code(sql, user=PRIYA, cat=None):
    with pytest.raises(ValidationError) as e:
        validate(sql, cat, user)
    return e.value.code, e.value.category


@pytest.mark.parametrize("qid", GOLDEN_IDS)
def test_accepts_every_golden_reference_sql(qid, cat_overlay):
    v = validate(golden_sql(qid), cat_overlay, PRIYA)
    assert v.sql.upper().startswith(("SELECT", "WITH"))
    assert "--" not in v.sql


def test_q10_has_seven_ctes_but_counts_only_physical_tables(cat_overlay):
    v = validate(golden_sql("Q10"), cat_overlay, PRIYA)
    assert sorted(v.tables_used) == sorted(["dim_fund", "fact_settlement", "fact_recon_break", "fact_cash_balance",
                                             "fact_pricing_exception", "fact_sla", "fact_nav", "fact_flows"])


@pytest.mark.parametrize("sql", [
    "SELECT fund_id FROM dim_fund WHERE region = 'EMEA' AND base_ccy = 'EUR'",
    "SELECT fund_id FROM dim_fund WHERE region = 'EMEA' OR region = 'US'",
    "SELECT f.fund_id FROM dim_fund f WHERE EXISTS (SELECT 1 FROM fact_nav n WHERE n.fund_id = f.fund_id)",
    "SELECT f.fund_id FROM dim_fund f WHERE NOT EXISTS (SELECT 1 FROM fact_sla s WHERE s.fund_id = f.fund_id)",
    "SELECT strftime('%Y-%m', nav_date) AS m, COUNT(*) FROM fact_nav GROUP BY 1",
    "SELECT instr(fund_name, 'Bond'), replace(fund_name, 'MS ', ''), ltrim(fund_name), rtrim(fund_name) FROM dim_fund",
    "SELECT max(a, b) FROM (SELECT 1 AS a, 2 AS b)",
    "SELECT substr(fund_name, 1, 5), iif(base_ccy = 'USD', 1, 0), coalesce(region, 'x'), printf('%d', 1) FROM dim_fund",
    "SELECT fund_name FROM dim_fund WHERE fund_name LIKE '%Bond%' OR fund_name GLOB 'MS*'",
    "SELECT CAST(target_aum_bn AS INTEGER), CASE WHEN target_aum_bn > 2 THEN 'big' ELSE 'small' END FROM dim_fund",
    "SELECT fund_id, ROW_NUMBER() OVER (PARTITION BY ccy ORDER BY nav_date) FROM fact_nav",
    'SELECT "fund_id" FROM "dim_fund"',
    "SELECT * FROM FACT_NAV",
    'SELECT * FROM "Fact_Nav"',
    "SELECT fund_id FROM dim_fund WHERE fund_id NOT IN (SELECT fund_id FROM fact_sla WHERE status = 'Pending')",
    "SELECT julianday('2026-09-04') - julianday('2026-08-10'), date('2026-09-04', '-7 days')",
])
def test_accepts_boolean_logic_exists_and_safe_functions(sql, cat_overlay):
    v = validate(sql, cat_overlay, PRIYA)
    assert v.sql


def test_strftime_output_keeps_format_string(cat_overlay):
    v = validate("SELECT strftime('%Y-%m', nav_date) FROM fact_nav", cat_overlay, PRIYA)
    assert "'%Y-%m'" in v.sql and "STRFTIME" in v.sql.upper()


@pytest.mark.parametrize("sql,expected", [
    ("DROP TABLE dim_fund", "statement_not_allowed"),
    ("INSERT INTO dim_fund VALUES (1)", "statement_not_allowed"),
    ("UPDATE dim_fund SET region='x'", "statement_not_allowed"),
    ("DELETE FROM fact_nav", "statement_not_allowed"),
    ("CREATE TABLE x AS SELECT 1", "statement_not_allowed"),
    ("SELECT 1; DROP TABLE dim_fund", "multiple_statements"),
    ("SELECT 1; SELECT 2", "multiple_statements"),
    ("WITH x AS (SELECT 1) DELETE FROM fact_nav", "statement_not_allowed"),
    ("PRAGMA table_info(dim_fund)", "statement_not_allowed"),
    ("PRAGMA writable_schema=1", "statement_not_allowed"),
    ("ATTACH DATABASE 'x.db' AS y", "statement_not_allowed"),
    ("SELECT * FROM sqlite_master", "system_table_not_allowed"),
    ('SELECT * FROM "sqlite_master"', "system_table_not_allowed"),
    ("SELECT * FROM [sqlite_master]", "system_table_not_allowed"),
    ("SELECT * FROM sqlite_schema", "system_table_not_allowed"),
    ("SELECT * FROM main.sqlite_master", "qualified_table_not_allowed"),
    ('SELECT * FROM main."dim_fund"', "qualified_table_not_allowed"),
    ("SELECT fund_id FROM fact_nav UNION SELECT name FROM sqlite_master", "system_table_not_allowed"),
    ("SELECT fund_id FROM fact_nav UNION SELECT x FROM secret_table", "unknown_table"),
    ("SELECT load_extension('x')", "function_not_allowed"),
    ("SELECT readfile('x')", "function_not_allowed"),
    ("SELECT randomblob(10)", "function_not_allowed"),
    ("SELECT * FROM pragma_table_info('dim_fund')", "table_function_not_allowed"),
    ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT n FROM r", "construct_not_allowed"),
    ("WITH fact_nav AS (SELECT 1 AS a) SELECT a FROM fact_nav", "cte_shadows_table"),
    ("SELECT * FROM dim_fund WHERE fund_id = ?", "construct_not_allowed"),
    ("SELECT * FROM dim_fund WHERE fund_id = :p", "construct_not_allowed"),
    ("SELECT * FROM nope", "unknown_table"),
    ("SELECT nope FROM dim_fund", "unknown_column"),
    ("SELECT fund_id FROM dim_fund d JOIN fact_nav n ON d.fund_id = n.fund_id", "ambiguous_column"),
    ("   ", "sql_empty"),
    ("SELECT " + "1," * 4000 + "1", "sql_too_long"),
])
def test_rejections_with_stable_codes(sql, expected, cat_overlay):
    got, _ = code(sql, cat=cat_overlay)
    assert got == expected


def test_comment_tricks_do_not_hide_statements(cat_overlay):
    assert code("SELECT 1; -- harmless\nDROP TABLE dim_fund", cat=cat_overlay)[0] == "multiple_statements"
    assert code("DR/**/OP TABLE dim_fund", cat=cat_overlay)[1] in ("security", "schema")
    v = validate("SELECT fund_id /* hello */ FROM dim_fund -- tail", cat_overlay, PRIYA)
    assert "hello" not in v.sql and "tail" not in v.sql


def test_security_vs_schema_categories(cat_overlay):
    assert code("DROP TABLE dim_fund", cat=cat_overlay)[1] == "security"
    assert code("SELECT nope FROM dim_fund", cat=cat_overlay)[1] == "schema"
    assert code("SELECT ceil(target_aum_bn) FROM dim_fund", cat=cat_overlay) == ("function_not_allowed", "schema")
    assert code("SELECT load_extension('x')", cat=cat_overlay)[1] == "security"


def test_table_limit_counts_distinct_physical_tables_not_cte_references(cat_overlay):
    ctes = ", ".join(f"c{i} AS (SELECT fund_id FROM fact_nav)" for i in range(20))
    joins = " ".join(f"JOIN c{i} ON c{i}.fund_id = f.fund_id" for i in range(20))
    sql = f"WITH {ctes} SELECT f.fund_id FROM dim_fund f {joins}"
    assert sorted(validate(sql, cat_overlay, PRIYA).tables_used) == ["dim_fund", "fact_nav"]  # 22 refs, 2 physical tables
    names = ["dim_fund", "dim_security", "dim_counterparty", "dim_date", "dim_fx_rate", "fact_transaction", "fact_settlement",
             "fact_nav", "fact_cash_balance", "fact_flows", "fact_recon_break", "fact_pricing_exception", "fact_revenue"]
    cross = "SELECT 1 FROM " + ", ".join(f"{n} t{i}" for i, n in enumerate(names))
    assert code(cross, cat=cat_overlay) == ("too_many_tables", "security")  # 13 distinct tables


def test_limit_enforcement(cat_overlay):
    cap = 500
    assert validate("SELECT * FROM dim_fund", cat_overlay, PRIYA, cap).sql.endswith("LIMIT 501")
    assert validate("SELECT * FROM dim_fund LIMIT 100000", cat_overlay, PRIYA, cap).sql.endswith("LIMIT 501")
    assert validate("SELECT * FROM dim_fund LIMIT 3", cat_overlay, PRIYA, cap).sql.endswith("LIMIT 3")
    assert validate("SELECT * FROM dim_fund LIMIT 500", cat_overlay, PRIYA, cap).sql.endswith("LIMIT 500")
    u = validate("SELECT fund_id FROM dim_fund UNION SELECT fund_id FROM fact_nav", cat_overlay, PRIYA, cap)
    assert u.sql.endswith("LIMIT 501")


def test_provenance_columns_and_join_pairs(cat_overlay):
    v = validate(golden_sql("Q3"), cat_overlay, PRIYA)
    assert "fact_settlement.txn_id = fact_transaction.txn_id" in v.join_pairs
    assert "fact_settlement.fund_id = dim_fund.fund_id" in v.join_pairs
    assert "gross_amount" in v.columns_by_table["fact_transaction"]
    assert "fail_reason_code" in v.columns_by_table["fact_settlement"]


def test_emitted_sql_revalidates(cat_overlay):
    for qid in GOLDEN_IDS:
        v = validate(golden_sql(qid), cat_overlay, PRIYA)
        again = validate(v.sql, cat_overlay, PRIYA)
        assert again.tables_used and again.sql
