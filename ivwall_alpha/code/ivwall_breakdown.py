#!/usr/bin/env python3
"""Per-asset-class breakdown of saved Stage A trade files."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivwall_runner import classify
from ivwall_engine import metrics

OUT = "/home/z/my-project/ivwall_results"
names = sys.argv[1:] if len(sys.argv) > 1 else [
    "fade_touch_sd2_ewma_cc", "rejection_sd2_ewma_cc", "exhaustion_sd2_ewma_cc",
    "breakout_sd1_atr", "fade_touch_sd1_ewma_cc", "fade_touch_sd2_parkinson",
]
for name in names:
    f = os.path.join(OUT, f"trades_{name}.parquet")
    if not os.path.exists(f):
        print(name, ": missing"); continue
    tr = pd.read_parquet(f)
    tr["iclass"] = tr["symbol"].map(classify)
    print(f"\n===== {name} (n={len(tr)}) =====")
    rows = []
    for cls, g in tr.groupby("iclass"):
        m = metrics(g)
        rows.append({"class": cls, **m})
    df = pd.DataFrame(rows).set_index("class").round(2)
    print(df.to_string())
