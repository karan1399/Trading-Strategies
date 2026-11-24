#!/usr/bin/env python3
"""Smoke test the dfib engine: one symbol, with vs without real spreads."""
import sys, time
from pathlib import Path
validation_dir = Path(__file__).resolve().parent
repo_root = validation_dir.parent.parent
sys.path.insert(0, str(validation_dir))
sys.path.insert(0, str(validation_dir.parent))
sys.path.insert(0, str(repo_root))
from dfib_engine import load_symbol, precompute_signals, simulate, SimCfg, metrics, split_is_oos

t0 = time.time()
df = load_symbol("1H", "SP500.p")
print("SP500.p 1H:", len(df), "bars,", df.index[0], "->", df.index[-1], "| digits", int(df["digits"].iloc[0]),
      "| median spread", float(df["spread"].median()), "points")

sigs, setups, rds = precompute_signals(df, "SP500.p", "1H", "EMA", ema_period=50, multivariant=True)
print("signals fired:", int((sigs != 0).sum()), f"({time.time()-t0:.1f}s)")

for spread_on in (False, True):
    tr = simulate(df, sigs, setups, rds, "SP500.p", "1H", SimCfg(exit_mode="trail_0.5r", spread_on=spread_on))
    m = metrics(tr)
    is_t, oos_t = split_is_oos(tr)
    mi, mo = metrics(is_t), metrics(oos_t)
    print(f"spread_on={spread_on!s:5} | all n={m['n']:4} R={m['total_r']:8.1f} PF={m['pf']:4.2f} "
          f"DD={m['max_dd_r']:5.1f} | IS R={mi['total_r']:7.1f} PF={mi['pf']:4.2f} | "
          f"OOS R={mo['total_r']:7.1f} PF={mo['pf']:4.2f}")

# VWAP family check on 15M
df15 = load_symbol("15M", "HK50.p")
sigs, setups, rds = precompute_signals(df15, "HK50.p", "15M", "VWAP", multivariant=True)
tr = simulate(df15, sigs, setups, rds, "HK50.p", "15M", SimCfg(exit_mode="trail_0.5r"))
m = metrics(tr)
print(f"HK50.p 15M VWAP trail_0.5r | bars={len(df15)} signals={int((sigs!=0).sum())} | "
      f"n={m['n']} R={m['total_r']:.1f} PF={m['pf']:.2f}")
