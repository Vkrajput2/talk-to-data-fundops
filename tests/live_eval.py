"""Shared live-evaluation engine (used by tests/test_live_agent.py and scripts/eval_models.py)."""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.memory import SessionStore
from app.agent.orchestrator import Orchestrator
from app.agent.validate import validate
from app.config import Settings
from app.db import ensure_database, run_query
from app.entitlements import MOCK_USERS
from app.llm import LLM, LLMBadOutput, LLMError
from app.metadata import AliasIndex, build_catalog, compute_date_context
from app.models import AskRequest

from tests.comparator import compare_results
from tests.sql_similarity import compare as sql_compare

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
GOLDEN_IDS = ["Q1", "Q2", "Q3", "Q4", "Q4F", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10"]
GOLDEN_Q = {
    "Q1": "List all active funds with region, strategy and latest AUM.",
    "Q2": "Which funds have available cash below 2% of AUM today?",
    "Q3": "Show all trades that failed to settle yesterday.",
    "Q4": "What is our settlement rate yesterday versus the four-week average?",
    "Q4F": "Break yesterday's fails down by broker.",
    "Q5": "Which reconciliation breaks are open and older than five days, and who owns them?",
    "Q6": "Are there any stale prices today that could put a NAV at risk?",
    "Q7": "Show this month's net flows by fund and how they compare with last month.",
    "Q8": "What drove the year-on-year change in management-fee revenue for the Global Equity Alpha Fund?",
    "Q9": "Which operational processes are at risk of breaching SLA today?",
    "Q10": "Which three funds need attention today, and why?",
}
ORDERED = {"Q1": True, "Q2": True, "Q3": True, "Q4": True, "Q4F": True, "Q5": True, "Q6": False, "Q7": True,
           "Q8": True, "Q9": True, "Q10": True}


@dataclass
class Item:
    id: str
    set: str
    question: str
    ref_sql: str
    ordered: bool
    setup: list[str] = field(default_factory=list)  # golden ids asked first in the same session
    base: str | None = None  # golden id for paraphrases (leave-one-out)


def load_items(sets=("golden", "paraphrase", "novel")) -> list[Item]:
    sql = {q: (GOLDEN / f"{q}.sql").read_text(encoding="utf-8") for q in GOLDEN_IDS}
    items: list[Item] = []
    if "golden" in sets:
        for q in GOLDEN_IDS:
            items.append(Item(q, "golden_full_bank", GOLDEN_Q[q], sql[q], ORDERED[q], ["Q4"] if q == "Q4F" else [], q))
    if "paraphrase" in sets:
        for base, ps in json.loads((GOLDEN / "paraphrases.json").read_text()).items():
            for p in ps:
                items.append(Item(p["id"], "paraphrase", p["question"], sql[base], ORDERED[base], p.get("setup", []), base))
    if "novel" in sets:
        for n in json.loads((GOLDEN / "novel_questions.json").read_text()):
            items.append(Item(n["id"], "novel", n["question"], n["sql"], n["ordered"]))
    return items


def build_orchestrator(settings: Settings, exclude_ids=frozenset()) -> Orchestrator:
    ensure_database(settings.data_dir, settings.db_path, settings.use_supplement)
    cat = build_catalog(settings.data_dir, settings.use_supplement)
    orch = Orchestrator(settings, cat, LLM(settings), compute_date_context(settings.db_path, settings.as_of_date,
                                                                          settings.yesterday_date),
                        AliasIndex(settings.db_path, cat.glossary), SessionStore())
    orch.exclude_bank_ids = frozenset(exclude_ids)
    return orch


def classify(res: dict) -> str:
    """Automatic failure classification: where did it go wrong?"""
    if res["passed"]:
        return ""
    err = res.get("exception")
    if err in ("llm_bad_output",):
        return "invalid_json_format"
    if err in ("llm_rate_limited", "llm_unavailable", "llm_auth_failed", "llm_not_configured"):
        return "provider_error"
    if res.get("error_code") in ("sql_failed", "sql_rejected"):
        return "sql_syntax_or_validation"
    sim = res.get("similarity") or {}
    d = sim.get("details", {})
    if d.get("error"):
        return "sql_syntax_or_validation"
    if d.get("missing_tables") or d.get("missing_joins"):
        return "wrong_tables_or_joins"
    if d.get("missing_predicates"):
        return "wrong_predicates_or_values"
    if d.get("missing_aggs"):
        return "wrong_aggregation"
    return "result_mismatch"


def evaluate_item(orch: Orchestrator, item: Item, tol: float = 0.011) -> dict:
    user = MOCK_USERS["priya"]
    ref = run_query(orch.s.db_path, validate(item.ref_sql, orch.catalog, user).sql)
    out = {"id": item.id, "set": item.set, "question": item.question, "route": None, "executed": False,
           "passed": False, "strict": False, "match_pct": 0.0, "repairs": 0, "latency_ms": 0, "sql": "",
           "ref_sql": item.ref_sql.strip(), "failure_reason": "", "failure_class": "", "tables": [],
           "similarity": None, "error_code": None, "exception": None, "columns": [], "rows_preview": [],
           "ref_rows_preview": ref.rows[:5], "ref_row_count": len(ref.rows), "row_count": 0}
    t0 = time.monotonic()
    sid = None
    try:
        for s in item.setup:
            r0 = orch.ask(AskRequest(question=GOLDEN_Q[s], user_id="priya", session_id=sid))
            sid = r0.session_id
        repairs_before = orch.llm.counts.get("repair", 0)
        resp = orch.ask(AskRequest(question=item.question, user_id="priya", session_id=sid))
    except LLMError as e:
        out["exception"] = e.code
        out["failure_reason"] = f"{e.code}: {str(e)[:160]}"
        out["latency_ms"] = int((time.monotonic() - t0) * 1000)
        out["failure_class"] = classify(out)
        return out
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    out.update(route=resp.route, sql=resp.sql, tables=[s.table for s in resp.sources], columns=resp.columns,
               rows_preview=resp.rows[:5], row_count=resp.row_count, error_code=resp.error.code if resp.error else None)
    out["repairs"] = orch.llm.counts.get("repair", 0) - repairs_before
    if resp.error:
        out["failure_reason"] = f"{resp.error.code}: {resp.error.message[:200]}"
    else:
        out["executed"] = True
        cmpres = compare_results(ref.columns, ref.rows, resp.columns, resp.rows, item.ordered, tol)
        out.update(passed=cmpres.passed, strict=cmpres.strict, match_pct=round(cmpres.result_match_pct, 1))
        if not cmpres.passed:
            out["failure_reason"] = (f"rows {len(resp.rows)} vs {len(ref.rows)}; match {cmpres.result_match_pct:.0f}%; "
                                     f"unmatched columns {cmpres.unmatched_ref_columns}")
    sim = sql_compare(item.ref_sql, resp.sql or "SELECT", None) if resp.sql else None
    if sim:
        out["similarity"] = {"tables": sim.tables_match, "joins": sim.joins_match, "predicates": sim.predicate_match,
                             "aggregation": sim.aggregation_match, "overall": sim.overall, "details": sim.details}
    out["failure_class"] = classify(out)
    return out


def summarise(items: list[dict]) -> dict:
    out = {}
    for s in sorted({i["set"] for i in items}):
        sub = [i for i in items if i["set"] == s]
        n, ok = len(sub), sum(1 for i in sub if i["passed"])
        out[s] = {"passed": ok, "total": n, "rate": round(ok / n, 3) if n else 0.0,
                  "executed": sum(1 for i in sub if i["executed"])}
    return out


def run_eval(settings: Settings, items: list[Item], bank_mode: str = "full", progress=None) -> dict:
    results = []
    orch = build_orchestrator(settings)
    for n, it in enumerate(items, 1):
        orch.exclude_bank_ids = frozenset({it.base}) if bank_mode == "leave_one_out" and it.base else frozenset()
        res = evaluate_item(orch, it)
        results.append(res)
        if progress:
            progress(n, len(items), res)
    return build_report(settings, results, bank_mode)


def build_report(settings: Settings, results: list[dict], bank_mode: str = "full") -> dict:
    return {"provider": settings.provider, "model": settings.model,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "supplement": settings.use_supplement, "bank_mode": bank_mode,
            "summary": summarise(results), "items": results}


def write_reports(report: dict, out_dir: Path, stem: str = "live_eval") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.json").write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    (out_dir / f"{stem}.md").write_text(render_md(report), encoding="utf-8")


def render_md(r: dict) -> str:
    L = [f"# Live evaluation: {r['provider']} / {r['model']}", "",
         f"- Timestamp: {r['timestamp']}", f"- Supplement loaded: {r['supplement']}", f"- Example bank mode: {r['bank_mode']}", "",
         "## Summary", "", "| Set | Passed | Total | Rate | Executed |", "|---|---|---|---|---|"]
    for s, v in r["summary"].items():
        L.append(f"| {s} | {v['passed']} | {v['total']} | {v['rate']:.0%} | {v['executed']} |")
    L += ["", "## Per question", "",
          "| id | set | executed | match % | tables | joins | predicates | aggs | repairs | latency ms | route | failure |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    yn = lambda b: "-" if b is None else ("yes" if b else "NO")
    for i in r["items"]:
        s = i.get("similarity") or {}
        fail = (i["failure_class"] + ": " + i["failure_reason"]).strip(": ") if not i["passed"] else ""
        L.append(f"| {i['id']} | {i['set']} | {yn(i['executed'])} | {i['match_pct']} | {yn(s.get('tables'))} | {yn(s.get('joins'))} | "
                 f"{yn(s.get('predicates'))} | {yn(s.get('aggregation'))} | {i['repairs']} | {i['latency_ms']} | {i['route'] or '-'} | "
                 f"{fail.replace('|', '/')[:120]} |")
    fails = [i for i in r["items"] if not i["passed"]]
    L += ["", "## Failures in detail", ""]
    if not fails:
        L.append("None.")
    for i in fails:
        L += [f"### {i['id']} ({i['set']}): {i['question']}", "", f"- class: {i['failure_class']}; reason: {i['failure_reason']}",
              "", "Reference SQL:", "```sql", i["ref_sql"], "```", "Generated SQL:", "```sql", i["sql"] or "(none)", "```",
              f"- generated rows (first 5): `{json.dumps(i['rows_preview'], default=str)[:400]}`",
              f"- expected rows (first 5): `{json.dumps(i['ref_rows_preview'], default=str)[:400]}`", ""]
    return "\n".join(L) + "\n"
