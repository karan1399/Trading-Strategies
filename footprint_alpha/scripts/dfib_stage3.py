#!/usr/bin/env python3
"""Stage 3, phase 1: pooled same-config portfolios.

For each family-level config (family, tf, param, multivariant, exit), pool the
trades of that ONE config across all 23 champion symbols. A config that works
pooled is a system; a config that only works per-symbol cherry-picked is noise.

Saves: pooled_summary.csv (IS/OOS metrics per config) and the trade ledgers of
the top-30 IS-ranked pooled configs.
"""
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import (load_symbol, precompute_signals, simulate, SimCfg,
                         metrics, split_is_oos)

OUT = "/home/z/my-project/dfib_results"
CHAMPIONS = [
    "SP500.p", "JP225.p", "DJ30.p", "NAS100.p", "HK50.p",
    "XAUUSD.pi", "XAGUSD.pi", "USWTI.p",
    "BTCUSD.pi", "ETHUSD.pi", "XLMUSD.p", "XRPUSD.pi", "ADAUSD.p",
    "USDCAD.pi", "EURUSD.pi", "GBPUSD.pi", "USDCHF.pi",
    "GBPJPY.pi", "CADJPY.pi", "CHFJPY.pi", "USDJPY.pi",
    "NZDSGD.pi", "GBPNOK.pi",
]
EXITS = ["fixed_1.5r", "fixed_2.0r", "fixed_2.5r", "fixed_3.0r", "fixed_3.5r",
         "fixed_4.0r", "trail_0.5r", "trail_1.0r", "trail_1.5r"]
EMA_PERIODS = {"15M": [20, 50, 100, 200], "1H": [20, 34, 50, 55, 89, 100, 160, 200, 233],
               "4H": [20, 34, 50, 55, 89, 100, 160, 200, 233]}
TFS = ["15M", "1H", "4H"]


def work(task):
    symbol, tf = task
    df = load_symbol(tf, symbol)
    out = []
    jobs = [("VWAP", 0)] + [("EMA", p) for p in EMA_PERIODS[tf]]
    for family, period in jobs:
        for mv in (True, False):
            sigs, setups, rds = precompute_signals(df, symbol, tf, family,
                                                   ema_period=period, multivariant=mv)
            if (sigs != 0).sum() == 0:
                continue
            for exit_mode in EXITS:
                tr = simulate(df, sigs, setups, rds, symbol, tf,
                              SimCfg(exit_mode=exit_mode, spread_on=True))
                if len(tr):
                    out.append((family, "SD_BANDS" if family == "VWAP" else f"EMA{period}",
                                mv, exit_mode, tr[["symbol", "entry_time", "r_net"]].copy()))
    return out


if __name__ == "__main__":
    tasks = [(s, tf) for s in CHAMPIONS for tf in TFS]
    t0 = time.time()
    pooled = defaultdict(list)
    with Pool(6) as p:
        for i, results in enumerate(p.imap_unordered(work, tasks)):
            for family, param, mv, exit_mode, tr in results:
                pooled[(family, param, mv, exit_mode)].append(tr)
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(tasks)}] {time.time()-t0:.0f}s", flush=True)

    rows, ledgers = [], {}
    for (family, param, mv, exit_mode), parts in pooled.items():
        tr = pd.concat(parts, ignore_index=True).sort_values("entry_time")
        is_t, oos_t = split_is_oos(tr)
        mi, mo = metrics(is_t), metrics(oos_t)
        ma = metrics(tr)
        key = f"{family}|{param}|mv={mv}|{exit_mode}"
        rows.append({"config": key, "family": family, "param": param, "multivariant": mv,
                     "exit": exit_mode, "n": ma["n"], "total_r": ma["total_r"], "pf": ma["pf"],
                     "win_rate": ma["win_rate"], "avg_r": ma["avg_r"], "max_dd_r": ma["max_dd_r"],
                     "t_stat": ma["t_stat"], "is_n": mi["n"], "is_r": mi["total_r"],
                     "is_pf": mi["pf"], "is_dd": mi["max_dd_r"],
                     "oos_n": mo["n"], "oos_r": mo["total_r"], "oos_pf": mo["pf"],
                     "oos_dd": mo["max_dd_r"]})
        ledgers[key] = tr
    summ = pd.DataFrame(rows).sort_values("is_r", ascending=False)
    summ.to_csv(os.path.join(OUT, "pooled_summary.csv"), index=False)
    print("pooled configs:", len(summ), f"({time.time()-t0:.0f}s)")

    qual = summ[(summ["is_n"] >= 100) & (summ["is_pf"] >= 1.10)].sort_values("is_r", ascending=False)
    print(f"\n== pooled configs with is_n>=100 & is_pf>=1.10: {len(qual)} ==")
    cols = ["config", "n", "is_n", "is_r", "is_pf", "is_dd", "oos_n", "oos_r", "oos_pf", "oos_dd", "pf", "t_stat"]
    print(qual[cols].head(25).round(2).to_string(index=False))

    # persist ledgers of the top-30 IS-ranked qualified pooled configs
    top = qual.head(30)
    led = []
    for _, r in top.iterrows():
        t = ledgers[r["config"]].copy()
        t["config"] = r["config"]
        led.append(t)
    if led:
        pd.concat(led, ignore_index=True).to_parquet(os.path.join(OUT, "pooled_top_ledgers.parquet"))
    print("saved pooled_summary.csv + pooled_top_ledgers.parquet")
