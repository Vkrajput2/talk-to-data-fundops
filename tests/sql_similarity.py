"""sqlglot-normalised SQL similarity: tables, join keys, predicates, aggregates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp


@dataclass
class Similarity:
    tables_match: bool
    joins_match: bool
    predicate_match: bool
    aggregation_match: bool
    overall: float
    details: dict = field(default_factory=dict)


def _subst(sql: str, date_ctx) -> str:
    if date_ctx is None:
        return sql
    return sql.replace("{as_of}", date_ctx.as_of)


def features(sql: str) -> dict:
    tree = sqlglot.parse_one(sql, read="sqlite")
    ctes = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    alias_map, tables = {}, set()
    for t in tree.find_all(exp.Table):
        n = t.name.lower()
        if n in ctes:
            continue
        tables.add(n)
        alias_map[(t.alias or t.name).lower()] = n
    joins = set()
    for eq in tree.find_all(exp.EQ):
        a, b = eq.left, eq.right
        if isinstance(a, exp.Column) and isinstance(b, exp.Column):
            ta, tb = alias_map.get((a.table or "").lower()), alias_map.get((b.table or "").lower())
            if ta and tb and ta != tb:
                joins.add(tuple(sorted([f"{ta}.{a.name.lower()}", f"{tb}.{b.name.lower()}"])))
    preds = set()
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between, exp.Like):
        cols = list(node.find_all(exp.Column))
        lits = [l.name.lower() for l in node.find_all(exp.Literal)]
        if cols and lits and not (isinstance(node, exp.EQ) and isinstance(node.left, exp.Column)
                                  and isinstance(node.right, exp.Column)):
            preds.add((cols[0].name.lower(), type(node).__name__, tuple(sorted(lits))))
    aggs = {type(a).__name__ for a in tree.find_all(exp.AggFunc)}
    return {"tables": tables, "joins": joins, "predicates": preds, "aggs": aggs}


def _jacc(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def compare(ref_sql: str, gen_sql: str, date_ctx=None) -> Similarity:
    try:
        r, g = features(_subst(ref_sql, date_ctx)), features(_subst(gen_sql, date_ctx))
    except Exception as e:  # unparsable generated SQL
        return Similarity(False, False, False, False, 0.0, {"error": str(e)[:100]})
    t_ok = r["tables"] <= g["tables"]
    j_ok = r["joins"] <= g["joins"] or len(r["tables"]) <= 1
    p_ok = {(c, k) for c, k, _ in r["predicates"]} <= {(c, k) for c, k, _ in g["predicates"]} or not r["predicates"]
    a_ok = r["aggs"] <= g["aggs"]
    overall = sum((_jacc(r["tables"], g["tables"]), _jacc(r["joins"], g["joins"]),
                   _jacc({(c, k) for c, k, _ in r["predicates"]}, {(c, k) for c, k, _ in g["predicates"]}),
                   _jacc(r["aggs"], g["aggs"]))) / 4
    return Similarity(t_ok, j_ok, p_ok, a_ok, round(overall, 3),
                      {"missing_tables": sorted(r["tables"] - g["tables"]), "extra_tables": sorted(g["tables"] - r["tables"]),
                       "missing_joins": sorted(r["joins"] - g["joins"]),
                       "missing_predicates": sorted({(c, k) for c, k, _ in r["predicates"]} - {(c, k) for c, k, _ in g["predicates"]}),
                       "missing_aggs": sorted(r["aggs"] - g["aggs"])})
