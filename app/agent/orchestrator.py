"""Six-stage pipeline: understand, resolve, generate, validate, execute (+repair), narrate."""
from __future__ import annotations

import logging
import time

from ..config import Settings
from ..db import QueryDenied, QueryError, QueryTimeout, run_query
from ..entitlements import PLACEHOLDER, User, get_user, sanitize_question
from ..llm import LLMBadOutput, LLMError, LLMUnavailable
from ..metadata import AliasIndex, Catalog, DateContext
from ..models import AskRequest, AskResponse, ErrorBody, GenerateOut, KPI, NarrateOut, ResolveOut, Source, UnderstandOut
from . import narrate as nar
from . import patterns, prompts
from .memory import SessionStore, Turn
from .retrieval import ExampleBank, substitute
from .validate import ValidatedQuery, ValidationError, validate

log = logging.getLogger("msim.agent")

TIME_TEXT = {
    "today": lambda d: f"today = {d.as_of}",
    "yesterday": lambda d: f"yesterday = {d.yesterday}",
    "this_month": lambda d: f"this month = {d.current_month}",
    "last_month": lambda d: f"last month = {d.previous_month}",
    "last_year": lambda d: f"same month last year = {d.revenue_prior_year_month}",
    "last_4_weeks": lambda d: f"last 4 weeks = {d.four_week_start} to {d.as_of}",
    "last_7_days": lambda d: f"last 7 days = {d.seven_days_start} to {d.as_of}",
}


class Orchestrator:
    def __init__(self, settings: Settings, catalog: Catalog, llm, date_ctx: DateContext, aliases: AliasIndex,
                 sessions: SessionStore | None = None, bank: ExampleBank | None = None, clock=time.monotonic):
        self.s, self.catalog, self.llm, self.dc, self.aliases = settings, catalog, llm, date_ctx, aliases
        self.sessions = sessions or SessionStore(settings.session_ttl_s, settings.max_sessions)
        self.bank = bank or ExampleBank()
        self.clock = clock
        self.exclude_bank_ids: frozenset = frozenset()  # used by the leave-one-out evaluation mode
        self.glossary = catalog.glossary
        self._fund_names = {e.id: e.name for a, e, _ in aliases.entries if e.type == "fund"}

    # ---------------------------------------------------------------------------------------------
    def _deadline_hit(self, deadline: float) -> bool:
        return self.clock() > deadline

    def _interpreted(self, u: UnderstandOut) -> str:
        base = u.interpreted_question or u.standalone_question
        parts = [TIME_TEXT[t](self.dc) for t in dict.fromkeys(u.time_refs) if t in TIME_TEXT]
        return f"{base} ({'; '.join(parts)})" if parts else base

    def _hints(self, text: str, user: User):
        out = []
        for ent, _, _ in self.aliases.find(text):
            if ent.type == "fund" and user.fund_ids is not None and ent.id not in user.fund_ids:
                continue
            out.append(ent)
        return out

    # ---------------------------------------------------------------------------------------------
    def ask(self, req: AskRequest) -> AskResponse:
        user = get_user(req.user_id)
        sid = self.sessions.open(req.session_id, user.user_id)
        deadline = self.clock() + self.s.request_deadline_s
        san = sanitize_question(req.question, user, self.aliases)
        if san.forbidden and not san.permitted:
            return self._denied(sid, san)
        warnings: list[str] = [san.denial_text] if san.forbidden else []
        turns = self.sessions.recent(sid)

        # stage 1 ---------------------------------------------------------------------------------
        try:
            sys_p, usr_p = prompts.understand(san.text, turns, self.dc)
            u = self.llm.complete(stage="understand", system=sys_p, user=usr_p, model_cls=UnderstandOut)
        except LLMBadOutput:
            u = UnderstandOut(standalone_question=san.text)
        except LLMError:
            if patterns.match_pattern(san.text):  # patterns stay available when the model is down
                u = UnderstandOut(standalone_question=san.text, intent="insight")
            else:
                raise
        if not turns:
            u.is_followup = False
        interpreted = self._interpreted(u)
        if u.needs_clarification:
            msg = u.clarification_question or "Could you rephrase the question?"
            return AskResponse(session_id=sid, interpreted_question=interpreted, intent=u.intent, sources=[], sql="",
                               columns=[], rows=[], kpis=[], narrative=msg, warnings=warnings + ["Clarification needed"],
                               route="clarification")
        standalone = u.standalone_question
        hints = self._hints(standalone, user)

        # pattern route -----------------------------------------------------------------------------
        built = None
        pname = patterns.match_pattern(san.text) or patterns.match_pattern(standalone)
        outcome = None
        if pname:
            try:
                built = self._build_pattern(pname, san.text if patterns.match_pattern(san.text) else standalone, hints)
                outcome = self._run(built.sql, user, None, deadline, repairable=False)
                if outcome["error"]:
                    log.error("pattern %s failed: %s", pname, outcome["error"].get("code"))
                    built, outcome = None, None
            except Exception as e:  # pattern bug: fall back to the LLM path once
                log.error("pattern %s raised %s", pname, type(e).__name__)
                built, outcome = None, None

        resolved = None
        examples = []
        if outcome is None:
            if self._deadline_hit(deadline):
                raise LLMUnavailable("The request time limit was reached.")
            try:
                s2, u2 = prompts.resolve(standalone, self.catalog, hints, self.dc)
                resolved = self.llm.complete(stage="resolve", system=s2, user=u2, model_cls=ResolveOut)
            except LLMBadOutput:
                resolved = None
            examples = [(e, sc) for e, sc in self.bank.retrieve(
                standalone, 3, exclude_ids=self.exclude_bank_ids, allowed_funds=user.fund_ids,
                fund_names=self._fund_names)]
            examples = [(type(e)(**{**e.__dict__, "sql": substitute(e.sql, self.dc)}), sc) for e, sc in examples]
            if self._deadline_hit(deadline):
                raise LLMUnavailable("The request time limit was reached.")
            s3, u3 = prompts.generate(standalone, self.catalog, resolved, hints, examples, turns, self.dc, self.glossary)
            gen = self.llm.complete(stage="generate", system=s3, user=u3, model_cls=GenerateOut)
            for a in gen.assumptions[:3]:
                warnings.append(f"Assumption: {a}")

            def repair(prev_sql, err):
                s4, u4 = prompts.generate(standalone, self.catalog, resolved, hints, examples, turns, self.dc,
                                          self.glossary, error_ctx=(prev_sql, err))
                return self.llm.complete(stage="repair", system=s4, user=u4, model_cls=GenerateOut).sql
            outcome = self._run(gen.sql, user, repair, deadline, repairable=True)

        route = f"pattern:{built.name}" if built else "llm"
        resolved_why = {t.table: t.why for t in (resolved.tables if resolved else []) if t.why}

        # failure ---------------------------------------------------------------------------------------
        if outcome["error"]:
            v: ValidatedQuery | None = outcome.get("validated")
            tabs = v.tables_used if v else [t.table for t in (resolved.tables if resolved else [])
                                            if t.table in self.catalog.tables]
            sources = self._sources(tabs, v, resolved_why, None, prefix="Attempted: ")
            err = outcome["error"]
            warnings.append(err["message"])
            return AskResponse(
                session_id=sid, interpreted_question=interpreted, intent=u.intent, sources=sources,
                sql=outcome["sql"], columns=[], rows=[], kpis=[], warnings=warnings, route=route,
                narrative=err["narrative"], error=ErrorBody(code=err["code"], message=err["message"]))

        v = outcome["validated"]
        res = outcome["result"]
        columns, rows = list(res.columns), [list(r) for r in res.rows]
        if built and built.post:
            columns, rows, w = built.post(columns, rows)
            warnings += w
        sources = self._sources(v.tables_used, v, resolved_why, built.sources if built else None)
        if res.truncated:
            warnings.append(f"Result limited to {self.s.row_cap} rows.")
        warnings += nar.currency_warnings(v, self.catalog, self.s.db_path, user, self.dc.as_of, self.s.row_cap)
        warnings += nar.partial_month_warning(v, self.dc)
        warnings = list(dict.fromkeys(warnings))

        # stage 6 -----------------------------------------------------------------------------------------
        nout = None
        if self._deadline_hit(deadline):
            warnings.append("Narrative unavailable: the request time limit was reached.")
        else:
            try:
                safe_warnings = ["Part of the question referred to a fund the user cannot access; it is excluded."
                                 if san.forbidden and w == san.denial_text else w for w in warnings]
                s6, u6 = prompts.narrate(standalone, u.intent, [s.model_dump() for s in sources], columns, rows,
                                         len(rows), res.truncated, safe_warnings, self.dc)
                nout = self.llm.complete(stage="narrate", system=s6, user=u6, model_cls=NarrateOut)
            except LLMError as e:
                warnings.append(f"Narrative unavailable: {e.code}")
        narrative = nout.narrative if nout and nout.narrative else nar.deterministic_narrative(len(rows), res.truncated)
        kpis = nar.build_kpis(nout.kpis if nout else [], columns, rows)
        if nout and nout.narrative:
            warnings += [w for w in nar.check_grounding(narrative, columns, rows, kpis) if w not in warnings]

        chart = nar.chart_for(columns, rows, interpreted)
        if chart is None and "fact_settlement" in v.tables_used and len(rows) < 3 and (
                u.intent == "trend" or "settlement rate" in standalone.lower()):
            chart = nar.settlement_rate_chart(self.s.db_path, self.catalog, user, self.dc, self.s.row_cap)

        self.sessions.add(sid, Turn(
            question=san.text, standalone=standalone, intent=u.intent, sql=v.sql, columns=columns,
            rows_preview=rows[:5], row_count=len(rows),
            entities=[{"type": h.type, "id": h.id, "name": h.name} for h in hints],
            resolved_dates={"as_of": self.dc.as_of, "yesterday": self.dc.yesterday}))
        return AskResponse(
            session_id=sid, interpreted_question=interpreted, intent=u.intent, sources=sources, sql=v.sql,
            columns=columns, rows=rows, kpis=kpis, narrative=narrative, chart=chart, warnings=warnings,
            row_count=len(rows), truncated=res.truncated, route=route)

    # ---------------------------------------------------------------------------------------------
    def _build_pattern(self, name: str, text: str, hints):
        if name == "fund_attention_ranking":
            return patterns.build_attention(self.dc, self.glossary, patterns.parse_top_n(text))
        funds = list(dict.fromkeys(h.id for h in hints if h.type == "fund"))
        return patterns.build_yoy(self.dc, funds)

    def _run(self, sql: str, user: User, repair, deadline: float, repairable: bool) -> dict:
        attempts, last_validated, cur = 0, None, sql
        while True:
            err_text = None
            try:
                v = validate(cur, self.catalog, user, self.s.row_cap)
                last_validated = v
                res = run_query(self.s.db_path, v.sql, self.s.row_cap, self.s.query_timeout_s)
                return {"error": None, "validated": v, "result": res, "sql": v.sql, "attempts": attempts}
            except ValidationError as e:
                if e.category != "schema":
                    return self._fail("sql_rejected", cur, last_validated,
                                      "The generated query was blocked by the safety checks and was not executed.",
                                      f"Query rejected ({e.code}).", attempts)
                err_text = e.message
            except QueryDenied:
                return self._fail("sql_rejected", cur, last_validated,
                                  "The generated query was blocked by the database safety layer.",
                                  "Query rejected (not authorized).", attempts)
            except QueryTimeout as e:
                err_text = str(e)
            except QueryError as e:
                err_text = str(e)
            attempts += 1
            if not repairable or repair is None or attempts > self.s.max_sql_repairs or self._deadline_hit(deadline):
                return self._fail("sql_failed", cur, last_validated,
                                  "I could not produce a working query for this question.",
                                  f"Query failed after {attempts} attempt(s): {err_text}", attempts)
            try:
                cur = repair(cur, err_text)
            except LLMBadOutput:
                return self._fail("sql_failed", cur, last_validated,
                                  "I could not produce a working query for this question.",
                                  f"Query failed: {err_text}", attempts)

    @staticmethod
    def _fail(code, sql, validated, narrative, message, attempts):
        return {"error": {"code": code, "narrative": narrative, "message": message}, "sql": sql,
                "validated": validated, "attempts": attempts}

    # ---------------------------------------------------------------------------------------------
    def _sources(self, tables, v, resolved_why, fixed, prefix="") -> list[Source]:
        out: list[Source] = []
        fixed_map = {s["table"]: s for s in (fixed or [])}
        pairs = v.join_pairs if v else []
        edges = [e.on_clause() for e in self.catalog.edges_between(tables)] if len(tables) > 1 else []
        for t in tables:
            if t not in self.catalog.tables:
                continue
            if t in fixed_map:
                s = fixed_map[t]
                out.append(Source(table=t, why=prefix + s["why"], columns=s["columns"], join_keys=s["join_keys"],
                                  synthetic=s.get("synthetic", "no")))
                continue
            why = resolved_why.get(t) or self.catalog.tables[t].role or t
            keys = self.catalog.key_columns(t)
            used = (v.columns_by_table.get(t, []) if v else [])
            ordered = [c for c in keys if c in used] + [c for c in used if c not in keys]
            cols = ordered[:8] or keys[:4]
            jk = [p for p in pairs if f"{t}." in p] or [e for e in edges if f"{t}." in e]
            out.append(Source(table=t, why=prefix + why, columns=cols, join_keys=jk,
                              synthetic=self.catalog.synthetic_tables.get(t, "no")))
        return out

    def _denied(self, sid: str, san) -> AskResponse:
        msg = san.denial_text
        return AskResponse(
            session_id=sid, interpreted_question="Request for data on a fund outside your entitlements",
            intent="lookup", sources=[], sql="", columns=[], rows=[], kpis=[],
            narrative=(f"{msg} Your access is limited to the funds assigned to your profile, so no query was run "
                       "and no data is returned for that fund."),
            warnings=[msg], row_count=0, route="denied", error=ErrorBody(code="fund_access_denied", message=msg))
