import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.db import build_database, run_query  # noqa: E402
from app.entitlements import MOCK_USERS  # noqa: E402
from app.metadata import AliasIndex, build_catalog, compute_date_context  # noqa: E402

GOLDEN = ROOT / "tests" / "golden"
GOLDEN_IDS = ["Q1", "Q2", "Q3", "Q4", "Q4F", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10"]
DATA = ROOT / "Data"


def golden_sql(qid: str) -> str:
    return (GOLDEN / f"{qid}.sql").read_text(encoding="utf-8")


def golden_expected() -> dict:
    return json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))


def norm(v):
    if isinstance(v, str):
        return v.strip().lower().removeprefix("ms ").removesuffix(" fund")
    return v


def rows_equal(actual, expected, ordered=True, tol=0.011) -> bool:
    def eq(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(a - b) <= tol
        return norm(a) == norm(b)
    if len(actual) != len(expected):
        return False
    if ordered:
        return all(len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b)) for a, b in zip(actual, expected))
    pool = [list(r) for r in actual]
    for e in expected:
        for i, a in enumerate(pool):
            if len(a) == len(e) and all(eq(x, y) for x, y in zip(a, e)):
                del pool[i]
                break
        else:
            return False
    return True


@pytest.fixture(scope="session")
def db_base(tmp_path_factory):
    p = tmp_path_factory.mktemp("db") / "base.db"
    build_database(DATA, p, include_supplement=False)
    return str(p)


@pytest.fixture(scope="session")
def db_overlay(tmp_path_factory):
    p = tmp_path_factory.mktemp("db") / "overlay.db"
    build_database(DATA, p, include_supplement=True)
    return str(p)


@pytest.fixture(scope="session")
def cat_overlay():
    return build_catalog(DATA, True)


@pytest.fixture(scope="session")
def cat_base():
    return build_catalog(DATA, False)


@pytest.fixture(scope="session")
def aliases(db_overlay, cat_overlay):
    return AliasIndex(db_overlay, cat_overlay.glossary)


@pytest.fixture(scope="session")
def date_ctx(db_overlay):
    return compute_date_context(db_overlay)


@pytest.fixture()
def settings(db_overlay):
    return Settings(db_path=db_overlay, data_dir=str(DATA), api_key=None, llm_max_retries=0)


@pytest.fixture()
def users():
    return MOCK_USERS


def q(db, sql):
    r = run_query(db, sql)
    return r.columns, r.rows
