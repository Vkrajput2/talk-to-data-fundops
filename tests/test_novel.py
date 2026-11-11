"""Novel-question reference SQL is checked against a second, independent stdlib-Python computation over the CSVs."""
import csv
import json
from collections import defaultdict

import pytest

from app.agent.validate import validate
from app.db import run_query
from app.entitlements import MOCK_USERS
from tests.conftest import DATA, GOLDEN, rows_equal

NOVEL = {n["id"]: n for n in json.loads((GOLDEN / "novel_questions.json").read_text())}
FUNDS = {r["fund_id"]: r["fund_name"] for r in csv.DictReader((DATA / "dim_fund.csv").open())}


def read(name, supp=True):
    base = DATA / f"{name}.csv"
    rows = list(csv.DictReader(base.open(encoding="utf-8-sig"))) if base.exists() else []
    p = DATA / "supplementary" / f"{name}.csv"
    if supp and p.exists():
        rows += list(csv.DictReader(p.open(encoding="utf-8")))
    return rows


def f(x):
    return float(x)


def exp_N1():
    r = [x for x in read("fact_cash_balance") if x["fund_id"] == "F005" and "2026-08-24" <= x["balance_date"] <= "2026-09-04"]
    return [[x["balance_date"], f(x["cash_pct_aum"])] for x in sorted(r, key=lambda x: x["balance_date"])]


def exp_N2():
    nav = {(x["fund_id"], x["nav_date"]): f(x["total_net_assets"]) for x in read("fact_nav")}
    ch = sorted((round(100 * (nav[(k, "2026-09-04")] - nav[(k, "2026-08-10")]) / nav[(k, "2026-08-10")], 2), FUNDS[k]) for k in FUNDS)
    return [[ch[0][1], ch[0][0]]]


def exp_N3():
    return [[x["month"], f(x["net_flow"]), x["ccy"]] for x in sorted(read("fact_flows"), key=lambda x: x["month"])
            if x["fund_id"] == "F001" and "2026-05" <= x["month"] <= "2026-09"]


def exp_N4():
    cnt = defaultdict(int)
    for x in read("fact_flows"):
        if x["month"] >= "2026-05" and f(x["net_flow"]) < 0:
            cnt[FUNDS[x["fund_id"]]] += 1
    best = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return [[best[0], best[1]]]


def exp_N5():
    r = [x for x in read("fact_flow_detail") if x["flow_type"] == "Redemption" and x["trade_date"][:7] == "2026-08"]
    m = min(r, key=lambda x: (f(x["amount"]), x["flow_id"]))
    return [[FUNDS[m["fund_id"]], m["investor_type"], f(m["amount"]), m["ccy"]]]


def exp_N6():
    t = defaultdict(float)
    for x in read("fact_flow_detail"):
        if x["fund_id"] == "F005" and x["trade_date"][:7] == "2026-09":
            t[x["investor_type"]] += f(x["amount"])
    return [[k, t[k]] for k in sorted(t)]


def exp_N7():
    t = defaultdict(list)
    for x in read("fact_sla_history"):
        if x["as_of_date"][:7] == "2026-08":
            t[x["process"]].append(int(x["breached"]))
    return [[p, round(100.0 * (1 - sum(v) / len(v)), 1)] for p, v in sorted(t.items())]


def exp_N8():
    t = defaultdict(int)
    for x in read("fact_sla_history"):
        if x["as_of_date"][:7] == "2026-08":
            t[FUNDS[x["fund_id"]]] += int(x["breached"])
    best = sorted(t.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return [[best[0], best[1]]]


def open_on(d):
    n = 0
    for x in read("fact_recon_break"):
        res = x.get("resolved_date") or None
        if x["open_date"] <= d and ((x["status"] == "Open" and res is None) or (res is not None and res > d)):
            n += 1
    return n


def exp_N9():
    return [["2026-08-28", open_on("2026-08-28")], ["2026-09-04", open_on("2026-09-04")]]


def exp_N10():
    return [[x["month"], f(x["mgmt_fee_revenue"]), x["ccy"]] for x in sorted(read("fact_revenue"), key=lambda x: x["month"])
            if x["fund_id"] == "F004" and "2025-09" <= x["month"] <= "2026-08"]


def exp_N11():
    fx = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27}
    return [[round(sum(f(x["net_flow"]) * fx[x["ccy"]] for x in read("fact_flows") if x["month"] == "2026-09"))]]


def exp_N12():
    t = defaultdict(int)
    for x in read("fact_sla"):
        if x["status"] == "Pending":
            t[x["process"]] += 1
    return [[p, n] for p, n in sorted(t.items())]


@pytest.mark.parametrize("nid", sorted(NOVEL, key=lambda s: int(s[1:])))
def test_reference_sql_matches_independent_computation(nid, db_overlay, cat_overlay):
    n = NOVEL[nid]
    v = validate(n["sql"], cat_overlay, MOCK_USERS["priya"])
    got = run_query(db_overlay, v.sql).rows
    want = globals()[f"exp_{nid}"]()
    assert want, nid
    assert rows_equal(got, want, ordered=n["ordered"], tol=0.01), (nid, got[:3], want[:3])


def test_pinned_hand_computed_values(db_overlay):
    assert run_query(db_overlay, NOVEL["N11"]["sql"]).rows == [[-18550000]]
    n9 = run_query(db_overlay, NOVEL["N9"]["sql"]).rows
    assert n9[1] == ["2026-09-04", 6]  # BRK001..BRK006; BRK007/008 open after the date; synthetic ones resolved
    assert n9[0][1] >= 1
    n6 = run_query(db_overlay, NOVEL["N6"]["sql"]).rows
    assert sum(r[1] for r in n6) == -135000000  # reconciles to fact_flows
    assert run_query(db_overlay, NOVEL["N3"]["sql"]).rows[-1] == ["2026-09", 110000000, "USD"]
    assert len(run_query(db_overlay, NOVEL["N1"]["sql"]).rows) == 10 and len(run_query(db_overlay, NOVEL["N10"]["sql"]).rows) == 12


def test_there_are_at_least_ten_novel_questions_and_paraphrases_for_every_golden():
    assert len(NOVEL) >= 10
    para = json.loads((GOLDEN / "paraphrases.json").read_text())
    assert sorted(para) == sorted(["Q1", "Q2", "Q3", "Q4", "Q4F", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10"])
    assert all(len(v) >= 3 for v in para.values())
