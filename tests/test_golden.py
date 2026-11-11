"""Reference (playbook) SQL must return the exact validated rows, with and without the synthetic supplement."""
import pytest

from app.db import run_query
from tests.conftest import GOLDEN_IDS, golden_expected, golden_sql, rows_equal

EXPECTED = golden_expected()


def test_expected_file_covers_all_eleven_questions():
    assert sorted(EXPECTED) == sorted(GOLDEN_IDS)
    assert EXPECTED["Q10"]["rows"][0] == ["MS EMEA Fixed Income Fund", 16]


@pytest.mark.parametrize("qid", GOLDEN_IDS)
@pytest.mark.parametrize("which", ["db_base", "db_overlay"])
def test_reference_sql_matches_expected_rows(qid, which, request):
    db = request.getfixturevalue(which)
    res = run_query(db, golden_sql(qid))
    exp = EXPECTED[qid]
    assert res.columns == exp["columns"]
    assert rows_equal(res.rows, exp["rows"], ordered=exp["ordered"]), (qid, res.rows)


def test_headline_numbers(db_overlay):
    q1 = run_query(db_overlay, golden_sql("Q1")).rows
    assert [r[4] for r in q1] == [5.13, 3.05, 2.35, 1.88, 1.45, 1.02]
    assert run_query(db_overlay, golden_sql("Q4")).rows == [[85.7, 98.1]]
    q4f = run_query(db_overlay, golden_sql("Q4F")).rows
    assert q4f == [["Helios Securities", 4, 11050000], ["Aurora Investment Bank", 1, 1720000]]
    q3 = run_query(db_overlay, golden_sql("Q3")).rows
    assert len(q3) == 5 and sum(1 for r in q3 if "Helios" in r[3]) == 4
    q8 = run_query(db_overlay, golden_sql("Q8")).rows[0]
    assert q8[:4] == [3137500, 2762500, 13.6, 13.6]
