import logging

import pytest

from app.config import RedactingFilter, Settings, load_settings
from app.llm import (LLM, LLMAuthFailed, LLMBadOutput, LLMNotConfigured, LLMRateLimited, LLMUnavailable, RawReply,
                     TransportError, extract_json)
from app.models import GenerateOut, UnderstandOut

SENTINEL = "gsk_SENTINEL_KEY_1234567890_abcdef"


class FakeTransport:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def chat(self, messages, max_tokens):
        self.calls.append((messages, max_tokens))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(script, retries=4):
    sleeps = []
    s = Settings(api_key=SENTINEL, llm_max_retries=retries)
    return LLM(s, FakeTransport(script), sleep=sleeps.append), sleeps


def ok(text, finish="stop"):
    return RawReply(text, finish, 10, 5)


def test_extract_json_handles_fences_reasoning_and_noise():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>maybe {"x": 0} ?</think>Here you go: {"sql": "SELECT 1", "n": {"k": "}"}} thanks') == {"sql": "SELECT 1", "n": {"k": "}"}}
    assert extract_json('{"bad": } then {"good": true}') == {"good": True}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parses_fenced_reply_with_reasoning_prefix():
    c, _ = client([ok('Reasoning: let me think.\n```json\n{"sql": "SELECT 1", "assumptions": ["x"]}\n```')])
    out = c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert out.sql == "SELECT 1" and out.assumptions == ["x"]


def test_retry_429_follows_backoff_schedule():
    rl = TransportError("rate_limit")
    c, sleeps = client([rl, rl, rl, ok('{"sql": "SELECT 1"}')])
    c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert len(sleeps) == 3
    for got, base in zip(sleeps, (1, 2, 4)):
        assert base <= got <= base + 0.25


def test_retry_after_header_is_honoured_and_capped():
    c, sleeps = client([TransportError("rate_limit", retry_after=7.0), TransportError("rate_limit", retry_after=500.0),
                        ok('{"sql": "SELECT 1"}')])
    c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert 7.0 <= sleeps[0] <= 7.25 and 20.0 <= sleeps[1] <= 20.25


def test_rate_limit_exhaustion_and_transient_exhaustion():
    c, sleeps = client([TransportError("rate_limit")] * 3, retries=2)
    with pytest.raises(LLMRateLimited):
        c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert len(sleeps) == 2
    c, _ = client([TransportError("transient")] * 2, retries=1)
    with pytest.raises(LLMUnavailable):
        c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)


def test_auth_error_does_not_retry():
    c, sleeps = client([TransportError("auth"), ok("{}")])
    with pytest.raises(LLMAuthFailed):
        c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert sleeps == [] and len(c.transport.calls) == 1


def test_bad_request_maps_to_unavailable_without_key():
    c, _ = client([TransportError("bad_request", f"status 400: {SENTINEL} invalid")])
    with pytest.raises(LLMUnavailable) as e:
        c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert SENTINEL not in str(e.value)


def test_invalid_json_is_reasked_once_then_succeeds_or_fails():
    c, _ = client([ok("not json"), ok('{"sql": "SELECT 2"}')])
    assert c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut).sql == "SELECT 2"
    second = c.transport.calls[1][0]
    assert second[-1]["role"] == "user" and "not valid" in second[-1]["content"]
    c, _ = client([ok("nope"), ok("still nope")])
    with pytest.raises(LLMBadOutput):
        c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert len(c.transport.calls) == 2


def test_schema_violation_triggers_reask():
    c, _ = client([ok('{"intent": "lookup"}'), ok('{"standalone_question": "q"}')])
    out = c.complete(stage="understand", system="s", user="u", model_cls=UnderstandOut)
    assert out.standalone_question == "q" and out.intent == "lookup"


def test_truncated_reply_retries_once_with_doubled_budget():
    c, _ = client([ok('{"sql": "SELECT 1', finish="length"), ok('{"sql": "SELECT 1"}')])
    out = c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)
    assert out.sql == "SELECT 1"
    budgets = [b for _, b in c.transport.calls]
    assert budgets == [3500, 7000]
    c, _ = client([ok("{}", finish="length"), ok('{"standalone_question": "q"}')])
    c.complete(stage="understand", system="s", user="u", model_cls=UnderstandOut)
    assert [b for _, b in c.transport.calls] == [1000, 2000]


def test_per_stage_budgets():
    for stage, want in (("understand", 1000), ("resolve", 1500), ("generate", 3500), ("repair", 3500), ("narrate", 1500)):
        c, _ = client([ok('{"sql": "x", "standalone_question": "q", "narrative": "n", "tables": []}')])
        c.complete(stage=stage, system="s", user="u", model_cls=GenerateOut)
        assert c.transport.calls[0][1] == want


def test_no_key_raises_not_configured_without_network():
    c = LLM(Settings(api_key=None))
    with pytest.raises(LLMNotConfigured):
        c.complete(stage="generate", system="s", user="u", model_cls=GenerateOut)


def test_key_never_reaches_logs_or_repr(caplog):
    s = Settings(api_key=SENTINEL)
    assert SENTINEL not in repr(s)
    flt = RedactingFilter(SENTINEL)
    logging.getLogger("t").addFilter(flt)
    rec = logging.LogRecord("t", logging.INFO, "f", 1, f"using {SENTINEL} and gsk_abcdefghijklmnop", None, None)
    flt.filter(rec)
    assert SENTINEL not in rec.msg and "gsk_abcdefghijklmnop" not in rec.msg and "***" in rec.msg


def test_provider_model_and_key_switch_by_environment_only(monkeypatch, tmp_path):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "GROQ_API_KEY", "GROK_API_DEV_KEY", "GROQ_MODEL", "LLM_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    none = tmp_path / "none.env"
    s = load_settings(none)
    assert (s.provider, s.model, s.api_key) == ("groq", "openai/gpt-oss-20b", None) and not s.llm_configured
    monkeypatch.setenv("GROK_API_DEV_KEY", "k-fallback-2")
    assert load_settings(none).api_key == "k-fallback-2"
    monkeypatch.setenv("GROQ_API_KEY", "k-fallback-1")
    assert load_settings(none).api_key == "k-fallback-1"
    monkeypatch.setenv("LLM_API_KEY", "k-primary")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    s = load_settings(none)
    assert s.api_key == "k-primary" and s.model == "llama-3.3-70b-versatile"
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.x.ai/v1")
    s = load_settings(none)
    assert (s.provider, s.model, s.base_url) == ("xai", "openai/gpt-oss-120b", "https://api.x.ai/v1")


def test_smoke_backend_refused_when_key_configured(monkeypatch, tmp_path):
    from app.config import ConfigError
    monkeypatch.setenv("LLM_BACKEND", "smoke")
    monkeypatch.setenv("LLM_API_KEY", "k")
    with pytest.raises(ConfigError, match="smoke"):
        load_settings(tmp_path / "none.env")
