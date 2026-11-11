"""Pydantic models: API contract and LLM stage payloads (tolerant of weaker models)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

INTENTS = ("lookup", "kpi", "exception", "trend", "insight", "summary")
TIME_REFS = ("today", "yesterday", "this_month", "last_month", "last_year", "last_4_weeks", "last_7_days", "none")
PATTERN_HINTS = ("none", "yoy_revenue_decomposition", "fund_attention_ranking")
KPI_OPS = ("cell", "count_rows", "sum", "max", "min")
KPI_FORMATS = ("integer", "decimal1", "decimal2", "percent1", "percent2", "currency_m", "currency_bn", "text")


# ---- API ---------------------------------------------------------------------------------------
class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    user_id: str = Field(min_length=1, max_length=64)

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class Source(BaseModel):
    table: str
    why: str
    columns: list[str] = []
    join_keys: list[str] = []
    synthetic: Literal["no", "partial", "full"] = "no"


class KPI(BaseModel):
    label: str
    value: Any = None
    display: str = ""
    unit: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str


class AskResponse(BaseModel):
    session_id: str
    interpreted_question: str
    intent: str
    sources: list[Source]
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    kpis: list[KPI]
    narrative: str
    chart: dict | None = None
    warnings: list[str] = []
    row_count: int = 0
    truncated: bool = False
    route: str = "llm"
    error: ErrorBody | None = None


# ---- LLM stage payloads -------------------------------------------------------------------------
class UnderstandOut(BaseModel):
    intent: str = "lookup"
    standalone_question: str
    interpreted_question: str = ""
    is_followup: bool = False
    time_refs: list[str] = []
    pattern_hint: str = "none"
    needs_clarification: bool = False
    clarification_question: str = ""

    @field_validator("intent")
    @classmethod
    def _intent(cls, v):
        v = (v or "").lower().strip()
        return v if v in INTENTS else "lookup"

    @field_validator("time_refs", mode="before")
    @classmethod
    def _refs(cls, v):
        if isinstance(v, str):
            v = [v]
        return [x for x in (v or []) if isinstance(x, str) and x in TIME_REFS][:8]

    @field_validator("pattern_hint", mode="before")
    @classmethod
    def _hint(cls, v):
        return v if v in PATTERN_HINTS else "none"

    @field_validator("standalone_question")
    @classmethod
    def _sq(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("standalone_question is empty")
        return v[:500]

    @field_validator("interpreted_question")
    @classmethod
    def _iq(cls, v):
        return (v or "").strip()[:400]


class ResolvedEntity(BaseModel):
    mention: str = ""
    type: str = ""
    resolved_id: str = ""
    resolved_name: str = ""
    table: str = ""
    column: str = ""


class TableChoice(BaseModel):
    table: str
    why: str = ""
    columns: list[str] = []
    join_keys: list[str] = []

    @field_validator("why")
    @classmethod
    def _why(cls, v):
        return (v or "")[:200]


class ResolveOut(BaseModel):
    entities: list[ResolvedEntity] = []
    tables: list[TableChoice] = []

    @field_validator("entities", "tables", mode="before")
    @classmethod
    def _none(cls, v):
        return v or []


class GenerateOut(BaseModel):
    sql: str
    assumptions: list[str] = []

    @field_validator("sql")
    @classmethod
    def _sql(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("sql is empty")
        return v[:6000]

    @field_validator("assumptions", mode="before")
    @classmethod
    def _a(cls, v):
        return [str(x)[:200] for x in (v or [])][:5]


class KpiSpec(BaseModel):
    label: str = ""
    op: str = "cell"
    column: str = ""
    row: int = -1
    format: str = "text"


class NarrateOut(BaseModel):
    narrative: str
    kpis: list[KpiSpec] = []
    chart_hint: str = "none"

    @field_validator("narrative")
    @classmethod
    def _n(cls, v):
        return (v or "").strip()[:900]

    @field_validator("kpis", mode="before")
    @classmethod
    def _k(cls, v):
        return (v or [])[:4]
