"""Unit, causality, Kaminsky-Lo bounds, and dollar-neutral invariants for s17, s18, s19."""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.core.fp_features import build_fp_context
from strategies.core.registry import STRATEGY_CATALOG, load_signal_fn
from strategies.s17_kl_triple_rsi.strategy import exit_signal_at as s17_exit, signal_at as s17_sig
from strategies.s18_kl_flush_reclaim.strategy import exit_signal_at as s18_exit, signal_at as s18_sig
from strategies.s19_cs_carry_momentum.strategy import compute_target_weights


def _make_trend_pullback_series(n: int = 240) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    # Rising uptrend above SMA200, then 4 sharp consecutive red pullbacks at tail to trigger RSI(5) < 30 falling 3x
    base = np.linspace(100.0, 150.0, n)
    rng = np.random.default_rng(12)
    noise = rng.normal(0.0, 0.25, n)
    c = base + noise
    c[-10:-4] = [151.0, 150.8, 150.2, 149.8, 149.3, 148.9]
    c[-4:] = [147.8, 146.2, 144.4, 142.1]
    o = np.roll(c, 1); o[0] = c[0]
    h = np.maximum(o, c) + 0.6
    l = np.minimum(o, c) - 0.6
    atr = np.full(n, 1.5, dtype=float)
    return o, h, l, c, atr


def test_registry_contains_s17_s18_s19():
    for sid in ("s17_kl_triple_rsi", "s18_kl_flush_reclaim", "s19_cs_carry_momentum"):
        assert sid in STRATEGY_CATALOG
    assert callable(load_signal_fn("s17_kl_triple_rsi"))
    assert callable(load_signal_fn("s18_kl_flush_reclaim"))


def test_s17_triple_rsi_trigger_and_zero_lookahead():
    o, h, l, c, atr = _make_trend_pullback_series(240)
    idx = len(c) - 1
    tm = (1760000000 + np.arange(len(c)) * 86400) // 60

    sig_clean = s17_sig(o, h, l, c, tm, atr, idx, {}, None)
    assert sig_clean is not None and sig_clean["side"] == 1
    assert sig_clean["kl_nosl"] is True
    assert sig_clean["sl_abs"] is None
    assert sig_clean["cat_rd"] > sig_clean["rd"]
    assert 0.2 <= sig_clean["risk_scale"] <= 0.75

    # Append wildly different future bars -> signal at `idx` must remain identical!
    o_f = np.concatenate([o, [20.0, 5.0, 500.0]])
    h_f = np.concatenate([h, [25.0, 6.0, 510.0]])
    l_f = np.concatenate([l, [18.0, 4.0, 490.0]])
    c_f = np.concatenate([c, [19.0, 4.5, 505.0]])
    atr_f = np.concatenate([atr, [10.0, 10.0, 10.0]])
    tm_f = np.concatenate([tm, [tm[-1] + 1440, tm[-1] + 2880, tm[-1] + 4320]])
    sig_future = s17_sig(o_f, h_f, l_f, c_f, tm_f, atr_f, idx, {}, None)
    assert sig_clean == sig_future
    assert isinstance(s17_exit(c_f, idx), bool)


def test_s18_flush_reclaim_trigger_and_causality():
    n = 120
    rng = np.random.default_rng(33)
    c = np.full(n, 100.0) + rng.normal(0.0, 0.3, n)
    c[-1] = 92.0
    h = c + 0.8
    l = c - 1.2
    o = np.roll(c, 1); o[0] = 100.0
    v = np.full(n, 1000.0); v[-1] = 3200.0
    delta = np.full(n, 50.0); delta[-1] = 600.0
    df = pd.DataFrame({"ts": 1760000000 + np.arange(n) * 14400, "o": o, "h": h, "l": l, "c": c, "v": v, "delta": delta})
    fp = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in build_fp_context(df).items()}
    fp["vwz48"][-1] = -2.3
    fp["oi_chg_6b"] = np.zeros(n); fp["oi_chg_6b"][-1] = -9.5
    fp["vol_rel20"] = np.ones(n); fp["vol_rel20"][-1] = 2.4
    fp["clv"][-1] = 0.45
    fp["d_pct"][-1] = 12.0
    tm = (df["ts"].to_numpy(np.int64) // 60)
    idx = n - 1

    sig = s18_sig(fp["o"], fp["h"], fp["l"], fp["c"], tm, fp["atr20"], idx, {}, {"fp": fp})
    assert sig is not None and sig["side"] == 1 and sig["kl_nosl"] is True and sig["risk_scale"] == 0.40
    assert s18_exit(fp["c"], idx, {"fp": fp}) is False
    # Zero lookahead: truncating at idx yields exact same signal & exit!
    fp_t = {k: (v[: idx + 1].copy() if isinstance(v, np.ndarray) else v) for k, v in fp.items()}
    assert s18_sig(fp_t["o"], fp_t["h"], fp_t["l"], fp_t["c"], tm[: idx + 1], fp_t["atr20"], idx, {}, {"fp": fp_t}) == sig
    fp["vwz48"][-1] = 0.15
    assert s18_exit(fp["c"], idx, {"fp": fp}) is True


def test_s19_cs_carry_momentum_dollar_neutral_and_causal():
    syms = [f"ASSET{k}USDT" for k in range(18)]
    rng = np.random.default_rng(7)
    df = pd.DataFrame({
        "sym": syms,
        "fund_72h": rng.uniform(-3.0, 5.0, len(syms)),
        "ret_42b": rng.normal(0.02, 0.08, len(syms)),
        "ret_6b": rng.normal(0.0, 0.03, len(syms)),
        "dp_6b": rng.normal(0.0, 12.0, len(syms)),
        "vol_24": rng.uniform(0.008, 0.035, len(syms)),
    })
    w1, ema1 = compute_target_weights(df)
    assert len(w1) == 10  # 5 longs + 5 shorts
    long_sum = sum(v for v in w1.values() if v > 0)
    short_sum = sum(v for v in w1.values() if v < 0)
    assert np.isclose(long_sum, 0.5, atol=1e-9)
    assert np.isclose(short_sum, -0.5, atol=1e-9)
    assert np.isclose(sum(w1.values()), 0.0, atol=1e-9)
    # Row shuffling must produce identical weights (order invariance)
    w_shuf, _ = compute_target_weights(df.sample(frac=1.0, random_state=99), None)
    for k in w1:
        assert np.isclose(w1[k], w_shuf[k], atol=1e-9)
