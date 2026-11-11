# Talk to Data: Fund Operations Control Tower (POC)

Ask fund-operations questions in plain English and get a source-linked answer: the interpreted question, the tables used and
why (with join keys), the exact SQL, a result table, KPI cards, a short narrative and, for trends, a simple chart.
Numbers come only from SQL. The narrative is checked against the result rows and flagged if it mentions a number that is not there.

All data in `Data/` is **fictional demo data**. No real client or market data is included.

## How it works

FastAPI serves a JSON API and a static HTML/JS UI (no build step). A six-stage agent runs for every question:

1. Understand: intent, follow-up rewrite using session memory.
2. Resolve entities and pick tables, with a "why" for each table (aliases such as "Helios" or "short" are resolved by code from the data).
3. Generate SQLite SQL (retrieval of the 3 most similar curated examples, glossary rules, join graph with `ON` clauses, date context).
   Q8 (YoY revenue decomposition) and Q10 (attention score) are registered patterns, so they never depend on the model.
4. Validate with sqlglot (single SELECT/WITH only, table, column and function allowlists, row limit) and rewrite the AST so that
   restricted users only ever see their funds.
5. Execute on a read-only SQLite connection (authorizer, timeout, row cap). Schema errors go to a repair loop (max 2 retries).
6. Narrate and run the number-grounding check.

## Run locally

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env      # then set LLM_API_KEY (see below); .env is git-ignored
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000. The SQLite file `msim_fundops.db` is built from `Data/*.csv` plus `Data/supplementary/*.csv` on first start
and rebuilt automatically when a CSV changes. Tested with Python 3.14 (host) and 3.12 (Docker image).

## Changing the model or API key

The key and the model are **environment variables only**. No code or prompt edit is needed to switch.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_PROVIDER` | `groq` (SDK), or an OpenAI-compatible name: `openai`, `xai`, `openrouter`, `ollama`, `azure`, `custom` | `groq` |
| `LLM_MODEL` (alias `GROQ_MODEL`) | model name as the provider spells it | `openai/gpt-oss-20b` |
| `LLM_API_KEY` | API key. Fallback variable names, in order: `GROQ_API_KEY`, `GROK_API_DEV_KEY` | none |
| `LLM_BASE_URL` | base URL of any OpenAI-compatible endpoint (needed for `azure`, `custom`) | provider default |
| `LLM_TEMPERATURE` | optional; sent only if set (Groq defaults to 0) | unset |

The key is read from the process environment (optionally loaded from `.env` by python-dotenv), never from anywhere else, and it is
never logged, returned by an API or written to a report. The active provider and model (never the key) are shown in the UI footer
and at `GET /health` and `GET /api/config`.

Examples (put these in `.env`):

```text
# Groq (default)
LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
# xAI
LLM_PROVIDER=xai
LLM_MODEL=grok-4
LLM_BASE_URL=https://api.x.ai/v1
# OpenAI
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
# Azure OpenAI (deployment URL as base, key sent as `api-key`)
LLM_PROVIDER=azure
LLM_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deployment>?api-version=2024-06-01
# OpenRouter
LLM_PROVIDER=openrouter
LLM_MODEL=meta-llama/llama-3.3-70b-instruct
# Ollama (local, no key)
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
LLM_BASE_URL=http://localhost:11434/v1
```

Note for Azure OpenAI: the generic client appends `/chat/completions` to `LLM_BASE_URL`; if your endpoint needs the query string
`?api-version=...`, use a base URL ending in `/openai/v1` or a proxy that adds it. This path was not tested against a live Azure resource.

The LLM layer does not rely on JSON mode, tool calling or temperature support. It strips code fences and reasoning text, takes the
first JSON object, validates it, re-asks once, retries once with a doubled token budget when a reply is truncated, and backs off on
429/5xx. After the model, deterministic guardrails (validator, allowlists, repair loop, execution check) catch most mistakes, and
the registered patterns for Q8 and Q10 keep working even if the model is unavailable.

### Is it the model or the prompt? (diagnosis)

Wrong SQL can come from the model rather than the prompts. Run the same question set across several models; only `LLM_MODEL` changes:

```powershell
.venv\Scripts\python scripts\eval_models.py --models openai/gpt-oss-20b,llama-3.3-70b-versatile,openai/gpt-oss-120b
.venv\Scripts\python scripts\eval_models.py --models a,b --sets golden,paraphrase,novel --bank leave_one_out
```

It writes `tests/reports/model_comparison.md/.json`: per question and model, executed, result match, SQL similarity, repairs, latency,
and an automatic failure class: `invalid_json_format`, `sql_syntax_or_validation`, `wrong_tables_or_joins`, `wrong_predicates_or_values`,
`wrong_aggregation`, `result_mismatch` (or `provider_error` for rate limits and outages, which say nothing about quality).
Reading the matrix: a question that fails on **every** model points at the prompt, the glossary, the example bank or the data;
a question whose result **differs between models** is model-specific, so try a stronger model before editing prompts.

## Synthetic data

The original CSVs are never modified. `scripts/generate_supplementary_data.py` (fixed seed, byte-identical on every run) writes
`Data/supplementary/`: NAV and cash history for all funds, weekend calendar rows, revenue and flow history, a flow-detail table that
reconciles exactly to `fact_flows`, SLA history, resolved reconciliation breaks and fixed FX rates. Every overlay row has
`is_synthetic = 1`; the sources panel shows a "synthetic" badge for tables that contain such rows. All golden results are identical with
and without the supplement (`MSIM_USE_SUPPLEMENT=0` loads the originals only). Details: [docs/data_notes.md](docs/data_notes.md).

`AS_OF_DATE` (default: latest data date, 2026-09-04) is "today" for snapshots; "yesterday" is the latest settlement date
(`YESTERDAY_DATE` overrides). The playbook anchors on Monday 2026-09-07 but its "yesterday" is 2026-09-04, hence this rule.

## Entitlements

Mock users: Priya Nair (all funds), David Osei (all funds) and a restricted analyst (F002, F005, F006). The server rewrites every
fund-scoped table reference into a filtered subquery, so aggregates, CTEs, subqueries and unions only see permitted funds.
If a restricted user names a fund they cannot access, the request returns HTTP 200 with `error.code = "fund_access_denied"`, empty
`rows`, empty `sql` and `sources`, and the same sentence in `warnings`; no LLM or SQL stage runs. If permitted and forbidden funds are
mixed, the answer covers the permitted ones and carries the warning. The UI shows an "Access denied" panel. The dropdown is not
authentication.

## API

`POST /api/ask` with `{question, session_id?, user_id}` returns `session_id, interpreted_question, intent, sources[{table, why, columns,
join_keys, synthetic}], sql, columns, rows, kpis, narrative, chart, warnings, row_count, truncated, route, error`. Other routes:
`GET /health`, `/api/config`, `/api/users`, `/api/schema`, interactive docs at `/api/docs`.

## Running with Docker

```powershell
copy .env.example .env       # set LLM_API_KEY
docker compose up --build    # http://localhost:8000
```

The image is multi-stage, runs as a non-root user, has a `/health` healthcheck, contains no secrets and builds the database at start.
For a keyless smoke test: `docker run -e LLM_BACKEND=smoke -p 8000:8000 talk-to-data:local` (scripted backend for Q1 only; refused when a key is set).

## Tests

```powershell
.venv\Scripts\python -m pytest -m "not live"      # offline: golden, validator, entitlements, agent (mocked LLM), API, files
```

No network or key is needed. If pinned dependencies do not install on your Python, use the Docker alternative:
`docker run --rm -v ${PWD}:/work -w /work python:3.12-slim sh -c "pip install -r requirements.txt -r requirements-dev.txt && python -m pytest -m 'not live'"`.
The golden tests run the playbook's reference SQL and assert the exact validated rows on both the original and the supplemented database.

## Live evaluation

```powershell
.venv\Scripts\python -m pytest -m live        # real model; key from the environment
```

Runs the 11 golden questions, 33 paraphrases (3 per question) and 12 novel questions over the supplementary data. Execution accuracy
compares the agent's result set with the reference result (order-insensitive where there is no ORDER BY semantics, column-name
insensitive, rounding tolerance). A sqlglot similarity score compares tables, join keys, predicates and aggregates. Reports:
`tests/reports/live_eval.json` and `live_eval.md`; `LIVE_BANK_MODE=leave_one_out` removes the canonical playbook example from retrieval.
The golden set uses the production example bank (which contains the playbook SQL), so paraphrases and novel questions are the fairer
measure. Results of the last run are in [tests/reports/live_eval.md](tests/reports/live_eval.md) and
[tests/reports/model_comparison.md](tests/reports/model_comparison.md); failures are reported as they happened.

## CI/CD and Azure

`.github/workflows/ci.yml` runs the offline tests and builds the image on every push and PR. `deploy-azure.yml` is manual
(`workflow_dispatch`, OIDC) and deploys to Azure Container Apps with the key as a Container Apps secret; see
[docs/azure-deploy.md](docs/azure-deploy.md). The Bicep template was not validated with a Bicep CLI in this environment.

## Security notes

- Public repository: no key, `.env`, database file or credential is tracked. Provide the key through an environment variable or a
  platform secret (GitHub secret, Container Apps secret).
- SQL safety is layered: sqlglot AST allowlists and re-emission, then a read-only SQLite connection with an authorizer that only allows
  `SELECT`/read and an allowlist of functions. Security-category rejections are never sent back to the model for repair.
- Question text, history and model output are treated as untrusted. The UI renders server values with `textContent` only.
- Reference tables without a fund column (`dim_security`, `dim_counterparty`, `dim_date`, `dim_fx_rate`) are not fund-scoped.
- Sessions are in memory (single worker) and lost on restart. No WCAG audit with assistive technology has been done.
