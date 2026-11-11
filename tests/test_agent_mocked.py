import pytest

from app.llm import LLMBadOutput, LLMNotConfigured, LLMUnavailable
from tests.conftest import GOLDEN_IDS, golden_expected, golden_sql, rows_equal
from tests.helpers import ask, make_llm, make_orch

QUESTIONS = {
    "Q1": "List all active funds with region, strategy and latest AUM.",
    "Q2": "Which funds have available cash below 2% of AUM today?",
    "Q3": "Show all trades that failed to settle yesterday.",
    "Q4": "What is our settlement rate yesterday versus the four-week average?",
    "Q5": "Which reconciliation breaks are open and older than five days, and who owns them?",
    "Q6": "Are there any stale prices today that could put a NAV at risk?",
    "Q7": "Show this month's net flows by fund and how they compare with last month.",
    "Q9": "Which operational processes are at risk of breaching SLA today?",
}


@pytest.mark.parametrize("qid", sorted(QUESTIONS))
def test_pipeline_returns_reference_rows_with_provenance(qid, db_overlay):
    llm = make_llm([golden_sql(qid)], understand={"time_refs": ["yesterday"] if qid in ("Q3", "Q4") else []})
    r = ask(make_orch(db_overlay, llm), QUESTIONS[qid])
    exp = golden_expected()[qid]
    assert r.error is None and r.route == "llm"
    assert rows_equal(r.rows, exp["rows"], ordered=exp["ordered"])
    assert r.columns == exp["columns"]
    assert r.sources and all(s.why and s.table for s in r.sources)
    assert r.sql and r.sql != golden_sql(qid) and "LIMIT 501" in r.sql  # SQL shown is the validated, emitted statement
    assert r.row_count == len(exp["rows"]) and r.truncated is False
    assert r.kpis and r.narrative == "The query returned rows."


def test_sources_have_join_keys_columns_and_why(db_overlay):
    r = ask(make_orch(db_overlay, make_llm([golden_sql("Q3")])), QUESTIONS["Q3"])
    by = {s.table: s for s in r.sources}
    assert set(by) == {"fact_settlement", "fact_transaction", "dim_fund"}
    assert "fact_settlement.txn_id = fact_transaction.txn_id" in by["fact_transaction"].join_keys
    assert "txn_id" in by["fact_transaction"].columns and "gross_amount" in by["fact_transaction"].columns
    assert by["dim_fund"].why == "Fund names"  # stage-2 reason for tables the model chose
    assert by["fact_settlement"].why  # falls back to the catalog role when the model gave none
    assert all(s.synthetic == "no" for s in r.sources)


def test_interpreted_question_gets_resolved_date_suffix_from_code(db_overlay):
    llm = make_llm([golden_sql("Q3")], understand={"time_refs": ["yesterday", "this_month"], "interpreted_question": "Failed trades"})
    r = ask(make_orch(db_overlay, llm), QUESTIONS["Q3"])
    assert r.interpreted_question == "Failed trades (yesterday = 2026-09-04; this month = 2026-09)"
    r2 = ask(make_orch(db_overlay, make_llm([golden_sql("Q1")])), QUESTIONS["Q1"])
    assert "(" not in r2.interpreted_question


def test_synthetic_badge_values_on_sources(db_overlay):
    sql = "SELECT f.fund_name, d.flow_type, SUM(d.amount) AS total FROM fact_flow_detail d JOIN dim_fund f ON d.fund_id = f.fund_id GROUP BY 1, 2"
    r = ask(make_orch(db_overlay, make_llm([sql])), "break down flows by type")
    by = {s.table: s.synthetic for s in r.sources}
    assert by == {"fact_flow_detail": "full", "dim_fund": "no"}
    r = ask(make_orch(db_overlay, make_llm(["SELECT nav_date, total_net_assets FROM fact_nav WHERE fund_id = 'F001'"])), "nav history")
    assert r.sources[0].synthetic == "partial"


def test_repair_succeeds_after_one_repair_call(db_overlay):
    llm = make_llm(["SELECT fund_idd FROM dim_fund", "SELECT fund_id FROM dim_fund ORDER BY fund_id"])
    r = ask(make_orch(db_overlay, llm), "list fund ids")
    assert r.error is None and [x[0] for x in r.rows] == ["F001", "F002", "F003", "F004", "F005", "F006"]
    assert llm.counts["generate"] == 1 and llm.counts["repair"] == 1
    repair_prompts = [c[2] for c in llm.calls if c[0] == "repair"]
    assert repair_prompts and "Unknown column fund_idd" in repair_prompts[0] and "SELECT fund_idd FROM dim_fund" in repair_prompts[0]


def test_bad_syntax_is_repaired_from_parser_message(db_overlay):
    llm = make_llm(["SELEC fund_id FROM dim_fund", "SELECT fund_id FROM dim_fund"])
    r = ask(make_orch(db_overlay, llm), "list fund ids")
    assert r.error is None and len(r.rows) == 6
    assert "syntax" in [c[2] for c in llm.calls if c[0] == "repair"][0].lower()


def test_repair_gives_up_after_two_repairs_with_clean_error(db_overlay):
    llm = make_llm(["SELECT a FROM dim_fund", "SELECT b FROM dim_fund", "SELECT c FROM dim_fund"])
    r = ask(make_orch(db_overlay, llm), "list things")
    assert r.error.code == "sql_failed" and r.rows == [] and r.columns == []
    assert llm.counts["repair"] == 2 and "SELECT c FROM dim_fund" in r.sql
    assert any("failed after 3 attempt" in w for w in r.warnings)
    assert r.sources and all(s.why.startswith("Attempted: ") for s in r.sources)
    assert r.narrative


def test_security_rejection_is_never_repaired(db_overlay):
    llm = make_llm(["DROP TABLE dim_fund"])
    r = ask(make_orch(db_overlay, llm), "please drop the table")
    assert r.error.code == "sql_rejected" and r.rows == []
    assert llm.counts.get("repair", 0) == 0 and llm.counts["generate"] == 1
    assert r.sql == "DROP TABLE dim_fund"
    r2 = ask(make_orch(db_overlay, make_llm(["SELECT load_extension('x')"])), "run an extension")
    assert r2.error.code == "sql_rejected"


def test_zero_rows_is_a_valid_answer(db_overlay):
    llm = make_llm(["SELECT fund_id FROM dim_fund WHERE fund_id = 'F999'"])
    r = ask(make_orch(db_overlay, llm), "show fund F999")
    assert r.error is None and r.rows == [] and r.row_count == 0
    assert llm.counts.get("repair", 0) == 0
    assert [(k.label, k.value) for k in r.kpis] == [("Rows returned", 0)]


def test_needs_clarification_makes_no_sql_call(db_overlay):
    llm = make_llm([golden_sql("Q1")], understand={"needs_clarification": True, "clarification_question": "Which fund do you mean?"})
    r = ask(make_orch(db_overlay, llm), "tell me stuff")
    assert r.narrative == "Which fund do you mean?" and r.sources == [] and r.sql == "" and r.route == "clarification"
    assert "Clarification needed" in r.warnings and llm.counts.get("generate", 0) == 0


def test_narrate_failure_degrades_gracefully(db_overlay):
    def boom(system, user, i):
        return LLMUnavailable("down")
    llm = make_llm([golden_sql("Q2")], extra={"narrate": boom})
    r = ask(make_orch(db_overlay, llm), QUESTIONS["Q2"])
    assert r.narrative == "The query returned 2 rows." and "Narrative unavailable: llm_unavailable" in r.warnings
    assert [(k.label, k.value) for k in r.kpis] == [("Rows returned", 2)]


def test_ungrounded_narrative_number_is_flagged(db_overlay):
    llm = make_llm([golden_sql("Q2")], narrate=lambda u, i: {"narrative": "2 funds are tight; US Core Bond holds 1.24% and 99 breaks exist.", "kpis": []})
    r = ask(make_orch(db_overlay, llm), QUESTIONS["Q2"])
    assert r.warnings.count("Narrative mentions 99 which is not in the query result.") == 1
    assert not [w for w in r.warnings if "1.24" in w]


def test_kpi_fallback_when_model_returns_none(db_overlay):
    llm = make_llm([golden_sql("Q4")], narrate=lambda u, i: {"narrative": "n", "kpis": []})
    r = ask(make_orch(db_overlay, llm), QUESTIONS["Q4"])
    assert [k.value for k in r.kpis] == [85.7, 98.1]
    llm = make_llm([golden_sql("Q3")], narrate=lambda u, i: {"narrative": "n", "kpis": [{"label": "x", "op": "cell", "column": "nope", "row": 0}]})
    r = ask(make_orch(db_overlay, llm), QUESTIONS["Q3"])
    assert [(k.label, k.value) for k in r.kpis] == [("Rows returned", 5)]


def test_q4_gets_settlement_rate_trend_chart(db_overlay):
    r = ask(make_orch(db_overlay, make_llm([golden_sql("Q4")])), QUESTIONS["Q4"])
    assert r.chart["type"] == "line" and len(r.chart["x"]) == 20 and r.chart["series"][0]["values"][-1] == 85.7
    r7 = ask(make_orch(db_overlay, make_llm([golden_sql("Q7")])), QUESTIONS["Q7"])
    assert r7.chart["type"] == "grouped_bar" and any("month-to-date" in w for w in r7.warnings)


def test_q4f_carries_plain_sum_currency_warning(db_overlay):
    r = ask(make_orch(db_overlay, make_llm([golden_sql("Q4F")])), "Break yesterday's fails down by broker")
    assert r.rows[0] == ["Helios Securities", 4, 11050000]
    assert any("without FX conversion" in w for w in r.warnings)


def test_truncation_adds_warning(db_overlay):
    r = ask(make_orch(db_overlay, make_llm(["SELECT * FROM fact_transaction"]), row_cap=50), "all trades")
    assert r.truncated is True and r.row_count == 50 and "Result limited to 50 rows." in r.warnings


def test_llm_errors_before_sql_propagate(db_overlay):
    def nope(system, user, i):
        return LLMNotConfigured("no key")
    with pytest.raises(LLMNotConfigured):
        ask(make_orch(db_overlay, make_llm([golden_sql("Q1")], extra={"understand": nope})), "list funds")


def test_bad_understand_output_degrades_to_plain_question(db_overlay):
    def bad(system, user, i):
        return LLMBadOutput("x")
    r = ask(make_orch(db_overlay, make_llm([golden_sql("Q1")], extra={"understand": bad})), QUESTIONS["Q1"])
    assert r.error is None and len(r.rows) == 6 and r.intent == "lookup"


def test_request_deadline_before_sql_raises_unavailable(db_overlay):
    ticks = iter(range(0, 10_000, 1000))
    orch = make_orch(db_overlay, make_llm([golden_sql("Q1")]), request_deadline_s=90)
    orch.clock = lambda: next(ticks)
    with pytest.raises(LLMUnavailable):
        ask(orch, QUESTIONS["Q1"])


def test_request_deadline_after_sql_degrades_narrative(db_overlay):
    llm = make_llm([golden_sql("Q2")])
    orch = make_orch(db_overlay, llm, request_deadline_s=90)
    orch.clock = lambda: 1000 if llm.counts.get("generate", 0) >= 1 else 0  # time runs out once SQL exists
    r = ask(orch, QUESTIONS["Q2"])
    assert r.error is None and len(r.rows) == 2
    assert "Narrative unavailable: the request time limit was reached." in r.warnings
    assert llm.counts.get("narrate", 0) == 0
