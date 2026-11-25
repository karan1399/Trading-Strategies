#!/usr/bin/env python3
"""Alpha360 Stage B: confluence overlays on Stage-A qualified cells.

For each (symbol, tf, fast, slow, reject, pattern) cell with >=2 IS-qualified
variants, rebuild the signals once, then test each confluence overlay (RSI
pullback zone, ATR regime, volume surge, session, cross-asset driver confirm)
on every qualified gate/exit variant. Keep overlay rows with is_n >= 10 that
improve IS PF (any R improvement). Selection stays IS-only.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, simulate, SimCfg, metrics, split_is_oos
from alpha360_engine import precompute_pair, apply_gates
from alpha360_common import (classify_family, confluence_masks, driver_slope_series,
                             apply_overlay)

OUT = "/home/z/my-project/alpha360_results"
MIN_N = 15
MIN_PF = 1.15

df = pd.read_parquet(os.path.join(OUT, "sweep_a.parquet"))
qual = df[(df["is_n"] >= MIN_N) & (df["is_pf"] >= MIN_PF) & (df["is_r"] > 0)].copy()
print(f"Stage A: {len(df)} rows, IS-qualified {len(qual)} "
      f"({qual['symbol'].nunique()} symbols)")

key = ["symbol", "tf", "fast", "slow", "reject", "pattern"]
cells = qual.groupby(key).agg(n_qual=("is_r", "size"), agg_is_r=("is_r", "sum")).reset_index()
cells = cells[cells["n_qual"] >= 2].sort_values("agg_is_r", ascending=False)
print(f"plateau cells: {len(cells)}")

_overlay_cache: dict = {}


def get_masks(sym, tf, fam):
    ck = (sym, tf)
    if ck in _overlay_cache:
        return _overlay_cache[ck]
    d = load_symbol(tf, sym)
    drivers = {}
    if fam == "gold":
        s = driver_slope_series(tf, "XAUUSD.pi")
        if s is not None:
            drivers["xau_drv"] = s
    elif fam == "jpy":
        s = driver_slope_series(tf, "USDJPY.pi")
        if s is not None:
            drivers["usdjpy_drv"] = s
    masks = confluence_masks(d, sym, tf, drivers)
    _overlay_cache[ck] = (d, masks)
    return d, masks


rows = []
for k, cell in cells.iterrows():
    sym, tf = cell["symbol"], cell["tf"]
    fam = classify_family(sym)
    try:
        d, masks = get_masks(sym, tf, fam)
    except Exception:
        continue
    sides, setups, rds, sok, rok = precompute_pair(
        d, int(cell["fast"]), int(cell["slow"]), cell["reject"], cell["pattern"])
    if (sides != 0).sum() == 0:
        continue
    variants = qual[(qual["symbol"] == sym) & (qual["tf"] == tf)
                    & (qual["fast"] == cell["fast"]) & (qual["slow"] == cell["slow"])
                    & (qual["reject"] == cell["reject"]) & (qual["pattern"] == cell["pattern"])]
    for _, v in variants.iterrows():
        s2, r2 = apply_gates(sides, rds, sok, rok, bool(v["slope"]), bool(v["regime"]))
        if (s2 != 0).sum() == 0:
            continue
        base = metrics(split_is_oos(simulate(
            d, s2, setups, r2, sym, tf, SimCfg(exit_mode=v["exit"])))[0])
        for name, (ml, ms) in masks.items():
            s3, r3 = apply_overlay(s2, r2, ml, ms)
            if (s3 != 0).sum() == 0:
                continue
            tr = simulate(d, s3, setups, r3, sym, tf, SimCfg(exit_mode=v["exit"]))
            if not len(tr):
                continue
            is_t, oos_t = split_is_oos(tr)
            mi, mo = metrics(is_t), metrics(oos_t)
            if mi["n"] < 10 or mi["pf"] < base["pf"] or mi["total_r"] <= 0:
                continue
            rows.append({"symbol": sym, "tf": tf, "fast": int(cell["fast"]),
                         "slow": int(cell["slow"]), "reject": cell["reject"],
                         "pattern": cell["pattern"], "slope": bool(v["slope"]),
                         "regime": bool(v["regime"]), "exit": v["exit"],
                         "overlay": name, "family": fam,
                         "is_n": mi["n"], "is_r": mi["total_r"], "is_pf": mi["pf"],
                         "is_dd": mi["max_dd_r"], "is_t": mi["t_stat"],
                         "oos_n": mo["n"], "oos_r": mo["total_r"], "oos_pf": mo["pf"],
                         "oos_dd": mo["max_dd_r"], "n": len(tr),
                         "total_r": metrics(tr)["total_r"], "pf": metrics(tr)["pf"],
                         "base_is_pf": base["pf"], "base_is_r": base["total_r"]})

out = pd.DataFrame(rows)
out.to_parquet(os.path.join(OUT, "sweep_b.parquet"))
print(f"\nStage B overlay rows: {len(out)} across {out['symbol'].nunique() if len(out) else 0} symbols")
if len(out):
    print(out.groupby("overlay").agg(n=("is_r", "size"), med_pf=("is_pf", "median"),
                                     med_oos_pf=("oos_pf", "median")).round(2).to_string())
    print("\ntop 15 by is_r:")
    cols = ["symbol", "tf", "fast", "slow", "reject", "pattern", "overlay", "exit",
            "is_n", "is_pf", "is_r", "oos_pf", "family"]
    print(out.sort_values("is_r", ascending=False).head(15)[cols].round(2).to_string(index=False))
