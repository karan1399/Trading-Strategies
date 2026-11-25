#!/usr/bin/env python3
"""Stage 2: analyze Stage 1 sweep, qualify on IS only, then look at OOS.

Discipline (locked before looking):
  1. Filter to real-spread rows.
  2. IS qualification: is_n >= 25, is_pf >= 1.15, is_r > 0.
  3. Rank IS candidates by is_r (then is_pf).
  4. ONLY THEN inspect OOS of the qualified set; report transfer honestly.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")

df = pd.read_parquet("/home/z/my-project/dfib_results/sweep_stage1.parquet")
sp = df[df["spread"]].copy()
print("rows:", len(df), "| real-spread rows:", len(sp))

print("\n== real-spread overview: family x tf ==")
g = sp.groupby(["family", "tf"]).agg(
    configs=("pf", "size"), med_pf=("pf", "median"), med_r=("total_r", "median"),
    pos=("total_r", lambda x: int((x > 0).sum())), best_r=("total_r", "max"),
    tot_n=("n", "sum")).round(2)
print(g.to_string())

print("\n== IS-qualified candidates (is_n>=25, is_pf>=1.15, is_r>0), ranked by IS R ==")
q = sp[(sp["is_n"] >= 25) & (sp["is_pf"] >= 1.15) & (sp["is_r"] > 0)].copy()
q = q.sort_values("is_r", ascending=False)
print(f"qualified: {len(q)} of {len(sp)} configs")
cols = ["symbol", "tf", "family", "param", "multivariant", "exit", "n",
        "is_n", "is_r", "is_pf", "oos_n", "oos_r", "oos_pf", "oos_dd", "total_r", "pf"]
print(q[cols].head(30).round(2).to_string(index=False))

if len(q):
    print("\n== OOS transfer of IS-qualified set ==")
    print(f"oos_r > 0: {(q['oos_r'] > 0).sum()}/{len(q)} | median OOS PF: {q['oos_pf'].median():.2f} | "
          f"median OOS R: {q['oos_r'].median():.1f} | IS PF>=1.3 subset: "
          f"{(q['is_pf'] >= 1.3).sum()} configs, of which OOS positive: "
          f"{((q['is_pf'] >= 1.3) & (q['oos_r'] > 0)).sum()}")
    print("\n== by symbol (best qualified config per symbol, IS-ranked) ==")
    best = q.groupby("symbol").head(1)
    print(best[cols].round(2).to_string(index=False))

print("\n== top 15 by FULL-SAMPLE total R (what their full-sample selection would pick) ==")
print(sp.sort_values("total_r", ascending=False)[cols].head(15).round(2).to_string(index=False))
