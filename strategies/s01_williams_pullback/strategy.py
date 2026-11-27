"""IG-S1: Williams %R Trend-Aligned Pullback + ADX & Reversal Filter (H4).

Fires when Williams %R(p) exits oversold (<= -80 -> > -80) in an EMA50 > EMA200 uptrend
or exits overbought (>= -20 -> < -20) in an EMA50 < EMA200 downtrend, filtered by ADX
trend strength and candle-direction reversal confirmation.
"""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import adx_arr, ema_arr, williams_r_arr
from strategies.core.fp_features import fp_get, fp_val


DEFAULT_PARAMS = {
    "wr_period": 14,
    "adx_min": 18.0,
    "rev_close": True,
    "sl_atr": 1.5,
    "tp_r": 3.5,
    "fp_mode": None,
    "fp_wr_thresh": -75.0,
    "fp_min_dp": 5.0,
    "fp_min_clv": 0.25,
    "fp_sl_atr": 2.0,
    "fp_trail_atr": 2.5,
}


def _fp_signal_at(c, atr, i: int, params: dict, fp: dict) -> dict | None:
    if i < 235:
        return None
    a_i = fp_val(fp, "atr20", i, float(atr[i]))
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    e89, e89_p = fp_val(fp, "ema89", i, float("nan")), fp_val(fp, "ema89", i - 3, float("nan"))
    e233 = fp_val(fp, "ema233", i, float("nan"))
    wr_c, wr_p = fp_val(fp, "wr14", i, float("nan")), fp_val(fp, "wr14", i - 1, float("nan"))
    if not (np.isfinite(e89) and np.isfinite(e89_p) and np.isfinite(e233) and np.isfinite(wr_c) and np.isfinite(wr_p)):
        return None
    dp, clv = fp_val(fp, "d_pct", i, 0.0), fp_val(fp, "clv", i, 0.0)
    bi, si = int(fp_val(fp, "buy_imb", i, 0.0)), int(fp_val(fp, "sell_imb", i, 0.0))
    thr = float(params.get("fp_wr_thresh", -75.0))
    min_dp = float(params.get("fp_min_dp", 5.0))
    min_clv = float(params.get("fp_min_clv", 0.25))
    stkL = e89 > e233 and e89 > e89_p and float(c[i]) > e233
    stkS = e89 < e233 and e89 < e89_p and float(c[i]) < e233
    side = 0
    if stkL and wr_p <= thr and wr_c > thr and dp > min_dp and clv > min_clv and bi >= si:
        side = 1
    elif stkS and wr_p >= (-100.0 - thr) and wr_c < (-100.0 - thr) and dp < -min_dp and clv < -min_clv and si >= bi:
        side = -1
    if side == 0:
        return None
    return {
        "side": side,
        "rd": float(max(float(params.get("fp_sl_atr", 2.0)) * a_i, 1e-9)),
        "sl_abs": None,
        "tp_abs": None,
        "exit_r": 0.0,
        "be_r": 1.0,
        "trail_atr": float(params.get("fp_trail_atr", 2.5)),
        "step_lock": True,
        "max_hold": 96,
        "note": f"S1_FP_WR={wr_c:.1f}|d%={dp:+.1f}",
    }


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    if i < 205 or i >= len(c):
        return None
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None:
        return _fp_signal_at(c, atr, i, params, fp)
    a_i = float(atr[i])
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None

    h_s, l_s, c_s = h[: i + 1], l[: i + 1], c[: i + 1]
    adx_min = float(params.get("adx_min", 18.0) or 0.0)
    if adx_min > 0.0 and float(adx_arr(h_s, l_s, c_s, 14)[i]) < adx_min:
        return None

    wr_p = int(params.get("wr_period", 14))
    wr = williams_r_arr(h_s, l_s, c_s, wr_p)
    if not (np.isfinite(wr[i]) and np.isfinite(wr[i - 1])):
        return None
    e50 = ema_arr(c_s, 50)
    e200 = ema_arr(c_s, 200)
    rev_close = bool(params.get("rev_close", True))

    side = 0
    if wr[i - 1] <= -80.0 and wr[i] > -80.0 and e50[i] > e200[i] and c_s[i] > e200[i]:
        if not rev_close or c[i] > o[i]:
            side = 1
    elif wr[i - 1] >= -20.0 and wr[i] < -20.0 and e50[i] < e200[i] and c_s[i] < e200[i]:
        if not rev_close or c[i] < o[i]:
            side = -1
    if side == 0:
        return None

    sl_atr = float(params.get("sl_atr", 1.5))
    tp_r = float(params.get("tp_r", 3.5))
    rd = max(sl_atr * a_i, 1e-9)
    return {
        "side": side, "rd": float(rd), "sl_abs": None, "tp_abs": None,
        "exit_r": tp_r, "note": f"S1_WR{wr_p}={wr[i]:.1f}",
    }
