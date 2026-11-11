import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import LLMAuthFailed, LLMBadOutput, LLMNotConfigured, LLMRateLimited, LLMUnavailable
from app.main import create_app
from tests.conftest import DATA, golden_sql
from tests.helpers import make_llm

SECRET = "gsk_API_TEST_SENTINEL_1234567890"


def client(db, llm, **kw):
    s = Settings(db_path=db, data_dir=str(DATA), api_key=SECRET, llm_max_retries=0, **kw)
    return TestClient(create_app(s, llm), raise_server_exceptions=False)


def ask(c, q, user="priya", sid=None):
    body = {"question": q, "user_id": user}
    if sid:
        body["session_id"] = sid
    return c.post("/api/ask", json=body)


def test_health_exposes_only_the_allowed_keys_and_never_the_key(db_overlay):
    c = client(db_overlay, make_llm([golden_sql("Q1")]))
    b = c.get("/health").json()
    assert set(b) == {"status", "llm_configured", "as_of_date", "provider", "model"}
    assert b["status"] == "ok" and b["as_of_date"] == "2026-09-04" and b["llm_configured"] is True
    for path in ("/health", "/api/config", "/api/users", "/api/schema", "/"):
        assert SECRET not in c.get(path).text, path


def test_users_and_config(db_overlay):
    c = client(db_overlay, make_llm([golden_sql("Q1")]))
    users = {u["user_id"]: u for u in c.get("/api/users").json()}
    assert users["priya"]["fund_scope"] == "all" and users["analyst_restricted"]["fund_scope"] == ["F002", "F005", "F006"]
    cfg = c.get("/api/config").json()
    assert cfg["yesterday_date"] == "2026-09-04" and cfg["supplement"] is True and "api_key" not in cfg


def test_schema_endpoint(db_overlay):
    b = client(db_overlay, make_llm([golden_sql("Q1")])).get("/api/schema").json()
    assert len(b["tables"]) == 16 and len(b["examples"]) == 11
    assert [e["id"] for e in b["examples"]] == ["Q1", "Q2", "Q3", "Q4", "Q4F", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10"]
    by = {t["name"]: t for t in b["tables"]}
    assert by["fact_flow_detail"]["synthetic"] == "full" and by["fact_nav"]["synthetic"] == "partial" and by["dim_fund"]["synthetic"] == "no"
    assert b["supplement"]["enabled"] is True and b["db_rows"]["fact_settlement"] == 632
    assert any("custodian" in n["column"] for n in b["non_joins"])


def test_static_ui_is_served_with_expected_markers_and_no_innerhtml(db_overlay):
    c = client(db_overlay, make_llm([golden_sql("Q1")]))
    html = c.get("/")
    assert html.status_code == 200
    for marker in ('id="user-select"', 'id="conversation"', 'id="ask-form"', 'id="question"', 'id="model-info"'):
        assert marker in html.text
    js = c.get("/static/app.js")
    assert js.status_code == 200 and "innerHTML" not in js.text and "/api/ask" in js.text
    assert "fund_access_denied" in js.text and "synthetic demo table" in js.text
    assert c.get("/static/styles.css").status_code == 200


def test_ask_q1_returns_six_funds_and_full_contract(db_overlay):
    c = client(db_overlay, make_llm([golden_sql("Q1")]))
    r = ask(c, "List all active funds with region, strategy and latest AUM.")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"session_id", "interpreted_question", "intent", "sources", "sql", "columns", "rows", "kpis", "narrative",
                      "chart", "warnings", "row_count", "truncated", "route", "error"}
    assert [x[4] for x in b["rows"]] == [5.13, 3.05, 2.35, 1.88, 1.45, 1.02]
    assert b["row_count"] == 6 and b["truncated"] is False and b["error"] is None
    assert {s["table"] for s in b["sources"]} == {"dim_fund", "fact_nav"}
    assert all(set(s) == {"table", "why", "columns", "join_keys", "synthetic"} and s["why"] for s in b["sources"])
    assert re.match(r"^[0-9a-f]{32}$", b["session_id"])


def test_forbidden_fund_response_body(db_overlay):
    c = client(db_overlay, make_llm([golden_sql("Q1")]))
    r = ask(c, "cash for F001", user="analyst_restricted")
    assert r.status_code == 200
    b = r.json()
    assert b["error"] == {"code": "fund_access_denied", "message": "You do not have access to fund F001."}
    assert (b["rows"], b["sql"], b["sources"], b["row_count"]) == ([], "", [], 0)
    assert b["warnings"] == ["You do not have access to fund F001."]


@pytest.mark.parametrize("body,status,code", [
    ({"question": "x", "user_id": "mallory"}, 403, "unknown_user"),
    ({"question": "", "user_id": "priya"}, 422, "invalid_request"),
    ({"question": "x" * 501, "user_id": "priya"}, 422, "invalid_request"),
    ({"question": "x", "user_id": "priya", "session_id": "bad id!"}, 422, "invalid_request"),
    ({"question": "x", "user_id": "priya", "extra": 1}, 422, "invalid_request"),
    ({"user_id": "priya"}, 422, "invalid_request"),
])
def test_request_errors(body, status, code, db_overlay):
    r = client(db_overlay, make_llm([golden_sql("Q1")])).post("/api/ask", json=body)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_session_belongs_to_one_user(db_overlay):
    c = client(db_overlay, make_llm([golden_sql("Q1")]))
    sid = ask(c, "list funds").json()["session_id"]
    r = ask(c, "list funds", user="david", sid=sid)
    assert r.status_code == 403 and r.json()["error"]["code"] == "session_forbidden"
    assert ask(c, "list funds", user="priya", sid=sid).status_code == 200


@pytest.mark.parametrize("exc,status,code", [
    (LLMNotConfigured("no key"), 503, "llm_not_configured"), (LLMAuthFailed("bad"), 503, "llm_auth_failed"),
    (LLMRateLimited("slow"), 503, "llm_rate_limited"), (LLMUnavailable("down"), 503, "llm_unavailable"),
])
def test_llm_failures_map_to_http_errors(exc, status, code, db_overlay):
    c = client(db_overlay, make_llm([golden_sql("Q1")], extra={"understand": lambda s, u, i: exc}))
    r = ask(c, "list funds")
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert SECRET not in r.text


def test_bad_sql_after_repairs_is_http_200_with_error_body(db_overlay):
    c = client(db_overlay, make_llm(["SELECT a FROM dim_fund"]))
    r = ask(c, "list things")
    b = r.json()
    assert r.status_code == 200 and b["error"]["code"] == "sql_failed" and b["rows"] == [] and b["sql"]


def test_unhandled_exception_hides_traceback(db_overlay):
    def boom(system, user, i):
        raise RuntimeError("secret internals /srv/app.py")
    c = client(db_overlay, make_llm([golden_sql("Q1")], extra={"understand": boom}))
    r = ask(c, "list funds")
    assert r.status_code == 500 and r.json() == {"error": {"code": "internal_error", "message": "Unexpected error"}}


def test_model_switch_is_visible_in_health_without_code_changes(db_overlay):
    from app.llm import LLM
    s = Settings(db_path=db_overlay, data_dir=str(DATA), api_key=SECRET, model="llama-3.3-70b-versatile", provider="groq")
    c = TestClient(create_app(s, LLM(s)))
    assert c.get("/health").json()["model"] == "llama-3.3-70b-versatile"
    assert c.get("/api/config").json()["provider"] == "groq"
