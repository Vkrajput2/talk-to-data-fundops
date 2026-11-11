import html
import re

from app.agent.memory import SessionStore
from app.agent.orchestrator import Orchestrator
from app.config import Settings
from app.llm import ScriptedLLM
from app.metadata import AliasIndex, build_catalog, compute_date_context
from app.models import AskRequest
from tests.conftest import DATA


def question_of(user_prompt: str) -> str:
    m = re.search(r"<question>(.*?)</question>", user_prompt, flags=re.S)
    return html.unescape(m.group(1)) if m else ""


def make_llm(sql_by_call=None, understand=None, narrate=None, resolve=None, extra=None):
    """sql_by_call: callable(question, call_index) -> sql, or a list of SQL strings."""
    def u(system, user, i):
        q = question_of(user)
        base = {"standalone_question": q, "intent": "lookup", "interpreted_question": q, "time_refs": []}
        if understand:
            base.update(understand(q, i) if callable(understand) else understand)
        return base

    def r(system, user, i):
        return resolve(question_of(user), i) if resolve else {"tables": [
            {"table": "dim_fund", "why": "Fund names", "columns": ["fund_id"], "join_keys": []}]}

    sql_calls = [0]  # generate and repair share one counter so scripted SQL goes in call order

    def g(system, user, _i):
        i = sql_calls[0]
        sql_calls[0] += 1
        if callable(sql_by_call):
            return {"sql": sql_by_call(question_of(user), i)}
        return {"sql": sql_by_call[min(i, len(sql_by_call) - 1)]}

    def n(system, user, i):
        return narrate(user, i) if narrate else {"narrative": "The query returned rows.", "kpis": []}
    handlers = {"understand": u, "resolve": r, "generate": g, "repair": g, "narrate": n}
    handlers.update(extra or {})
    return ScriptedLLM(handlers)


def make_orch(db, llm, **settings_kw):
    s = Settings(db_path=db, data_dir=str(DATA), llm_max_retries=0, **settings_kw)
    cat = build_catalog(DATA, True)
    return Orchestrator(s, cat, llm, compute_date_context(db), AliasIndex(db, cat.glossary), SessionStore())


def ask(orch, question, user="priya", session=None):
    return orch.ask(AskRequest(question=question, user_id=user, session_id=session))
