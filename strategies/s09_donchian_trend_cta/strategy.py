"""IG-S9: Donchian Channel Breakout + Wilder ADX + Kaufman ER Trend Filter (H4).

Enters cleanly when close[i] breaks the previous N-bar Donchian high/low strictly aligned
with EMA50/EMA200 regime, ADX >= adx_min, and Efficiency Ratio >= er_min.
"""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import adx_arr, ema_arr, er_arr, rolling_max_prev, rolling_min_prev
from strategies.core.fp_features import fp_get, fp_val


DEFAULT_PARAMS = {
    "donchian_n": 20,
    "sl_atr": 1.5,
    "adx_min": 18.0,
    "er_min": 0.20,
    "tp_r": 3.0,
    "fp_mode": None,
    "fp_donchian_n": 22,
    "fp_min_clv": 0.30,
    "fp_sl_atr": 2.0,
    "fp_trail_atr": 2.5,
    "fp_be_r": 1.0,
}


def _fp_signal_at(h, l, c, atr, i: int, params: dict, fp: dict) -> dict | None:
    don_n = int(params.get("fp_donchian_n", 22))
    if i < max(235, don_n + 2):
        return None
    a_i = fp_val(fp, "atr20", i, float(atr[i]))
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    e233 = fp_val(fp, "ema233", i, float("nan"))
    if not np.isfinite(e233):
        return None

    prev_hi = float(np.max(h[i - don_n : i]))
    prev_lo = float(np.min(l[i - don_n : i]))
    clv = fp_val(fp, "clv", i, 0.0)
    dp = fp_val(fp, "d_pct", i, 0.0)
    bi = int(fp_val(fp, "buy_imb", i, 0.0))
    si = int(fp_val(fp, "sell_imb", i, 0.0))
    min_clv = float(params.get("fp_min_clv", 0.30))

    side = 0
    if float(c[i]) > prev_hi and float(c[i]) > e233 and clv > min_clv and dp > 0.0 and bi >= si:
        side = 1
    elif float(c[i]) < prev_lo and float(c[i]) < e233 and clv < -min_clv and dp < 0.0 and si >= bi:
        side = -1
    if side == 0:
        return None

    rd = max(float(params.get("fp_sl_atr", 2.0)) * a_i, 1e-9)
    return {
        "side": side,
        "rd": float(rd),
        "sl_abs": None,
        "tp_abs": None,
        "exit_r": 0.0,
        "be_r": float(params.get("fp_be_r", 1.0)),
        "trail_atr": float(params.get("fp_trail_atr", 2.5)),
        "step_lock": True,
        "max_hold": 96,
        "note": f"S9_FP_D{don_n}|clv={clv:+.2f}|d%={dp:+.1f}",
    }


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None and i < len(c):
        return _fp_signal_at(h, l, c, atr, i, params, fp)
    don_n = int(params.get("donchian_n", 20))
    if i < max(205, don_n + 2) or i >= len(c):
        return None
    a_i = float(atr[i])
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None

    h_s, l_s, c_s = h[: i + 1], l[: i + 1], c[: i + 1]
    adx_min = float(params.get("adx_min", 18.0))
    er_min = float(params.get("er_min", 0.20))
    if float(adx_arr(h_s, l_s, c_s, 14)[i]) < adx_min:
        return None
    if float(er_arr(c_s, 14)[i]) < er_min:
        return None

    e50 = ema_arr(c_s, 50)
    e200 = ema_arr(c_s, 200)
    prev_hh = rolling_max_prev(h_s, don_n)
    prev_ll = rolling_min_prev(l_s, don_n)
    if not (np.isfinite(prev_hh[i]) and np.isfinite(prev_ll[i])):
        return None

    side = 0
    if c_s[i] > prev_hh[i] and e50[i] > e200[i] and c_s[i] > e200[i]:
        side = 1
    elif c_s[i] < prev_ll[i] and e50[i] < e200[i] and c_s[i] < e200[i]:
        side = -1
    if side == 0:
        return None

    sl_atr = float(params.get("sl_atr", 1.5))
    tp_r = float(params.get("tp_r", 3.0))
    rd = max(sl_atr * a_i, 1e-9)
    return {
        "side": side, "rd": float(rd), "sl_abs": None, "tp_abs": None,
        "exit_r": tp_r, "note": f"S9_D{don_n}",
    }
