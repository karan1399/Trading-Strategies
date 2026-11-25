#!/usr/bin/env python3
"""Stage 1 sweep: Delta-Fibonacci families on the 23 champion assets,
15M/1H/4H, real spreads vs no-spread (cost-gap documentation), IS/OOS split.

Selection discipline: rank on IS only; OOS is never touched until finalists.
5M is excluded: the parquet store holds 5,000 bars = ~17 days of 5M data,
too thin for any honest IS/OOS statement (documented in the report).
"""
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, precompute_signals, simulate, SimCfg, metrics, split_is_oos

OUT = "/home/z/my-project/dfib_results"
os.makedirs(OUT, exist_ok=True)

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


def rowset(symbol, tf):
    df = load_symbol(tf, symbol)
    rows = []
    jobs = [("VWAP", 0)] + [("EMA", p) for p in EMA_PERIODS[tf]]
    for family, period in jobs:
        for mv in (True, False):
            sigs, setups, rds = precompute_signals(
                df, symbol, tf, family, ema_period=period, multivariant=mv)
            if (sigs != 0).sum() == 0:
                continue
            for exit_mode in EXITS:
                for spread_on in (True, False):
                    tr = simulate(df, sigs, setups, rds, symbol, tf,
                                  SimCfg(exit_mode=exit_mode, spread_on=spread_on))
                    if not len(tr):
                        continue
                    is_t, oos_t = split_is_oos(tr)
                    mi, mo = metrics(is_t), metrics(oos_t)
                    ma = metrics(tr)
                    rows.append({
                        "symbol": symbol, "tf": tf, "family": family,
                        "param": "SD_BANDS" if family == "VWAP" else f"EMA{period}",
                        "multivariant": mv, "exit": exit_mode, "spread": spread_on,
                        "n": ma["n"], "total_r": ma["total_r"], "pf": ma["pf"],
                        "win_rate": ma["win_rate"], "avg_r": ma["avg_r"],
                        "max_dd_r": ma["max_dd_r"], "t_stat": ma["t_stat"],
                        "is_n": mi["n"], "is_r": mi["total_r"], "is_pf": mi["pf"],
                        "oos_n": mo["n"], "oos_r": mo["total_r"], "oos_pf": mo["pf"],
                        "oos_dd": mo["max_dd_r"],
                    })
    return rows


def work(task):
    symbol, tf = task
    try:
        return rowset(symbol, tf)
    except Exception as e:  # noqa: BLE001
        return [{"symbol": symbol, "tf": tf, "error": str(e)}]


if __name__ == "__main__":
    import json
    done_path = os.path.join(OUT, "sweep_stage1.jsonl")
    done = set()
    if os.path.exists(done_path):
        with open(done_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    done.add((r["symbol"], r["tf"]))
                except Exception:
                    pass
    tasks = [(s, tf) for s in CHAMPIONS for tf in TFS if (s, tf) not in done]
    print(f"tasks: {len(tasks)} remaining (of {len(CHAMPIONS)*len(TFS)})", flush=True)
    t0 = time.time()
    sink = open(done_path, "a")
    with Pool(6) as p:
        for i, rows in enumerate(p.imap_unordered(work, tasks)):
            if rows and "error" in rows[0]:
                print(f"  ERROR {rows[0]['symbol']} {rows[0]['tf']}: {rows[0]['error']}", flush=True)
            for r in rows:
                if "error" not in r:
                    sink.write(json.dumps(r) + "\n")
            sink.flush()
            head = rows[0] if rows else {}
            print(f"  [{i+1}/{len(tasks)}] {head.get('symbol','?')} {head.get('tf','?')} "
                  f"(+{len(rows)} rows) {time.time()-t0:.0f}s", flush=True)
    sink.close()

    df = pd.DataFrame([json.loads(l) for l in open(done_path)])
    df.to_parquet(os.path.join(OUT, "sweep_stage1.parquet"))
    print("saved", len(df), "rows ->", os.path.join(OUT, "sweep_stage1.parquet"))

    sp = df[df["spread"]]
    print("\n== real-spread overview: family x tf (median PF, median total R, total n) ==")
    print(sp.groupby(["family", "tf"]).agg(
        configs=("pf", "size"), med_pf=("pf", "median"), med_r=("total_r", "median"),
        pos_cfg=("total_r", lambda x: int((x > 0).sum())), tot_n=("n", "sum")).round(2).to_string())
    print("\n== cost gap: same configs, spread on vs off ==")
    off = df[~df["spread"]][["symbol", "tf", "family", "param", "multivariant", "exit", "total_r", "pf", "n"]]
    on = sp[["symbol", "tf", "family", "param", "multivariant", "exit", "total_r", "pf", "n"]]
    gap = off.merge(on, on=["symbol", "tf", "family", "param", "multivariant", "exit"],
                    suffixes=("_nospread", "_spread"))
    print("configs with positive total R, no-spread:", int((gap["total_r_nospread"] > 0).sum()),
          "| still positive with spreads:", int(((gap["total_r_nospread"] > 0) & (gap["total_r_spread"] > 0)).sum()))
    print("median total R: no-spread", round(gap["total_r_nospread"].median(), 1),
          "-> with spreads", round(gap["total_r_spread"].median(), 1))
    gap.to_parquet(os.path.join(OUT, "cost_gap.parquet"))
