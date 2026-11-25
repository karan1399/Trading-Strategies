#!/usr/bin/env python3
"""Alpha360 daily signal watcher -> MT5 order tickets.

Run locally from the alpha360_ema_rejection/ folder AFTER refreshing the
parquet store (repo scripts/ops/dump_mt5_parquet.py). It rebuilds each
DIVERSIFIED member's signals on the latest data with the exact backtest
semantics (closed-bar signals only) and prints:

  - an ORDER TICKET for every signal on the last closed bar (and any bar
    within --bars N), with SL/TP prices and position size
  - WATCH ZONES (current fast/slow EMA values) for members without a signal

Env: ALPHA360_RISK_USD (default 25), ALPHA360_PARQUET_ROOT (default
../historical_parquet). No orders are sent - this prints tickets for MANUAL
MT5 execution.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)) or ".")

from dfib_engine import load_symbol, wilder_atr, ema, metrics  # noqa: E402
from alpha360_engine import precompute_pair, apply_gates  # noqa: E402
from alpha360_common import (classify_family, confluence_masks,  # noqa: E402
                             driver_slope_series, apply_overlay)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PARQUET_ROOT = os.environ.get("ALPHA360_PARQUET_ROOT") or os.path.join(REPO, "historical_parquet")
RISK_USD = float(os.environ.get("ALPHA360_RISK_USD", 25.0))
LOOKBACK = int(sys.argv[sys.argv.index("--bars") + 1]) if "--bars" in sys.argv else 1
BASKET = "basket_picks_conc.csv" if "--all" in sys.argv else "basket_picks_div.csv"

FAM_CONTRACT = {"gold": 100.0, "metals_other": 5000.0, "jpy": 100000.0, "crypto": 1.0,
                "usd_major": 100000.0, "energy": 1000.0, "index": 10.0, "fx_cross": 100000.0}
SPEC = {}
_uni = os.path.join(REPO, "desk", "mt5_symbols_universe.json")
if os.path.exists(_uni):
    for _cat, _lst in json.load(open(_uni)).items():
        SPEC.update({s["name"]: s for s in _lst})
QUOTE_SYM = {"USD": None, "AUD": "AUDUSD", "CAD": "USDCAD", "CHF": "USDCHF",
             "CNH": "USDCNH", "EUR": "EURUSD", "GBP": "GBPUSD", "HKD": "USDHKD",
             "JPY": "USDJPY", "NOK": "USDNOK", "NZD": "NZDUSD", "SEK": "USDSEK",
             "SGD": "USDSGD"}
INVERTED = {"CAD", "CHF", "CNH", "HKD", "JPY", "NOK", "SEK", "SGD"}


def quote_of(sym):
    base = sym.upper().split(".")[0]
    return "USD" if sym.endswith(".p") else (base[3:6] if len(base) == 6 else "USD")


def quote_to_usd(qc):
    if qc == "USD":
        return 1.0
    psym = QUOTE_SYM.get(qc)
    if not psym:
        return 1.0
    try:
        d = load_symbol("4H", psym + ".pi")
        px = float(d["close"].iloc[-1])
        return (1.0 / px) if qc in INVERTED else px
    except Exception:
        return 1.0


def lots_for(sym, sl_dist, fam):
    spec = SPEC.get(sym, {})
    contract = float(spec.get("trade_contract_size") or 0) or FAM_CONTRACT.get(fam, 100000.0)
    q2u = quote_to_usd(quote_of(sym))
    raw = RISK_USD / (sl_dist * contract * q2u) if sl_dist > 0 else 0.0
    vmin = float(spec.get("volume_min") or 0.01)
    vstep = float(spec.get("volume_step") or 0.01)
    return max(vmin, round(raw / vstep) * vstep), contract, vmin, vstep


def exit_plan(exit_mode, side, entry, rds):
    if exit_mode.startswith("fixed"):
        rr = float(exit_mode.split("_")[1].rstrip("r"))
        tp = entry + rr * rds if side == 1 else entry - rr * rds
        return round(tp, 5), f"TP {rr:.1f}R"
    step = float(exit_mode.split("_")[1].rstrip("r"))
    return None, f"no fixed TP - trail {step:.1f}R step from +1.5R, stop to BE at +1R"


def pick_path(name):
    for c in [os.path.join(HERE, name), os.path.join(HERE, "results", name)]:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"{name} not found (run alpha360_stage_c.py first)")


picks = pd.read_csv(pick_path(BASKET))
tickets, watches = [], []
for _, p in picks.iterrows():
    sym, tf = p["symbol"], p["tf"]
    try:
        d = load_symbol(tf, sym)
    except Exception as e:
        watches.append((sym, tf, f"DATA MISSING ({e})"))
        continue
    sides, setups, rds, sok, rok = precompute_pair(d, int(p["fast"]), int(p["slow"]),
                                                   p["reject"], p["pattern"])
    if p["overlay"] and p["overlay"] != "none":
        fam = classify_family(sym)
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
        if p["overlay"] in masks:
            ml, ms = masks[p["overlay"]]
            sides, rds = apply_overlay(sides, rds, ml, ms)
    s2, r2 = apply_gates(sides, rds, sok, rok, bool(p["slope"]), bool(p["regime"]))

    n = len(s2)
    last_i = n - 1
    hit = None
    for i in range(last_i, max(last_i - LOOKBACK - 1, 0), -1):
        if s2[i] != 0:
            hit = i
            break
    o = d["open"].to_numpy(float); h = d["high"].to_numpy(float)
    l = d["low"].to_numpy(float); c = d["close"].to_numpy(float)
    atr = wilder_atr(h, l, c, 14)
    ef, es = ema(c, int(p["fast"])), ema(c, int(p["slow"]))
    i = last_i
    zone = f"{min(ef[i], es[i]):.5g} - {max(ef[i], es[i]):.5g}"
    if hit is None:
        dist = (c[i] - (ef[i] + es[i]) / 2) / atr[i]
        watches.append((sym, tf, f"no signal; close {c[i]:.5g}, zone {zone} "
                                 f"({dist:+.1f} ATR from mid), ATR {atr[i]:.5g}"))
        continue
    side = int(s2[hit])
    entry_ref = float(c[last_i])  # enter at market on the next bar open
    rdist = float(r2[hit]) if r2[hit] > 0 else 1.2 * atr[last_i]
    sl = entry_ref - rdist if side == 1 else entry_ref + rdist
    tp, plan = exit_plan(p["exit"], side, entry_ref, rdist)
    lots, contract, vmin, vstep = lots_for(sym, rdist, classify_family(sym))
    tickets.append({
        "symbol": sym, "tf": tf, "action": "BUY" if side == 1 else "SELL",
        "pattern": f"{p['pattern']} @ EMA {p['fast']}/{p['slow']} ({p['reject']})"
                   + (f" + {p['overlay']}" if p["overlay"] != "none" else ""),
        "signal_bar": str(d.index[hit]), "entry": round(entry_ref, 5),
        "SL": round(sl, 5), "TP": (round(tp, 5) if tp else "-"),
        "risk_dist": round(rdist, 5), "lots": lots, "plan": plan,
        "contract": contract, "fresh_bars": last_i - hit,
    })

print(f"Alpha360 watcher - {len(picks)} members, last closed bar + {LOOKBACK} lookback, "
      f"risk ${RISK_USD:.0f}/trade")
print(f"parquet root: {PARQUET_ROOT}\n")
if tickets:
    for t in tickets:
        print("=" * 76)
        print(f"ORDER TICKET  {t['action']} {t['symbol']}  [{t['tf']}]  "
              f"(signal {t['signal_bar']}, {t['fresh_bars']} bar(s) old)")
        print(f"  setup   : {t['pattern']}")
        print(f"  entry   : at market on next bar open (~{t['entry']})")
        print(f"  stop    : {t['SL']}   ({t['risk_dist']} price risk)")
        print(f"  target  : {t['TP']}   ({t['plan']})")
        print(f"  size    : {t['lots']} lots  (contract {t['contract']:g}, "
              f"min/step from broker specs)")
else:
    print("No order tickets - no member has a signal in the lookback window.\n")
print("=" * 76)
print("WATCH LIST (no signal yet):")
for sym, tf, note in watches:
    print(f"  {sym:<14} {tf:<3} {note}")
print("\nRule: signals are valid on CLOSED bars only - never enter intra-bar. "
      "One position per member; skip if a same-family position is already open "
      "and the family cap (3) is reached.")
