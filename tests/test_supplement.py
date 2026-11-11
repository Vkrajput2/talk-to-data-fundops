"""The synthetic supplement is deterministic, flagged, reconciles, and never changes golden results."""
import csv
import hashlib
import json
import sqlite3
import sys

import pytest

from app.db import run_query
from tests.conftest import DATA, GOLDEN_IDS, golden_expected, golden_sql, rows_equal

sys.path.insert(0, str(DATA.parent / "scripts"))
import generate_supplementary_data as gen  # noqa: E402

BASE_TABLES = ["dim_date", "fact_nav", "fact_cash_balance", "fact_flows", "fact_revenue", "fact_recon_break"]


def test_generator_is_byte_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(a)
    gen.generate(b)
    names = sorted(p.name for p in a.iterdir())
    assert names == sorted(p.name for p in b.iterdir()) and len(names) >= 11
    for n in names:
        assert (a / n).read_bytes() == (b / n).read_bytes(), n
    # and equal to the committed files
    for n in names:
        assert (a / n).read_bytes() == (DATA / "supplementary" / n).read_bytes(), n


def test_generator_leaves_original_csvs_untouched(tmp_path):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DATA.glob("*.csv")}
    gen.generate(tmp_path / "o")
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DATA.glob("*.csv")}
    assert before == after and len(before) == 14


@pytest.mark.parametrize("qid", GOLDEN_IDS)
def test_golden_results_identical_with_and_without_supplement(qid, db_base, db_overlay):
    a = run_query(db_base, golden_sql(qid))
    b = run_query(db_overlay, golden_sql(qid))
    assert a.columns == b.columns and a.rows == b.rows
    assert rows_equal(b.rows, golden_expected()[qid]["rows"], ordered=golden_expected()[qid]["ordered"])


def test_original_rows_identical_and_flagged_zero(db_base, db_overlay):
    cb, co = sqlite3.connect(db_base), sqlite3.connect(db_overlay)
    for t in BASE_TABLES:
        cols = [r[1] for r in cb.execute(f'PRAGMA table_info("{t}")')]
        sel = ",".join(f'"{c}"' for c in cols)
        base_rows = cb.execute(f"SELECT {sel} FROM {t}").fetchall()
        got = co.execute(f"SELECT {sel} FROM {t} WHERE is_synthetic = 0").fetchall()
        assert sorted(map(repr, base_rows)) == sorted(map(repr, got)), t
        assert co.execute(f"SELECT COUNT(*) FROM {t} WHERE is_synthetic = 1").fetchone()[0] > 0


def test_anchor_day_rows_byte_identical_to_original_csv(db_overlay):
    con = sqlite3.connect(db_overlay)
    for table, datecol in (("fact_nav", "nav_date"), ("fact_cash_balance", "balance_date")):
        with (DATA / f"{table}.csv").open() as fh:
            for r in csv.DictReader(fh):
                if r[datecol] != "2026-09-04":
                    continue
                row = con.execute(f"SELECT * FROM {table} WHERE fund_id=? AND {datecol}=?", (r["fund_id"], "2026-09-04")).fetchone()
                assert row[-1] == 0
                for got, (col, want) in zip(row, r.items()):
                    try:
                        assert got == float(want), (table, col)  # numeric columns: same value as the CSV
                    except ValueError:
                        assert got == want, (table, col)  # text/date columns: identical string


def test_every_fund_has_history_for_all_business_days(db_overlay):
    con = sqlite3.connect(db_overlay)
    days = [r[0] for r in con.execute("SELECT date FROM dim_date WHERE is_business_day=1 AND date BETWEEN '2026-08-10' AND '2026-09-04'")]
    assert len(days) == 20
    for t, c in (("fact_nav", "nav_date"), ("fact_cash_balance", "balance_date")):
        rows = con.execute(f"SELECT fund_id, COUNT(DISTINCT {c}) FROM {t} WHERE {c} BETWEEN '2026-08-10' AND '2026-09-04' GROUP BY fund_id").fetchall()
        assert len(rows) == 6 and all(n == 20 for _, n in rows), (t, rows)
    assert con.execute("SELECT MAX(nav_date) FROM fact_nav").fetchone()[0] == "2026-09-04"
    assert con.execute("SELECT MAX(balance_date) FROM fact_cash_balance").fetchone()[0] == "2026-09-04"


def test_flow_detail_reconciles_exactly_to_flows(db_overlay):
    con = sqlite3.connect(db_overlay)
    flows = con.execute("SELECT fund_id, month, subscriptions, redemptions, net_flow FROM fact_flows").fetchall()
    assert len(flows) == 30
    for fid, month, subs, reds, net in flows:
        s, r = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN flow_type='Subscription' THEN amount END),0), "
            "COALESCE(SUM(CASE WHEN flow_type='Redemption' THEN amount END),0) FROM fact_flow_detail "
            "WHERE fund_id=? AND substr(trade_date,1,7)=?", (fid, month)).fetchone()
        assert (s, r, s + r) == (int(subs), int(reds), int(net)), (fid, month)


def test_dim_date_covers_every_date_used(db_overlay):
    con = sqlite3.connect(db_overlay)
    assert con.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0] == 730
    for t, c in (("fact_nav", "nav_date"), ("fact_cash_balance", "balance_date"), ("fact_settlement", "settlement_date"),
                 ("fact_transaction", "trade_date"), ("fact_recon_break", "open_date"), ("fact_flow_detail", "trade_date"),
                 ("fact_sla_history", "as_of_date")):
        missing = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} NOT IN (SELECT date FROM dim_date)").fetchone()[0]
        assert missing == 0, t
    for d in ("2025-08-15", "2026-08-31", "2026-09-07"):
        assert con.execute("SELECT COUNT(*) FROM dim_date WHERE date=?", (d,)).fetchone()[0] == 1
    assert con.execute("SELECT is_business_day FROM dim_date WHERE date='2026-09-05'").fetchone()[0] == 0


def test_revenue_flows_and_recon_additions_are_safe(db_overlay):
    con = sqlite3.connect(db_overlay)
    months = {r[0] for r in con.execute("SELECT month FROM fact_revenue WHERE is_synthetic=1")}
    assert "2026-08" not in months and "2025-08" not in months and "2026-09" in months and "2025-09" in months
    fm = {r[0] for r in con.execute("SELECT month FROM fact_flows WHERE is_synthetic=1")}
    assert fm == {"2026-05", "2026-06", "2026-07"}
    assert con.execute("SELECT COUNT(*) FROM fact_recon_break WHERE is_synthetic=1 AND status<>'Resolved'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM fact_recon_break WHERE is_synthetic=1 AND resolved_date IS NOT NULL "
                       "AND resolved_date > '2026-09-04'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM fact_sla_history WHERE as_of_date >= '2026-09-04'").fetchone()[0] == 0


def test_fx_rates_one_row_per_date_ccy(db_overlay):
    con = sqlite3.connect(db_overlay)
    assert con.execute("SELECT COUNT(*) - COUNT(DISTINCT rate_date || ccy) FROM dim_fx_rate").fetchone()[0] == 0
    assert dict(con.execute("SELECT ccy, usd_rate FROM dim_fx_rate WHERE rate_date='2026-09-04'")) == {"USD": 1.0, "EUR": 1.08, "GBP": 1.27}
    assert con.execute("SELECT COUNT(*) FROM dim_fx_rate WHERE rate_date='2025-03-01'").fetchone()[0] == 3
    # Q4F value: plain sum 11,050,000 and USD-equivalent 11,420,400 (USD 6,420,000 + EUR 4,630,000 * 1.08)
    r = run_query(db_overlay, """SELECT SUM(t.gross_amount), SUM(t.gross_amount * fx.usd_rate)
        FROM fact_settlement s JOIN fact_transaction t ON s.txn_id=t.txn_id
        JOIN dim_fx_rate fx ON fx.ccy=t.ccy AND fx.rate_date='2026-09-04'
        WHERE s.settlement_date='2026-09-04' AND s.status='Failed' AND s.broker_name='Helios Securities'""").rows[0]
    assert r[0] == 11050000 and round(r[1], 2) == 11420400.0


def test_synthetic_flag_documented_in_notes_and_manifest():
    notes = (DATA.parent / "docs" / "data_notes.md").read_text(encoding="utf-8")
    manifest = json.loads((DATA / "supplementary" / "manifest.json").read_text())
    for t in manifest["inserted"]:
        assert t in notes, t
