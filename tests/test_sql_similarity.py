from tests.conftest import golden_sql
from tests.sql_similarity import compare, features

REF = golden_sql("Q3")


def test_features_of_q3_reference():
    f = features(REF)
    assert f["tables"] == {"fact_settlement", "fact_transaction", "dim_fund"}
    assert ("fact_settlement.txn_id", "fact_transaction.txn_id") in f["joins"]
    assert ("fact_settlement.fund_id", "dim_fund.fund_id") in f["joins"] or ("dim_fund.fund_id", "fact_settlement.fund_id") in f["joins"]
    assert ("status", "EQ", ("failed",)) in f["predicates"] and ("settlement_date", "EQ", ("2026-09-04",)) in f["predicates"]


def test_identical_and_reformatted_sql_score_one():
    s = compare(REF, REF.lower().replace("\n", " "))
    assert (s.tables_match, s.joins_match, s.predicate_match, s.aggregation_match, s.overall) == (True, True, True, True, 1.0)


def test_missing_join_table_and_predicate_are_reported():
    gen = ("SELECT s.broker_name, s.fail_reason_code FROM fact_settlement s WHERE s.settlement_date = '2026-09-04'")
    s = compare(REF, gen)
    assert not s.tables_match and not s.joins_match and not s.predicate_match
    assert s.details["missing_tables"] == ["dim_fund", "fact_transaction"]
    assert ("status", "EQ") in s.details["missing_predicates"]
    assert 0 < s.overall < 0.6


def test_aggregation_difference():
    ref = "SELECT f.fund_name, SUM(t.gross_amount) FROM fact_transaction t JOIN dim_fund f ON t.fund_id = f.fund_id GROUP BY 1"
    gen = "SELECT f.fund_name, AVG(t.gross_amount) FROM fact_transaction t JOIN dim_fund f ON t.fund_id = f.fund_id GROUP BY 1"
    s = compare(ref, gen)
    assert s.tables_match and s.joins_match and not s.aggregation_match and s.details["missing_aggs"] == ["Sum"]


def test_unparsable_generated_sql_scores_zero():
    s = compare(REF, "SELEC nonsense (")
    assert s.overall == 0.0 and not s.tables_match and "error" in s.details
