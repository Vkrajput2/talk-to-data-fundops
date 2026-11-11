"""Settings and secret handling. The API key is read from the environment only."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
KEY_VARS = ("LLM_API_KEY", "GROQ_API_KEY", "GROK_API_DEV_KEY")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConfigError(ValueError):
    pass


def _opt(name: str) -> str | None:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else None


def _int(name: str, default: int, lo: int = 1, hi: int = 10**9) -> int:
    v = _opt(name)
    if v is None:
        return default
    try:
        n = int(v)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{name} out of range")
    return n


def _date(name: str, *aliases: str) -> str | None:
    for n in (name, *aliases):
        v = _opt(n)
        if v is not None:
            if not _ISO.match(v):
                raise ConfigError(f"{n} must be YYYY-MM-DD")
            try:
                dt.date.fromisoformat(v)
            except ValueError:
                raise ConfigError(f"{n} is not a valid date") from None
            return v
    return None


@dataclass
class Settings:
    provider: str = "groq"
    model: str = "openai/gpt-oss-20b"
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    backend: str = "llm"  # "llm" or "smoke"
    db_path: str = str(ROOT / "msim_fundops.db")
    data_dir: str = str(ROOT / "Data")
    use_supplement: bool = True
    as_of_date: str | None = None
    yesterday_date: str | None = None
    port: int = 8000
    row_cap: int = 500
    query_timeout_s: float = 5.0
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 6
    llm_temperature: float | None = None
    max_sql_repairs: int = 2
    session_ttl_s: int = 7200
    max_sessions: int = 200
    request_deadline_s: int = 90

    @property
    def llm_configured(self) -> bool:
        # a local OpenAI-compatible endpoint (e.g. Ollama) may not need a key
        return bool(self.api_key) or self.backend == "smoke" or (
            self.provider == "ollama" and bool(self.base_url))


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    key = next((v for v in (_opt(n) for n in KEY_VARS) if v), None)
    as_of = _date("AS_OF_DATE", "ANCHOR_DATE")
    yday = _date("YESTERDAY_DATE")
    if as_of and yday and yday > as_of:
        raise ConfigError("YESTERDAY_DATE must be <= AS_OF_DATE")
    temp = _opt("LLM_TEMPERATURE")
    s = Settings(
        provider=(_opt("LLM_PROVIDER") or "groq").lower(),
        model=_opt("LLM_MODEL") or _opt("GROQ_MODEL") or "openai/gpt-oss-20b",
        api_key=key,
        base_url=_opt("LLM_BASE_URL"),
        backend=(_opt("LLM_BACKEND") or "llm").lower(),
        db_path=_opt("MSIM_DB_PATH") or str(ROOT / "msim_fundops.db"),
        data_dir=_opt("MSIM_DATA_DIR") or str(ROOT / "Data"),
        use_supplement=(_opt("MSIM_USE_SUPPLEMENT") or "1") != "0",
        as_of_date=as_of,
        yesterday_date=yday,
        port=_int("PORT", 8000, 1, 65535),
        row_cap=_int("ROW_CAP", 500),
        query_timeout_s=float(_int("QUERY_TIMEOUT_S", 5)),
        llm_timeout_s=float(_int("LLM_TIMEOUT_S", 60)),
        llm_max_retries=_int("LLM_MAX_RETRIES", 6, 0, 10),
        llm_temperature=float(temp) if temp is not None else None,
        max_sql_repairs=_int("MAX_SQL_REPAIRS", 2, 0, 5),
        request_deadline_s=_int("REQUEST_DEADLINE_S", 90),
    )
    if s.backend == "smoke" and key:
        raise ConfigError("LLM_BACKEND=smoke is refused when an API key is configured")
    install_redaction(key)
    return s


class RedactingFilter(logging.Filter):
    """Replace the key value and key-looking tokens in every log record."""

    def __init__(self, secret: str | None):
        super().__init__()
        self.secret = secret

    def redact(self, text: str) -> str:
        if self.secret:
            text = text.replace(self.secret, "***")
        return re.sub(r"(gsk_|xai-|sk-)[A-Za-z0-9_\-]{8,}", "***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.redact(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


def install_redaction(secret: str | None) -> None:
    root = logging.getLogger()
    for f in list(root.filters):
        if isinstance(f, RedactingFilter):
            root.removeFilter(f)
    flt = RedactingFilter(secret)
    root.addFilter(flt)
    for h in root.handlers:
        h.addFilter(flt)


def redact(text: str, secret: str | None) -> str:
    return RedactingFilter(secret).redact(text)
