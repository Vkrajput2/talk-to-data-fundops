"""FastAPI app: JSON API + static UI."""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent.memory import SessionForbidden, SessionStore
from .agent.orchestrator import Orchestrator
from .config import Settings, load_settings
from .db import ensure_database, table_row_counts
from .entitlements import (MOCK_USERS, EntitlementEnforcementError, UnknownUser, check_users_against_funds)
from .llm import LLMError, build_llm
from .metadata import AliasIndex, build_catalog, compute_date_context
from .models import AskRequest, AskResponse

log = logging.getLogger("msim.api")
STATIC = Path(__file__).with_name("static")

EXAMPLES = [
    ("Q1", "Active funds", "List all active funds with region, strategy and latest AUM."),
    ("Q2", "Tight cash", "Which funds have available cash below 2% of AUM today?"),
    ("Q3", "Failed trades", "Show all trades that failed to settle yesterday."),
    ("Q4", "Settlement rate", "What is our settlement rate yesterday versus the four-week average?"),
    ("Q4F", "Fails by broker", "Break yesterday's fails down by broker."),
    ("Q5", "Aged breaks", "Which reconciliation breaks are open and older than five days, and who owns them?"),
    ("Q6", "Stale prices", "Are there any stale prices today that could put a NAV at risk?"),
    ("Q7", "Net flows", "Show this month's net flows by fund and how they compare with last month."),
    ("Q8", "Revenue drivers", "What drove the year-on-year change in management-fee revenue for the Global Equity Alpha Fund?"),
    ("Q9", "SLA risk", "Which operational processes are at risk of breaching SLA today?"),
    ("Q10", "Funds needing attention", "Which three funds need attention today, and why?"),
]


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(settings: Settings | None = None, llm=None) -> FastAPI:
    s = settings or load_settings()
    ensure_database(s.data_dir, s.db_path, s.use_supplement)
    catalog = build_catalog(s.data_dir, s.use_supplement)
    date_ctx = compute_date_context(s.db_path, s.as_of_date, s.yesterday_date)
    aliases = AliasIndex(s.db_path, catalog.glossary)
    check_users_against_funds({e.id for _, e, _ in aliases.entries if e.type == "fund"})
    llm = llm or build_llm(s)
    if not s.llm_configured:
        log.warning("No LLM API key configured: registered patterns work, other questions return llm_not_configured")
    orch = Orchestrator(s, catalog, llm, date_ctx, aliases, SessionStore(s.session_ttl_s, s.max_sessions))

    app = FastAPI(title="Talk to Data - Fund Operations Control Tower", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.orchestrator = orch
    app.state.settings = s

    @app.exception_handler(RequestValidationError)
    async def _val(_: Request, exc: RequestValidationError):
        return _err(422, "invalid_request", "The request body is invalid (question 1-500 characters, user_id required).")

    @app.exception_handler(UnknownUser)
    async def _uu(_: Request, exc):
        log.warning("unknown user rejected")
        return _err(403, "unknown_user", "Unknown user.")

    @app.exception_handler(SessionForbidden)
    async def _sf(_: Request, exc):
        log.warning("session/user mismatch")
        return _err(403, "session_forbidden", "This session belongs to another user.")

    @app.exception_handler(LLMError)
    async def _llm(_: Request, exc: LLMError):
        log.warning("llm error %s", exc.code)
        return _err(exc.http_status, exc.code, str(exc))

    @app.exception_handler(EntitlementEnforcementError)
    async def _ent(_: Request, exc):
        log.error("entitlement enforcement failed")
        return _err(500, "entitlement_enforcement_failed", "The request could not be safely scoped.")

    @app.exception_handler(Exception)
    async def _any(_: Request, exc: Exception):
        log.error("unhandled %s", type(exc).__name__, exc_info=True)
        return _err(500, "internal_error", "Unexpected error")

    @app.get("/health")
    def health():
        return {"status": "ok", "llm_configured": s.llm_configured, "as_of_date": date_ctx.as_of,
                "provider": llm.provider, "model": llm.model}

    @app.get("/api/config")
    def config():
        return {"provider": llm.provider, "model": llm.model, "llm_configured": s.llm_configured,
                "as_of_date": date_ctx.as_of, "yesterday_date": date_ctx.yesterday, "supplement": s.use_supplement}

    @app.get("/api/users")
    def users():
        return [{"user_id": u.user_id, "name": u.name, "role": u.role, "fund_scope": u.scope_label()}
                for u in MOCK_USERS.values()]

    @app.get("/api/schema")
    def schema():
        return {
            "tables": [{"name": t.name, "role": t.role, "synthetic": catalog.synthetic_tables.get(t.name, "no"),
                        "columns": [asdict(c) for c in t.columns.values()]} for t in catalog.tables.values()],
            "joins": [{"from": f"{e.left_table}.{e.left_col}", "to": f"{e.right_table}.{e.right_col}",
                       "kind": e.kind, "note": e.note} for e in catalog.edges],
            "non_joins": [{"column": a, "reason": b} for a, b in catalog.non_joins],
            "glossary": {k: v for k, v in catalog.glossary.items() if k not in ("sql_rules", "table_roles")},
            "date_context": asdict(date_ctx),
            "supplement": {"enabled": s.use_supplement, "tables": dict(catalog.synthetic_tables)},
            "examples": [{"id": i, "label": l, "question": q} for i, l, q in EXAMPLES],
            "db_rows": table_row_counts(s.db_path),
        }

    @app.post("/api/ask", response_model=AskResponse)
    def ask(req: AskRequest):
        return orch.ask(req)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


def __getattr__(name: str):  # lazy module-level app for `uvicorn app.main:app`
    if name == "app":
        global _app
        try:
            return _app
        except NameError:
            logging.basicConfig(level=logging.INFO)
            _app = create_app()
            return _app
    raise AttributeError(name)
