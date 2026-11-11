"""Guard: every test must make a real assertion. Fails naming file and function."""
import ast
import re

from tests.conftest import ROOT

TESTS = ROOT / "tests"


def problems(source: str, filename: str = "x.py") -> list[str]:
    tree = ast.parse(source)
    out = []
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
    for fn in funcs:
        body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
        label = f"{filename}::{fn.name}"
        if not body or all(isinstance(n, ast.Pass) for n in body):
            out.append(f"{label}: empty body")
            continue
        asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.With) and any(
            isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, "attr", "") in ("raises", "warns")
            for i in n.items)]
        helper_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("check", "verify")]
        skips = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "skip"]
        if not asserts and not raises and not helper_calls:
            out.append(f"{label}: no assertion")
        for a in asserts:
            t = a.test
            if isinstance(t, ast.Constant) and t.value:
                out.append(f"{label}: assert on a constant")
            if isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq) and ast.dump(t.left) == ast.dump(t.comparators[0]):
                out.append(f"{label}: assert x == x")
        if len(asserts) and all(
                isinstance(a.test, ast.Compare) and isinstance(a.test.comparators[0], ast.Constant) and a.test.comparators[0].value in (200, 201)
                and ast.unparse(a.test.left).endswith("status_code") for a in asserts):
            out.append(f"{label}: only checks a 2xx status code")
        for dec in fn.decorator_list:
            if re.search(r"skip(?!if)", ast.unparse(dec)):
                out.append(f"{label}: unconditional skip")
        _ = skips
    return out


def test_every_repo_test_makes_real_assertions():
    files = sorted(TESTS.glob("test_*.py"))
    assert len(files) >= 20
    total = 0
    for f in files:
        src = f.read_text(encoding="utf-8")
        total += len(re.findall(r"^def test_", src, flags=re.M))
        assert problems(src, f.name) == [], f.name
    assert total >= 150


def test_guard_detects_each_bad_pattern():
    assert problems("def test_a():\n    pass\n") == ["x.py::test_a: empty body"]
    assert problems("def test_a():\n    '''doc'''\n") == ["x.py::test_a: empty body"]
    assert problems("def test_a():\n    x = 1\n") == ["x.py::test_a: no assertion"]
    assert problems("def test_a():\n    assert True\n") == ["x.py::test_a: assert on a constant"]
    assert problems("def test_a(r):\n    assert r == r\n") == ["x.py::test_a: assert x == x"]
    assert problems("def test_a(r):\n    assert r.status_code == 200\n") == ["x.py::test_a: only checks a 2xx status code"]
    assert problems("import pytest\n@pytest.mark.skip\ndef test_a():\n    assert 1 == 2\n") == ["x.py::test_a: unconditional skip"]
    assert problems("def test_a(r):\n    assert r.status_code == 200\n    assert r.json()['k'] == 3\n") == []


def test_pytest_ini_only_deselects_the_live_marker():
    ini = (ROOT / "pytest.ini").read_text()
    assert 'addopts = -m "not live"' in ini and "markers" in ini and "live:" in ini
    assert "--ignore" not in ini and "deselect" not in ini and "-k " not in ini


def test_golden_suite_covers_all_questions_on_both_databases():
    from tests.test_golden import GOLDEN_IDS
    src = (TESTS / "test_golden.py").read_text()
    assert len(GOLDEN_IDS) == 11 and '"db_base", "db_overlay"' in src and "parametrize(\"qid\", GOLDEN_IDS)" in src
