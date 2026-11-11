import json
import re

from app.agent.retrieval import ExampleBank, substitute, tokenize
from app.agent.validate import validate
from app.db import run_query
from app.entitlements import MOCK_USERS
from tests.conftest import GOLDEN

BANK = ExampleBank()
PARA = json.loads((GOLDEN / "paraphrases.json").read_text())
NOVEL = json.loads((GOLDEN / "novel_questions.json").read_text())


def test_ids_unique_and_entries_complete():
    ids = [e.id for e in BANK.examples]
    assert len(ids) == len(set(ids)) >= 25
    assert all(e.question.strip() and e.sql.strip() and e.tables for e in BANK.examples)


def test_every_bank_sql_validates_and_runs_after_date_substitution(db_overlay, cat_overlay, date_ctx):
    for e in BANK.examples:
        sql = substitute(e.sql, date_ctx)
        assert "{" not in sql and "}" not in sql, e.id
        v = validate(sql, cat_overlay, MOCK_USERS["priya"])
        r = run_query(db_overlay, v.sql)
        assert r.columns, e.id
        assert set(e.tables) <= set(v.tables_used) | set(e.tables)


def test_retrieval_returns_relevant_examples_for_paraphrases():
    top = BANK.retrieve("Which portfolios are holding less than 2% of their assets in available cash today?", 3)
    assert top[0][0].id == "Q2" and len(top) == 3 and top[0][1] > top[1][1] >= top[2][1]
    ids = [e.id for e, _ in BANK.retrieve("Split yesterday's failed trades by broker", 3)]
    assert "Q4F" in ids
    assert BANK.retrieve("Q2 style", 2, exclude_ids={"Q2"})[0][0].id != "Q2"


def test_retrieval_hides_other_funds_from_restricted_users():
    names = {"F001": "MS Global Equity Alpha Fund", "F004": "MS US Core Bond Fund", "F002": "MS EMEA Fixed Income Fund"}
    ids = {e.id for e, _ in BANK.retrieve("net flows for the US Core Bond fund", 30, allowed_funds={"F002"}, fund_names=names)}
    assert "B19" not in ids and "Q8" not in ids  # these name F004 / F001
    all_ids = {e.id for e, _ in BANK.retrieve("net flows for the US Core Bond fund", 30)}
    assert "B19" in all_ids and "Q8" in all_ids


def test_tokenizer_drops_stopwords_and_plural_s():
    assert tokenize("Which funds are the breaks?") == ["fund", "break"]


def test_no_paraphrase_or_novel_leaks_into_the_bank():
    bank_q = {" ".join(tokenize(e.question)) for e in BANK.examples}
    for base, items in PARA.items():
        base_q = next(e for e in BANK.examples if e.id == base).question
        bt = set(tokenize(base_q))
        for it in items:
            pt = set(tokenize(it["question"]))
            assert " ".join(tokenize(it["question"])) not in bank_q, it["id"]
            assert len(pt & bt) / len(pt | bt) < 0.8, it["id"]
    norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
    bank_sql = {norm(e.sql) for e in BANK.examples}
    for n in NOVEL:
        assert norm(n["sql"]) not in bank_sql and " ".join(tokenize(n["question"])) not in bank_q, n["id"]
