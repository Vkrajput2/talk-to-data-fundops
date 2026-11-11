"""End-to-end tests through the REAL model. Run with:  py -m pytest -m live

Needs an API key in the environment (LLM_API_KEY / GROQ_API_KEY / GROK_API_DEV_KEY, or a local .env).
Writes tests/reports/live_eval.json and live_eval.md (even when tests fail). Failures are real failures.
"""
import os

import pytest

from app.config import load_settings
from tests.live_eval import (build_orchestrator, build_report, evaluate_item, load_items, summarise, write_reports, ROOT)

pytestmark = pytest.mark.live

RESULTS: list[dict] = []
ITEMS = load_items(("golden", "paraphrase", "novel"))
BANK_MODE = os.environ.get("LIVE_BANK_MODE", "full")  # full | leave_one_out


@pytest.fixture(scope="module")
def settings():
    s = load_settings()
    if not s.llm_configured:
        pytest.skip("no LLM API key configured in the environment")
    return s


@pytest.fixture(scope="module")
def orch(settings):
    return build_orchestrator(settings)


@pytest.fixture(scope="module", autouse=True)
def write_report_at_end(settings):
    yield
    if RESULTS:
        write_reports(build_report(settings, RESULTS, BANK_MODE), ROOT / "tests" / "reports")


@pytest.mark.parametrize("item", ITEMS, ids=[i.id for i in ITEMS])
def test_agent_matches_reference_result(item, orch):
    orch.exclude_bank_ids = frozenset({item.base}) if BANK_MODE == "leave_one_out" and item.base else frozenset()
    res = evaluate_item(orch, item)
    RESULTS.append(res)
    assert res["passed"], f"{item.id} [{res['failure_class']}] {res['failure_reason']} | sql: {res['sql'][:300]}"


def test_accuracy_bars_are_met():
    """Agent bar: >= 10/11 golden, and >= 80% of paraphrase + novel questions (honest, not hidden)."""
    if not RESULTS:
        pytest.skip("no live results collected")
    summ = summarise(RESULTS)
    g = summ.get("golden_full_bank")
    if g:
        assert g["passed"] >= 10, summ
    rest = [i for i in RESULTS if i["set"] in ("paraphrase", "novel")]
    if rest:
        assert sum(1 for i in rest if i["passed"]) / len(rest) >= 0.8, summ
