"""Deterministic synthetic supplement for the Talk to Data POC.

Writes Data/supplementary/*.csv, data_dictionary_supplementary.csv and manifest.json.
The original CSVs in Data/ are never modified (SHA-256 verified before/after).
Stdlib only. Run:  py scripts/generate_supplementary_data.py
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import random
import sys
from pathlib import Path

SEED = 20260904
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "Data"
OUT = DATA / "supplementary"
ANCHOR = dt.date(2026, 9, 4)
WINDOW_START = dt.date(2026, 8, 10)

FX = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(name: str) -> list[dict]:
    with (DATA / f"{name}.csv").open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def fmt(v) -> str:
    if isinstance(v, float):
        return repr(v)
    return str(v)


def write_csv(name: str, header: list[str], rows: list[list], out_dir: Path) -> int:
    with (out_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([fmt(v) for v in r])
    return len(rows)


def iso(d: dt.date) -> str:
    return d.isoformat()


def uniform(rng, a, b):
    return a + (b - a) * rng.random()


def generate(out_dir: Path = OUT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    originals = sorted(DATA.glob("*.csv"))
    before = {p.name: sha256(p) for p in originals}

    rng = random.Random(SEED)
    funds = sorted(read_csv("dim_fund"), key=lambda r: r["fund_id"])
    fund_ccy = {f["fund_id"]: f["base_ccy"] for f in funds}
    dim_date = read_csv("dim_date")
    existing_dates = {r["date"] for r in dim_date}
    biz = sorted(dt.date.fromisoformat(r["date"]) for r in dim_date if r["is_business_day"] == "1")
    window = [d for d in biz if WINDOW_START <= d <= ANCHOR]

    manifest = {"seed": SEED, "inserted": {}, "files": {}}
    header_of = {}

    def emit(name, header, rows):
        n = write_csv(name, header, rows, out_dir)
        manifest["inserted"][name] = n
        header_of[name] = header

    # ---- dim_date: missing calendar dates (weekends) -------------------------------------
    rows = []
    d = dt.date(2025, 1, 1)
    while d <= dt.date(2026, 12, 31):
        if iso(d) not in existing_dates:
            rows.append([iso(d), d.year, d.month, d.day, d.strftime("%A"), 0,
                         f"{d.year}-Q{(d.month - 1) // 3 + 1}", 1])
        d += dt.timedelta(days=1)
    emit("dim_date", ["date", "year", "month", "day", "weekday", "is_business_day", "quarter", "is_synthetic"], rows)

    # ---- fact_nav / fact_cash_balance ------------------------------------------------------
    nav_orig = read_csv("fact_nav")
    cash_orig = read_csv("fact_cash_balance")
    nav_rows, cash_rows = [], []
    for f in funds:
        fid = f["fund_id"]
        nav_have = {r["nav_date"]: r for r in nav_orig if r["fund_id"] == fid}
        cash_have = {r["balance_date"]: r for r in cash_orig if r["fund_id"] == fid}
        # Backward walk from the anchor
        cur = {"tna": float(nav_have[iso(ANCHOR)]["total_net_assets"]),
               "units": float(nav_have[iso(ANCHOR)]["units"])}
        p0 = float(cash_have[iso(ANCHOR)]["cash_pct_aum"])
        cur_pct = p0
        series = {iso(ANCHOR): dict(cur)}
        pct_series = {iso(ANCHOR): cur_pct}
        for d in reversed(window[:-1]):
            k = iso(d)
            if k in nav_have:
                cur = {"tna": float(nav_have[k]["total_net_assets"]), "units": float(nav_have[k]["units"])}
            else:
                r = -0.004 + 0.009 * rng.random()
                u = -0.002 + 0.004 * rng.random()
                cur = {"tna": float(round(cur["tna"] / (1 + r))), "units": float(round(cur["units"] * (1 + u)))}
            series[k] = dict(cur)
            if k in cash_have:
                cur_pct = float(cash_have[k]["cash_pct_aum"])
            else:
                cur_pct = min(9.0, max(0.5, p0 + 0.6 * (cur_pct - p0) + (-0.12 + 0.24 * rng.random())))
            pct_series[k] = cur_pct
        ordered = [iso(d) for d in window]
        for i, k in enumerate(ordered):
            if k == iso(ANCHOR):
                continue
            s = series[k]
            if k not in nav_have:
                npu = round(s["tna"] / s["units"], 4)
                if i == 0:
                    dod = round(-0.3 + 0.6 * rng.random(), 2)
                else:
                    prev = series[ordered[i - 1]]
                    dod = round(100 * (npu / round(prev["tna"] / prev["units"], 4) - 1), 2)
                nav_rows.append([fid, k, s["tna"], s["units"], npu, dod, "Final", fund_ccy[fid], 1])
            if k not in cash_have:
                bal = float(round(s["tna"] * pct_series[k] / 100))
                cash_rows.append([fid, k, bal, s["tna"], round(100 * bal / s["tna"], 2), fund_ccy[fid], 1])
    emit("fact_nav", ["fund_id", "nav_date", "total_net_assets", "units", "nav_per_unit",
                      "dod_variance_pct", "nav_status", "ccy", "is_synthetic"], nav_rows)
    emit("fact_cash_balance", ["fund_id", "balance_date", "available_balance", "total_net_assets",
                               "cash_pct_aum", "ccy", "is_synthetic"], cash_rows)

    # ---- fact_revenue ---------------------------------------------------------------------
    rev = read_csv("fact_revenue")
    rev_rows = []
    for f in funds:
        fid = f["fund_id"]
        r0 = next(r for r in rev if r["fund_id"] == fid and r["month"] == "2025-08")
        r1 = next(r for r in rev if r["fund_id"] == fid and r["month"] == "2026-08")
        a0, a1 = float(r0["avg_aum"]), float(r1["avg_aum"])
        fee0, fee1 = float(r0["fee_rate_pct"]), float(r1["fee_rate_pct"])
        ex0, ex1 = float(r0["expense_ratio"]), float(r1["expense_ratio"])
        months = ["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
                  "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
        for k, m in enumerate(months, start=1):
            avg = float(round(a0 * (a1 / a0) ** (k / 12) * (0.99 + 0.02 * rng.random()), -4))
            fee = fee1 if abs(fee0 - fee1) < 1e-9 else round(fee0 + (fee1 - fee0) * k / 12, 4)
            ex = round(ex0 + (ex1 - ex0) * k / 12, 2)
            rev_rows.append([f"RV{fid}{m.replace('-', '')}", fid, m, float(round(avg * fee / 1200)), avg, fee, ex,
                             fund_ccy[fid], 1])
        avg = float(round(a1 * (0.99 + 0.025 * rng.random()), -4))
        rev_rows.append([f"RV{fid}202609", fid, "2026-09", float(round(avg * fee1 / 1200 * 4 / 30)), avg,
                         fee1, ex1, fund_ccy[fid], 1])
    emit("fact_revenue", ["rev_id", "fund_id", "month", "mgmt_fee_revenue", "avg_aum", "fee_rate_pct",
                          "expense_ratio", "ccy", "is_synthetic"], rev_rows)

    # ---- fact_flows -----------------------------------------------------------------------
    flows = read_csv("fact_flows")
    flow_rows = []
    for f in funds:
        fid = f["fund_id"]
        r1 = next(r for r in flows if r["fund_id"] == fid and r["month"] == "2026-08")
        S, R = float(r1["subscriptions"]), float(r1["redemptions"])
        for m in ("2026-05", "2026-06", "2026-07"):
            subs = float(round(S / 1e6 * (0.5 + rng.random())) * 1e6)
            reds = -float(round(abs(R) / 1e6 * (0.5 + rng.random())) * 1e6)
            flow_rows.append([f"FL{fid}{m.replace('-', '')}", fid, m, subs, reds, subs + reds, fund_ccy[fid], 1])
    emit("fact_flows", ["flow_id", "fund_id", "month", "subscriptions", "redemptions", "net_flow", "ccy",
                        "is_synthetic"], flow_rows)

    # ---- fact_flow_detail (reconciles exactly to fact_flows) ----------------------------------
    all_flows = [(r["fund_id"], r["month"], float(r["subscriptions"]), float(r["redemptions"]), r["ccy"])
                 for r in flows] + [(r[1], r[2], r[3], r[4], r[6]) for r in flow_rows]
    all_flows.sort(key=lambda x: (x[0], x[1]))
    det_rows = []
    for fid, month, subs, reds, ccy in all_flows:
        days = [d for d in biz if iso(d).startswith(month) and d <= ANCHOR]
        for kind, total, tag in (("Subscription", int(subs), "S"), ("Redemption", int(reds), "R")):
            n = rng.randint(3, 6)
            weights = [rng.randint(1, 100) for _ in range(n)]
            ws = sum(weights)
            parts = [abs(total) * w // ws for w in weights]
            parts[-1] += abs(total) - sum(parts)
            sign = 1 if total >= 0 else -1
            for i, amt in enumerate(parts, start=1):
                inv = "Institutional" if rng.random() < 0.7 else "Retail"
                day = days[rng.randint(0, len(days) - 1)]
                det_rows.append([f"FD{fid}{month.replace('-', '')}{tag}{i:02d}", fid, iso(day), inv, kind,
                                 sign * amt, ccy, 1])
    emit("fact_flow_detail", ["flow_id", "fund_id", "trade_date", "investor_type", "flow_type", "amount",
                              "ccy", "is_synthetic"], det_rows)

    # ---- fact_sla_history -----------------------------------------------------------------
    sla = read_csv("fact_sla")
    targets = {}
    for r in sla:
        targets.setdefault(r["process"], int(float(r["target_minutes"])))
    sla_rows, seq = [], 0
    for d in window[:-1]:
        for f in funds:
            for proc in sorted(targets):
                seq += 1
                tgt = targets[proc]
                el = round(tgt * (0.5 + 0.58 * rng.random()))
                sla_rows.append([f"SLH{seq:05d}", f["fund_id"], proc, tgt, el, round(100 * el / tgt, 1),
                                 "Completed", iso(d), 1 if el > tgt else 0, 1])
    emit("fact_sla_history", ["hist_id", "fund_id", "process", "target_minutes", "elapsed_minutes",
                              "pct_of_sla", "status", "as_of_date", "breached", "is_synthetic"], sla_rows)

    # ---- fact_recon_break: resolved history --------------------------------------------------
    brk = read_csv("fact_recon_break")
    types = sorted({r["break_type"] for r in brk})
    owners = sorted({r["owner"] for r in brk})
    custs = sorted({r["custodian"] for r in brk})
    secs = sorted({r["security_id"] for r in brk})
    brk_rows = []
    for i in range(1, 25):
        fid = funds[rng.randint(0, len(funds) - 1)]["fund_id"]
        op = dt.date(2026, 7, 20) + dt.timedelta(days=rng.randint(0, 38))
        dur = rng.randint(1, 12)
        res = min(op + dt.timedelta(days=dur), ANCHOR)
        amt = float(round(150000 + rng.randint(0, 1050000), -4))
        brk_rows.append([f"BRK{100 + i}", fid, types[rng.randint(0, len(types) - 1)], amt, fund_ccy[fid],
                         (res - op).days, "Resolved", owners[rng.randint(0, len(owners) - 1)],
                         custs[rng.randint(0, len(custs) - 1)], secs[rng.randint(0, len(secs) - 1)],
                         iso(op), iso(res), 1])
    emit("fact_recon_break", ["break_id", "fund_id", "break_type", "break_amount", "ccy", "age_days", "status",
                              "owner", "custodian", "security_id", "open_date", "resolved_date",
                              "is_synthetic"], brk_rows)

    # ---- dim_fx_rate --------------------------------------------------------------------------
    fx_dates = set()
    d = dt.date(2026, 8, 1)
    while d <= dt.date(2026, 9, 30):
        fx_dates.add(d)
        d += dt.timedelta(days=1)
    for y in (2025, 2026):
        for m in range(1, 13):
            fx_dates.add(dt.date(y, m, 1))
    fx_rows = [[iso(d), c, FX[c], 1] for d in sorted(fx_dates) for c in sorted(FX)]
    emit("dim_fx_rate", ["rate_date", "ccy", "usd_rate", "is_synthetic"], fx_rows)

    # ---- supplementary dictionary -------------------------------------------------------------
    syn = "1 = synthetic demo-supplement row, 0 = original"
    dd = []
    for t in ("dim_date", "fact_nav", "fact_cash_balance", "fact_flows", "fact_revenue", "fact_recon_break"):
        dd.append([t, "is_synthetic", "NUMBER", "", syn])
    dd.append(["fact_recon_break", "resolved_date", "DATE", "", "Date the break was resolved (synthetic history only)"])
    new_tables = {
        "fact_flow_detail": [
            ("flow_id", "TEXT", "PK", "Flow detail record"), ("fund_id", "TEXT", "FK dim_fund", "Fund"),
            ("trade_date", "DATE", "", "Trade date"), ("investor_type", "TEXT", "", "Institutional / Retail"),
            ("flow_type", "TEXT", "", "Subscription / Redemption"), ("amount", "NUMBER", "", "Flow amount (+ sub, - red)"),
            ("ccy", "TEXT", "", "Fund currency")],
        "fact_sla_history": [
            ("hist_id", "TEXT", "PK", "SLA history record"), ("fund_id", "TEXT", "FK dim_fund", "Fund"),
            ("process", "TEXT", "", "Operational process"), ("target_minutes", "NUMBER", "", "SLA target (min)"),
            ("elapsed_minutes", "NUMBER", "", "Elapsed (min)"), ("pct_of_sla", "NUMBER", "", "Elapsed as % of target"),
            ("status", "TEXT", "", "Completed"), ("as_of_date", "DATE", "", "Date of the SLA record"),
            ("breached", "NUMBER", "", "1 if elapsed > target")],
        "dim_fx_rate": [
            ("rate_date", "DATE", "", "Rate date"), ("ccy", "TEXT", "", "Currency"),
            ("usd_rate", "NUMBER", "", "USD per 1 unit of ccy (fixed synthetic rate)")],
    }
    for t, cols in new_tables.items():
        for c, ty, key, desc in cols:
            dd.append([t, c, ty, key, desc])
        dd.append([t, "is_synthetic", "NUMBER", "", syn])
    with (out_dir / "data_dictionary_supplementary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["table", "column", "type", "key", "description"])
        w.writerows(dd)

    after = {p.name: sha256(p) for p in originals}
    if before != after:
        raise RuntimeError("original CSVs were modified")
    manifest["files"] = {p.name: {"sha256": sha256(p)} for p in sorted(out_dir.glob("*.csv"))}
    for name, n in manifest["inserted"].items():
        manifest["files"][f"{name}.csv"]["rows"] = n
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    m = generate(out)
    print("inserted:", json.dumps(m["inserted"], sort_keys=True))
