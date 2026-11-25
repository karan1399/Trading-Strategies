#!/usr/bin/env python3
"""Alpha360 Stage C: full-exit plateau, dual basket construction, single OOS.

Pre-registered rules (locked before any OOS look):
  1. Candidates: Stage-A qualified rows (overlay=none) + Stage-B improving
     overlay rows. Qualify: is_n >= 15, is_pf >= 1.15, is_r > 0.
  2. Cell = (symbol, tf, fast, slow, reject, pattern, overlay). Expand the
     top 60 cells (by qualified-variant count, then agg IS R) to the full
     4 gates x 9 exits grid; plateau-median pick per cell.
  3. Baskets (IS-only ranking by pick is_r, one cell per symbol, cap 15):
       CONCENTRATED - no family cap (baseline construction)
       DIVERSIFIED  - max 3 members per symbol family
  4. Validate ONCE on OOS; battery (MC, block bootstrap, thirds, DSR with
     n_trials = all Stage A + B configs); spread stress x1/x1.5/x2.
  5. Compare both against the 89/233 baseline basket on the same OOS window.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, simulate, SimCfg, metrics, split_is_oos
from dfib_robustness import mc_iid, block_bootstrap, dsr, rolling_thirds
from alpha360_engine import precompute_pair, apply_gates
from alpha360_common import classify_family, confluence_masks, driver_slope_series, apply_overlay

OUT = "/home/z/my-project/alpha360_results"
EXITS = ["fixed_1.5r", "fixed_2.0r", "fixed_2.5r", "fixed_3.0r", "fixed_3.5r",
         "fixed_4.0r", "trail_0.5r", "trail_1.0r", "trail_1.5r"]
GATES = [(True, False), (False, False), (True, True), (False, True)]
MIN_N, MIN_PF = 15, 1.15

# ---------------------------------------------------------------- load -------
a = pd.read_parquet(os.path.join(OUT, "sweep_a.parquet"))
a["overlay"] = "none"
b = pd.read_parquet(os.path.join(OUT, "sweep_b.parquet"))
n_trials = len(a) + len(b)
cand = pd.concat([a, b], ignore_index=True)
qual = cand[(cand["is_n"] >= MIN_N) & (cand["is_pf"] >= MIN_PF) & (cand["is_r"] > 0)].copy()
print(f"candidates {len(cand)} (trials for DSR: {n_trials}), qualified {len(qual)}")

key = ["symbol", "tf", "fast", "slow", "reject", "pattern", "overlay"]
cells = qual.groupby(key).agg(n_qual=("is_r", "size"), agg_is_r=("is_r", "sum")).reset_index()
# plateau evidence (matches the baseline study's registered rule: >=3 qualified
# variants in the neighborhood) + IS-only ranking by aggregate R
cells = cells[cells["n_qual"] >= 3].sort_values("agg_is_r", ascending=False)
# pool diversity: at most 3 cells per symbol in the expansion set (IS-only choice)
pool, per_sym = [], {}
for _, c in cells.iterrows():
    if per_sym.get(c["symbol"], 0) >= 3:
        continue
    pool.append(c)
    per_sym[c["symbol"]] = per_sym.get(c["symbol"], 0) + 1
    if len(pool) == 90:
        break
top_cells = pd.DataFrame(pool)
print(f"cells: {len(cells)} with n_qual>=3, pool {len(top_cells)} cells "
      f"across {top_cells['symbol'].nunique()} symbols -> full grid")

# ------------------------------------------------- full-grid expansion -------
_mask_cache: dict = {}


def build_signals(cell):
    sym, tf = cell["symbol"], cell["tf"]
    fam = classify_family(sym)
    d = load_symbol(tf, sym)
    sides, setups, rds, sok, rok = precompute_pair(
        d, int(cell["fast"]), int(cell["slow"]), cell["reject"], cell["pattern"])
    if cell["overlay"] != "none":
        drivers = {}
        if fam == "gold":
            s = driver_slope_series(tf, "XAUUSD.pi")
            if s is not None:
                drivers["xau_drv"] = s
        elif fam == "jpy":
            s = driver_slope_series(tf, "USDJPY.pi")
            if s is not None:
                drivers["usdjpy_drv"] = s
        masks = confluence_masks(d, sym, tf, drivers)
        if cell["overlay"] not in masks:
            return None
        ml, ms = masks[cell["overlay"]]
        sides, rds = apply_overlay(sides, rds, ml, ms)
    return d, sides, setups, rds, sok, rok


grid_rows = []
for _, cell in top_cells.iterrows():
    try:
        built = build_signals(cell)
    except Exception:
        continue
    if built is None:
        continue
    d, sides, setups, rds, sok, rok = built
    for slope, regime in GATES:
        s2, r2 = apply_gates(sides, rds, sok, rok, slope, regime)
        if (s2 != 0).sum() == 0:
            continue
        for exit_mode in EXITS:
            tr = simulate(d, s2, setups, r2, cell["symbol"], cell["tf"],
                          SimCfg(exit_mode=exit_mode))
            if not len(tr):
                continue
            is_t, oos_t = split_is_oos(tr)
            mi, mo = metrics(is_t), metrics(oos_t)
            grid_rows.append({**{k: cell[k] for k in key}, "slope": slope,
                              "regime": regime, "exit": exit_mode,
                              "is_n": mi["n"], "is_r": mi["total_r"], "is_pf": mi["pf"],
                              "is_t": mi["t_stat"],
                              "oos_n": mo["n"], "oos_r": mo["total_r"], "oos_pf": mo["pf"]})
grid = pd.DataFrame(grid_rows)
print(f"full grid rows: {len(grid)}")

# ------------------------------------------------------- plateau picks -------
gqual = grid[(grid["is_n"] >= MIN_N) & (grid["is_pf"] >= MIN_PF) & (grid["is_r"] > 0)]
picks = []
for _, cell in top_cells.iterrows():
    members = gqual[(gqual[key] == cell[key]).all(axis=1)] if len(gqual) else pd.DataFrame()
    if not len(members):
        continue
    med = members.sort_values("is_r").iloc[len(members) // 2]
    if med["is_n"] < 20:  # baseline-consistent minimum sample for the pick itself
        continue
    picks.append({**{k: cell[k] for k in key}, "family": classify_family(cell["symbol"]),
                  "n_qual": int(cell["n_qual"]),
                  "slope": bool(med["slope"]), "regime": bool(med["regime"]),
                  "exit": med["exit"], "pick_is_r": med["is_r"], "pick_is_pf": med["is_pf"],
                  "pick_is_n": int(med["is_n"]), "pick_oos_r": med["oos_r"],
                  "pick_oos_pf": med["oos_pf"], "pick_oos_n": int(med["oos_n"])})
picks = pd.DataFrame(picks).sort_values("pick_is_r", ascending=False)
print(f"plateau picks: {len(picks)}")


def make_basket(picks, family_cap=None, size=15):
    out_rows, fam_count, syms = [], {}, set()
    for _, p in picks.iterrows():
        f = p["family"]
        if p["symbol"] in syms:
            continue
        if family_cap and fam_count.get(f, 0) >= family_cap:
            continue
        out_rows.append(p)
        syms.add(p["symbol"])
        fam_count[f] = fam_count.get(f, 0) + 1
        if len(out_rows) == size:
            break
    return pd.DataFrame(out_rows)


conc = make_basket(picks, family_cap=None)
div = make_basket(picks, family_cap=3)
print(f"\nCONCENTRATED basket ({len(conc)}):")
print(conc[["symbol", "tf", "fast", "slow", "reject", "pattern", "overlay", "exit",
            "pick_is_pf", "pick_is_n", "pick_oos_pf"]].round(2).to_string(index=False))
print(f"\nDIVERSIFIED basket ({len(div)}):")
print(div[["symbol", "tf", "fast", "slow", "reject", "pattern", "overlay", "exit",
           "pick_is_pf", "pick_is_n", "pick_oos_pf"]].round(2).to_string(index=False))

# ------------------------------------------------------------ ledgers --------
def build_ledger(basket, tag):
    ledgers = []
    for _, p in basket.iterrows():
        cell = {k: p[k] for k in key}
        try:
            built = build_signals(cell)
        except Exception:
            continue
        if built is None:
            continue
        d, sides, setups, rds, sok, rok = built
        s2, r2 = apply_gates(sides, rds, sok, rok, bool(p["slope"]), bool(p["regime"]))
        for cmult, ctag in [(1.0, "x1"), (1.5, "x1.5"), (2.0, "x2")]:
            tr = simulate(d, s2, setups, r2, p["symbol"], p["tf"],
                          SimCfg(exit_mode=p["exit"], cost_mult=cmult)).copy()
            tr["member"] = (f"{p['symbol']}|{p['tf']}|EMA{p['fast']}/{p['slow']}|"
                            f"{p['reject']}|{p['pattern']}|{p['overlay']}|{p['exit']}")
            tr["cost_mult"] = ctag
            ledgers.append(tr)
    led = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame()
    led.to_parquet(os.path.join(OUT, f"basket_ledgers_{tag}.parquet"))
    basket.to_csv(os.path.join(OUT, f"basket_picks_{tag}.csv"), index=False)
    return led


def battery(led, name, summary):
    print(f"\n===== {name} =====")
    for ctag in ["x1", "x1.5", "x2"]:
        t = led[led["cost_mult"] == ctag]
        is_t, oos_t = split_is_oos(t)
        mi, mo, ma = metrics(is_t), metrics(oos_t), metrics(t)
        print(f"[{ctag}] all n={ma['n']} R={ma['total_r']:.1f} PF={ma['pf']:.2f} "
              f"DD={ma['max_dd_r']:.1f} | IS n={mi['n']} R={mi['total_r']:.1f} PF={mi['pf']:.2f} | "
              f"OOS n={mo['n']} R={mo['total_r']:.1f} PF={mo['pf']:.2f} DD={mo['max_dd_r']:.1f}")
        if ctag == "x1":
            t = t.copy()
            r = t["r_net"].to_numpy(float)
            mc, bb = mc_iid(r), block_bootstrap(r)
            th = rolling_thirds(t)
            ds = dsr(r, n_trials=n_trials)
            print("   MC  :", {k: round(v, 3) for k, v in mc.items()})
            print("   BB  :", {k: round(v, 3) for k, v in bb.items()})
            print("   3rds:", [(x["third"], round(x["total_r"], 1), round(x["pf"], 2)) for x in th])
            print("   DSR :", {k: round(v, 3) for k, v in ds.items()}, f"(trials={n_trials})")
            t["month"] = pd.to_datetime(t["entry_time"]).dt.strftime("%Y-%m")
            monthly = t.groupby("month")["r_net"].agg(["sum", "size"])
            print("   monthly R:", {m: round(v, 1) for m, v in monthly["sum"].items()})
            summary[name] = {"all": {k: round(v, 2) for k, v in ma.items()},
                             "is": {k: round(v, 2) for k, v in mi.items()},
                             "oos": {k: round(v, 2) for k, v in mo.items()},
                             "mc": {k: round(v, 3) for k, v in mc.items()},
                             "bb": {k: round(v, 3) for k, v in bb.items()},
                             "thirds": [(x["third"], round(x["total_r"], 1), round(x["pf"], 2)) for x in th],
                             "dsr": {k: round(v, 4) for k, v in ds.items()},
                             "monthly_r": {m: round(v, 2) for m, v in monthly["sum"].items()},
                             "n_members": int(led["member"].nunique())}


summary = {"n_trials": int(n_trials)}
led_c = build_ledger(conc, "conc")
led_d = build_ledger(div, "div")
battery(led_c, "CONCENTRATED", summary)
battery(led_d, "DIVERSIFIED", summary)

# ------------------------------------------------- baseline comparison -------
base = pd.read_parquet("/home/z/my-project/htf_results/basket_ledgers.parquet")
bt = base[base["cost_mult"] == "x1"]
bi, bo = split_is_oos(bt)
print("\n===== BASELINE (89/233, cost x1) =====")
print(f"all n={metrics(bt)['n']} R={metrics(bt)['total_r']:.1f} PF={metrics(bt)['pf']:.2f} | "
      f"IS n={metrics(bi)['n']} R={metrics(bi)['total_r']:.1f} PF={metrics(bi)['pf']:.2f} | "
      f"OOS n={metrics(bo)['n']} R={metrics(bo)['total_r']:.1f} PF={metrics(bo)['pf']:.2f}")
summary["baseline"] = {"all": {k: round(v, 2) for k, v in metrics(bt).items()},
                       "is": {k: round(v, 2) for k, v in metrics(bi).items()},
                       "oos": {k: round(v, 2) for k, v in metrics(bo).items()}}

with open(os.path.join(OUT, "stage_c_summary.json"), "w") as f:
    json.dump(summary, f, indent=1, default=str)
print("\nsaved stage_c_summary.json")
