"""Mock users and server-side fund entitlements (AST rewrite)."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

PLACEHOLDER = "[a fund you cannot access]"


class UnknownUser(Exception):
    pass


class EntitlementEnforcementError(Exception):
    pass


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: str
    fund_ids: frozenset[str] | None  # None = all funds

    def scope_label(self):
        return "all" if self.fund_ids is None else sorted(self.fund_ids)


MOCK_USERS = {
    "priya": User("priya", "Priya Nair", "Operations Analyst", None),
    "david": User("david", "David Osei", "Operations Manager", None),
    "analyst_restricted": User("analyst_restricted", "Restricted Analyst", "Analyst (restricted)",
                               frozenset({"F002", "F005", "F006"})),
}


def get_user(user_id: str) -> User:
    try:
        return MOCK_USERS[user_id]
    except KeyError:
        raise UnknownUser(user_id[:32]) from None


def check_users_against_funds(fund_ids: set[str]) -> None:
    for u in MOCK_USERS.values():
        if u.fund_ids is not None and not u.fund_ids <= fund_ids:
            raise ValueError(f"user {u.user_id} is entitled to funds missing from dim_fund")


@dataclass
class Sanitized:
    text: str
    forbidden: list[tuple[str, str, str]] = field(default_factory=list)  # (typed text, fund id, name)
    permitted: list = field(default_factory=list)  # Entity list (permitted funds)

    @property
    def denial_text(self) -> str:
        typed = ", ".join(dict.fromkeys(t for t, _, _ in self.forbidden))
        return f"You do not have access to fund {typed}."


def sanitize_question(question: str, user: User, aliases) -> Sanitized:
    """Replace any forbidden fund mention by a placeholder BEFORE any LLM stage sees the text."""
    hits = aliases.find(question)
    out, last = [], 0
    forbidden, permitted = [], []
    for ent, s, e in hits:
        if ent.type != "fund":
            continue
        if user.fund_ids is not None and ent.id not in user.fund_ids:
            out.append(question[last:s])
            out.append(PLACEHOLDER)
            last = e
            forbidden.append((question[s:e], ent.id, ent.name))
        else:
            permitted.append(ent)
    out.append(question[last:])
    return Sanitized("".join(out), forbidden, permitted)


def _tname(node: exp.Table) -> str:
    return (node.name or "").lower()


def _cte_names(tree: exp.Expression) -> set[str]:
    return {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}


def scope_to_funds(tree: exp.Expression, fund_scoped: set[str], fund_ids) -> exp.Expression:
    """Replace every fund-scoped table reference with a filtered subquery (fail closed)."""
    if fund_ids is None:
        return tree
    ctes = _cte_names(tree)
    targets = [t for t in tree.find_all(exp.Table) if _tname(t) in fund_scoped and _tname(t) not in ctes]
    for t in targets:
        alias = t.alias or t.name
        inner = exp.select("*").from_(exp.Table(this=exp.to_identifier(_tname(t)))).where(
            exp.In(this=exp.column("fund_id"),
                   expressions=[exp.Literal.string(f) for f in sorted(fund_ids)]))
        inner.meta["scoped"] = True
        sub = exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias)))
        if t is tree:
            tree = sub
        else:
            t.replace(sub)
    verify_scoped(tree, fund_scoped)
    return tree


def verify_scoped(tree: exp.Expression, fund_scoped: set[str]) -> None:
    ctes = _cte_names(tree)
    for t in tree.find_all(exp.Table):
        if _tname(t) not in fund_scoped or _tname(t) in ctes:
            continue
        frm = t.parent
        sel = frm.parent if isinstance(frm, exp.From) else None
        ok = (isinstance(sel, exp.Select) and bool(sel.meta.get("scoped"))
              and isinstance(sel.parent, exp.Subquery) and sel.parent.this is sel
              and not sel.args.get("joins"))
        if not ok:
            raise EntitlementEnforcementError("fund-scoped table reference is not wrapped")
