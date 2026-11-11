"""Run the same question set across several models and print a comparison matrix.

  py scripts/eval_models.py --models openai/gpt-oss-20b,llama-3.3-70b-versatile,openai/gpt-oss-120b
  py scripts/eval_models.py --models a,b --sets golden,novel --limit 12

Only the model changes between runs (the prompt, bank, patterns and data are identical), so a failure that differs
across models is model-specific, while a failure on every model points at the prompt, the example bank or the data.
The API key comes from the environment (.env via python-dotenv); it is never printed or written to the reports.
Reports: tests/reports/model_comparison.json and model_comparison.md
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from tests.live_eval import build_report, load_items, run_eval, write_reports  # noqa: E402


def classify_scope(per_model: dict[str, dict]) -> dict[str, str]:
    """For every question: all-pass | all-fail (prompt/data-specific) | model-specific."""
    ids = [i["id"] for i in next(iter(per_model.values()))["items"]]
    out = {}
    for qid in ids:
        res = [next(i for i in r["items"] if i["id"] == qid) for r in per_model.values()]
        real = [x for x in res if x["failure_class"] != "provider_error"]
        if not real:
            out[qid] = "inconclusive (provider errors)"
        elif all(x["passed"] for x in real):
            out[qid] = "all models pass"
        elif not any(x["passed"] for x in real):
            out[qid] = "fails on all models -> prompt/data-specific"
        else:
            out[qid] = "differs by model -> model-specific"
    return out


def render(models: list[str], per_model: dict[str, dict]) -> str:
    L = ["# Model comparison", "", "Same prompts, example bank, patterns and data for every model; only `LLM_MODEL` changes.", "",
         "## Summary", "", "| Model | " + " | ".join(next(iter(per_model.values()))["summary"].keys()) + " |",
         "|---|" + "---|" * len(next(iter(per_model.values()))["summary"])]
    for m in models:
        r = per_model[m]
        L.append(f"| {m} | " + " | ".join(f"{v['passed']}/{v['total']} ({v['rate']:.0%})" for v in r["summary"].values()) + " |")
    L += ["", "## Per question (executed / result match % / SQL similarity / repairs / latency s)", "",
          "| id | " + " | ".join(models) + " | scope |", "|---|" + "---|" * (len(models) + 1)]
    scope = classify_scope(per_model)
    ids = [i["id"] for i in per_model[models[0]]["items"]]
    for qid in ids:
        cells = []
        for m in models:
            i = next(x for x in per_model[m]["items"] if x["id"] == qid)
            sim = (i.get("similarity") or {}).get("overall")
            status = "PASS" if i["passed"] else f"FAIL[{i['failure_class'] or '?'}]"
            cells.append(f"{status} {'x' if i['executed'] else '-'} {i['match_pct']}% sim={sim if sim is not None else '-'} "
                         f"r={i['repairs']} {i['latency_ms'] / 1000:.0f}s")
        L.append(f"| {qid} | " + " | ".join(cells) + f" | {scope[qid]} |")
    L += ["", "## Failure classes per model", ""]
    for m in models:
        cnt: dict[str, int] = {}
        for i in per_model[m]["items"]:
            if not i["passed"]:
                cnt[i["failure_class"] or "unknown"] = cnt.get(i["failure_class"] or "unknown", 0) + 1
        L.append(f"- {m}: " + (", ".join(f"{k}={v}" for k, v in sorted(cnt.items())) or "no failures"))
    L += ["", "Reading guide: `invalid_json_format` and `provider_error` are model/provider problems; "
          "`wrong_tables_or_joins`, `wrong_predicates_or_values`, `wrong_aggregation` and `result_mismatch` that repeat on every "
          "model point to the prompt, glossary or example bank; failures that differ between models are model-specific.", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", required=True, help="comma separated model names")
    ap.add_argument("--sets", default="golden,novel", help="golden,paraphrase,novel")
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions of the selected sets")
    ap.add_argument("--bank", default="full", choices=["full", "leave_one_out"])
    ap.add_argument("--out", default=str(ROOT / "tests" / "reports"))
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    base = load_settings()
    if not base.llm_configured:
        print("No API key configured (set LLM_API_KEY / GROQ_API_KEY / GROK_API_DEV_KEY).", file=sys.stderr)
        return 2
    items = load_items(tuple(s.strip() for s in a.sets.split(",")))
    if a.limit:
        items = items[: a.limit]
    per_model = {}
    for m in models:
        s = dataclasses.replace(base, model=m)
        t0 = time.time()
        print(f"== {m}: {len(items)} questions", flush=True)

        def progress(n, total, res):
            print(f"  [{n}/{total}] {res['id']:<8} {'PASS' if res['passed'] else 'FAIL'} {res['failure_class']:<26} "
                  f"{res['latency_ms'] / 1000:.0f}s", flush=True)
        per_model[m] = run_eval(s, items, a.bank, progress)
        print(f"   done in {time.time() - t0:.0f}s: {json.dumps(per_model[m]['summary'])}", flush=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "model_comparison.json").write_text(json.dumps({"models": models, "results": per_model,
                                                           "scope": classify_scope(per_model)}, indent=1, default=str) + "\n")
    (out / "model_comparison.md").write_text(render(models, per_model), encoding="utf-8")
    print(f"wrote {out / 'model_comparison.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
