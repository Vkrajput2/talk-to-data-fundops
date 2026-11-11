"""Example bank + stdlib TF-IDF retrieval."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

BANK_PATH = Path(__file__).with_name("example_bank.json")
STOP = set("a an the of for in on at to by is are was were do does did what which who how show list me us all with "
           "and or vs than from that this these those please give".split())


def tokenize(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if t in STOP:
            continue
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        out.append(t)
    return out


@dataclass
class Example:
    id: str
    question: str
    sql: str
    tables: list[str]
    intent: str
    pattern: bool


def substitute(sql: str, date_ctx) -> str:
    m = {
        "as_of": date_ctx.as_of, "yesterday": date_ctx.yesterday,
        "cash_date": date_ctx.snapshot_dates.get("fact_cash_balance", date_ctx.as_of),
        "nav_date": date_ctx.snapshot_dates.get("fact_nav", date_ctx.as_of),
        "sla_date": date_ctx.snapshot_dates.get("fact_sla", date_ctx.as_of),
        "current_month": date_ctx.current_month, "previous_month": date_ctx.previous_month,
        "four_week_start": date_ctx.four_week_start, "rev_cur": date_ctx.revenue_month,
        "rev_prior": date_ctx.revenue_prior_year_month,
    }
    for k, v in m.items():
        sql = sql.replace("{" + k + "}", v)
    return sql


class ExampleBank:
    def __init__(self, path: Path = BANK_PATH):
        self.examples = [Example(**e) for e in json.loads(path.read_text(encoding="utf-8"))]
        self._tok = [tokenize(e.question) for e in self.examples]
        n = len(self.examples)
        df = Counter(t for toks in self._tok for t in set(toks))
        self._idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
        self._vec = [self._vector(toks) for toks in self._tok]

    def _vector(self, toks):
        tf = Counter(toks)
        v = {t: c * self._idf.get(t, math.log(1 + len(self.examples)) + 1) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def retrieve(self, question: str, k: int = 3, exclude_ids=frozenset(), allowed_funds=None,
                 fund_names: dict[str, str] | None = None) -> list[tuple[Example, float]]:
        q = self._vector(tokenize(question))
        scored = []
        for ex, vec in zip(self.examples, self._vec):
            if ex.id in exclude_ids:
                continue
            if allowed_funds is not None and self._mentions_forbidden(ex, allowed_funds, fund_names or {}):
                continue
            s = sum(w * vec.get(t, 0.0) for t, w in q.items())
            scored.append((ex, s))
        scored.sort(key=lambda p: (-p[1], p[0].id))
        return scored[:k]

    @staticmethod
    def _mentions_forbidden(ex: Example, allowed: set[str], names: dict[str, str]) -> bool:
        text = ex.sql + " " + ex.question
        for fid in re.findall(r"\bF\d{3}\b", text):
            if fid not in allowed:
                return True
        low = text.lower()
        for fid, name in names.items():
            if fid in allowed:
                continue
            short = re.sub(r"^MS | Fund$", "", name).lower()
            if short in low or name.lower() in low:
                return True
        return False
