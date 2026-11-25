#!/usr/bin/env python3
"""Smoke test alpha360 engine on XAUUSD + EURUSD, both TFs, all modes."""
import sys
import time

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, simulate, SimCfg, metrics, split_is_oos
from alpha360_engine import PAIRS, REJECTS, PATTERNS, precompute_pair, apply_gates

t0 = time.time()
for sym in ["XAUUSD.pi", "EURUSD.pi"]:
    for tf in ["1H", "4H"]:
        df = load_symbol(tf, sym)
        total_sigs = 0
        for fast, slow in PAIRS:
            for reject in REJECTS:
                for pattern in PATTERNS:
                    sides, setups, rds, sok, rok = precompute_pair(df, fast, slow, reject, pattern)
                    nsig = int((sides != 0).sum())
                    total_sigs += nsig
                    if nsig == 0:
                        continue
                    s2, r2, su2 = apply_gates(sides, rds, setups, sok, rok, True, True)
                    tr = simulate(df, s2, su2, r2, sym, tf, SimCfg(exit_mode="fixed_2.0r"))
                    if len(tr):
                        is_t, oos_t = split_is_oos(tr)
                        mi = metrics(is_t)
                        if mi["n"] >= 8 and mi["pf"] > 1.3:
                            print(f"  {sym} {tf} {fast}/{slow} {reject} {pattern}: "
                                  f"n={mi['n']} pf={mi['pf']:.2f} r={mi['total_r']:.1f}")
        print(f"{sym} {tf}: {len(df)} bars, total raw signals={total_sigs} "
              f"({time.time()-t0:.1f}s elapsed)")
print(f"SMOKE OK ({time.time()-t0:.1f}s)")
