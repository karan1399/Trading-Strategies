#!/usr/bin/env python3
"""RR sweep for the ML base config: with sl=3x bar range and market entry,
higher RR lowers the fee-adjusted breakeven win rate. Finds the config with
the best 'lift feasibility' = base win rate minus breakeven, for the ML to
exploit. Adds to the trials count for deflated-Sharpe honesty."""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fp_core import (BASE, FEE_BPS_SIDE, load_all, label_events)  # noqa: E402

RES = os.path.join(BASE, "results")
FEATS_CACHE = os.path.join(BASE, "processed", "feats_all.parquet")


def eval_cfg(feats, bars, pos, neg, rr, slm, hz, n_is=None):
    n = len(feats)
    cpos = feats.cpos.to_numpy()
    side = np.zeros(n, np.int8)
    side[pos & ~neg] = 1
    side[neg & ~pos] = -1
    both = pos & neg
    side[both] = np.where(cpos[both] > 0.5, -1, 1)
    f2 = feats.copy()
    f2["side"] = side
    m = (pos | neg) & (side != 0)
    if n_is is not None:
        m = m.copy(); m[n_is:] = False  # IS only for selection
    ev = label_events(f2, bars, m, rr=rr, horizon=hz, sl_mult=slm,
                      entry="market")
    r = ev.exit_r.to_numpy()
    fee_r = 2 * FEE_BPS_SIDE * 1e-4 * ev.entry.to_numpy() / ev.sl_dist.to_numpy()
    adj = r - fee_r
    wins, losses = r[r > 0], r[r <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) else np.inf
    be = 1 / (1 + rr) + float(fee_r.mean()) / rr  # fee-adj breakeven win rate
    return {"n": int(len(ev)), "win": float((r > 0).mean()), "mean_r": float(r.mean()),
            "fee_adj": float(adj.mean()), "pf": pf,
            "be_p": float(be), "gap": float((r > 0).mean() - be),
            "avg_fee_r": float(fee_r.mean())}


def main():
    feats = pd.read_parquet(FEATS_CACHE).reset_index(drop=True)
    bars, _, _, days = load_all(load_grid=False)
    assert len(feats) == len(bars)
    n = len(feats)
    n_is = int(n * 0.7)
    pos_s, neg_s = feats.zero_bot.to_numpy(bool), feats.zero_top.to_numpy(bool)
    pos_z, neg_z = feats.zb2.to_numpy(bool), feats.zt2.to_numpy(bool)
    pos_w, neg_w = feats.weak_bot.to_numpy(bool), feats.weak_top.to_numpy(bool)
    rows, trials = [], 0
    print("config                          n    win%   meanR  feeAdj  PF    be%   gap(pp)")
    for nm, pos, neg in (("strict", pos_s, neg_s), ("z2", pos_z, neg_z),
                         ("weak", pos_w, neg_w)):
        for rr in (2.0, 3.0, 4.0):
            for slm, hz in ((3.0, 48), (3.0, 96), (4.0, 96)):
                trials += 1
                r = eval_cfg(feats, bars, pos, neg, rr, slm, hz, n_is=n_is)
                rows.append({**r, "cfg": f"{nm}|mkt|sl{int(slm)}|rr{rr}|h{hz}"})
                print(f"{nm} mkt sl={int(slm)} rr={rr} h={hz:<3} IS "
                      f"n={r['n']:>5} win={r['win']*100:5.1f} "
                      f"meanR={r['mean_r']:+.3f} feeAdj={r['fee_adj']:+.3f} "
                      f"PF={r['pf']:4.2f} be={r['be_p']*100:5.1f} "
                      f"gap={r['gap']*100:+5.1f}")
    best = max(rows, key=lambda x: x["gap"])
    print(f"\ntrials in sweep: {trials}")
    print(f"best lift-feasibility (IS): {best['cfg']} gap={best['gap']*100:+.1f}pp "
          f"(win {best['win']*100:.1f}% vs breakeven {best['be_p']*100:.1f}%)")
    with open(os.path.join(RES, "fp_rr_sweep.json"), "w") as f:
        json.dump({"rows": rows, "trials": trials, "best": best}, f, indent=1,
                  default=float)
    print("saved fp_rr_sweep.json")


if __name__ == "__main__":
    main()
