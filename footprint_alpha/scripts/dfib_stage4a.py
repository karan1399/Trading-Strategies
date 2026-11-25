#!/usr/bin/env python3
"""Stage 4a: full-universe (151 symbols) sweep at 1H/4H — the deep timeframes.

Real spreads, gate off, same grid as Stage 1 (VWAP + EMA 20/50/100/200, mv on/off,
9 exits). Saves per-symbol per-config metrics for IS-only selection in 4b.
"""
import json
import os
import sys
import time
from multiprocessing import Pool

import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import (load_symbol, precompute_signals, simulate, SimCfg,
                         metrics, split_is_oos, list_symbols)

OUT = "/home/z/my-project/dfib_results"
EXITS = ["fixed_1.5r", "fixed_2.0r", "fixed_2.5r", "fixed_3.0r", "fixed_3.5r",
         "fixed_4.0r", "trail_0.5r", "trail_1.0r", "trail_1.5r"]
EMA_PERIODS = [20, 50, 100, 200]
TFS = ["1H", "4H"]


def work(task):
    symbol, tf = task
    try:
        df = load_symbol(tf, symbol)
    except Exception:
        return []
    rows = []
    jobs = [("VWAP", 0)] + [("EMA", p) for p in EMA_PERIODS]
    for family, period in jobs:
        for mv in (True, False):
            sigs, setups, rds = precompute_signals(df, symbol, tf, family,
                                                   ema_period=period, multivariant=mv)
            if (sigs != 0).sum() == 0:
                continue
            for exit_mode in EXITS:
                tr = simulate(df, sigs, setups, rds, symbol, tf,
                              SimCfg(exit_mode=exit_mode, spread_on=True))
                if not len(tr):
                    continue
                is_t, oos_t = split_is_oos(tr)
                mi, mo = metrics(is_t), metrics(oos_t)
                rows.append({"symbol": symbol, "tf": tf, "family": family,
                             "param": "SD_BANDS" if family == "VWAP" else f"EMA{period}",
                             "multivariant": mv, "exit": exit_mode,
                             "is_n": mi["n"], "is_r": mi["total_r"], "is_pf": mi["pf"],
                             "is_dd": mi["max_dd_r"], "is_t": mi["t_stat"],
                             "oos_n": mo["n"], "oos_r": mo["total_r"], "oos_pf": mo["pf"],
                             "oos_dd": mo["max_dd_r"],
                             "n": len(tr), "total_r": metrics(tr)["total_r"],
                             "pf": metrics(tr)["pf"]})
    return rows


if __name__ == "__main__":
    tasks = [(s, tf) for tf in TFS for s in list_symbols(tf)]
    print(f"tasks: {len(tasks)}", flush=True)
    t0 = time.time()
    sink_path = os.path.join(OUT, "stage4_full_universe.jsonl")
    sink = open(sink_path, "w")
    with Pool(6) as p:
        for i, rows in enumerate(p.imap_unordered(work, tasks)):
            for r in rows:
                sink.write(json.dumps(r) + "\n")
            if (i + 1) % 30 == 0:
                sink.flush()
                print(f"  [{i+1}/{len(tasks)}] {time.time()-t0:.0f}s", flush=True)
    sink.close()
    df = pd.DataFrame([json.loads(l) for l in open(sink_path)])
    df.to_parquet(os.path.join(OUT, "stage4_full_universe.parquet"))
    print("saved", len(df), "rows |", df["symbol"].nunique(), "symbols |", f"{time.time()-t0:.0f}s")
    q = df[(df["is_n"] >= 25) & (df["is_pf"] >= 1.2) & (df["is_r"] > 0)]
    print(f"IS-qualified configs: {len(q)} across {q['symbol'].nunique()} symbols")
    print(q.groupby(["tf", "family"]).size().to_string())
