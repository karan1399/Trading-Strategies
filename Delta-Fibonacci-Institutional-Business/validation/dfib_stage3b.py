#!/usr/bin/env python3
"""Stage 3b: pooled configs under microstructure refinements, two universes.

Axes: cost gate (inf / 0.33), stop floor (1.2 / 1.5 ATR), universe (23 champions
/ 59 FX symbols), reduced config shortlist. IS-only qualification gates:
is_n >= 100 and is_pf >= 1.15. OOS is reported but never selected on.
"""
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool

import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import (load_symbol, precompute_signals, simulate, SimCfg,
                         metrics, split_is_oos, list_symbols)

OUT = "/home/z/my-project/dfib_results"
sys.path.insert(0, "/home/z/my-project/scripts")
from ivwall_runner import classify

CHAMPIONS = [
    "SP500.p", "JP225.p", "DJ30.p", "NAS100.p", "HK50.p",
    "XAUUSD.pi", "XAGUSD.pi", "USWTI.p",
    "BTCUSD.pi", "ETHUSD.pi", "XLMUSD.p", "XRPUSD.pi", "ADAUSD.p",
    "USDCAD.pi", "EURUSD.pi", "GBPUSD.pi", "USDCHF.pi",
    "GBPJPY.pi", "CADJPY.pi", "CHFJPY.pi", "USDJPY.pi",
    "NZDSGD.pi", "GBPNOK.pi",
]
FX_ALL = [s for s in list_symbols("4H") if classify(s) == "fx"]
UNIVERSES = {"champ23": CHAMPIONS, "fx59": FX_ALL}
EXITS = ["fixed_2.0r", "fixed_3.0r", "trail_0.5r"]
EMA_PERIODS = {"15M": [20, 50, 100, 200], "1H": [20, 50, 100, 200], "4H": [20, 50, 100, 200]}
TFS = ["15M", "1H", "4H"]
GATES = [("gateoff", math_inf := float("inf"), 1.2), ("gate0.33", 0.33, 1.2),
         ("floor1.5", float("inf"), 1.5), ("gate0.33+floor1.5", 0.33, 1.5)]


def work(task):
    uni, symbol, tf = task
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
                for gname, gate, floor in GATES:
                    tr = simulate(df, sigs, setups, rds, symbol, tf,
                                  SimCfg(exit_mode=exit_mode, spread_on=True,
                                         max_cost_frac=gate, rd_floor_atr=floor))
                    if len(tr):
                        out.append((f"{uni}|{family}|{'SD_BANDS' if family == 'VWAP' else f'EMA{period}'}|{mv}|{exit_mode}|{gname}",
                                    tr[["symbol", "entry_time", "r_net"]].copy()))
    return out


if __name__ == "__main__":
    tasks = [(uni, s, tf) for uni, syms in UNIVERSES.items() for s in syms for tf in TFS]
    print(f"tasks: {len(tasks)}", flush=True)
    t0 = time.time()
    pooled = defaultdict(list)
    with Pool(6) as p:
        for i, results in enumerate(p.imap_unordered(work, tasks)):
            for key, tr in results:
                pooled[key].append(tr)
            if (i + 1) % 40 == 0:
                print(f"  [{i+1}/{len(tasks)}] {time.time()-t0:.0f}s", flush=True)

    rows = {}
    for key, parts in pooled.items():
        tr = pd.concat(parts, ignore_index=True).sort_values("entry_time")
        is_t, oos_t = split_is_oos(tr)
        rows[key] = {"config": key, **{f"is_{k}": v for k, v in metrics(is_t).items()},
                     **{f"oos_{k}": v for k, v in metrics(oos_t).items()}}
    summ = pd.DataFrame(rows.values())
    summ.to_csv(os.path.join(OUT, "pooled_refined_summary.csv"), index=False)
    print("configs:", len(summ), f"({time.time()-t0:.0f}s)")

    qual = summ[(summ["is_n"] >= 100) & (summ["is_pf"] >= 1.15)].sort_values("is_total_r", ascending=False)
    print(f"\n== IS-qualified (is_n>=100, is_pf>=1.15): {len(qual)} ==")
    cols = ["config", "is_n", "is_total_r", "is_pf", "is_max_dd_r", "oos_n", "oos_total_r", "oos_pf", "oos_max_dd_r"]
    if len(qual):
        print(qual[cols].head(25).round(2).to_string(index=False))
    else:
        print("NONE")
        near = summ[(summ["is_n"] >= 100)].sort_values("is_pf", ascending=False).head(15)
        print("\n== closest 15 by IS PF == ")
        print(near[cols].round(2).to_string(index=False))
