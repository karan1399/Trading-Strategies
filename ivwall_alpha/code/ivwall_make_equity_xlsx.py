#!/usr/bin/env python3
"""Build IV_Wall_Equity_Simulation.xlsx from equity sim artifacts."""
import json
import os
import sys

import pandas as pd
from openpyxl import Workbook

sys.path.insert(0, "/home/z/my-project/skills/xlsx")
sys.path.insert(0, "/home/z/my-project/skills/xlsx/templates")
from base import font_title, font_header, font_body, font_caption, fill_header, fill_data_row, border_header, align_header, align_number, align_text

DL = "/home/z/my-project/download/ivwall"

wb = Workbook()
wb.properties.creator = "Z.ai"


def put_df(ws, df, heading, note=None, num_cols=None, short=None):
    ws.title = (short or heading)[:31]
    c = ws.cell(row=1, column=1, value=heading)
    c.font = font_title()
    row_i = 3
    if note:
        n = ws.cell(row=2, column=1, value=note)
        n.font = font_caption()
        row_i = 4
    for j, col in enumerate(df.columns, start=1):
        cell = ws.cell(row=row_i, column=j, value=str(col))
        cell.font = font_header(); cell.fill = fill_header()
        cell.border = border_header(); cell.alignment = align_header()
    for i, (_, row) in enumerate(df.iterrows()):
        for j, col in enumerate(df.columns, start=1):
            v = row[col]
            if isinstance(v, float):
                v = round(v, 4)
            cell = ws.cell(row=row_i + 1 + i, column=j, value=v)
            cell.font = font_body(); cell.fill = fill_data_row(i)
            if col in (num_cols or []):
                cell.alignment = align_number(); cell.number_format = "0.00"
            else:
                cell.alignment = align_text()
    for j, col in enumerate(df.columns, start=1):
        letter = ws.cell(row=row_i, column=j).column_letter
        ws.column_dimensions[letter].width = min(max(12, len(str(col)) + 4), 40)
    ws.freeze_panes = ws.cell(row=row_i + 1, column=1)


legs = pd.read_csv(os.path.join(DL, "equity_legs_summary.csv"))
put_df(wb.active, legs, "$5,000 Equity Simulation - Legs (IV Wall alpha basket, 1% compounding risk)",
       "A: 4H timeframe 2025 leg (only timeframe covering 2025); A': 4H 2026 leg continuing equity; "
       "B: validated 15M flagship config on its data window (2026-09-10 to 2026-11-20)",
       num_cols=["start", "end", "net_usd", "ret_pct", "win_rate", "pf_usd", "max_dd_usd", "max_dd_pct"], short="Legs")

lot = pd.read_csv(os.path.join(DL, "mt5_lot_table.csv"))
put_df(wb.create_sheet(), lot, "MT5 Lot Calibration - 20 alpha pairs (USD account, standard lots)",
       "lots = risk$ / (stop_pips x pip_value_usd); risk$ = 1% of equity (50 USD at 5,000 start); "
       "volumes rounded down to 0.01; recompute risk$ daily as equity changes",
       num_cols=["median_stop_15M_pips", "median_stop_4H_pips", "pip_value_usd_per_lot", "lots_at_50usd_risk", "usd_per_pip_at_that_lot"], short="MT5 Lot Table")

c4 = pd.read_csv(os.path.join(DL, "equity_curve_4h_alpha_2025_2026.csv"))
put_df(wb.create_sheet(), c4, "Equity Curve Ledger - 4H alpha basket, 2025-01-02 to 2026-11-19",
       "risk_usd = max(10, 1% of prior equity); pnl_usd = r_net x risk_usd; trades sequenced by entry time",
       num_cols=["entry", "stop", "target", "r_net", "risk_usd", "pnl_usd", "equity_usd"], short="Curve 4H 2025-2026")

c15 = pd.read_csv(os.path.join(DL, "equity_curve_15m_alpha.csv"))
put_df(wb.create_sheet(), c15, "Equity Curve Ledger - 15M flagship basket (deployable window)",
       "Standalone $5,000 deployment of the validated 15M rules on the available data window",
       num_cols=["entry", "stop", "target", "r_net", "risk_usd", "pnl_usd", "equity_usd"], short="Curve 15M 2026")

sim = json.load(open(os.path.join(DL, "equity_sim_summary.json")))
mo = pd.DataFrame(sim["monthly"])
put_df(wb.create_sheet(), mo, "Monthly Net P&L (USD) - 4H alpha basket",
       "From the compounded 4H equity curve; 2026 months show the regime decay discussed in the report",
       num_cols=["net_pnl_usd"], short="Monthly P&L")

path = os.path.join(DL, "IV_Wall_Equity_Simulation.xlsx")
wb.save(path)
print("saved", path)
