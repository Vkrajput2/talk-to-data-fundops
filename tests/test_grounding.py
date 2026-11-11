from app.agent.narrate import (PLAIN_SUM, build_kpis, check_grounding, chart_for, currency_warnings, fallback_kpis,
                               partial_month_warning)
from app.agent.validate import validate
from app.db import run_query
from app.entitlements import MOCK_USERS
from app.models import KpiSpec
from tests.conftest import golden_sql

PRIYA = MOCK_USERS["priya"]


def test_grounded_narrative_has_no_warning():
    cols, rows = ["rate_yesterday_pct", "rate_4wk_avg_pct"], [[85.7, 98.1]]
    assert check_grounding("Yesterday's rate was 85.7% against 98.1% over four weeks.", cols, rows) == []


def test_fabricated_number_gives_exactly_one_warning():
    cols, rows = ["rate_yesterday_pct", "rate_4wk_avg_pct"], [[85.7, 98.1]]
    w = check_grounding("The rate was 85.7%, and 42 trades were affected.", cols, rows)
    assert w == ["Narrative mentions 42 which is not in the query result."]


def test_units_rounding_and_ratios():
    cols, rows = ["fail_value"], [[11050000]]
    assert check_grounding("Fail value is 11.05m.", cols, rows) == []
    assert check_grounding("Fail value is 11,050,000.", cols, rows) == []
    cols2, rows2 = ["amount"], [[5240000]]
    assert check_grounding("Largest is 5.1m", cols2, rows2) == ["Narrative mentions 5.1m which is not in the query result."]
    assert check_grounding("About 85.7% settled", ["r"], [[0.857]]) == []
    assert check_grounding("AUM is 5.13bn", ["aum_bn"], [[5.13]]) == []
    assert check_grounding("AUM is 5.13bn", ["aum"], [[5130000000]]) == []
    assert check_grounding("Revenue 3.2m", ["rev"], [[3137500]]) == ["Narrative mentions 3.2m which is not in the query result."]


def test_dates_ids_and_row_count_are_not_claims():
    cols, rows = ["break_id", "age_days"], [["BRK001", 13], ["BRK002", 9]]
    text = "On 2026-09-04 (4 September 2026, September 4, 4th Sep) BRK001 and BRK002 are open; 2 breaks in 2026-09 for F001."
    assert check_grounding(text, cols, rows) == []


def test_text_cells_do_not_contribute_numbers(db_overlay):
    r = run_query(db_overlay, golden_sql("Q3"))
    ok = "The largest fail is US Treasury 3.5% 2032 at 5,240,000 with Helios Securities."
    assert check_grounding(ok, r.columns, r.rows) == []
    assert check_grounding(ok + " and 7.7%", r.columns, r.rows) == ["Narrative mentions 7.7% which is not in the query result."]
    r6 = run_query(db_overlay, golden_sql("Q6"))
    assert check_grounding("Helvetia Bank 6.0% 2033 has been stale for 2 days", r6.columns, r6.rows) == []
    assert check_grounding("the 2032 Treasury failed", r.columns, r.rows) == []


def test_currency_plain_sum_warning_only_when_mixed_currency(db_overlay, cat_overlay):
    def warn(sql):
        v = validate(sql, cat_overlay, PRIYA)
        return currency_warnings(v, cat_overlay, db_overlay, PRIYA, "2026-09-04")
    assert warn(golden_sql("Q4F")) == [PLAIN_SUM]
    single = ("SELECT SUM(t.gross_amount) FROM fact_transaction t JOIN dim_fund f ON t.fund_id = f.fund_id "
              "WHERE f.fund_name = 'MS US Core Bond Fund'")
    assert warn(single) == []
    assert warn(golden_sql("Q7")) == []  # grouped by fund; ccy is a plain column of the group
    fx = ("SELECT SUM(t.gross_amount * fx.usd_rate) FROM fact_transaction t "
          "JOIN dim_fx_rate fx ON fx.ccy = t.ccy AND fx.rate_date = '2026-09-04'")
    assert warn(fx) == ["USD-equivalent at synthetic FX rates as of 2026-09-04."]
    assert warn("SELECT ccy, SUM(gross_amount) FROM fact_transaction GROUP BY ccy") == []


def test_partial_month_warning(cat_overlay, date_ctx):
    q7 = validate(golden_sql("Q7"), cat_overlay, PRIYA)
    assert partial_month_warning(q7, date_ctx) == [
        "2026-09 is month-to-date; comparisons with full months are not like-for-like."]
    assert partial_month_warning(validate(golden_sql("Q5"), cat_overlay, PRIYA), date_ctx) == []


def test_kpis_come_from_cells_never_from_the_model():
    cols, rows = ["rate_yesterday_pct", "rate_4wk_avg_pct"], [[85.7, 98.1]]
    k = build_kpis([KpiSpec(label="Yesterday", op="cell", column="rate_yesterday_pct", row=0, format="percent1")], cols, rows)
    assert [(x.label, x.value, x.display) for x in k] == [("Yesterday", 85.7, "85.7%")]
    # empty or invalid specs fall back to cell KPIs from the result
    for specs in ([], [KpiSpec(label="x", op="cell", column="nope", row=0)]):
        k = build_kpis(specs, cols, rows)
        assert [x.value for x in k] == [85.7, 98.1]
    k = build_kpis([], ["a"], [[1], [2], [3]])
    assert [(x.label, x.value) for x in k] == [("Rows returned", 3)]
    assert build_kpis([], ["a"], [])[0].value == 0
    k = build_kpis([KpiSpec(label="Total", op="sum", column="v", format="integer")], ["v"], [[1], [2], [3]])
    assert k[0].value == 6 and fallback_kpis(["a"], [])[0].display == "0"


def test_chart_rules_on_real_golden_results(db_overlay):
    r = run_query(db_overlay, golden_sql("Q7"))
    ch = chart_for(r.columns, r.rows)
    assert ch["type"] == "grouped_bar" and len(ch["x"]) == 6
    em = ch["x"].index("MS Emerging Markets Equity Fund")
    assert ch["series"][0]["values"][em] == -135000000 and ch["series"][1]["values"][em] == -25000000
    assert chart_for(["a"], [[1]]) is None
    ts = chart_for(["d", "v"], [["2026-09-01", 1], ["2026-09-02", 2], ["2026-09-03", 3]])
    assert ts["type"] == "line" and ts["series"][0]["values"] == [1, 2, 3]
