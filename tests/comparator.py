"""Order/column-name-insensitive, value-normalised result comparison (execution accuracy)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCALES = (1e3, 1e6, 1e9)


@dataclass
class Comparison:
    row_count_match: bool
    result_match_pct: float
    cell_match_pct: float
    matched_columns: dict = field(default_factory=dict)
    unmatched_ref_columns: list = field(default_factory=list)
    unit_scaled: bool = False
    strict: bool = False
    passed: bool = False


def norm_text(v) -> str:
    s = str(v).strip().lower()
    s = re.sub(r"^ms ", "", s)
    s = re.sub(r" fund$", "", s)
    return s


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def cells_equal(a, b, tol: float, scale: bool, flag: list | None = None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if _is_num(a) and _is_num(b):
        if abs(a - b) <= tol + 1e-9:
            return True
        if scale:
            for k in SCALES:
                for x, y in ((a, b * k), (a * k, b)):
                    if abs(x - y) <= tol * k + 1e-9:
                        if flag is not None:
                            flag.append(True)
                        return True
        return False
    if _is_num(a) != _is_num(b):
        try:
            return abs(float(a) - float(b)) <= tol + 1e-9
        except (TypeError, ValueError):
            return False
    return norm_text(a) == norm_text(b)


def _col_matches(ref_vals, gen_vals, ordered, tol, scale, flag) -> bool:
    if len(ref_vals) != len(gen_vals):
        return False
    if ordered:
        return all(cells_equal(a, b, tol, scale, flag) for a, b in zip(ref_vals, gen_vals))
    left = list(gen_vals)
    for a in ref_vals:
        for i, b in enumerate(left):
            if cells_equal(a, b, tol, scale, flag):
                del left[i]
                break
        else:
            return False
    return True


def compare_results(ref_cols, ref_rows, gen_cols, gen_rows, ordered=False, tol=0.011, scale=True) -> Comparison:
    flag: list = []
    rc = len(ref_rows) == len(gen_rows)
    mapping: dict = {}
    unmatched = []
    used = set()
    for j, rname in enumerate(ref_cols):
        rv = [r[j] for r in ref_rows]
        pick = None
        for i, gname in enumerate(gen_cols):
            if i in used:
                continue
            if _col_matches(rv, [r[i] for r in gen_rows], ordered, tol, scale, flag):
                if pick is None or gname.lower() == rname.lower():
                    pick = i
        if pick is None:
            unmatched.append(rname)
        else:
            mapping[rname] = gen_cols[pick]
            used.add(pick)
    # partial mapping: an unmatched reference column that agrees on >= 50% of rows (rows aligned via matched columns)
    partial: dict = {}
    key_refs = [j for j, c in enumerate(ref_cols) if c in mapping]
    key_gens = [list(gen_cols).index(mapping[ref_cols[j]]) for j in key_refs]

    def key(vals):
        return tuple(round(float(v), 2) if _is_num(v) else norm_text(v) for v in vals)
    gen_by_key = {}
    for r in gen_rows:
        gen_by_key.setdefault(key([r[i] for i in key_gens]), r)
    for rname in unmatched:
        j = list(ref_cols).index(rname)
        best, best_s = None, 0.0
        for i, gname in enumerate(gen_cols):
            if i in used:
                continue
            hits = 0
            for k, rr in enumerate(ref_rows):
                g = gen_by_key.get(key([rr[x] for x in key_refs])) if key_refs else (gen_rows[k] if k < len(gen_rows) else None)
                if g is not None and cells_equal(rr[j], g[i], tol, scale):
                    hits += 1
            s = hits / (len(ref_rows) or 1)
            if s > best_s:
                best, best_s = i, s
        if best is not None and best_s >= 0.5:
            partial[rname] = gen_cols[best]
            used.add(best)
    # row-level match on the matched (and partially matched) columns
    row_map = {**mapping, **partial}
    mrefs = [j for j, c in enumerate(ref_cols) if c in row_map]
    mgens = [list(gen_cols).index(row_map[ref_cols[j]]) for j in mrefs]
    gen_proj = [[r[i] for i in mgens] for r in gen_rows]
    matched_rows = 0
    pool = list(gen_proj)
    for k, rr in enumerate(ref_rows):
        proj = [rr[j] for j in mrefs]
        if ordered:
            ok = k < len(gen_proj) and all(cells_equal(a, b, tol, scale) for a, b in zip(proj, gen_proj[k]))
            matched_rows += 1 if ok else 0
        else:
            for i, g in enumerate(pool):
                if all(cells_equal(a, b, tol, scale) for a, b in zip(proj, g)):
                    matched_rows += 1
                    del pool[i]
                    break
    n = len(ref_rows) or 1
    cell_pct = 100.0 * len(mapping) / (len(ref_cols) or 1)
    numeric_cols = [c for j, c in enumerate(ref_cols) if any(_is_num(r[j]) for r in ref_rows)]
    nums_ok = all(c in mapping for c in numeric_cols)
    text_cols = [c for c in ref_cols if c not in numeric_cols]
    text_ok = (not text_cols) or sum(c in mapping for c in text_cols) / len(text_cols) >= 0.5
    strict = rc and not unmatched and matched_rows == len(ref_rows)
    passed = rc and nums_ok and text_ok and matched_rows == len(ref_rows)
    return Comparison(rc, 100.0 * matched_rows / n, cell_pct, mapping, unmatched, bool(flag), strict, passed)
