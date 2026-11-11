"""Stage 4: sqlglot validator + entitlement rewrite. Executed SQL is always re-emitted from the AST."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify

from ..db import ALLOWED_FUNCTIONS
from ..entitlements import User, scope_to_funds

MAX_SQL_CHARS = 6000
MAX_PHYSICAL_TABLES = 12  # distinct catalog tables (CTE references do not count)
ROOT_OK = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_NAMES = ("Into", "Pragma", "Attach", "Detach", "Parameter", "Placeholder", "Lock", "Command")
FORBIDDEN_NODES = tuple(c for c in (getattr(exp, n, None) for n in _NAMES) if c is not None)
# Nodes that sqlglot models as Func but that are operators/constructs in SQLite, not function calls
NOT_CALLS = tuple(c for c in (getattr(exp, n, None) for n in (
    "Connector", "Binary", "Unary", "Predicate", "Exists", "Case", "Cast", "TryCast", "Paren", "Window",
    "Distinct", "Between", "In", "Not", "Subquery", "Alias", "Tuple", "Array")) if c is not None)
DANGEROUS = frozenset({"load_extension", "readfile", "writefile", "fts3_tokenizer", "randomblob", "zeroblob",
                       "random", "edit", "sqlite_compileoption_get", "sqlite_source_id"})
_CALL = re.compile(r"^\s*(\w+)\s*\(")


class ValidationError(Exception):
    def __init__(self, code: str, category: str, message: str):
        super().__init__(message)
        self.code, self.category, self.message = code, category, message


@dataclass
class ValidatedQuery:
    sql: str
    tables_used: list[str]
    columns_by_table: dict[str, list[str]]
    join_pairs: list[str]
    warnings: list[str] = field(default_factory=list)
    unscoped_tree: exp.Expression | None = None


def _sec(code, msg):
    return ValidationError(code, "security", msg)


def _sch(code, msg):
    return ValidationError(code, "schema", msg)


def function_name(node: exp.Func) -> str | None:
    """SQLite-visible function name, or None if the node is not rendered as a call."""
    if isinstance(node, NOT_CALLS):
        return None
    if isinstance(node, exp.Anonymous):
        return (node.name or "").lower()
    m = _CALL.match(node.sql(dialect="sqlite"))
    return m.group(1).lower() if m else None


def parse_single(sql: str) -> exp.Expression:
    if not isinstance(sql, str) or not sql.strip():
        raise _sec("sql_empty", "Empty SQL.")
    if len(sql) > MAX_SQL_CHARS:
        raise _sec("sql_too_long", "SQL is too long.")
    try:
        stmts = sqlglot.parse(sql, read="sqlite")
    except (ParseError, SqlglotError) as e:
        raise _sch("sql_syntax", f"SQL syntax error: {str(e).splitlines()[0][:200]}") from None
    stmts = [s for s in stmts if s is not None]
    if not stmts:
        raise _sec("sql_empty", "Empty SQL.")
    if len(stmts) > 1:
        raise _sec("multiple_statements", "Only a single statement is allowed.")
    return stmts[0]


def check_structure(tree: exp.Expression, catalog) -> set[str]:
    """Rules 3-5. Returns distinct physical catalog tables referenced."""
    if not isinstance(tree, ROOT_OK):
        raise _sec("statement_not_allowed", "Only SELECT / WITH ... SELECT statements are allowed.")
    for n in tree.walk():
        node = n[0] if isinstance(n, tuple) else n
        if isinstance(node, FORBIDDEN_NODES):
            raise _sec("construct_not_allowed", "Construct not allowed in a read-only query.")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise _sec("construct_not_allowed", "WITH RECURSIVE is not allowed.")
    ctes = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    physical: set[str] = set()
    for t in tree.find_all(exp.Table):
        if t.args.get("db") or t.args.get("catalog"):
            raise _sec("qualified_table_not_allowed", "Schema-qualified tables are not allowed.")
        if not isinstance(t.this, exp.Identifier):
            raise _sec("table_function_not_allowed", "Table functions are not allowed.")
        name = t.name.lower()
        if "sqlite_" in name or name == "_build_info":
            raise _sec("system_table_not_allowed", "System tables are not allowed.")
        if name in ctes:
            continue
        if name not in catalog.tables:
            close = ", ".join(sorted(catalog.tables)[:5])
            raise _sch("unknown_table", f"Unknown table '{t.name}'. Known tables include: {close}.")
        physical.add(name)
    for n in ctes:
        if n in catalog.tables:
            raise _sec("cte_shadows_table", "A CTE may not reuse a table name.")
    if len(physical) > MAX_PHYSICAL_TABLES:
        raise _sec("too_many_tables", f"At most {MAX_PHYSICAL_TABLES} distinct tables are allowed.")
    return physical


def check_functions(tree: exp.Expression) -> None:
    for f in tree.find_all(exp.Func):
        name = function_name(f)
        if name is None or name in ALLOWED_FUNCTIONS:
            continue
        if name in DANGEROUS or name.startswith("pragma_") or name.startswith("sqlite_"):
            raise _sec("function_not_allowed", f"Function '{name}' is not allowed.")
        raise _sch("function_not_allowed", f"Function '{name}' is not allowed; use only standard SQLite functions "
                   "such as strftime, date, round, coalesce, substr, sum, avg, count.")


def _qualified_copy(tree, catalog):
    try:
        return qualify(tree.copy(), schema=catalog.mapping_schema, dialect="sqlite",
                       validate_qualify_columns=True, expand_stars=True, identify=False)
    except OptimizeError as e:
        msg = str(e)
        m = re.search(r"column[: ]+'?\"?([\w.]+)", msg, flags=re.I)
        col = m.group(1) if m else "?"
        owners = [t.name.lower() for t in tree.find_all(exp.Table)
                  if t.name.lower() in catalog.tables and col.lower() in catalog.tables[t.name.lower()].columns]
        if "mbiguous" in msg or len(set(owners)) > 1:
            raise _sch("ambiguous_column", f"Ambiguous column {col}; qualify it with a table alias.") from None
        raise _sch("unknown_column", f"Unknown column {col}. Check the table columns given in the schema.") from None
    except SqlglotError as e:
        raise _sch("unknown_column", f"Could not resolve columns: {str(e)[:150]}") from None


def provenance(qtree: exp.Expression) -> tuple[dict[str, list[str]], list[str]]:
    alias_map: dict[str, str] = {}
    ctes = {c.alias.lower() for c in qtree.find_all(exp.CTE) if c.alias}
    for t in qtree.find_all(exp.Table):
        if t.name.lower() in ctes:
            continue
        alias_map[(t.alias or t.name).lower()] = t.name.lower()
    cols: dict[str, list[str]] = {}
    for c in qtree.find_all(exp.Column):
        base = alias_map.get((c.table or "").lower())
        if base and c.name and c.name != "*" and c.name.lower() not in cols.setdefault(base, []):
            cols[base].append(c.name.lower())
    pairs: list[str] = []
    for j in qtree.find_all(exp.Join):
        on = j.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            a, b = eq.left, eq.right
            if isinstance(a, exp.Column) and isinstance(b, exp.Column):
                ta, tb = alias_map.get((a.table or "").lower()), alias_map.get((b.table or "").lower())
                if ta and tb and ta != tb:
                    p = f"{ta}.{a.name.lower()} = {tb}.{b.name.lower()}"
                    if p not in pairs:
                        pairs.append(p)
    return cols, pairs


def enforce_limit(tree: exp.Expression, row_cap: int) -> exp.Expression:
    lim = tree.args.get("limit")
    val = None
    if lim is not None and isinstance(lim.expression, exp.Literal) and not lim.expression.is_string:
        try:
            val = int(lim.expression.name)
        except ValueError:
            val = None
    if val is None or val > row_cap:
        tree = tree.limit(row_cap + 1, copy=False)
    return tree


def validate(sql: str, catalog, user: User, row_cap: int = 500) -> ValidatedQuery:
    tree = parse_single(sql)
    physical = check_structure(tree, catalog)
    check_functions(tree)
    qtree = _qualified_copy(tree, catalog)
    cols, pairs = provenance(qtree)
    unscoped = tree.copy()
    tree = scope_to_funds(tree, catalog.fund_scoped, user.fund_ids)
    tree = enforce_limit(tree, row_cap)
    out = tree.sql(dialect="sqlite", comments=False)
    # invariant: emitted SQL is itself a single, allowed statement
    try:
        again = parse_single(out)
        check_structure(again, catalog)
    except ValidationError as e:
        raise ValidationError("validated_sql_not_idempotent", "internal", e.message) from None
    ordered = [t for t in dict.fromkeys(
        t.name.lower() for t in unscoped.find_all(exp.Table)) if t in physical]
    return ValidatedQuery(out, ordered, cols, pairs, unscoped_tree=unscoped)
