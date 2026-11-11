from tests.comparator import compare_results as cmp

REF_C = ["fund_name", "aum"]
REF = [["MS US Core Bond Fund", 3.05], ["MS EM Fund", 1.02], ["MS Asia Fund", 1.88]]


def test_identical_results_pass():
    c = cmp(REF_C, REF, REF_C, [list(r) for r in REF], ordered=True)
    assert c.passed and c.strict and c.result_match_pct == 100.0 and c.cell_match_pct == 100.0


def test_renamed_columns_pass():
    c = cmp(REF_C, REF, ["name", "value"], REF)
    assert c.passed and c.matched_columns == {"fund_name": "name", "aum": "value"}


def test_shuffled_unordered_passes_but_fails_when_ordered():
    shuffled = [REF[2], REF[0], REF[1]]
    assert cmp(REF_C, REF, REF_C, shuffled, ordered=False).passed
    c = cmp(REF_C, REF, REF_C, shuffled, ordered=True)
    assert not c.passed


def test_rounding_tolerance():
    close = [["MS US Core Bond Fund", 3.04], ["MS EM Fund", 1.02], ["MS Asia Fund", 1.88]]
    assert cmp(REF_C, REF, REF_C, close, tol=0.05).passed
    far = [["MS US Core Bond Fund", 3.25], ["MS EM Fund", 1.02], ["MS Asia Fund", 1.88]]
    assert not cmp(REF_C, REF, REF_C, far, tol=0.05).passed


def test_fund_name_normalisation():
    assert cmp(["f"], [["MS US Core Bond Fund"]], ["f"], [["US Core Bond"]]).passed


def test_unit_scaling():
    ref = [["A", 5130000000.0]]
    c = cmp(["n", "aum"], ref, ["n", "aum_bn"], [["A", 5.13]], tol=0.011)
    assert c.passed and c.unit_scaled
    assert not cmp(["n", "aum"], ref, ["n", "aum_bn"], [["A", 5.13]], scale=False).passed


def test_extra_generated_column_is_fine_and_missing_reference_column_fails():
    c = cmp(REF_C, REF, REF_C + ["extra"], [r + ["x"] for r in REF])
    assert c.passed and c.strict
    c = cmp(REF_C, REF, ["fund_name"], [[r[0]] for r in REF])
    assert not c.passed and c.unmatched_ref_columns == ["aum"]


def test_one_wrong_cell_in_five_rows_is_eighty_percent_and_fails():
    ref = [["a", 1], ["b", 2], ["c", 3], ["d", 4], ["e", 5]]
    gen = [["a", 1], ["b", 2], ["c", 3], ["d", 4], ["e", 99]]
    c = cmp(["k", "v"], ref, ["k", "v"], gen)
    assert c.result_match_pct == 80.0 and not c.passed and c.unmatched_ref_columns == ["v"]


def test_row_count_mismatch_fails():
    c = cmp(["k"], [[1], [2], [3], [4], [5]], ["k"], [[1], [2], [3], [4]])
    assert not c.row_count_match and not c.passed
