"""Provider-agnostic LLM layer. Switching provider/model/key needs environment edits only.

Env: LLM_PROVIDER (groq|openai|xai|openrouter|ollama|azure|custom), LLM_MODEL (alias GROQ_MODEL),
LLM_API_KEY (fallbacks GROQ_API_KEY, GROK_API_DEV_KEY), LLM_BASE_URL (any OpenAI-compatible endpoint).
No reliance on response_format, tool calling or temperature support: JSON is parsed tolerantly.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from pydantic import BaseModel, ValidationError

from .config import Settings, redact

log = logging.getLogger("msim.llm")

STAGE_BUDGETS = {"understand": 1000, "resolve": 1500, "generate": 3500, "repair": 3500, "narrate": 1500}
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
    "grok": "https://api.x.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "groq": "https://api.groq.com/openai/v1",
}


class LLMError(Exception):
    code = "llm_error"
    http_status = 503


class LLMNotConfigured(LLMError):
    code = "llm_not_configured"


class LLMAuthFailed(LLMError):
    code = "llm_auth_failed"


class LLMRateLimited(LLMError):
    code = "llm_rate_limited"


class LLMUnavailable(LLMError):
    code = "llm_unavailable"


class LLMBadOutput(LLMError):
    code = "llm_bad_output"
    http_status = 502


class TransportError(Exception):
    """kind: rate_limit | transient | auth | bad_request"""

    def __init__(self, kind: str, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


@dataclass
class RawReply:
    text: str
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, *, stage: str, system: str, user: str, model_cls: type[BaseModel]) -> BaseModel: ...


# ---- tolerant JSON extraction ---------------------------------------------------------------------
def extract_json(text: str) -> dict:
    """Strip reasoning/fences and return the first balanced JSON object."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    t = re.sub(r"```(?:json|JSON)?", "", t)
    n = len(t)
    for start in range(n):
        if t[start] != "{":
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, n):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
    raise ValueError("no JSON object found in the reply")


# ---- transports -------------------------------------------------------------------------------------
class GroqTransport:
    def __init__(self, s: Settings):
        import groq
        self._groq = groq
        self.s = s
        kwargs = {"api_key": s.api_key, "timeout": s.llm_timeout_s, "max_retries": 0}
        if s.base_url:
            kwargs["base_url"] = s.base_url
        self.client = groq.Groq(**kwargs)
        self.temperature = s.llm_temperature if s.llm_temperature is not None else 0.0
        self._send_reasoning = "gpt-oss" in s.model.lower()

    def chat(self, messages, max_tokens) -> RawReply:
        g = self._groq
        params = dict(model=self.s.model, messages=messages, max_tokens=max_tokens, temperature=self.temperature)
        if self._send_reasoning:
            params["reasoning_effort"] = "low"
        try:
            try:
                r = self.client.chat.completions.create(**params)
            except g.BadRequestError as e:
                if "reasoning_effort" in params and "reasoning" in str(e).lower():
                    self._send_reasoning = False
                    params.pop("reasoning_effort")
                    r = self.client.chat.completions.create(**params)
                else:
                    raise
        except g.RateLimitError as e:
            ra = None
            try:
                ra = float(e.response.headers.get("retry-after"))
            except Exception:
                pass
            raise TransportError("rate_limit", "rate limited", ra) from None
        except (g.APITimeoutError, g.APIConnectionError, g.InternalServerError) as e:
            raise TransportError("transient", type(e).__name__) from None
        except (g.AuthenticationError, g.PermissionDeniedError):
            raise TransportError("auth", "authentication failed") from None
        except g.APIStatusError as e:
            if getattr(e, "status_code", 0) >= 500:
                raise TransportError("transient", f"status {e.status_code}") from None
            raise TransportError("bad_request", f"status {getattr(e, 'status_code', '?')}: {str(e)[:200]}") from None
        ch = r.choices[0]
        usage = getattr(r, "usage", None)
        return RawReply(ch.message.content or "", ch.finish_reason or "stop",
                        getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)


class OpenAICompatTransport:
    """Generic OpenAI-compatible /chat/completions over httpx (OpenAI, xAI, OpenRouter, Ollama, Azure...)."""

    def __init__(self, s: Settings):
        import httpx
        self.httpx = httpx
        self.s = s
        base = (s.base_url or DEFAULT_BASE_URLS.get(s.provider, "")).rstrip("/")
        if not base:
            raise LLMNotConfigured("LLM_BASE_URL is required for this provider")
        self.url = base + "/chat/completions"
        self.headers = {"Content-Type": "application/json"}
        if s.api_key:
            if s.provider == "azure":
                self.headers["api-key"] = s.api_key
            else:
                self.headers["Authorization"] = f"Bearer {s.api_key}"
        self.temperature = s.llm_temperature

    def chat(self, messages, max_tokens) -> RawReply:
        body = {"model": self.s.model, "messages": messages, "max_tokens": max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        try:
            r = self.httpx.post(self.url, json=body, headers=self.headers, timeout=self.s.llm_timeout_s)
        except (self.httpx.TimeoutException, self.httpx.TransportError) as e:
            raise TransportError("transient", type(e).__name__) from None
        if r.status_code == 429:
            ra = None
            try:
                ra = float(r.headers.get("retry-after"))
            except (TypeError, ValueError):
                pass
            raise TransportError("rate_limit", "rate limited", ra)
        if r.status_code in (401, 403):
            raise TransportError("auth", "authentication failed")
        if r.status_code >= 500:
            raise TransportError("transient", f"status {r.status_code}")
        if r.status_code >= 400:
            raise TransportError("bad_request", f"status {r.status_code}: {r.text[:200]}")
        data = r.json()
        ch = data["choices"][0]
        u = data.get("usage") or {}
        return RawReply((ch.get("message") or {}).get("content") or "", ch.get("finish_reason") or "stop",
                        u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


# ---- client ------------------------------------------------------------------------------------------
class LLM:
    """The single client used by the agent. Handles retry/backoff, budgets and tolerant parsing."""

    def __init__(self, settings: Settings, transport=None, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.provider = settings.provider
        self.model = settings.model
        self.sleep = sleep
        self._transport = transport
        self.calls = 0
        self.counts: dict[str, int] = {}  # completed stage calls, e.g. counts["repair"]

    @property
    def transport(self):
        if self._transport is None:
            s = self.settings
            if not s.llm_configured:
                raise LLMNotConfigured("No LLM API key is configured. Set LLM_API_KEY (or GROQ_API_KEY).")
            if s.provider == "groq" and not s.base_url:
                self._transport = GroqTransport(s)
            else:
                self._transport = OpenAICompatTransport(s)
        return self._transport

    # --- one HTTP call with retry/backoff ----
    def _call(self, messages, max_tokens) -> RawReply:
        tr = self.transport
        retries = self.settings.llm_max_retries
        attempt = 0
        while True:
            try:
                self.calls += 1
                return tr.chat(messages, max_tokens)
            except TransportError as e:
                if e.kind == "auth":
                    raise LLMAuthFailed("The LLM provider rejected the credentials.") from None
                if e.kind == "bad_request":
                    raise LLMUnavailable(redact(f"LLM request rejected: {e}", self.settings.api_key)) from None
                if attempt >= retries:
                    if e.kind == "rate_limit":
                        raise LLMRateLimited("The LLM provider is rate limiting requests; try again shortly.") from None
                    raise LLMUnavailable("The LLM provider is unavailable.") from None
                delay = min(float(e.retry_after), 20.0) if e.retry_after else (2 ** attempt)
                delay += random.random() * 0.25
                log.warning("llm %s, retry %d in %.1fs", e.kind, attempt + 1, delay)
                self.sleep(delay)
                attempt += 1

    def complete(self, *, stage: str, system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        budget = STAGE_BUDGETS.get(stage, 1500)
        self.counts[stage] = self.counts.get(stage, 0) + 1
        system = system + "\n\nReply with ONE JSON object only. No markdown, no commentary."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        t0 = time.monotonic()
        reply = self._call(messages, budget)
        if reply.finish_reason == "length":  # truncated: retry once with a doubled budget
            log.info("llm stage=%s truncated at %d tokens; retrying with %d", stage, budget, budget * 2)
            reply = self._call(messages, budget * 2)
        err = None
        for attempt in (0, 1):
            try:
                obj = extract_json(reply.text)
                out = model_cls.model_validate(obj)
                log.info("llm stage=%s model=%s ms=%d tokens=%d/%d", stage, self.model,
                         (time.monotonic() - t0) * 1000, reply.prompt_tokens, reply.completion_tokens)
                return out
            except (ValueError, ValidationError) as e:
                err = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
                if attempt == 1:
                    break
                messages = messages + [
                    {"role": "assistant", "content": (reply.text or "")[:1500]},
                    {"role": "user", "content": f"Your last reply was not valid: {err}. "
                     "Reply again with ONLY the JSON object requested."}]
                reply = self._call(messages, budget * (2 if reply.finish_reason == "length" else 1))
        raise LLMBadOutput(f"The model did not return usable JSON for stage {stage}.")


def build_llm(settings: Settings):
    if settings.backend == "smoke":
        from .smoke import SmokeLLM
        log.warning("LLM_BACKEND=smoke: using the scripted smoke backend (no real model)")
        return SmokeLLM()
    return LLM(settings)


# ---- test double ---------------------------------------------------------------------------------------
class ScriptedLLM:
    """Deterministic test double. handlers: stage -> callable(system, user, call_index) -> dict | BaseModel | Exception."""

    provider = "mock"
    model = "mock"

    def __init__(self, handlers: dict[str, Callable] | None = None):
        self.handlers = handlers or {}
        self.calls: list[tuple[str, str, str]] = []
        self.counts: dict[str, int] = {}

    def complete(self, *, stage, system, user, model_cls):
        self.calls.append((stage, system, user))
        i = self.counts.get(stage, 0)
        self.counts[stage] = i + 1
        h = self.handlers.get(stage)
        if h is None:
            raise LLMUnavailable(f"no scripted handler for stage {stage}")
        out = h(system, user, i)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, BaseModel):
            return out
        return model_cls.model_validate(out)

    def stage_calls(self, stage):
        return [c for c in self.calls if c[0] == stage]
