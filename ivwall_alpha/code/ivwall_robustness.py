#!/usr/bin/env python3
"""Robustness battery for the FX IV-wall fade variants:
iid Monte Carlo, stationary block bootstrap, Deflated Sharpe Ratio,
rolling thirds, IS-selected symbol basket, cost stress, timeframe checks.
"""
import math
import multiprocessing as mp
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivwall_engine import Config, run_config, list_symbols, metrics
from ivwall_runner import classify

OUT = "/home/z/my-project/ivwall_results"
FX = [s for s in list_symbols("15M") if classify(s) == "fx"]
FX_1H = [s for s in list_symbols("1H") if classify(s) == "fx"]
FX_4H = [s for s in list_symbols("4H") if classify(s) == "fx"]
FX_5M = [s for s in list_symbols("5M") if classify(s) == "fx"]


def mc_iid(r: np.ndarray, n: int = 10000, seed: int = 7):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(r), size=(n, len(r)))
    samples = r[idx]
    totals = samples.sum(axis=1)
    eq = np.cumsum(samples, axis=1)
    dd = np.maximum.accumulate(eq, axis=1) - eq
    return {
        "p_profit": float((totals > 0).mean()),
        "total_ci5": float(np.percentile(totals, 5)),
        "total_ci95": float(np.percentile(totals, 95)),
        "dd95": float(np.percentile(dd[:, -1], 95)),
        "dd99": float(np.percentile(dd[:, -1], 99)),
    }


def block_bootstrap(r: np.ndarray, n: int = 5000, avg_block: int = 5, seed: int = 11):
    """Stationary bootstrap ( Politis-Romano ) on the trade sequence."""
    rng = np.random.default_rng(seed)
    T = len(r)
    p = 1.0 / avg_block
    totals, dds = [], []
    for _ in range(n):
        seq = []
        while len(seq) < T:
            start = rng.integers(0, T)
            ln = 1 + (rng.random() < p and rng.geometric(1 - p) - 1 or rng.geometric(1 - p) - 1)
            seq.extend(r[(start + i) % T] for i in range(ln))
        seq = np.array(seq[:T])
        eq = np.cumsum(seq)
        totals.append(eq[-1])
        dds.append((np.maximum.accumulate(eq) - eq).max())
    totals, dds = np.array(totals), np.array(dds)
    return {"p_profit": float((totals > 0).mean()), "total_ci5": float(np.percentile(totals, 5)),
            "total_ci95": float(np.percentile(totals, 95)), "dd95": float(np.percentile(dds, 95))}


def dsr(r: np.ndarray, n_trials: int, v_sr: float | None = None):
    """Bailey & Lopez de Prado deflated Sharpe ratio (per-trade SR).
    v_sr: empirical variance of per-trade SR across the trial configs."""
    T = len(r)
    sr = r.mean() / (r.std(ddof=1) + 1e-12)
    g3 = float(pd.Series(r).skew())
    g4 = float(pd.Series(r).kurt()) + 3.0
    emc = 0.5772156649  # Euler-Mascheroni
    sr0 = math.sqrt(v_sr if v_sr else 1.0) * (
        (1 - emc) * _z(1 - 1.0 / n_trials) + emc * _z(1 - 1.0 / (n_trials * math.e)))
    num = (sr - sr0) * math.sqrt(T - 1)
    den = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4.0 * sr * sr))
    return {"sr": float(sr), "sr0": float(sr0), "dsr_prob": float(_cdf(num / den)), "skew": g3, "kurt": g4 - 3.0}


def _z(p):
    from scipy.stats import norm
    return float(norm.ppf(max(min(p, 1 - 1e-9), 1e-9)))


def _cdf(x):
    from scipy.stats import norm
    return float(norm.cdf(x))


def rolling_thirds(tr: pd.DataFrame):
    tr = tr.sort_values("entry_time").reset_index(drop=True)
    out = []
    for i in range(3):
        seg = tr.iloc[len(tr) * i // 3: len(tr) * (i + 1) // 3]
        out.append(metrics(seg))
    return out


def run_cfg_on(tf, syms, **kw):
    cfg = Config(name="x", strategy="fade_touch", vol_estimator="ewma_cc",
                 target_mode="wall_m", min_rr=0.3, **kw)
    tr = run_config(tf, syms, cfg)
    if len(tr):
        tr["iclass"] = tr["symbol"].map(classify)
    return tr


def main():
    variants = {
        "C1_sd2_m0.5_b1": dict(sd_wall=2.0, target_m=0.5, buffer_atr=1.0),
        "C2_sd2.5_m0.5_b1": dict(sd_wall=2.5, target_m=0.5, buffer_atr=1.0),
        "C3_sd2_m1_b0.5": dict(sd_wall=2.0, target_m=1.0, buffer_atr=0.5),
    }
    rows = []
    for vn, kw in variants.items():
        f = os.path.join(OUT, f"trades_fadeB_{vn.replace('C','') if False else ''}")  # reuse saved
    # reload saved trades instead of re-running
    saved = {
        "C1_sd2_m0.5_b1": "fadeB_sd2_ewma_cc_m0.5_b1_none",
        "C2_sd2.5_m0.5_b1": "fadeB_sd2.5_ewma_cc_m0.5_b1_none",
        "C3_sd2_m1_b0.5": "fadeB_sd2_ewma_cc_m1_b0.5_none",
    }
    # empirical variance of per-trade SR across the 96 tried configs (honest deflation)
    import glob
    srs = []
    for f in glob.glob(os.path.join(OUT, "trades_fade*.parquet")):
        t = pd.read_parquet(f, columns=["r_net"])
        if len(t) > 30:
            srs.append(t["r_net"].mean() / (t["r_net"].std(ddof=1) + 1e-12))
    v_sr = float(np.var(srs)) if len(srs) > 5 else None
    n_trials = max(96, len(srs))
    print(f"\n[DSR] empirical V[SR] across {len(srs)} saved configs = {v_sr:.4f}, n_trials={n_trials}")

    for vn, fname in saved.items():
        tr = pd.read_parquet(os.path.join(OUT, f"trades_{fname}.parquet"))
        r = tr["r_net"].values
        thirds = rolling_thirds(tr)
        row = {
            "variant": vn, "n": len(r), **{k: round(v, 3) for k, v in metrics(tr).items()},
            **{f"mc_{k}": round(v, 3) for k, v in mc_iid(r).items()},
            **{f"bb_{k}": round(v, 3) for k, v in block_bootstrap(r).items()},
            **{f"dsr_{k}": round(v, 3) for k, v in dsr(r, n_trials=n_trials, v_sr=v_sr).items()},
            **{f"t{i+1}_pf": round(thirds[i]["pf"], 2) for i in range(3)},
            **{f"t{i+1}_r": round(thirds[i]["total_r"], 1) for i in range(3)},
        }
        rows.append(row)
    res = pd.DataFrame(rows)
    print("== ROBUSTNESS (FX, 15M) ==")
    print(res.T.to_string())

    # ---- IS-selected symbol basket (top-20 FX by IS PF), OOS evaluation
    tr = pd.read_parquet(os.path.join(OUT, "trades_fadeB_sd2_ewma_cc_m0.5_b1_none.parquet"))
    q = tr["entry_time"].quantile(0.70)
    is_tr = tr[tr.entry_time <= q]
    g = is_tr.groupby("symbol").agg(n=("r_net", "count"), pf=("r_net", lambda x: x[x > 0].sum() / max(1e-9, abs(x[x <= 0].sum()))), tot=("r_net", "sum"))
    top20 = g[(g.n >= 5) & (g.pf > 1)].sort_values("tot", ascending=False).head(20).index.tolist()
    tr2 = tr[tr.symbol.isin(top20)]
    q2 = tr2["entry_time"].quantile(0.70)
    print(f"\n== BASKET top-{len(top20)} IS-selected symbols ==")
    print(" IS :", {k: round(v, 3) for k, v in metrics(tr2[tr2.entry_time <= q2]).items()})
    print(" OOS:", {k: round(v, 3) for k, v in metrics(tr2[tr2.entry_time > q2]).items()})

    # ---- cost stress: double spread + tight spread filter (re-run engine)
    for extra, tag in [(dict(cost_mult=2.0), "2x spread"), (dict(spread_frac_atr_max=0.5, cost_mult=2.0), "2x spread + tight filter")]:
        t = run_cfg_on("15M", FX, sd_wall=2.0, target_m=0.5, buffer_atr=1.0, **extra)
        m = metrics(t)
        print(f"\n== C1 cost-stress {tag} == n={m['n']} total_r={m['total_r']:.1f} pf={m['pf']:.3f}")

    # ---- timeframe checks
    for tf, syms, bars in [("1H", FX_1H, "208d"), ("4H", FX_4H, "2.3y"), ("5M", FX_5M, "17d")]:
        t = run_cfg_on(tf, syms, sd_wall=2.0, target_m=0.5, buffer_atr=1.0)
        m = metrics(t)
        print(f"\n== C1 on {tf} ({bars}) == n={m['n']} total_r={m['total_r']:.1f} pf={m['pf']:.3f} wr={m['win_rate']:.3f} dd={m['max_dd_r']:.1f}")

    # ---- flat-threshold sweep (regime filter done right)
    for ft in [0.4, 0.7]:
        t = run_cfg_on("15M", FX, sd_wall=2.0, target_m=0.5, buffer_atr=1.0, trend_filter="fade_flat", flat_threshold=ft)
        m = metrics(t)
        print(f"\n== C1 + fade_flat thr={ft} == n={m['n']} total_r={m['total_r']:.1f} pf={m['pf']:.3f}")


if __name__ == "__main__":
    main()
