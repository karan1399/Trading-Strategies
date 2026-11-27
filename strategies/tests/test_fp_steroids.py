"""Unit and zero-lookahead causality tests for strategies/fp_features and s01..s11 footprint upgrades."""
from __future__ import annotations

import importlib
import numpy as np
import pandas as pd

from strategies.core.fp_features import build_fp_context
from strategies.core.registry import STRATEGY_CATALOG, load_signal_fn
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd


def _make_df(n: int = 320, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.008, size=n)
    c = 60000.0 * np.exp(np.cumsum(steps))
    o = np.roll(c, 1); o[0] = 60000.0
    h = np.maximum(o, c) * (1.0 + rng.uniform(0.0005, 0.005, size=n))
    l = np.minimum(o, c) * (1.0 - rng.uniform(0.0005, 0.005, size=n))
    v = rng.uniform(500.0, 5000.0, size=n)
    delta = rng.uniform(-0.4, 0.4, size=n) * v
    ts = 1764115200 + np.arange(n) * 3600
    return pd.DataFrame({
        "ts": ts,
        "o": o, "h": h, "l": l, "c": c, "v": v, "delta": delta,
        "buy_imb": rng.integers(0, 5, size=n),
        "sell_imb": rng.integers(0, 5, size=n),
        "zero_top": rng.random(n) > 0.85,
        "zero_bot": rng.random(n) > 0.85,
        "poc": (o + c) * 0.5,
        "dur_m": np.full(n, 60),
    })


def test_zero_lookahead_causality_invariant():
    """Mutating future bars > i must not alter fp_features or s01..s11 signals at bar i."""
    df = _make_df(320)
    idx = 270

    fp_full = build_fp_context(df)
    df_future_mutated = df.copy()
    df_future_mutated.loc[idx + 1 :, ["o", "h", "l", "c"]] *= 2.5
    df_future_mutated.loc[idx + 1 :, "delta"] *= -50.0
    fp_trunc = build_fp_context(df_future_mutated.iloc[: idx + 1])

    for key in ("ema89", "ema233", "rsi2", "wr14", "adx14", "d_pct", "clv", "vpin_rank", "vpm_rk", "vwz48", "vr4"):
        v1 = float(fp_full[key][idx])
        v2 = float(fp_trunc[key][idx])
        if np.isnan(v1) and np.isnan(v2):
            continue
        assert np.isclose(v1, v2, rtol=1e-9, atol=1e-9), f"Leakage detected in {key}: {v1} vs {v2}"

    tm = (df["ts"].to_numpy() // 60).astype(np.int64)
    for sid, meta in STRATEGY_CATALOG.items():
        if meta["kind"] == "overlay":
            continue
        fn = load_signal_fn(sid)
        mod = importlib.import_module(meta["module"])
        p = {**getattr(mod, "DEFAULT_PARAMS", {}), "fp_mode": True}
        sig1 = fn(fp_full["o"], fp_full["h"], fp_full["l"], fp_full["c"], tm, fp_full["atr20"], idx, p, {"fp": fp_full})
        sig2 = fn(
            fp_trunc["o"], fp_trunc["h"], fp_trunc["l"], fp_trunc["c"], tm[: idx + 1], fp_trunc["atr20"], idx, p, {"fp": fp_trunc},
        )
        assert sig1 == sig2, f"Future mutation changed signal at bar {idx} for {sid}"


def test_pure_ohlc_backward_compatibility():
    """When extra=None, s01..s11 run legacy OHLC logic cleanly without errors."""
    df = _make_df(320, seed=99)
    fp = build_fp_context(df)
    tm = (df["ts"].to_numpy() // 60).astype(np.int64)
    for sid, meta in STRATEGY_CATALOG.items():
        if meta["kind"] == "overlay":
            continue
        fn = load_signal_fn(sid)
        mod = importlib.import_module(meta["module"])
        p = dict(getattr(mod, "DEFAULT_PARAMS", {}))
        res = fn(fp["o"], fp["h"], fp["l"], fp["c"], tm, fp["atr20"], 265, p, None)
        assert res is None or ("side" in res and res["side"] in (-1, 1))


def test_s08_vol_of_conviction_bounds():
    """s08_vOF respects strict bounds and modulates sizing directionally with OF toxicity."""
    c = 60000.0 * np.exp(np.cumsum(np.random.default_rng(7).normal(0.0, 0.005, size=260)))
    r_boost = vol_of_conviction_risk_usd(c, "H1", side=1, delta_pct=18.0, imb_diff=2, vpin_rank=0.40, risk_target_usd=50.0, risk_min_usd=32.5, risk_max_usd=62.5)
    r_toxic = vol_of_conviction_risk_usd(c, "H1", side=1, delta_pct=-12.0, imb_diff=-2, vpin_rank=0.90, risk_target_usd=50.0, risk_min_usd=32.5, risk_max_usd=62.5)
    assert 32.5 <= r_toxic < r_boost <= 62.5
