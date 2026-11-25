#!/usr/bin/env python3
"""Assemble the alpha360_ema_rejection folder in the repo working copy:
engine code, results (parquet/csv), charts, README. Deliverables (docx/pdf/
xlsx) are copied in by the report stage, not here."""
import os
import shutil

SRC = "/home/z/my-project/scripts"
RES = "/home/z/my-project/alpha360_results"
DL = "/home/z/my-project/download/alpha360"
REPO = "/home/z/my-project/repos/karan-ts"
DEST = os.path.join(REPO, "alpha360_ema_rejection")

os.makedirs(os.path.join(DEST, "results"), exist_ok=True)
os.makedirs(os.path.join(DEST, "charts"), exist_ok=True)

for f in ["alpha360_engine.py", "alpha360_common.py", "alpha360_sweep_a.py",
          "alpha360_sweep_b.py", "alpha360_stage_c.py", "alpha360_summarize.py"]:
    shutil.copy2(os.path.join(SRC, f), os.path.join(DEST, f))

for f in ["sweep_a.parquet", "sweep_b.parquet", "stage_c_summary.json",
          "basket_picks_conc.csv", "basket_picks_div.csv",
          "basket_ledgers_conc.parquet", "basket_ledgers_div.parquet"]:
    p = os.path.join(RES, f)
    if os.path.exists(p):
        shutil.copy2(p, os.path.join(DEST, "results", f))

ch = os.path.join(DL, "charts")
if os.path.isdir(ch):
    for f in os.listdir(ch):
        if f.endswith(".png"):
            shutil.copy2(os.path.join(ch, f), os.path.join(DEST, "charts", f))

for f in ["Alpha360_EMA_Rejection_Basket.xlsx", "Alpha360_EMA_Rejection_Report.pdf",
          "Alpha360_EMA_Rejection_Report.docx", "alpha360_summary.json",
          "basket_equity_ledger.csv"]:
    p = os.path.join(DL, f)
    if os.path.exists(p):
        shutil.copy2(p, os.path.join(DEST, f))

print("assembled", DEST)
for root, dirs, files in os.walk(DEST):
    for f in files:
        print(" ", os.path.relpath(os.path.join(root, f), DEST),
              f"{os.path.getsize(os.path.join(root, f))//1024}KB")
