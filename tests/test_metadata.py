import pytest

from app.config import ConfigError, Settings, load_settings
from app.metadata import AliasIndex, compute_date_context
from tests.conftest import DATA


def test_join_graph_has_fk_inferred_and_no_forbidden_edges(cat_overlay):
    pairs = {(e.left_table, e.left_col, e.right_table, e.right_col) for e in cat_overlay.edges}
    assert ("fact_settlement", "txn_id", "fact_transaction", "txn_id") in pairs
    assert ("fact_settlement", "fund_id", "dim_fund", "fund_id") in pairs
    assert ("fact_settlement", "broker_id", "dim_counterparty", "cp_id") in pairs
    assert ("fact_flow_detail", "fund_id", "dim_fund", "fund_id") in pairs
    assert ("fact_sla_history", "fund_id", "dim_fund", "fund_id") in pairs
    assert any(e.kind == "fx" for e in cat_overlay.edges)
    flat = " ".join(f"{a}.{b} {c}.{d}" for a, b, c, d in pairs)
    assert "custodian" not in flat and "dim_date.month" not in flat
    assert {e.kind for e in cat_overlay.edges if e.left_col == "broker_id" and e.left_table == "fact_settlement"} == {"inferred"}
    assert "fact_settlement JOIN fact_transaction ON fact_settlement.txn_id = fact_transaction.txn_id" in cat_overlay.join_text()


def test_fund_scoped_sets(cat_base, cat_overlay):
    facts = {"fact_transaction", "fact_settlement", "fact_nav", "fact_cash_balance", "fact_flows", "fact_recon_break",
             "fact_pricing_exception", "fact_revenue", "fact_sla"}
    assert cat_base.fund_scoped == facts | {"dim_fund"}
    assert cat_overlay.fund_scoped == facts | {"dim_fund", "fact_flow_detail", "fact_sla_history"}
    assert "dim_fx_rate" not in cat_overlay.fund_scoped


def test_synthetic_tables(cat_base, cat_overlay):
    assert cat_base.synthetic_tables == {}
    full = {t for t, v in cat_overlay.synthetic_tables.items() if v == "full"}
    partial = {t for t, v in cat_overlay.synthetic_tables.items() if v == "partial"}
    assert full == {"fact_flow_detail", "fact_sla_history", "dim_fx_rate"}
    assert partial == {"dim_date", "fact_nav", "fact_cash_balance", "fact_flows", "fact_revenue", "fact_recon_break"}


def test_date_context_same_with_and_without_supplement(db_base, db_overlay):
    for db in (db_base, db_overlay):
        d = compute_date_context(db)
        assert (d.as_of, d.yesterday, d.current_month, d.previous_month) == ("2026-09-04", "2026-09-04", "2026-09", "2026-08")
        assert (d.revenue_month, d.revenue_prior_year_month) == ("2026-08", "2025-08")
        assert d.current_month_partial is True and d.four_week_start == "2026-08-08"


def test_date_context_overrides(db_overlay):
    d = compute_date_context(db_overlay, "2026-09-03")
    assert d.as_of == "2026-09-03" and d.yesterday == "2026-09-03"
    assert d.snapshot_dates["fact_nav"] == "2026-09-03"
    d2 = compute_date_context(db_overlay, None, "2026-09-02")
    assert d2.yesterday == "2026-09-02" and d2.as_of == "2026-09-04"


def test_config_anchor_alias_and_validation(monkeypatch, tmp_path):
    for k in ("AS_OF_DATE", "ANCHOR_DATE", "YESTERDAY_DATE", "LLM_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANCHOR_DATE", "2026-09-02")
    assert load_settings(tmp_path / "none.env").as_of_date == "2026-09-02"
    monkeypatch.setenv("AS_OF_DATE", "2026-09-03")
    assert load_settings(tmp_path / "none.env").as_of_date == "2026-09-03"
    monkeypatch.setenv("YESTERDAY_DATE", "2026-09-04")
    with pytest.raises(ConfigError, match="YESTERDAY_DATE"):
        load_settings(tmp_path / "none.env")
    monkeypatch.setenv("YESTERDAY_DATE", "2026-09-01")
    monkeypatch.setenv("AS_OF_DATE", "not-a-date")
    with pytest.raises(ConfigError, match="AS_OF_DATE"):
        load_settings(tmp_path / "none.env")


def test_alias_index_resolves_playbook_names(aliases):
    def one(text):
        return {(e.type, e.id) for e, _, _ in aliases.find(text)}
    assert one("fails with Helios") == {("broker", "B01")}
    assert one("anything from Aurora?") == {("broker", "B04")}
    assert ("fail_code", "LACK") in one("short fails")
    assert one("EM Equity cash") == {("fund", "F005")}
    assert one("US Core Bond and EMEA Fixed Income") == {("fund", "F004"), ("fund", "F002")}
    assert one("what about F001") == {("fund", "F001")}
    assert ("process", "NAV Publication") in one("NAV Publication sla")
