#!/usr/bin/env python3
"""Alpha360 Stage A: coarse full-universe sweep.

151 symbols x [1H, 4H] x 11 pairs x 3 reject modes x 6 patterns x 4 gate
combos x 2 representative exits (fixed_2.0r, trail_1.0r) = up to 316k config
rows. IS/OOS 70/30 chronological, real spreads, resumable JSONL.
"""
import json
import os
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, simulate, SimCfg, metrics, split_is_oos, list_symbols
from dfib_engine import wilder_atr, ema
from alpha360_engine import PAIRS, REJECTS, PATTERNS, precompute_pair, apply_gates

OUT = "/home/z/my-project/alpha360_results"
os.makedirs(OUT, exist_ok=True)
EXITS = ["fixed_2.0r", "trail_1.0r"]
TFS = ["1H", "4H"]
GATES = [(True, False), (False, False), (True, True), (False, True)]


def work(task):
    symbol, tf = task
    try:
        df = load_symbol(tf, symbol)
    except Exception:
        return []
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    atr = wilder_atr(h, l, c, 14)
    ema_cache = {p: ema(c, p) for p in set([f for f, s in PAIRS] + [s for f, s in PAIRS])}
    rows = []
    for fast, slow in PAIRS:
        ef, es = ema_cache[fast], ema_cache[slow]
        for reject in REJECTS:
            for pattern in PATTERNS:
                sides, setups, rds, sok, rok = precompute_pair(
                    df, fast, slow, reject, pattern, ef=ef, es=es, atr=atr)
                if (sides != 0).sum() == 0:
                    continue
                for slope, regime in GATES:
                    s2, r2 = apply_gates(sides, rds, sok, rok, slope, regime)
                    if (s2 != 0).sum() == 0:
                        continue
                    for exit_mode in EXITS:
                        tr = simulate(df, s2, setups, r2, symbol, tf, SimCfg(exit_mode=exit_mode))
                        if not len(tr):
                            continue
                        is_t, oos_t = split_is_oos(tr)
                        mi, mo = metrics(is_t), metrics(oos_t)
                        rows.append({"symbol": symbol, "tf": tf, "fast": fast, "slow": slow,
                                     "reject": reject, "pattern": pattern,
                                     "slope": slope, "regime": regime, "exit": exit_mode,
                                     "is_n": mi["n"], "is_r": mi["total_r"], "is_pf": mi["pf"],
                                     "is_dd": mi["max_dd_r"], "is_t": mi["t_stat"],
                                     "oos_n": mo["n"], "oos_r": mo["total_r"],
                                     "oos_pf": mo["pf"], "oos_dd": mo["max_dd_r"],
                                     "n": len(tr), "total_r": metrics(tr)["total_r"],
                                     "pf": metrics(tr)["pf"]})
    return rows


if __name__ == "__main__":
    tasks = [(s, tf) for tf in TFS for s in list_symbols(tf)]
    done_path = os.path.join(OUT, "sweep_a.jsonl")
    done = set()
    if os.path.exists(done_path):
        for line in open(done_path):
            try:
                r = json.loads(line)
                done.add((r["symbol"], r["tf"]))
            except Exception:
                pass
    tasks = [t for t in tasks if t not in done]
    print(f"tasks: {len(tasks)} remaining of 302", flush=True)
    t0 = time.time()
    sink = open(done_path, "a")
    with Pool(2) as p:
        for i, rows in enumerate(p.imap_unordered(work, tasks)):
            for r in rows:
                sink.write(json.dumps(r) + "\n")
            if (i + 1) % 10 == 0:
                sink.flush()
                print(f"  [{i+1}/{len(tasks)}] {time.time()-t0:.0f}s", flush=True)
    sink.close()
    import pandas as pd
    df = pd.DataFrame([json.loads(l) for l in open(done_path)])
    df.to_parquet(os.path.join(OUT, "sweep_a.parquet"))
    print("saved", len(df), "rows |", df["symbol"].nunique(), "symbols", f"({time.time()-t0:.0f}s)")
    q = df[(df["is_n"] >= 15) & (df["is_pf"] >= 1.15) & (df["is_r"] > 0)]
    print(f"IS-qualified: {len(q)} / {len(df)} across {q['symbol'].nunique()} symbols")
    print(q.groupby(["pattern"]).size().to_string())
    print(q.groupby(["reject"]).size().to_string())
    top = q.sort_values("is_r", ascending=False).head(20)
    print(top[["symbol", "tf", "fast", "slow", "reject", "pattern", "slope", "regime",
               "exit", "is_n", "is_pf", "is_r", "oos_pf"]].to_string(index=False))
