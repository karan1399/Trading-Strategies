#!/usr/bin/env python3
"""AEC-Trend validation: walk-forward selection, bootstrap CIs, Monte Carlo
permutation (does the auction-complete footprint signal beat RANDOM entries on
the same regime+pullback candidate set?), deflated Sharpe, portfolio equity.

Protocol:
  * Selection uses IN-SAMPLE ONLY (2026-10-01 .. 2026-11-07).
  * Out-of-sample (2026-11-08 .. 2026-11-23) is touched once, for the reported
    numbers, never for tuning.
"""
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from aec_backtest import (DATA, FEE, SLIP, SLIP_STOP, build_grid, simulate_asset,
                          summarize)

OUT = "/home/z/my-project/binance_orderflow/data/research"
N_PERM = 300
N_BOOT = 10000


def phi(x):
    from math import erf, sqrt
    return 0.5 * (1 + erf(x / sqrt(2.0)))


def ppf(p):
    # inverse normal (Beasley-Springer-Moro approximation is overkill; use scipy if present)
    try:
        from scipy.stats import norm
        return float(norm.ppf(p))
    except Exception:
        import math
        return math.sqrt(2) * math.erfinv(2 * p - 1) * math.sqrt(2) / math.sqrt(2)  # fallback rough


def deflated_sharpe(r: np.ndarray, n_trials: int, sr_trials_std: float) -> float:
    """Probability that observed per-trade SR beats the expected max SR under n_trials."""
    T = len(r)
    if T < 10:
        return 0.0
    sr = r.mean() / (r.std() + 1e-12)
    g3 = float(pd.Series(r).skew())
    g4 = float(pd.Series(r).kurt() + 3)
    gamma = 0.5772156649
    N = max(2, n_trials)
    sr0 = sr_trials_std * ((1 - gamma) * ppf(1 - 1.0 / N) + gamma * ppf(1 - 1.0 / (N * np.e)))
    denom = np.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4.0 * sr * sr))
    return float(phi((sr - sr0) * np.sqrt(T - 1) / denom))


def sample_entries(cand_idx: np.ndarray, k: int, rng, min_gap: int = 2) -> np.ndarray:
    """Sample k entry bars from candidates respecting a min gap (greedy on shuffle)."""
    order = rng.permutation(cand_idx)
    picked = []
    last = -10**9
    for b in order:
        if b - last >= min_gap:
            picked.append(b)
            last = b
        if len(picked) >= k:
            break
    return np.array(picked, dtype=int)


def perm_test(df: pd.DataFrame, cfg: dict, split_ts: int, n_perm: int, seed: int = 7):
    """Perm A: same regime+pullback candidate set, AC flag randomized."""
    rng = np.random.default_rng(seed)
    a = df.copy()
    ac_col = f"{cfg['ac']}_low" if cfg["side"] == "long" else f"{cfg['ac']}_high"
    long_side = cfg["side"] == "long"
    reg_col = "trend_up_strict" if long_side else "trend_dn_strict"
    if cfg["entry"] == "reclaim":
        base_cols = (["touch_low5", "depth_up_ok", "reclaim_up"] if long_side
                     else ["touch_high5", "depth_dn_ok", "reclaim_dn"])
        cand = a[reg_col].to_numpy(bool)
        for bc in base_cols:
            cand &= a[bc].to_numpy(bool)
    else:
        pb_col = f"pb_low_{cfg['entry'][-2:]}" if long_side else f"pb_high_{cfg['entry'][-2:]}"
        cand = (a[reg_col].to_numpy(bool) & a[pb_col].to_numpy(bool))
    cand[:60] = False
    cand[-2:] = False
    cand_idx = np.where(cand)[0]
    k_actual = int((cand & a[ac_col].to_numpy(bool)).sum())
    if k_actual < 10 or len(cand_idx) < k_actual + 5:
        return None
    grid = [dict(cfg)] * n_perm
    M = np.zeros((len(a), n_perm), dtype=bool)
    for p in range(n_perm):
        bars = sample_entries(cand_idx, k_actual, rng)
        M[bars, p] = True
    tr = simulate_asset(a.assign(**{c: a[c] for c in a.columns}), grid, split_ts, entry_override=M)
    r = tr["ret_bps"].to_numpy(float) if len(tr) else np.array([])
    return {"perm_mean_bps": float(r.mean()) if len(r) else 0.0,
            "perm_n": int(len(r)),
            "actual_mean_bps": None,  # filled by caller
            "perm_draws": r.tolist()[:2000]}


def main():
    scen_override = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--scen="):
        scen_override = int(sys.argv[1].split("=")[1])
    T = pd.read_parquet(f"{OUT}/aec_trades.parquet")
    S = pd.read_csv(f"{OUT}/aec_scenarios.csv")
    ds = pd.read_parquet(f"{OUT}/research_15m.parquet")
    split_ts = int(pd.Timestamp("2026-11-08", tz="UTC").value // 10 ** 6)
    is_t = S["IS_tstat"].to_numpy(float)
    n_is = S["IS_n"].to_numpy(float)
    sr_trials = np.where(n_is > 5, is_t / np.maximum(n_is, 1), np.nan)
    sr_trials_std = float(np.nanstd(sr_trials))
    n_trials = len(S)

    # ---- walk-forward selection (IS only)
    eligible = S[(S["IS_n"] >= 60)].copy()
    top = eligible.sort_values("IS_tstat", ascending=False).head(8)
    print("TOP-8 (IS-selected) with untouched OOS:")
    print(top[["scen", "regime", "entry", "ac", "stop_mult", "exit", "side",
               "IS_n", "IS_avg_bps", "IS_tstat", "OOS_n", "OOS_avg_bps", "OOS_pf", "OOS_tstat"]].to_string(index=False))

    # choose the primary config = highest IS t-stat (or --scen override for the
    # regime-conditional family hypothesis, reported separately)
    if scen_override is not None:
        best = S[S["scen"] == scen_override].iloc[0].to_dict()
    else:
        best = top.iloc[0].to_dict()
    cfg = {k: best[k] for k in ("regime", "entry", "ac", "stop_mult", "exit", "side")}
    print(f"\nPRIMARY CONFIG: {cfg}")
    tr = T[(T["scen"] == best["scen"])]
    oos = tr[tr["oos"]]
    is_ = tr[~tr["oos"]]
    s_is, s_oos = summarize(is_), summarize(oos)
    print(f"IS : {s_is}\nOOS: {s_oos}")

    # ---- per-symbol OOS stability
    per_sym = oos.groupby("symbol").apply(summarize, include_groups=False).apply(pd.Series)
    if len(per_sym):
        pos_syms = int((per_sym["avg_bps"] > 0).sum())
        print(f"\nOOS per-symbol: {pos_syms}/{len(per_sym)} symbols positive after costs")
        print(per_sym[["n", "win%", "avg_bps", "pf"]].to_string())

    # ---- ablation: does AC add value vs ac_none baseline? (same family, IS + OOS)
    fam = ["regime", "entry", "stop_mult", "exit", "side"]
    base = S[(S["regime"] == cfg["regime"]) & (S["entry"] == cfg["entry"]) &
             (S["stop_mult"] == cfg["stop_mult"]) & (S["exit"] == cfg["exit"]) &
             (S["side"] == cfg["side"])]
    print("\nABLATION (same family, ac variants):")
    print(base[["ac", "IS_n", "IS_avg_bps", "IS_tstat", "OOS_n", "OOS_avg_bps", "OOS_pf"]].to_string(index=False))

    # ---- bootstrap on OOS trades
    r = oos["ret_bps"].to_numpy(float)
    rng = np.random.default_rng(11)
    if len(r) >= 10:
        idx = rng.integers(0, len(r), size=(N_BOOT, len(r)))
        means = r[idx].mean(axis=1)
        tots = r[idx].sum(axis=1)
        lo, hi = np.percentile(means, [2.5, 97.5])
        pf_boot = []
        for row in idx[:2000]:
            rr = r[row]
            pos_s, neg_s = rr[rr > 0].sum(), -rr[rr < 0].sum()
            pf_boot.append(pos_s / max(1e-9, neg_s))
        print(f"\nBOOTSTRAP OOS (n={len(r)} trades): mean {r.mean():.1f} bps, "
              f"95% CI [{lo:.1f}, {hi:.1f}] bps; P(mean>0)={float((means>0).mean()):.3f}; "
              f"PF 95% CI [{np.percentile(pf_boot,2.5):.2f}, {np.percentile(pf_boot,97.5):.2f}]")
    # ---- MC permutation
    sym_frames = [g for _, g in ds.groupby("symbol")]
    actual_per_sym = oos.groupby("symbol")["ret_bps"].mean().to_dict()
    perms, actuals = [], []
    for d in sym_frames:
        d = d.sort_values("ts").reset_index(drop=True)
        res = perm_test(d, cfg, split_ts, N_PERM)
        if res is None:
            continue
        sym_oos = oos[oos["symbol"] == d["symbol"].iloc[0]]
        if len(sym_oos) == 0:
            continue
        res["actual_mean_bps"] = float(sym_oos["ret_bps"].mean())
        actuals.append(res["actual_mean_bps"])
        perms.append(res["perm_mean_bps"])
        print(f"perm {d['symbol'].iloc[0]}: actual {res['actual_mean_bps']:.1f} vs "
              f"perm-mean {res['perm_mean_bps']:.1f} bps (n_perm={res['perm_n']})")
    if perms:
        all_perm = np.array(perms)
        all_act = float(np.mean(actuals))
        p_val = float((all_perm >= all_act).mean())
        print(f"\nMC PERMUTATION (A: AC randomized within regime+pullback, {N_PERM}/asset):")
        print(f"  actual pooled mean {all_act:.1f} bps vs random {np.mean(all_perm):.1f} "
              f"(p = {p_val:.3f}) -> {'SIGNIFICANT' if p_val < 0.05 else 'NOT significant'}")

    # ---- deflated Sharpe
    dsr = deflated_sharpe(r, n_trials, sr_trials_std)
    print(f"\nDEFLATED SHARPE (n_trials={n_trials}): P(SR_max OOS > 0) = {dsr:.3f}")

    # ---- portfolio equity (OOS, equal notional per trade, additive)
    if len(oos):
        o = oos.sort_values("exit_ts")
        eq = o["ret_bps"].cumsum().to_numpy()
        peak = np.maximum.accumulate(np.concatenate([[0], eq]))[1:]
        dd = (eq - peak).min()
        print(f"\nPORTFOLIO OOS equity: total {eq[-1]/100:.2f}% on equal-notional trades, "
              f"maxDD {dd/100:.2f}%, trades {len(o)}")

    out = {"config": cfg, "IS": s_is, "OOS": s_oos,
           "per_symbol_OOS": {k: v for k, v in per_sym.T.to_dict().items()} if len(per_sym) else {},
           "bootstrap": {"mean_bps": float(r.mean()), "n": len(r)} if len(r) else {},
           "permutation": {"p_value": p_val if perms else None, "n_perm_per_asset": N_PERM,
                           "actual_pooled_mean": all_act if perms else None,
                           "random_pooled_mean": float(np.mean(all_perm)) if perms else None},
           "deflated_sharpe": dsr, "n_trials": n_trials,
           "ablation_family": base[["ac", "IS_n", "IS_avg_bps", "IS_tstat",
                                    "OOS_n", "OOS_avg_bps", "OOS_pf"]].to_dict("records")}
    suffix = f"_scen{scen_override}" if scen_override else ""
    with open(f"{OUT}/aec_validation{suffix}.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"\nsaved -> {OUT}/aec_validation.json")


if __name__ == "__main__":
    main()
