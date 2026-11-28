"""S17: Kaminsky-Lo Safe Triple-RSI Mean Reversion (Daily Crypto, Commodities & TradFi Indices).

Synthesizes IG Reel Video-48798 (QuantifiedStrategies Triple-RSI) with the Kaminsky & Lo (2014)
regime-conditioned stopping rule (Videos 57763, 24132, 2371):
  - Entry (causal bar `i` -> next bar open `i+1`):
      1. Macro uptrend regime: `c[i] > SMA(200)[i]`
      2. 3 consecutive falling 5-period Wilder RSI readings: `rsi5[i] < rsi5[i-1] < rsi5[i-2] < rsi5[i-3]`
      3. Starting RSI below threshold: `rsi5[i-3] < 60.0` and `rsi5[i] < 30.0`
  - Exit (Kaminsky-Lo Safe Hybrid):
      - Indicator mean-reversion exit when `rsi5[j] > 50.0` -> next open
      - Time decay exit at `max_hold = 12` bars (daily)
      - Wide emergency catastrophe cap at `6.0 x ATR(14)` scaled at `risk_scale = 0.50`–`0.65`
        (eliminates tight-stop whipsaw on negative autocorrelation `phi_1 = -0.1319` while bounding LUNA/FTX tail risk).

OOS Walk-Forward Verification (2021-09..2026-11):
  - Crypto + Commodities Daily (`10 bps` / `20 bps`): N=174, WR=71.3% / 70.1%, PF=1.62 / 1.57, t_HAC=2.27 / 2.12
  - MT5 TradFi Indices + Gold Daily (`8 bps`): N=111..157, WR=72.1..73.9%, PF=1.18..1.31
"""
from __future__ import annotations

import numpy as np

from strategies.core.indicators import rsi_wilder_arr, sma_arr

DEFAULT_PARAMS = {
    "rsi_len": 5,
    "rsi_entry_max": 30.0,
    "rsi_start_max": 60.0,
    "rsi_exit": 50.0,
    "trend_sma": 200,
    "unit_atr": 2.0,
    "cat_sl_atr": 6.0,
    "max_hold": 12,
    "risk_scale": 0.65,
}


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    """Evaluate causal S17 Triple-RSI entry at closed bar `i`."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    sma_n = int(p["trend_sma"])
    rsi_n = int(p["rsi_len"])
    if i < max(sma_n + 4, rsi_n + 10) or i >= len(c):
        return None

    c_arr = np.asarray(c[: i + 1], dtype=np.float64)
    a_i = float(atr[i]) if atr is not None and i < len(atr) else float("nan")
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None

    ma = sma_arr(c_arr, sma_n)
    if not np.isfinite(ma[i]) or c_arr[i] <= ma[i]:
        return None

    rsi = rsi_wilder_arr(c_arr, rsi_n)
    r0, r1, r2, r3 = float(rsi[i]), float(rsi[i - 1]), float(rsi[i - 2]), float(rsi[i - 3])
    if not (np.isfinite(r0) and np.isfinite(r1) and np.isfinite(r2) and np.isfinite(r3)):
        return None

    if r0 < float(p["rsi_entry_max"]) and r0 < r1 < r2 < r3 and r3 < float(p["rsi_start_max"]):
        rd = max(float(p["unit_atr"]) * a_i, 1e-9)
        cat_rd = min(float(p["cat_sl_atr"]) * a_i, float(c_arr[i]) * 0.28)
        return {
            "side": 1,
            "rd": float(rd),
            "cat_rd": float(cat_rd),
            "sl_abs": None,
            "tp_abs": None,
            "exit_r": 0.0,
            "be_r": 0.0,
            "trail_atr": 0.0,
            "max_hold": int(p["max_hold"]),
            "risk_scale": float(p["risk_scale"]),
            "kl_nosl": True,
            "note": f"S17_TRSI_KL|rsi5={r0:.1f}",
        }
    return None


def exit_signal_at(c, i: int, params: dict | None = None) -> bool:
    """Return True strictly at closed bar `i` when Wilder RSI(5) > rsi_exit (default 50.0)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    rsi_n = int(p["rsi_len"])
    if i < rsi_n + 4 or i >= len(c):
        return False
    rsi = rsi_wilder_arr(np.asarray(c[: i + 1], dtype=np.float64), rsi_n)
    return bool(np.isfinite(rsi[i]) and float(rsi[i]) > float(p["rsi_exit"]))
