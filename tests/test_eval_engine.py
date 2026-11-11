"""The live-evaluation engine and model-comparison tooling, exercised with a scripted (offline) model."""
import json
import sys

from app.config import Settings
from app.llm import LLMBadOutput, LLMRateLimited, ScriptedLLM
from tests.conftest import DATA, ROOT
from tests.helpers import make_llm, make_orch
from tests.live_eval import build_report, classify, evaluate_item, load_items, render_md, summarise, write_reports

sys.path.insert(0, str(ROOT / "scripts"))
import eval_models  # noqa: E402

ITEMS = {i.id: i for i in load_items(("golden", "paraphrase", "novel"))}


def test_item_sets_have_the_required_sizes():
    sets = {}
    for i in ITEMS.values():
        sets[i.set] = sets.get(i.set, 0) + 1
    assert sets == {"golden_full_bank": 11, "paraphrase": 33, "novel": 12}
    assert ITEMS["Q4F"].setup == ["Q4"] and ITEMS["Q4F-p1"].setup == ["Q4"] and ITEMS["Q4F-p1"].base == "Q4F"


def test_correct_sql_passes_and_is_fully_similar(db_overlay):
    orch = make_orch(db_overlay, make_llm([ITEMS["Q5"].ref_sql]))
    res = evaluate_item(orch, ITEMS["Q5"])
    assert res["passed"] and res["strict"] and res["match_pct"] == 100.0 and res["repairs"] == 0
    assert res["similarity"]["tables"] and res["similarity"]["joins"] and res["similarity"]["overall"] == 1.0
    assert res["failure_class"] == "" and res["tables"] == ["fact_recon_break", "dim_fund"]


def test_wrong_predicate_is_classified_and_reported(db_overlay):
    wrong = ITEMS["Q5"].ref_sql.replace("b.age_days>5", "b.age_days>7")
    res = evaluate_item(make_orch(db_overlay, make_llm([wrong])), ITEMS["Q5"])
    assert not res["passed"] and res["executed"] and res["failure_class"] == "result_mismatch"
    assert "rows 3 vs 4" in res["failure_reason"]


def test_wrong_table_choice_is_classified(db_overlay):
    wrong = "SELECT s.broker_name, COUNT(*) AS fails, 0 AS fail_value FROM fact_settlement s WHERE s.status = 'Failed' GROUP BY s.broker_name"
    res = evaluate_item(make_orch(db_overlay, make_llm([wrong])), ITEMS["Q4F"])
    assert not res["passed"] and res["failure_class"] == "wrong_tables_or_joins"


def test_syntax_failure_and_repairs_are_counted(db_overlay):
    res = evaluate_item(make_orch(db_overlay, make_llm(["SELECT x FROM nowhere"])), ITEMS["Q1"])
    assert not res["passed"] and res["failure_class"] == "sql_syntax_or_validation" and res["repairs"] == 2
    assert res["error_code"] == "sql_failed"
    res = evaluate_item(make_orch(db_overlay, make_llm(["SELEC fund_name FROM dim_fund", ITEMS["Q9"].ref_sql])), ITEMS["Q9"])
    assert res["passed"] and res["repairs"] == 1


def test_model_errors_are_classified(db_overlay):
    bad = make_llm(["x"], extra={"understand": lambda s, u, i: LLMBadOutput("x"), "resolve": lambda s, u, i: LLMBadOutput("x"),
                                 "generate": lambda s, u, i: LLMBadOutput("x")})
    assert evaluate_item(make_orch(db_overlay, bad), ITEMS["Q1"])["failure_class"] == "invalid_json_format"
    rl = make_llm(["x"], extra={"understand": lambda s, u, i: LLMRateLimited("x")})
    assert evaluate_item(make_orch(db_overlay, rl), ITEMS["Q1"])["failure_class"] == "provider_error"


def test_followup_item_asks_setup_question_in_same_session(db_overlay):
    llm = make_llm(lambda q, i: ITEMS["Q4"].ref_sql if "settlement rate" in q else ITEMS["Q4F"].ref_sql)
    res = evaluate_item(make_orch(db_overlay, llm), ITEMS["Q4F-p1"])
    assert res["passed"] and res["ref_row_count"] == 2


def test_report_files_contain_model_and_per_question_details(db_overlay, tmp_path):
    orch = make_orch(db_overlay, make_llm([ITEMS["Q9"].ref_sql]))
    ok = evaluate_item(orch, ITEMS["Q9"])
    bad = evaluate_item(make_orch(db_overlay, make_llm(["SELECT 1 AS x"])), ITEMS["Q2"])
    rep = build_report(Settings(provider="groq", model="m-1"), [ok, bad])
    write_reports(rep, tmp_path)
    j = json.loads((tmp_path / "live_eval.json").read_text())
    assert (j["provider"], j["model"]) == ("groq", "m-1") and j["summary"]["golden_full_bank"] == {
        "passed": 1, "total": 2, "rate": 0.5, "executed": 2}
    md = (tmp_path / "live_eval.md").read_text()
    assert "groq / m-1" in md and "| Q9 |" in md and "Failures in detail" in md and "### Q2" in md and "SELECT 1 AS x" in md
    assert "key" not in json.dumps(j).lower().replace("key_columns", "")


def test_model_comparison_scopes_failures_per_question(db_overlay):
    def run(model, sql_for):
        items = [ITEMS["Q9"], ITEMS["Q2"]]
        orch = make_orch(db_overlay, make_llm(lambda q, i: sql_for(q)))
        return build_report(Settings(provider="groq", model=model), [evaluate_item(orch, it) for it in items])
    good = run("a", lambda q: ITEMS["Q9"].ref_sql if "SLA" in q else ITEMS["Q2"].ref_sql)
    half = run("b", lambda q: ITEMS["Q9"].ref_sql if "SLA" in q else "SELECT fund_name, 0 AS cash_pct_aum FROM dim_fund")
    per = {"a": good, "b": half}
    scope = eval_models.classify_scope(per)
    assert scope == {"Q9": "all models pass", "Q2": "differs by model -> model-specific"}
    md = eval_models.render(["a", "b"], per)
    assert "| a | 2/2 (100%)" in md and "| b | 1/2 (50%)" in md and "model-specific" in md and "FAIL[" in md
    assert eval_models.classify_scope({"a": half, "b": half})["Q2"] == "fails on all models -> prompt/data-specific"


def test_classify_and_summarise_pure_functions():
    assert classify({"passed": True}) == ""
    assert classify({"passed": False, "similarity": {"details": {"missing_aggs": ["Sum"]}}}) == "wrong_aggregation"
    assert classify({"passed": False, "similarity": {"details": {"missing_predicates": [("a", "EQ")]}}}) == "wrong_predicates_or_values"
    s = summarise([{"set": "x", "passed": True, "executed": True}, {"set": "x", "passed": False, "executed": True}])
    assert s == {"x": {"passed": 1, "total": 2, "rate": 0.5, "executed": 2}}
