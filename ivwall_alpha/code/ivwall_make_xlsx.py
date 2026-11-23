#!/usr/bin/env python3
"""Build IV-Wall results workbook (multi-sheet) using the xlsx skill design tokens."""
import json
import os
import sys

import pandas as pd
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

sys.path.insert(0, "/home/z/my-project/skills/xlsx")
sys.path.insert(0, "/home/z/my-project/skills/xlsx/templates")
from base import (use_palette, font_title, font_header, font_body, font_caption,
                  fill_header, fill_data_row, border_header, align_header,
                  align_number, align_text, ACCENT_POSITIVE, ACCENT_NEGATIVE)

OUT = "/home/z/my-project/ivwall_results"
DL = "/home/z/my-project/download/ivwall"
os.makedirs(DL, exist_ok=True)

use_palette_explicit = getattr(sys.modules["base"], "use_palette_explicit", None)
if use_palette_explicit:
    use_palette_explicit("professional")

summary = json.load(open(os.path.join(DL, "summary.json")))

wb = Workbook()
wb.properties.creator = "Z.ai"

NUM_FMT = "0.00"


def put_df(ws, df, title, note=None, num_cols=None, width_map=None, short=None):
    ws.title = short or title[:31]
    r0 = 1
    c = ws.cell(row=1, column=1, value=title)
    c.font = font_title()
    row_i = 3
    if note:
        n = ws.cell(row=2, column=1, value=note)
        n.font = font_caption()
        row_i = 4
    hdr_row = row_i
    for j, col in enumerate(df.columns, start=1):
        cell = ws.cell(row=hdr_row, column=j, value=str(col))
        cell.font = font_header(); cell.fill = fill_header()
        cell.border = border_header(); cell.alignment = align_header()
    for i, (_, row) in enumerate(df.iterrows()):
        for j, col in enumerate(df.columns, start=1):
            v = row[col]
            if isinstance(v, float):
                v = round(v, 4)
            cell = ws.cell(row=hdr_row + 1 + i, column=j, value=v)
            cell.font = font_body()
            cell.fill = fill_data_row(i)
            if col in (num_cols or []):
                cell.alignment = align_number(); cell.number_format = NUM_FMT
            else:
                cell.alignment = align_text()
        if (hdr_row + 1 + i) % 25 == 0:
            pass
    for j, col in enumerate(df.columns, start=1):
        w = (width_map or {}).get(col, None)
        ws.column_dimensions[ws.cell(row=hdr_row, column=j).column_letter].width = \
            w if w else min(max(12, len(str(col)) + 4), 40)
    ws.freeze_panes = ws.cell(row=hdr_row + 1, column=1)
    return ws


# ---- Sheet 1: Summary
rows = []
def add(k, v, note=""):
    rows.append({"Metric": k, "Value": v, "Note": note})

fs = summary["flagship"]; bs = summary["basket"]
add("Universe", "151 symbols x 5 timeframes (1M/5M/15M/1H/4H), 5,000 bars each", "MT5 Blueberry export, historical_parquet/")
add("Strategy family", "IV Wall 2SD session fade (FX only)", "session-anchored expected-move envelope")
add("Flagship trades", fs["full"]["n"], "59 FX symbols, 15M bars, ~72 days")
add("Flagship net P&L", round(fs["full"]["total_r"], 1), "R units; $945.6 at $10 risk/trade")
add("Flagship profit factor", round(fs["full"]["pf"], 3), "win rate " + str(round(fs["full"]["win_rate"], 3)))
add("Flagship max DD", round(fs["full"]["max_dd_r"], 1), "R")
add("Flagship IS / OOS PF", f"{fs['is']['pf']:.2f} / {fs['oos']['pf']:.2f}", "70/30 chronological split")
add("Flagship MC P(profit)", fs["mc"]["p_profit"], "10,000 iid resamples")
add("Flagship MC 5th-pct total", round(fs["mc"]["total_ci5"], 1), "R")
add("Flagship block-bootstrap P", fs["bb"]["p_profit"], "stationary blocks, avg length 5")
add("Flagship DSR", fs["dsr"]["dsr_prob"], "Bailey-LdP deflated, 96 trials; below 0.5 bar — honest limitation")
add("Basket trades", bs["full"]["n"], "top-20 IS-selected FX symbols")
add("Basket net P&L", round(bs["full"]["total_r"], 1), "IS " + f"{bs['is']['total_r']:.0f}R / OOS {bs['oos']['total_r']:.0f}R")
add("Basket IS / OOS PF", f"{bs['is']['pf']:.2f} / {bs['oos']['pf']:.2f}", "OOS t-stat " + str(round(bs["oos"]["t_stat"], 2)))
add("Basket MC P(profit)", bs["mc"]["p_profit"], "5th-pct " + f"{bs['mc']['total_ci5']:.0f}R")
add("Basket max DD (bootstrap)", round(bs["bb"]["dd95"], 1), "95th pct DD, R")
add("Cost stress 2x spread PF", round(summary["cost_stress_2x"]["pf"], 3), "survives doubled spreads")
add("Timeframe check", "15M only", "1H PF " + f"{summary['timeframes']['1H']['pf']:.2f}, 4H PF {summary['timeframes']['4H']['pf']:.2f}, 5M PF {summary['timeframes']['5M']['pf']:.2f}")
add("Trials run", summary["grid_counts"]["trials_total"], "96 parameter configs screened")
df1 = pd.DataFrame(rows)
ws = wb.active
put_df(ws, df1, "IV Wall Strategy — Results Summary", "Backtest of session-anchored expected-move (IV wall) envelopes on karan1399/Trading-Strategies historical_parquet data", short="Summary",
       num_cols=["Value"])

# ---- Sheet 2: Stage A screening
stA = pd.read_csv(os.path.join(OUT, "stage_a_15M.csv"))
colsA = ["name", "all_n", "all_total_r", "all_pf", "all_win_rate", "is_n", "is_total_r", "is_pf", "oos_n", "oos_total_r", "oos_pf"]
put_df(wb.create_sheet(), stA[colsA].copy(), "Stage A — Screening Grid (24 configs x 151 symbols, 15M)", short="Stage A Screening",
       "Four playbook families x 2 wall depths x 3 volatility estimators", num_cols=["all_total_r", "is_total_r", "oos_total_r", "all_pf", "is_pf", "oos_pf"])

# ---- Sheet 3: Stage B refinement
stB = pd.read_csv(os.path.join(OUT, "stage_b_15M.csv"))
colsB = ["name", "all_n", "all_pf", "is_n", "is_total_r", "is_pf", "oos_n", "oos_total_r", "oos_pf", "oos_max_dd_r"]
put_df(wb.create_sheet(), stB[colsB].copy(), "Stage B — FX Refinement Grid (72 configs x 59 FX symbols, 15M)", short="Stage B FX Grid",
       "fade_touch x sd{1.5,2,2.5} x vol{ewma_cc,atr} x target_m{0,0.5,1} x buffer{0.5,1.0} x trend{none,fade_flat}",
       num_cols=["all_pf", "is_total_r", "is_pf", "oos_total_r", "oos_pf", "oos_max_dd_r"])

# ---- Sheet 4: Robustness
rows = []
for vn, key in [("Flagship (all-FX)", "flagship"), ("Basket (top-20)", "basket")]:
    d = summary[key]
    rows.append({"Variant": vn, "n": d["full"]["n"], "Total R": round(d["full"]["total_r"], 1),
                 "PF": round(d["full"]["pf"], 3), "Win rate": round(d["full"]["win_rate"], 3),
                 "Max DD (R)": round(d["full"]["max_dd_r"], 1),
                 "MC P(profit)": d["mc"]["p_profit"], "MC 5th pct R": round(d["mc"]["total_ci5"], 1),
                 "MC DD95 R": round(d["mc"]["dd95"], 1),
                 "BB P(profit)": d["bb"]["p_profit"], "BB DD95 R": round(d["bb"]["dd95"], 1),
                 "Per-trade SR": round(d["dsr"]["sr"], 3), "DSR prob": round(d["dsr"]["dsr_prob"], 3),
                 "T1 PF": round(d["thirds"][0]["pf"], 2), "T2 PF": round(d["thirds"][1]["pf"], 2),
                 "T3 PF": round(d["thirds"][2]["pf"], 2)})
rob = pd.DataFrame(rows)
put_df(wb.create_sheet(), rob, "Robustness Battery — Monte Carlo, Block Bootstrap, Deflated Sharpe, Rolling Thirds", short="Robustness",
       f"DSR deflated against {summary['dsr_params']['n_trials']} trials using empirical V[SR] across {summary['dsr_params']['n_sr_configs']} configs",
       num_cols=["Total R", "PF", "Win rate", "Max DD (R)", "MC P(profit)", "MC 5th pct R", "MC DD95 R", "BB P(profit)", "BB DD95 R", "Per-trade SR", "DSR prob", "T1 PF", "T2 PF", "T3 PF"])

# ---- Sheet 5: Class breakdown
cb = summary["class_breakdown"]
cls = pd.DataFrame([{"Class": k, "n": v["n"], "Total R": round(v["total_r"], 1), "PF": round(v["pf"], 3),
                     "Win rate": round(v["win_rate"], 3), "Max DD R": round(v["max_dd_r"], 1)} for k, v in cb.items()])
put_df(wb.create_sheet(), cls, "Pooled 2SD Wall-Fade — P&L by Asset Class (Stage A)", short="Class Breakdown",
       "Only FX shows a positive edge; crypto/index/metal/commodity fade attempts lose after costs",
       num_cols=["Total R", "PF", "Win rate", "Max DD R"])

# ---- Sheet 6: Basket symbols + per-symbol
flag = pd.read_parquet(os.path.join(OUT, "trades_flagship.parquet"))
flag["iclass"] = flag["symbol"].map(lambda s: s.split(".")[0])
q = flag["entry_time"].quantile(0.70)
g = flag[flag.entry_time <= q].groupby("symbol").agg(
    IS_trades=("r_net", "count"), IS_total_R=("r_net", "sum")).reset_index()
g["Basket"] = g["symbol"].isin(summary["basket_symbols"])
g["IS_total_R"] = g["IS_total_R"].round(1)
g = g.sort_values(["Basket", "IS_total_R"], ascending=[False, False])
put_df(wb.create_sheet(), g, "FX Symbol Ledger — IS Selection (Basket = top-20)", short="FX Symbol Ledger",
       "Basket symbols chosen on IS data only (IS PF>1, IS trades>=5); OOS untouched by selection",
       num_cols=["IS_trades", "IS_total_R"])

# ---- Sheet 7: Flagship trades
tr = flag[["symbol", "entry_time", "exit_time", "side", "wall_k", "wall_side", "entry", "stop", "target", "r_gross", "r_net", "reason", "bars_held"]].copy()
tr["entry_time"] = tr["entry_time"].astype(str); tr["exit_time"] = tr["exit_time"].astype(str)
put_df(wb.create_sheet(), tr, "Flagship Trade Ledger (523 trades)", short="Trade Ledger",
       "side: -1 short at upper wall, +1 long at lower wall; r_net includes real spread costs both sides",
       num_cols=["r_gross", "r_net", "bars_held"])

xlsx_path = os.path.join(DL, "IV_Wall_Results.xlsx")
wb.save(xlsx_path)
print("saved", xlsx_path)
