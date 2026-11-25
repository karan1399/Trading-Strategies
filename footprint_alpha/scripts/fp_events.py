#!/usr/bin/env python3
"""Reverse-engineering the 'auction complete' (0-print at extreme) - v2.

Multi-direction, fee-aware exploration on ~11 months of BTCUSDT footprint:
  variants : strict 0-print / weak (<=5% counter-flow at extreme) /
             z2 (zero counter-flow aggregated over top-2 levels) /
             mirror (buyers still paying the offer at the high AND close near
             high -> CONTINUATION side, the inverse hypothesis)
  entries  : market at next open (taker 5bps/side) vs LIMIT retest at the
             signal-bar extreme (maker 2bps/side, fill within 6 bars)
  grid     : sl_mult x {1,2,3} x horizon {12,48} bars, rr fixed 1.5
Every evaluated configuration is counted for the deflated-Sharpe adjustment.
Writes footprint/results/fp_events.json + console tables.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fp_core import (BASE, FEE_BPS_SIDE, FEE_MAKER_BPS, load_all,  # noqa: E402
                     edges_wide, grid_feats_stream, build_features, label_events)
RES = os.path.join(BASE, "results")
os.makedirs(RES, exist_ok=True)
FEATS_CACHE = os.path.join(BASE, "processed", "feats_all.parquet")
RR = 1.5
TRIALS = 0  # evaluated (config, metric) combos -> deflated-Sharpe input


def fee_bps(entry_mode):
    return FEE_BPS_SIDE if entry_mode == "market" else FEE_MAKER_BPS


def eval_config(feats, bars, mask, side_arr, entry, sl_mult, horizon, name):
    global TRIALS
    TRIALS += 1
    f2 = feats.copy()
    f2["side"] = side_arr
    ev = label_events(f2, bars, mask & (side_arr != 0), rr=RR,
                      horizon=horizon, sl_mult=sl_mult, entry=entry)
    n_events = int((mask & (side_arr != 0)).sum())
    if len(ev) == 0:
        return {"n": 0, "n_events": n_events}
    r = ev.exit_r.to_numpy()
    wins, losses = r[r > 0], r[r <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() < 0 else float("inf")
    fee_r = 2 * fee_bps(entry) * 1e-4 * ev.entry.to_numpy() / ev.sl_dist.to_numpy()
    t = float(r.mean() / (r.std() / np.sqrt(len(r)))) if len(r) > 2 else 0.0
    return {"n": int(len(ev)), "n_events": n_events,
            "fill_rate": len(ev) / max(n_events, 1),
            "win_rate": float((r > 0).mean()), "mean_r": float(r.mean()),
            "pf": pf, "t": t,
            "avg_fee_r": float(fee_r.mean()),
            "fee_adj_mean_r": float((r - fee_r).mean()),
            "sharpe_trade": float(r.mean() / r.std()) if len(r) > 2 else 0.0}


def main():
    bars, edges, _, days = load_all(load_grid=False)
    ew = edges_wide(edges)
    gf = grid_feats_stream(bars, days)
    feats = build_features(bars, ew, gf)
    feats.to_parquet(FEATS_CACHE)
    c = feats.close.to_numpy()
    n = len(c)
    print(f"bars={n} days={len(days)} span {days[0]}..{days[-1]}")

    cpos = feats.cpos.to_numpy()
    zt, zb = feats.zero_top.to_numpy(bool), feats.zero_bot.to_numpy(bool)
    wt, wb = feats.weak_top.to_numpy(bool), feats.weak_bot.to_numpy(bool)
    z2t, z2b = feats.zt2.to_numpy(bool), feats.zb2.to_numpy(bool)
    ut = feats.unf_strong_top.to_numpy(bool)
    ub = feats.unf_strong_bot.to_numpy(bool)

    def mk_side(pos, neg, both_rule="reversal"):
        s = np.zeros(n, np.int8)
        s[pos & ~neg] = 1
        s[neg & ~pos] = -1
        both = pos & neg
        if both_rule == "reversal":     # closed near high -> SELL (fresh top)
            s[both] = np.where(cpos[both] > 0.5, -1, 1)
        else:                           # mirror: closed near high -> LONG
            s[both] = np.where(cpos[both] > 0.5, 1, -1)
        return s

    variants = {
        "strict": (zt | zb, mk_side(zb, zt)),      # 0@top -> SELL, 0@bot -> BUY
        "weak": (wt | wb, mk_side(wb, wt)),
        "z2": (z2t | z2b, mk_side(z2b, z2t)),
        "mirror": (ut | ub, mk_side(ut, ub, "continuation")),  # CONTINUATION side
    }

    print("\n=== base rates ===")
    for name, (m, s) in variants.items():
        print(f"{name:<8} events={int((m & (s != 0)).sum()):>6} "
              f"({(m & (s != 0)).mean()*100:5.2f}% of bars)  "
              f"long={int((m & (s == 1)).sum())} short={int((m & (s == -1)).sum())}")

    print("\n=== forward close-close returns (side-adjusted, bps) ===")
    fwd = {}
    for name, (m, s) in variants.items():
        idx = np.where(m & (s != 0) & (np.arange(n) < n - 25))[0]
        row = {}
        for h in (1, 3, 6, 12, 24):
            v = (c[idx + h] / c[idx] - 1) * np.where(s[idx] == 1, 1.0, -1.0)
            row[h] = (float(v.mean() * 1e4),
                      float(v.mean() / (v.std() / np.sqrt(len(v)))))
        fwd[name] = row
        print(f"{name:<8} n={len(idx):>6}  " + "  ".join(
            f"h{h}: {row[h][0]:+.1f} (t={row[h][1]:+.1f})" for h in row))

    print("\n=== barrier grid (pessimistic, rr=1.5) ===")
    results = {}
    for name, (m, s) in variants.items():
        for entry in ("market", "limit"):
            for slm in (1.0, 2.0, 3.0):
                for hz in (12, 48):
                    r = eval_config(feats, bars, m, s, entry, slm, hz, name)
                    key = f"{name}|{entry}|sl{int(slm)}|h{hz}"
                    results[key] = r
                    print(f"{name:<8} {entry:<6} sl={int(slm)}x h={hz:<3} "
                          f"n={r['n']:>5} fill={r.get('fill_rate',1)*100:4.0f}% "
                          f"win={r.get('win_rate',0)*100:5.1f}% "
                          f"meanR={r.get('mean_r',0):+.3f} "
                          f"feeAdj={r.get('fee_adj_mean_r',0):+.3f} "
                          f"PF={r.get('pf',0):5.2f} t={r.get('t',0):+.1f}")

    print(f"\nTRIALS evaluated: {TRIALS} (deflated-Sharpe adjustment input)")
    ranked = sorted((k for k in results if results[k].get("n", 0) > 200),
                    key=lambda k: results[k]["fee_adj_mean_r"], reverse=True)
    print("\n=== top 5 fee-viable configs: IS (70%) vs OOS (30%) ===")
    n_is = int(n * 0.7)
    summary_top = {}
    for k in ranked[:5]:
        parts = k.split("|")
        name, entry = parts[0], parts[1]
        slm_f, hz_i = float(parts[2][2:]), int(parts[3][1:])
        m, s = variants[name]
        fis = feats.iloc[:n_is]; bis = bars.iloc[:n_is]
        roos_f = feats.iloc[n_is:]; roos_b = bars.iloc[n_is:]
        ris = eval_config(fis, bis, m[:n_is], s[:n_is], entry, slm_f, hz_i, name)
        roos = eval_config(roos_f, roos_b, m[n_is:], s[n_is:], entry, slm_f, hz_i, name)
        summary_top[k] = {"is": ris, "oos": roos}
        print(f"{k:<26} IS  n={ris['n']:>4} feeAdj={ris.get('fee_adj_mean_r',0):+.3f} "
              f"PF={ris.get('pf',0):.2f}   |  OOS n={roos.get('n',0):>4} "
              f"win={roos.get('win_rate',0)*100:5.1f}% "
              f"feeAdj={roos.get('fee_adj_mean_r',0):+.3f} PF={roos.get('pf',0):.2f}")

    out = {"bars": n, "days": len(days), "rr": RR, "trials": TRIALS,
           "base_rates": {k: int((v[0] & (v[1] != 0)).sum())
                          for k, v in variants.items()},
           "forward_bps": fwd, "grid": results, "top_configs": summary_top}
    with open(os.path.join(RES, "fp_events.json"), "w") as f:
        json.dump(out, f, indent=1, default=float)
    print("\nsaved fp_events.json")


if __name__ == "__main__":
    main()
