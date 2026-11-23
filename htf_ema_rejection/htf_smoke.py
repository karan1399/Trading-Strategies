#!/usr/bin/env python3
"""Smoke test HTF rejection patterns on XAUUSD H1 (the images' exact chart)."""
import sys

import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, simulate, SimCfg, metrics, split_is_oos
from htf_reject import precompute_htf, PATTERNS

df = load_symbol("1H", "XAUUSD.pi")
print("XAUUSD.pi 1H:", len(df), "bars", df.index[0].date(), "->", df.index[-1].date())

for period in (89, 233):
    for pattern in PATTERNS:
        sigs, setups, rds = precompute_htf(df, "XAUUSD.pi", "1H", period, pattern,
                                           slope_gate=True, regime_gate=False)
        tr = simulate(df, sigs, setups, rds, "XAUUSD.pi", "1H", SimCfg(exit_mode="fixed_3.0r"))
        m = metrics(tr)
        print(f"EMA{period} {pattern:12} signals={int((sigs!=0).sum()):3} n={m['n']:3} "
              f"R={m['total_r']:7.1f} PF={m['pf']:5.2f}")

# do the pictured setups fire? (img3: bullish rejection at rising EMA89 around 8-9 Nov; img1/2: bearish at falling EMA mid-Nov)
sigs, setups, rds = precompute_htf(df, "XAUUSD.pi", "1H", 89, "P1_wick")
tr = simulate(df, sigs, setups, rds, "XAUUSD.pi", "1H", SimCfg(exit_mode="fixed_3.0r"))
tr["entry_time"] = pd.to_datetime(tr["entry_time"])
nov = tr[(tr["entry_time"] >= "2026-11-01")]
print("\nNov-2026 P1_wick EMA89 trades (the images' window):")
print(nov[["side", "entry_time", "r_net", "reason"]].to_string(index=False))
