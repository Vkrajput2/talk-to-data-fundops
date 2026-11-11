"""Scripted LLM for the container smoke test ONLY (LLM_BACKEND=smoke; refused when a key is configured)."""
from __future__ import annotations

from .models import GenerateOut, NarrateOut, ResolveOut, UnderstandOut

Q1_SQL = ("SELECT f.fund_id, f.fund_name, f.region, f.strategy, ROUND(n.total_net_assets / 1e9, 2) AS aum_bn, "
          "f.base_ccy FROM dim_fund f JOIN fact_nav n ON f.fund_id = n.fund_id "
          "WHERE n.nav_date = (SELECT MAX(nav_date) FROM fact_nav) ORDER BY aum_bn DESC")


class SmokeLLM:
    provider = "smoke"
    model = "smoke"

    def complete(self, *, stage, system, user, model_cls):
        if stage == "understand":
            q = user.split("<question>")[-1].split("</question>")[0]
            return UnderstandOut(standalone_question=q.strip() or "List all active funds", intent="lookup",
                                 interpreted_question="List all active funds with region, strategy and latest AUM")
        if stage == "resolve":
            return ResolveOut.model_validate({"tables": [
                {"table": "dim_fund", "why": "Fund names, region and strategy", "columns": ["fund_id"], "join_keys": []},
                {"table": "fact_nav", "why": "Latest net assets (AUM) per fund", "columns": ["fund_id", "nav_date"],
                 "join_keys": ["dim_fund.fund_id = fact_nav.fund_id"]}]})
        if stage in ("generate", "repair"):
            return GenerateOut(sql=Q1_SQL)
        return NarrateOut(narrative="Smoke backend: results come straight from the database.", kpis=[])
