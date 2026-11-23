#!/usr/bin/env python3
"""Consolidate Delta-Fibonacci validation results into dfib_summary.json + xlsx."""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
sys.path.insert(0, "/home/z/my-project/skills/xlsx")
sys.path.insert(0, "/home/z/my-project/skills/xlsx/templates")
from dfib_engine import metrics, split_is_oos
from dfib_robustness import mc_iid, block_bootstrap, dsr, rolling_thirds
from base import (font_title, font_header, font_body, font_caption, fill_header,
                  fill_data_row, border_header, align_header, align_number, align_text)
from openpyxl import Workbook

OUT = "/home/z/my-project/dfib_results"
DL = "/home/z/my-project/download/dfib"

ver = pd.read_csv(os.path.join(OUT, "champions_verification.csv"))
picks = pd.read_csv(os.path.join(OUT, "portfolio_picks.csv"))
led = pd.read_parquet(os.path.join(OUT, "portfolio_ledgers.parquet"))
led1 = led[led["cost_mult"] == "x1"].sort_values("entry_time").reset_index(drop=True)
sweep = pd.read_parquet(os.path.join(OUT, "sweep_stage1.parquet"))
sweep_sp = sweep[sweep["spread"]]
gap = pd.read_parquet(os.path.join(OUT, "cost_gap.parquet"))

is_t, oos_t = split_is_oos(led1)
mi, mo = metrics(is_t), metrics(oos_t)
r = led1["r_net"].to_numpy(float)

# equity sim (concurrency-aware, mirrors dfib_make_charts)
events = []
for _, row in led1.iterrows():
    events.append((row["entry_time"], 1)); events.append((row["exit_time"], -1))
events.sort()
cur = peak = 0
for _, d in events:
    cur += d; peak = max(peak, cur)
frac = min(0.01, 0.02 / max(peak, 1))
eq = 5000.0; eqs = []
for rr in r:
    eq += rr * max(10.0, frac * eq)
    eqs.append(eq)
equity = {
    "start_usd": 5000.0, "end_usd": float(eqs[-1]), "max_concurrent": int(peak),
    "per_trade_risk_frac": frac, "max_dd_pct": float(((np.maximum.accumulate(eqs) - eqs) / np.maximum.accumulate(eqs)).max() * 100),
    "is_end_usd": float(eqs[int((led1["entry_time"] <= led1["entry_time"].quantile(0.7)).sum()) - 1]),
}

summary = {
    "verdict": "DO NOT LAUNCH - no variant survives real-cost out-of-sample validation",
    "verification": {
        "n_setups": int(len(ver)),
        "their_total_r": float(ver["their_net_r"].sum()),
        "their_trades": int(ver["their_n"].sum()),
        "ours_nospread_r": float(ver["ours_r_nospread"].sum()),
        "ours_spread_r": float(ver["ours_r_spread"].sum()),
        "median_pf_theirs": float(ver["their_pf"].median()),
        "median_pf_nospread": float(ver["ours_pf_nospread"].median()),
        "median_pf_spread": float(ver["ours_pf_spread"].median()),
        "positive_share_spread": float((ver["ours_r_spread"] > 0).mean()),
    },
    "stage1_family": {
        f"{f}_{tf}": {"configs": int(len(g)), "median_pf": float(g["pf"].median()),
                      "positive": int((g["total_r"] > 0).sum())}
        for (f, tf), g in sweep_sp.groupby(["family", "tf"])
    },
    "cost_gap": {
        "positive_nospread": int((gap["total_r_nospread"] > 0).sum()),
        "positive_spread": int(((gap["total_r_nospread"] > 0) & (gap["total_r_spread"] > 0)).sum()),
        "median_r_nospread": float(gap["total_r_nospread"].median()),
        "median_r_spread": float(gap["total_r_spread"].median()),
    },
    "pooled": {"qualified": 0, "closest_is_pf": 1.04,
               "note": "0 of 240 pooled configs (2 universes x gates x floors) pass is_n>=100 & is_pf>=1.15"},
    "portfolio": {
        "members": picks.to_dict("records"),
        "is": mi, "oos": mo, "all": metrics(led1),
        "mc": mc_iid(r), "bb": block_bootstrap(r),
        "dsr": dsr(r, n_trials=27090), "thirds": rolling_thirds(led1),
        "cost_stress": {"x1.5": metrics(led[led["cost_mult"] == "x1.5"]),
                        "x2": metrics(led[led["cost_mult"] == "x2"])},
        "equity": equity,
        "trials_deflated": 27090,
    },
}
with open(os.path.join(DL, "dfib_summary.json"), "w") as f:
    json.dump(summary, f, indent=1, default=str)
print("dfib_summary.json written")
print(json.dumps(summary["portfolio"]["oos"], indent=1, default=str))
print(json.dumps(equity, indent=1))

# ---------------- xlsx ----------------
wb = Workbook(); wb.properties.creator = "Z.ai"


def put_df(ws, df, heading, note=None, num_cols=None, short=None):
    ws.title = (short or heading)[:31]
    c = ws.cell(row=1, column=1, value=heading); c.font = font_title()
    row_i = 3
    if note:
        n = ws.cell(row=2, column=1, value=note); n.font = font_caption(); row_i = 4
    for j, col in enumerate(df.columns, start=1):
        cell = ws.cell(row=row_i, column=j, value=str(col))
        cell.font = font_header(); cell.fill = fill_header()
        cell.border = border_header(); cell.alignment = align_header()
    for i, (_, row) in enumerate(df.iterrows()):
        for j, col in enumerate(df.columns, start=1):
            v = row[col]
            if isinstance(v, float):
                v = round(v, 3)
            cell = ws.cell(row=row_i + 1 + i, column=j, value=v)
            cell.font = font_body(); cell.fill = fill_data_row(i)
            if col in (num_cols or []):
                cell.alignment = align_number(); cell.number_format = "0.00"
            else:
                cell.alignment = align_text()
    for j, col in enumerate(df.columns, start=1):
        letter = ws.cell(row=row_i, column=j).column_letter
        ws.column_dimensions[letter].width = min(max(12, len(str(col)) + 4), 42)
    ws.freeze_panes = ws.cell(row=row_i + 1, column=1)


verdict = pd.DataFrame([
    ["Their audit claim (362 setups)", f"+{summary['verification']['their_total_r']/1000:.1f}kR", "1.17 (median)", "zero spread, full-sample selection"],
    ["Corrected engine, spreads OFF", f"+{summary['verification']['ours_nospread_r']/1000:.1f}kR", f"{summary['verification']['median_pf_nospread']:.2f} (median)", "reproduces their claim"],
    ["Corrected engine, real spreads", f"{summary['verification']['ours_spread_r']/1000:.1f}kR", f"{summary['verification']['median_pf_spread']:.2f} (median)", f"only {summary['verification']['positive_share_spread']*100:.0f}% of setups positive"],
    ["Pooled same-config systems", "0 of 240 qualified", "best IS PF 1.04", "2 universes x cost gates x stop floors"],
    ["15-symbol plateau portfolio", f"IS {mi['total_r']:+.0f}R PF {mi['pf']:.2f}", f"OOS {mo['total_r']:+.0f}R PF {mo['pf']:.2f}", "edge does not transfer"],
], columns=["Layer", "Result", "PF", "Note"])
put_df(wb.active, verdict, "Delta-Fibonacci Validation - Verdict Ladder",
       "Every layer tested with real broker spreads, 70/30 IS/OOS discipline, no full-sample selection",
       num_cols=[], short="Verdict")

ver2 = ver.drop(columns=[c for c in ver.columns if "error" in c], errors="ignore")
put_df(wb.create_sheet(), ver2, "Champion Setup Verification (their 362 picks)",
       "Their recorded numbers vs corrected engine, spreads off and on",
       num_cols=["their_net_r", "their_pf", "ours_r_nospread", "ours_pf_nospread",
                 "ours_r_spread", "ours_pf_spread", "ours_dd_spread"], short="Champion Verify")

fam = sweep_sp.groupby(["family", "tf"]).agg(
    configs=("pf", "size"), median_pf=("pf", "median"), median_r=("total_r", "median"),
    positive=("total_r", lambda x: int((x > 0).sum())), best_r=("total_r", "max"),
    trades=("n", "sum")).reset_index()
put_df(wb.create_sheet(), fam, "Stage 1 Sweep - 23 Champion Assets (family x timeframe)",
       "Real spreads; 10,350 configs; no family median reaches PF 1.0",
       num_cols=["median_pf", "median_r", "best_r"], short="Stage1 Family")

put_df(wb.create_sheet(), picks, "Stage 4 Portfolio Picks (15 symbols, median-of-plateau, IS only)",
       "Selection rule locked before OOS: is_n>=25, is_pf>=1.20, >=3 qualified configs per cell, median pick",
       num_cols=["cell_is_r", "pick_is_r", "pick_is_pf", "pick_oos_r", "pick_oos_pf"], short="Portfolio Picks")

led_out = led1[["symbol", "tf", "side", "setup", "entry_time", "exit_time", "entry",
                "exit", "risk_dist", "r_net", "reason", "portfolio_member"]].copy()
led_out["entry_time"] = led_out["entry_time"].astype(str)
led_out["exit_time"] = led_out["exit_time"].astype(str)
put_df(wb.create_sheet(), led_out, "Portfolio Trade Ledger (real spreads, 1,399 trades)",
       "Trades of the 15 members, chronological; OOS = last 30% by entry time",
       num_cols=["entry", "exit", "risk_dist", "r_net"], short="Portfolio Ledger")

batt = pd.DataFrame([
    ["IS (selection window)", mi["n"], round(mi["total_r"], 1), round(mi["pf"], 2), round(mi["max_dd_r"], 1)],
    ["OOS (validation)", mo["n"], round(mo["total_r"], 1), round(mo["pf"], 2), round(mo["max_dd_r"], 1)],
    ["Monte Carlo (10k)", "-", "-", round(summary["portfolio"]["mc"]["p_profit"], 3), "-"],
    ["Block bootstrap", "-", "-", round(summary["portfolio"]["bb"]["p_profit"], 3), "-"],
    ["Deflated Sharpe (27,090 trials)", "-", "-", round(summary["portfolio"]["dsr"]["dsr_prob"], 3), "-"],
    ["Spreads x1.5", metrics(led[led["cost_mult"] == "x1.5"])["n"],
     round(metrics(led[led["cost_mult"] == "x1.5"])["total_r"], 1),
     round(metrics(led[led["cost_mult"] == "x1.5"])["pf"], 2), "-"],
    ["Spreads x2", metrics(led[led["cost_mult"] == "x2"])["n"],
     round(metrics(led[led["cost_mult"] == "x2"])["total_r"], 1),
     round(metrics(led[led["cost_mult"] == "x2"])["pf"], 2), "-"],
], columns=["Layer", "Trades", "Net R", "PF / prob", "Max DD (R)"])
put_df(wb.create_sheet(), batt, "Portfolio Robustness Battery",
       "MC/BB/DSR computed on the full mixed stream - the OOS row is the honest gate",
       num_cols=["Net R", "PF / prob"], short="Battery")

path = os.path.join(DL, "Delta_Fibonacci_Validation.xlsx")
wb.save(path)
print("saved", path)
