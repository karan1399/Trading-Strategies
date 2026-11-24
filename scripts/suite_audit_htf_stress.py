#!/usr/bin/env python3
"""Gold-family spread stress for the HTF basket (their frozen picks).
Re-runs the 8 gold cells at 1x / 2x / 3x enforced spread; pools OOS by day."""
import math
import sys

import numpy as np

sys.path.insert(0, "/home/z/my-project/scripts")
from suite_audit_htf import (GOLD_FAMILY, BASKET, load_symbol, precompute, simulate,
                             day_split_metrics)

GOLD_CELLS = [("XAUAUD.pi", "4H", 233, "P4_cluster", True, False, "fixed_4.0r"),
              ("XAUCNH.pi", "4H", 233, "P4_cluster", False, True, "trail_0.5r"),
              ("XAUUSD.pi", "4H", 233, "P4_cluster", False, False, "fixed_2.0r"),
              ("XAUEUR.pi", "4H", 233, "P4_cluster", True, False, "fixed_1.5r"),
              ("XAUSGD.pi", "4H", 233, "P4_cluster", True, False, "fixed_3.5r"),
              ("XAUGBP.pi", "4H", 233, "P4_cluster", False, False, "trail_1.0r"),
              ("GAUUSD.pi", "4H", 233, "P4_cluster", False, False, "trail_1.0r"),
              ("GAUCNH.pi", "4H", 233, "P4_cluster", False, False, "fixed_3.5r")]

for mult in (1.0, 2.0, 3.0):
    pooled = {}
    for sym, tf, per, pat, sl, rg, ex in GOLD_CELLS:
        df = load_symbol(tf, sym)
        if df is None:
            continue
        df["spr"] = df["spr"] * mult
        s, r = precompute(df, per, pat, sl, rg)
        tr = simulate(df, s, r, ex)
        if not len(tr):
            continue
        days = sorted({str(d) for d in df["datetime"].dt.date})
        _, tag = day_split_metrics(tr, days)
        for _, t in tag[tag["leg"] == "OOS"].iterrows():
            pooled.setdefault(t["entry_day"], 0.0)
            pooled[t["entry_day"]] += t["r_net"]
    v = np.array(list(pooled.values()))
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 else 0.0
    print(f"spread x{mult}: n_days={len(v):3d}  OOS sum={v.sum():+8.1f}R  PF-ish mean/day={v.mean():+.3f}R  t={t:+.2f}")
