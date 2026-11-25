#!/usr/bin/env python3
"""Quant-desk ML trainer for the auction-complete signal.

Walk-forward purged CV on ~11 months of BTCUSDT footprint events:
  - LightGBM binary classifier: P(trade wins) per event
  - folds roll every ~54 days, train = all history before test start minus
    a 2-day embargo (labels look forward <=54 bars)
  - isotonic calibration fitted on out-of-fold probabilities of folds 1..4
  - probability threshold chosen on folds 1..4 fee-adjusted expectancy
  - fold 5 (final ~50 days) is NEVER used for any selection: it evaluates
    the frozen threshold + calibration
Artifacts -> footprint/model/ (lgbm txt, isotonic pkl, meta.json, oof.parquet)
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import joblib  # noqa: E402
import lightgbm as lgb  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402

from fp_core import (BASE, FEE_BPS_SIDE, FEE_MAKER_BPS, load_all,  # noqa: E402
                     edges_wide, grid_feats, build_features, label_events)

RES = os.path.join(BASE, "results")
MODEL_DIR = os.path.join(BASE, "model")
os.makedirs(RES, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)

FEATS_CACHE = os.path.join(BASE, "processed", "feats_all.parquet")

FEATURES = ["side", "t1_buy_sh", "t1_vol", "b1_buy_sh", "t13_buy_sh",
            "b13_sell_sh", "edge_ratio_top", "delta", "body", "cpos",
            "upwick", "lowick", "range_atr", "range_med", "n_tr_log",
            "vol_med", "atr_pct", "trend_f", "trend_s", "mom6", "mom24",
            "pos24", "hr_sin", "hr_cos", "poc_pos", "poc_delta", "d_conc3",
            "hi_buy_sh", "hi_sell_sh", "lo_buy_sh", "lo_sell_sh", "hi_d",
            "lo_d", "n_levels", "max_lvl_sh"]

RR = 1.5  # overwritten in main() from argv[5]
LGB_PARAMS = dict(objective="binary", num_leaves=31, learning_rate=0.05,
                  min_child_samples=40, feature_fraction=0.8,
                  bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
                  verbosity=-1, seed=7)


def fee_bps(entry_mode):
    return FEE_BPS_SIDE if entry_mode == "market" else FEE_MAKER_BPS


def build_dataset(variant="weak", entry="market", sl_mult=2.0, horizon=48):
    feats = pd.read_parquet(FEATS_CACHE)
    feats["bar_g"] = feats.index          # keep index as column
    feats = feats.reset_index(drop=True)
    bars, _, _, days = load_all(load_grid=False)
    n = len(feats)
    cpos = feats.cpos.to_numpy()
    if variant == "strict":
        pos, neg = feats.zero_bot.to_numpy(bool), feats.zero_top.to_numpy(bool)
    elif variant == "z2":
        pos, neg = feats.zb2.to_numpy(bool), feats.zt2.to_numpy(bool)
    else:
        pos, neg = feats.weak_bot.to_numpy(bool), feats.weak_top.to_numpy(bool)
    side = np.zeros(n, np.int8)
    side[pos & ~neg] = 1
    side[neg & ~pos] = -1
    both = pos & neg
    side[both] = np.where(cpos[both] > 0.5, -1, 1)
    mask = (pos | neg)
    f2 = feats.copy()
    f2["side"] = side
    ev = label_events(f2, bars, mask & (side != 0), rr=RR, horizon=horizon,
                      sl_mult=sl_mult, entry=entry)
    ev = ev[FEATURES + ["win", "exit_r", "n_bars", "entry", "sl_dist",
                        "ts", "bar_g"]].copy()  # 'side' already in FEATURES
    ev["fee_r"] = 2 * fee_bps(entry) * 1e-4 * ev.entry / ev.sl_dist
    return ev, days, n


def fit_fold(tr_x, tr_y, va_x, va_y, seed=7):
    p = dict(LGB_PARAMS, seed=seed)
    dtr = lgb.Dataset(tr_x, label=tr_y)
    dva = lgb.Dataset(va_x, label=va_y, reference=dtr)
    m = lgb.train(p, dtr, num_boost_round=600, valid_sets=[dva],
                  callbacks=[lgb.early_stopping(60, verbose=False)])
    return m


def metrics(y, p, r, fee_r):
    out = {}
    for thr in (0.45, 0.5, 0.55, 0.6, 0.65):
        m = p >= thr
        if m.sum() < 30:
            continue
        out[thr] = {"n": int(m.sum()), "win": float(y[m].mean()),
                    "mean_r": float(r[m].mean()),
                    "fee_adj": float((r[m] - fee_r[m]).mean())}
    return out


def main():
    variant = sys.argv[1] if len(sys.argv) > 1 else "weak"
    entry = sys.argv[2] if len(sys.argv) > 2 else "limit"
    sl_mult = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    horizon = int(sys.argv[4]) if len(sys.argv) > 4 else 48
    global RR
    RR = float(sys.argv[5]) if len(sys.argv) > 5 else 1.5
    ev, days, n_bars = build_dataset(variant, entry, sl_mult, horizon)
    print(f"dataset: variant={variant} entry={entry} sl={sl_mult}x h={horizon} "
          f"events={len(ev)} win_rate={ev.win.mean()*100:.1f}% "
          f"fee_adj_mean_r={(ev.exit_r - ev.fee_r).mean():+.3f}")

    ev["day_idx"] = ev.ts.dt.floor("D").rank(method="dense").astype(int) - 1
    n_days = ev.day_idx.max() + 1
    fold_starts = list(range(60, n_days, 54))
    folds = []
    for i, s in enumerate(fold_starts):
        e = fold_starts[i + 1] if i + 1 < len(fold_starts) else n_days
        folds.append((s, e))
    print(f"walk-forward: {len(folds)} folds over {n_days} days; "
          f"final untouched fold = days [{folds[-1][0]},{folds[-1][1]})")

    oof_rows = []
    for fi, (s, e) in enumerate(folds):
        tr = ev[ev.day_idx < s - 2]          # 2-day embargo
        te = ev[(ev.day_idx >= s) & (ev.day_idx < e)]
        if len(tr) < 300 or len(te) < 40:
            continue
        va_mask = tr.day_idx >= (s - 2 - 30)  # internal early-stop window
        m = fit_fold(tr[~va_mask][FEATURES].to_numpy(),
                     tr[~va_mask].win.to_numpy(),
                     tr[va_mask][FEATURES].to_numpy(),
                     tr[va_mask].win.to_numpy())
        p = m.predict(te[FEATURES].to_numpy(), num_iteration=m.best_iteration)
        te = te.copy()
        te["p"] = p
        oof_rows.append(te)
        mt = metrics(te.win.to_numpy(), p, te.exit_r.to_numpy(), te.fee_r.to_numpy())
        print(f"fold{fi} days[{s},{e}) train={len(tr):>5} test={len(te):>4} "
              f"auc-ish-P(above) best_thr="
              + " ".join(f"{t}:{v['fee_adj']:+.2f}(n{v['n']})" for t, v in mt.items()))
        m.save_model(os.path.join(MODEL_DIR, f"lgbm_fold{fi}.txt"))
    oof = pd.concat(oof_rows, ignore_index=True)

    # ---- calibration + threshold from folds 0..n-2 (all but final fold)
    sel = oof[oof.day_idx < folds[-1][0]]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(sel.p.to_numpy(), sel.win.to_numpy())
    best_thr, best_val = 0.5, -9.9
    for thr in np.arange(0.40, 0.70, 0.01):
        m = sel.p.to_numpy() >= thr
        if m.sum() < max(150, 0.15 * len(sel)):
            continue
        v = (sel.exit_r.to_numpy()[m] - sel.fee_r.to_numpy()[m]).mean()
        if v > best_val:
            best_val, best_thr = v, thr
    # final fold: frozen threshold + calibration, NOTHING was tuned on it
    fin = oof[oof.day_idx >= folds[-1][0]]
    mf = fin.p.to_numpy() >= best_thr
    fin_adj = (fin.exit_r.to_numpy() - fin.fee_r.to_numpy())
    print(f"\nselected on folds 0..{len(folds)-2}: thr={best_thr:.2f} "
          f"fee_adj={best_val:+.3f}")
    print(f"FINAL fold ({len(fin)} events): taken={int(mf.sum())} "
          f"win={fin.win.to_numpy()[mf].mean()*100 if mf.sum() else 0:.1f}% "
          f"raw_mean_r={fin.exit_r.to_numpy()[mf].mean() if mf.sum() else 0:+.3f} "
          f"fee_adj_mean_r={fin_adj[mf].mean() if mf.sum() else 0:+.3f}")

    oof.to_parquet(os.path.join(RES, "oof.parquet"))
    joblib.dump(iso, os.path.join(MODEL_DIR, "isotonic.pkl"))
    meta = {"variant": variant, "entry": entry, "sl_mult": sl_mult,
            "horizon": horizon, "rr": RR, "features": FEATURES,
            "threshold": float(best_thr), "sel_fee_adj": float(best_val),
            "final": {"n_events": int(len(fin)), "n_taken": int(mf.sum()),
                      "win_rate": float(fin.win.to_numpy()[mf].mean()) if mf.sum() else None,
                      "mean_r": float(fin.exit_r.to_numpy()[mf].mean()) if mf.sum() else None,
                      "fee_adj_mean_r": float(fin_adj[mf].mean()) if mf.sum() else None},
            "n_events_total": int(len(ev)),
            "win_rate_all": float(ev.win.mean())}
    with open(os.path.join(MODEL_DIR, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    # refit final deployment model on ALL events (used by the watcher; its
    # honest performance is the final-fold numbers above)
    m_all = fit_fold(ev[FEATURES].to_numpy(), ev.win.to_numpy(),
                     ev[FEATURES].to_numpy()[:500], ev.win.to_numpy()[:500])
    m_all.save_model(os.path.join(MODEL_DIR, "lgbm_deploy.txt"))
    print("saved model artifacts to", MODEL_DIR)


if __name__ == "__main__":
    main()
