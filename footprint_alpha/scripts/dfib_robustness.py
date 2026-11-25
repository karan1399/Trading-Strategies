#!/usr/bin/env python3
"""Quant robustness battery for the Delta-Fibonacci study.

Ports the IV Wall battery (validated in the previous research cycle):
  mc_iid           - 10k-draw IID Monte Carlo resampling of R multiples
  block_bootstrap  - stationary block bootstrap (avg block 5 trades)
  dsr              - Bailey & Lopez de Prado Deflated Sharpe with empirical V[SR]
  rolling_thirds   - chronological thirds PF/total R
  cost_stress      - re-simulate at cost multiplier (1.5x / 2x spreads)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats as sps


def mc_iid(r: np.ndarray, n: int = 10000, seed: int = 7):
    rng = np.random.default_rng(seed)
    draws = rng.choice(r, size=(n, len(r)), replace=True)
    totals = draws.sum(axis=1)
    cum = np.cumsum(draws, axis=1)
    dd = (np.maximum.accumulate(cum, axis=1) - cum).max(axis=1)
    return {
        "p_profit": float((totals > 0).mean()),
        "total_ci5": float(np.percentile(totals, 5)),
        "total_ci95": float(np.percentile(totals, 95)),
        "dd95": float(np.percentile(dd, 95)),
        "dd99": float(np.percentile(dd, 99)),
    }


def block_bootstrap(r: np.ndarray, n: int = 5000, avg_block: int = 5, seed: int = 11):
    rng = np.random.default_rng(seed)
    nr = len(r)
    p = 1.0 / avg_block
    totals = np.empty(n)
    dds = np.empty(n)
    for s in range(n):
        idx, pos = [], 0
        while pos < nr:
            L = rng.geometric(p)
            start = rng.integers(0, nr)
            idx.extend(range(start, min(start + L, nr)))
            pos += L
        path = r[np.array(idx[:nr])]
        cum = np.cumsum(path)
        totals[s] = cum[-1]
        dds[s] = (np.maximum.accumulate(cum) - cum).max()
    return {
        "p_profit": float((totals > 0).mean()),
        "total_ci5": float(np.percentile(totals, 5)),
        "total_ci95": float(np.percentile(totals, 95)),
        "dd95": float(np.percentile(dds, 95)),
    }


def dsr(r: np.ndarray, n_trials: int, v_sr: float | None = None):
    """Deflated Sharpe probability. r = per-trade R multiples (non-annualized)."""
    n = len(r)
    if n < 5 or r.std(ddof=1) == 0:
        return {"sr": 0.0, "sr0": 0.0, "dsr_prob": 0.0}
    sr = r.mean() / r.std(ddof=1)
    if v_sr is None:
        v_sr = (1.0 / (n - 1)) * (1 + 0.5 * sr ** 2)  # conservative fallback
    sr0 = sr - sps.norm.ppf(1 - 1.0 / max(n_trials, 2)) * np.sqrt(v_sr)
    z = sr0 * np.sqrt(n - 1)
    return {
        "sr": float(sr),
        "sr0": float(sr0),
        "dsr_prob": float(sps.norm.cdf(z)),
        "skew": float(sps.skew(r)),
        "kurt": float(sps.kurtosis(r)),
    }


def rolling_thirds(tr: pd.DataFrame):
    out = []
    n = len(tr)
    for i in range(3):
        seg = tr.iloc[i * n // 3:(i + 1) * n // 3]
        r = seg["r_net"].to_numpy(float)
        w, l = r[r > 0], r[r <= 0]
        out.append({
            "third": i + 1,
            "n": int(len(r)),
            "total_r": float(r.sum()),
            "pf": float(w.sum() / abs(l.sum())) if l.sum() < 0 else 99.0,
            "win_rate": float((r > 0).mean()),
        })
    return out


def empirical_v_sr(config_r_lists: list[np.ndarray]) -> float:
    """Cross-config variance of per-trade Sharpe estimates (trial variance)."""
    srs = [rs.mean() / rs.std(ddof=1) for rs in config_r_lists if len(rs) > 10 and rs.std(ddof=1) > 0]
    return float(np.var(srs, ddof=1)) if len(srs) > 2 else None
