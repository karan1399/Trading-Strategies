#!/usr/bin/env python3
"""Stage 4b: IS-only plateau selection -> portfolio -> ONE OOS validation + battery.

Pre-registered selection rule (locked before any OOS look):
  1. IS-qualified: is_n >= 25, is_pf >= 1.20, is_r > 0 (real spreads).
  2. Plateau: a (symbol, tf, family) cell qualifies only if >= 3 IS-qualified
     configs share it (parameter stability).
  3. Within a cell, pick the MEDIAN-is_r qualified config (not the best).
  4. Portfolio: rank cells by aggregate IS R of their qualified configs;
     take the top cell per symbol, cap 15 symbols.
  5. Validate the portfolio ONCE on OOS; run the full battery.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import (load_symbol, precompute_signals, simulate, SimCfg,
                         metrics, split_is_oos)
from dfib_robustness import mc_iid, block_bootstrap, dsr, rolling_thirds

OUT = "/home/z/my-project/dfib_results"

df = pd.read_parquet(os.path.join(OUT, "stage4_full_universe.parquet"))
qual = df[(df["is_n"] >= 25) & (df["is_pf"] >= 1.20) & (df["is_r"] > 0)].copy()
print(f"IS-qualified: {len(qual)} configs / {qual['symbol'].nunique()} symbols")

# plateau cells with >= 3 qualified configs
cells = qual.groupby(["symbol", "tf", "family"]).agg(
    n_qual=("is_r", "size"), agg_is_r=("is_r", "sum")).reset_index()
cells = cells[cells["n_qual"] >= 3].sort_values("agg_is_r", ascending=False)
print(f"plateau cells (>=3 qualified): {len(cells)}")

picks = []
for _, cell in cells.iterrows():
    sym = cell["symbol"]
    if any(p["symbol"] == sym for p in picks):
        continue  # one cell per symbol
    members = qual[(qual["symbol"] == cell["symbol"]) & (qual["tf"] == cell["tf"])
                   & (qual["family"] == cell["family"])]
    med = members.sort_values("is_r").iloc[len(members) // 2]  # median-of-plateau
    picks.append({"symbol": cell["symbol"], "tf": cell["tf"], "family": cell["family"],
                  "param": med["param"], "multivariant": bool(med["multivariant"]),
                  "exit": med["exit"], "cell_is_r": cell["agg_is_r"],
                  "pick_is_r": med["is_r"], "pick_is_pf": med["is_pf"], "pick_is_n": med["is_n"],
                  "pick_oos_r": med["oos_r"], "pick_oos_pf": med["oos_pf"], "pick_oos_n": med["oos_n"]})
    if len(picks) == 15:
        break
picks = pd.DataFrame(picks)
print(f"\nportfolio picks ({len(picks)}):")
print(picks.drop(columns=["family"]).round(2).to_string(index=False))

# re-simulate picks for full ledgers
ledgers = []
for _, p in picks.iterrows():
    dfp = load_symbol(p["tf"], p["symbol"])
    period = 0 if p["family"] == "VWAP" else int(p["param"].replace("EMA", ""))
    sigs, setups, rds = precompute_signals(dfp, p["symbol"], p["tf"], p["family"],
                                           ema_period=period, multivariant=p["multivariant"])
    for cmult, tag in [(1.0, "x1"), (1.5, "x1.5"), (2.0, "x2")]:
        tr = simulate(dfp, sigs, setups, rds, p["symbol"], p["tf"],
                      SimCfg(exit_mode=p["exit"], spread_on=True, cost_mult=cmult))
        tr = tr.copy()
        tr["portfolio_member"] = f"{p['symbol']}|{p['tf']}|{p['param']}|{p['exit']}"
        tr["cost_mult"] = tag
        ledgers.append(tr)
led = pd.concat(ledgers, ignore_index=True)
led.to_parquet(os.path.join(OUT, "portfolio_ledgers.parquet"))

for tag in ["x1", "x1.5", "x2"]:
    t = led[led["cost_mult"] == tag]
    is_t, oos_t = split_is_oos(t)
    mi, mo = metrics(is_t), metrics(oos_t)
    ma = metrics(t)
    print(f"\n== PORTFOLIO @ spread {tag}: "
          f"all n={ma['n']} R={ma['total_r']:.1f} PF={ma['pf']:.2f} DD={ma['max_dd_r']:.1f} | "
          f"IS n={mi['n']} R={mi['total_r']:.1f} PF={mi['pf']:.2f} | "
          f"OOS n={mo['n']} R={mo['total_r']:.1f} PF={mo['pf']:.2f} DD={mo['oos_dd'] if 'oos_dd' in mo else mo['max_dd_r']:.1f}")
    if tag == "x1":
        r = t["r_net"].to_numpy(float)
        print("   MC     :", {k: round(v, 3) for k, v in mc_iid(r).items()})
        print("   BB     :", {k: round(v, 3) for k, v in block_bootstrap(r).items()})
        print("   thirds :", [(x["third"], round(x["total_r"], 1), round(x["pf"], 2)) for x in rolling_thirds(t)])
        print("   DSR    :", {k: round(v, 3) for k, v in dsr(r, n_trials=27090).items()})

# member-level OOS table
print("\n== member OOS (informational, not selection) ==")
mem = led[led["cost_mult"] == "x1"]
rows = []
for m, g in mem.groupby("portfolio_member"):
    is_t, oos_t = split_is_oos(g)
    rows.append({"member": m, "is_r": metrics(is_t)["total_r"], "is_pf": metrics(is_t)["pf"],
                 "oos_r": metrics(oos_t)["total_r"], "oos_pf": metrics(oos_t)["pf"],
                 "oos_n": metrics(oos_t)["n"]})
print(pd.DataFrame(rows).round(2).to_string(index=False))
picks.to_csv(os.path.join(OUT, "portfolio_picks.csv"), index=False)
print("\nsaved portfolio_picks.csv + portfolio_ledgers.parquet")
