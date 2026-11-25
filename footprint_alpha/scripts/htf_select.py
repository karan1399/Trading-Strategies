#!/usr/bin/env python3
"""HTF rejection alpha-basket selection (pre-registered rules, IS only) ->
single OOS validation + full battery.

Rules (locked before any OOS look):
  1. IS-qualified: is_n >= 20, is_pf >= 1.20, is_r > 0 (real spreads).
  2. Plateau cell = (symbol, tf, period, pattern) with >= 3 qualified configs
     (neighborhood = slope/regime/exit variants).
  3. Pick the MEDIAN-is_r qualified config within each cell.
  4. Portfolio: rank cells by aggregate IS R; one cell per symbol; cap 15.
  5. Validate ONCE on OOS; battery + equity sim.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, simulate, SimCfg, metrics, split_is_oos
from dfib_robustness import mc_iid, block_bootstrap, dsr, rolling_thirds
from htf_reject import precompute_htf

OUT = "/home/z/my-project/htf_results"

df = pd.read_parquet(os.path.join(OUT, "sweep.parquet"))
qual = df[(df["is_n"] >= 20) & (df["is_pf"] >= 1.20) & (df["is_r"] > 0)].copy()
print(f"IS-qualified: {len(qual)} / {len(df)}")

cells = qual.groupby(["symbol", "tf", "period", "pattern"]).agg(
    n_qual=("is_r", "size"), agg_is_r=("is_r", "sum")).reset_index()
cells = cells[cells["n_qual"] >= 3].sort_values("agg_is_r", ascending=False)
print(f"plateau cells: {len(cells)}")
print(cells.head(20).round(1).to_string(index=False))

picks = []
for _, cell in cells.iterrows():
    sym = cell["symbol"]
    if any(p["symbol"] == sym for p in picks):
        continue
    members = qual[(qual["symbol"] == sym) & (qual["tf"] == cell["tf"])
                   & (qual["period"] == cell["period"]) & (qual["pattern"] == cell["pattern"])]
    med = members.sort_values("is_r").iloc[len(members) // 2]
    picks.append({"symbol": sym, "tf": cell["tf"], "period": cell["period"],
                  "pattern": cell["pattern"], "cell_is_r": cell["agg_is_r"],
                  "slope": bool(med["slope"]), "regime": bool(med["regime"]),
                  "exit": med["exit"], "pick_is_r": med["is_r"], "pick_is_pf": med["is_pf"],
                  "pick_is_n": med["is_n"], "pick_oos_r": med["oos_r"],
                  "pick_oos_pf": med["oos_pf"], "pick_oos_n": med["oos_n"]})
    if len(picks) == 15:
        break
picks = pd.DataFrame(picks)
print(f"\nalpha basket ({len(picks)} members):")
print(picks.round(2).to_string(index=False))

ledgers = []
for _, p in picks.iterrows():
    dfp = load_symbol(p["tf"], p["symbol"])
    sigs, setups, rds = precompute_htf(dfp, p["symbol"], p["tf"], int(p["period"]),
                                       p["pattern"], slope_gate=p["slope"],
                                       regime_gate=p["regime"])
    for cmult, tag in [(1.0, "x1"), (1.5, "x1.5"), (2.0, "x2")]:
        tr = simulate(dfp, sigs, setups, rds, p["symbol"], p["tf"],
                      SimCfg(exit_mode=p["exit"], spread_on=True, cost_mult=cmult))
        tr = tr.copy()
        tr["member"] = f"{p['symbol']}|{p['tf']}|EMA{p['period']}|{p['pattern']}|{p['exit']}"
        tr["cost_mult"] = tag
        ledgers.append(tr)
led = pd.concat(ledgers, ignore_index=True)
led.to_parquet(os.path.join(OUT, "basket_ledgers.parquet"))
picks.to_csv(os.path.join(OUT, "basket_picks.csv"), index=False)

for tag in ["x1", "x1.5", "x2"]:
    t = led[led["cost_mult"] == tag]
    is_t, oos_t = split_is_oos(t)
    mi, mo, ma = metrics(is_t), metrics(oos_t), metrics(t)
    print(f"\n== BASKET @ spread {tag}: all n={ma['n']} R={ma['total_r']:.1f} PF={ma['pf']:.2f} "
          f"DD={ma['max_dd_r']:.1f} | IS n={mi['n']} R={mi['total_r']:.1f} PF={mi['pf']:.2f} | "
          f"OOS n={mo['n']} R={mo['total_r']:.1f} PF={mo['pf']:.2f} DD={mo['max_dd_r']:.1f}")
    if tag == "x1":
        r = t["r_net"].to_numpy(float)
        print("   MC     :", {k: round(v, 3) for k, v in mc_iid(r).items()})
        print("   BB     :", {k: round(v, 3) for k, v in block_bootstrap(r).items()})
        print("   thirds :", [(x["third"], round(x["total_r"], 1), round(x["pf"], 2)) for x in rolling_thirds(t)])
        print("   DSR    :", {k: round(v, 3) for k, v in dsr(r, n_trials=86634).items()})

print("\n== member OOS (informational) ==")
mem = led[led["cost_mult"] == "x1"]
rows = []
for m, g in mem.groupby("member"):
    is_t, oos_t = split_is_oos(g)
    rows.append({"member": m, "is_r": metrics(is_t)["total_r"], "is_pf": metrics(is_t)["pf"],
                 "oos_r": metrics(oos_t)["total_r"], "oos_pf": metrics(oos_t)["pf"],
                 "oos_n": metrics(oos_t)["n"]})
print(pd.DataFrame(rows).round(2).to_string(index=False))
