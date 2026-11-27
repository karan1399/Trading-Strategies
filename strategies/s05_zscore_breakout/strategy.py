"""IG-S5: Fresh Volatility Z-Score Cross Trend Breakout (H4).

Fixes the stale-extension chase bug by requiring a genuine Z-score threshold
crossing (prev_z < z_ent <= z for longs; mirror for shorts), combined with
EMA50/EMA200 alignment and optional ADX + ATR volatility expansion filters.

BTCUSDT Footprint-Steroid Research Note (1Y 1m-exact, 12bp RT + 1bp/8h):
Remained net negative after costs even with footprint + VR4 gates (-13.2R H1,
-7.5R H1+VR4, -5.1R H4). Deprecated for BTC footprint allocation when `fp_mode`
retires/blocks signal; prefer VWAP Z-score fade (`s06`) instead.
"""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import adx_arr, ema_arr, rolling_std_arr, sma_arr
from strategies.core.fp_features import fp_get


DEFAULT_PARAMS = {
    "lookback": 50,
    "z_entry": 1.0,
    "cross_only": True,
    "adx_min": 20.0,
    "atr_exp": True,
    "sl_atr": 2.0,
    "tp_r": 3.5,
    "fp_mode": None,
}


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None:
        return None
    lb = int(params.get("lookback", 50))
    if i < max(205, lb + 5) or i >= len(c):
        return None
    a_i = float(atr[i])
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None

    h_s, l_s, c_s = h[: i + 1], l[: i + 1], c[: i + 1]
    adx_min = float(params.get("adx_min", 20.0) or 0.0)
    if adx_min > 0.0 and float(adx_arr(h_s, l_s, c_s, 14)[i]) < adx_min:
        return None
    if bool(params.get("atr_exp", True)):
        atr_s = atr[: i + 1]
        if i < 20 or not np.isfinite(atr_s[i - 19]):
            return None
        if a_i < float(np.mean(atr_s[i - 19 : i + 1])):
            return None

    mu = sma_arr(c_s, lb)
    sd = rolling_std_arr(c_s, lb)
    if not np.isfinite(sd[i]) or sd[i] <= 1e-12 or not np.isfinite(sd[i - 1]) or sd[i - 1] <= 1e-12:
        return None
    e50 = ema_arr(c_s, 50)
    e200 = ema_arr(c_s, 200)

    z = (float(c_s[i]) - float(mu[i])) / float(sd[i])
    zp = (float(c_s[i - 1]) - float(mu[i - 1])) / float(sd[i - 1])
    z_ent = float(params.get("z_entry", 1.0))
    cross_only = bool(params.get("cross_only", True))
    sl_atr = float(params.get("sl_atr", 2.0))
    tp_r = float(params.get("tp_r", 3.5))

    side = 0
    long_trig = (zp < z_ent <= z) if cross_only else (z >= z_ent)
    short_trig = (zp > -z_ent >= z) if cross_only else (z <= -z_ent)
    if long_trig and e50[i] > e200[i] and c_s[i] > e200[i]:
        side = 1
    elif short_trig and e50[i] < e200[i] and c_s[i] < e200[i]:
        side = -1
    if side == 0:
        return None

    rd = max(sl_atr * a_i, 1e-9)
    return {
        "side": side, "rd": float(rd), "sl_abs": None, "tp_abs": None,
        "exit_r": tp_r, "note": f"S5_zx={z:+.2f}",
    }
