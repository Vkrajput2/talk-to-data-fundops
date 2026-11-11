import csv
import json
import shutil
import sqlite3

import pytest

from app.db import LoaderError, build_database, ensure_database, fingerprint, table_row_counts
from tests.conftest import DATA

BASE_COUNTS = {"dim_fund": 6, "dim_security": 12, "dim_counterparty": 8, "dim_date": 522, "fact_transaction": 632,
               "fact_settlement": 632, "fact_nav": 16, "fact_cash_balance": 16, "fact_flows": 12,
               "fact_recon_break": 10, "fact_pricing_exception": 4, "fact_revenue": 12, "fact_sla": 6}
MANIFEST = json.loads((DATA / "supplementary" / "manifest.json").read_text())


def test_base_load_has_exact_row_counts(db_base):
    assert table_row_counts(db_base) == BASE_COUNTS


def test_base_column_count_is_104(db_base):
    con = sqlite3.connect(db_base)
    total = sum(len(con.execute(f'PRAGMA table_info("{t}")').fetchall()) for t in BASE_COUNTS)
    assert total == 104


def test_overlay_counts_equal_base_plus_manifest(db_overlay):
    counts = table_row_counts(db_overlay)
    assert len(counts) == 16
    for t, base in BASE_COUNTS.items():
        assert counts[t] == base + MANIFEST["inserted"].get(t, 0), t
    for t in ("fact_flow_detail", "fact_sla_history", "dim_fx_rate"):
        assert counts[t] == MANIFEST["inserted"][t]
    assert "_build_info" not in counts


def test_types_are_loaded_from_dictionary(db_overlay):
    con = sqlite3.connect(db_overlay)
    d = con.execute("SELECT typeof(total_net_assets), typeof(nav_date), typeof(units) FROM fact_nav LIMIT 1").fetchone()
    assert d[0] in ("integer", "real") and d[1] == "text"
    assert con.execute("SELECT typeof(fail_reason_code) FROM fact_settlement WHERE status='Settled' LIMIT 1").fetchone()[0] == "null"


def test_no_rows_skipped_and_no_tmp_left(tmp_path):
    out = tmp_path / "x.db"
    build_database(DATA, out, True)
    assert all(v == 0 for v in build_database.last_skipped.values())
    assert not list(tmp_path.glob("*.tmp"))


def test_header_mismatch_is_fatal(tmp_path):
    d = tmp_path / "Data"
    shutil.copytree(DATA, d)
    p = d / "dim_fund.csv"
    p.write_text(p.read_text().replace("fund_name", "fund_title", 1))
    with pytest.raises(LoaderError, match="dim_fund"):
        build_database(d, tmp_path / "x.db", False)
    assert not (tmp_path / "x.db").exists() and not (tmp_path / "x.db.tmp").exists()


def test_supplement_duplicate_key_inside_file_is_fatal(tmp_path):
    d = tmp_path / "Data"
    shutil.copytree(DATA, d)
    p = d / "supplementary" / "dim_fx_rate.csv"
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines + [lines[1]]) + "\n")
    with pytest.raises(LoaderError, match="duplicate"):
        build_database(d, tmp_path / "x.db", True)


def test_supplement_row_with_existing_key_is_skipped_and_counted(tmp_path):
    d = tmp_path / "Data"
    shutil.copytree(DATA, d)
    orig = list(csv.reader((DATA / "fact_nav.csv").open()))
    p = d / "supplementary" / "fact_nav.csv"
    lines = p.read_text().splitlines()
    dup = ",".join(orig[1] + ["1"])  # an original (fund, date) row repeated as synthetic
    p.write_text("\n".join(lines + [dup]) + "\n")
    out = tmp_path / "x.db"
    build_database(d, out, True)
    assert build_database.last_skipped["fact_nav"] == 1
    assert table_row_counts(out)["fact_nav"] == 16 + MANIFEST["inserted"]["fact_nav"]


def test_fingerprint_changes_and_ensure_rebuilds(tmp_path):
    d = tmp_path / "Data"
    shutil.copytree(DATA, d)
    db = tmp_path / "x.db"
    assert ensure_database(d, db, True) is True
    assert ensure_database(d, db, True) is False  # unchanged -> reuse
    before = fingerprint(d, True)
    p = d / "supplementary" / "dim_fx_rate.csv"
    p.write_text(p.read_text().replace("1.08", "1.09", 1))
    assert fingerprint(d, True) != before
    assert ensure_database(d, db, True) is True


def test_pk_declarations_unchanged_by_overlay(db_base, db_overlay):
    def pks(db):
        con = sqlite3.connect(db)
        out = {}
        for t in BASE_COUNTS:
            out[t] = [(r[1], r[5]) for r in con.execute(f'PRAGMA table_info("{t}")') if r[1] != "is_synthetic"
                      and r[1] != "resolved_date"]
        return out
    assert pks(db_base) == pks(db_overlay)
