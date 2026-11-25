#!/usr/bin/env python3
"""HTF rejection full-universe sweep: 151 symbols x [1H, 4H] x
EMA{89,233} x patterns{P1..P4} x slope{x2} x regime{x2} x 9 exits.
Real spreads, no lookahead, IS/OOS 70/30. Resumable JSONL output.
"""
import json
import os
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, simulate, SimCfg, metrics, split_is_oos, list_symbols
from htf_reject import precompute_htf, PATTERNS

OUT = "/home/z/my-project/htf_results"
os.makedirs(OUT, exist_ok=True)
EXITS = ["fixed_1.5r", "fixed_2.0r", "fixed_2.5r", "fixed_3.0r", "fixed_3.5r",
         "fixed_4.0r", "trail_0.5r", "trail_1.0r", "trail_1.5r"]
PERIODS = [89, 233]
TFS = ["1H", "4H"]
FILTERS = [(True, False), (False, False), (True, True), (False, True)]


def work(task):
    symbol, tf = task
    try:
        df = load_symbol(tf, symbol)
    except Exception:
        return []
    rows = []
    for period in PERIODS:
        for pattern in PATTERNS:
            for slope, regime in FILTERS:
                sigs, setups, rds = precompute_htf(df, symbol, tf, period, pattern,
                                                   slope_gate=slope, regime_gate=regime)
                if (sigs != 0).sum() == 0:
                    continue
                for exit_mode in EXITS:
                    tr = simulate(df, sigs, setups, rds, symbol, tf,
                                  SimCfg(exit_mode=exit_mode))
                    if not len(tr):
                        continue
                    is_t, oos_t = split_is_oos(tr)
                    mi, mo = metrics(is_t), metrics(oos_t)
                    rows.append({"symbol": symbol, "tf": tf, "period": period,
                                 "pattern": pattern, "slope": slope, "regime": regime,
                                 "exit": exit_mode,
                                 "is_n": mi["n"], "is_r": mi["total_r"], "is_pf": mi["pf"],
                                 "is_dd": mi["max_dd_r"], "is_t": mi["t_stat"],
                                 "oos_n": mo["n"], "oos_r": mo["total_r"],
                                 "oos_pf": mo["pf"], "oos_dd": mo["max_dd_r"],
                                 "n": len(tr), "total_r": metrics(tr)["total_r"],
                                 "pf": metrics(tr)["pf"]})
    return rows


if __name__ == "__main__":
    tasks = [(s, tf) for tf in TFS for s in list_symbols(tf)]
    done_path = os.path.join(OUT, "sweep.jsonl")
    done = set()
    if os.path.exists(done_path):
        for line in open(done_path):
            try:
                r = json.loads(line)
                done.add((r["symbol"], r["tf"]))
            except Exception:
                pass
    tasks = [t for t in tasks if t not in done]
    print(f"tasks: {len(tasks)} of 302", flush=True)
    t0 = time.time()
    sink = open(done_path, "a")
    with Pool(6) as p:
        for i, rows in enumerate(p.imap_unordered(work, tasks)):
            for r in rows:
                sink.write(json.dumps(r) + "\n")
            if (i + 1) % 25 == 0:
                sink.flush()
                print(f"  [{i+1}/{len(tasks)}] {time.time()-t0:.0f}s", flush=True)
    sink.close()
    import pandas as pd
    df = pd.DataFrame([json.loads(l) for l in open(done_path)])
    df.to_parquet(os.path.join(OUT, "sweep.parquet"))
    print("saved", len(df), "config-rows |", df["symbol"].nunique(), "symbols", f"({time.time()-t0:.0f}s)")
    q = df[(df["is_n"] >= 20) & (df["is_pf"] >= 1.2) & (df["is_r"] > 0)]
    print(f"IS-qualified: {len(q)} / {len(df)} across {q['symbol'].nunique()} symbols")
    print(q.groupby(["tf", "pattern"]).size().to_string())
