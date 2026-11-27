"""IG-S10: Trend-Aligned EMA21/EMA50 Pullback & Body Rejection (H4 Crypto / H1 Commod).

Enters when price taps sloping fast EMA within 0.25*ATR aligned with EMA200 regime,
rejected by a directional candle body >= body_min of bar range and ADX >= adx_min.
"""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import adx_arr, ema_arr
from strategies.core.fp_features import fp_flag, fp_get, fp_val


DEFAULT_PARAMS = {
    "ema_period": 50,
    "adx_min": 22.0,
    "body_min": 0.30,
    "tp_r": 3.5,
    "fp_mode": None,
    "fp_min_dp": 5.0,
    "fp_min_clv": 0.25,
    "fp_zero_branch": True,
    "fp_min_vpm": 0.40,
    "fp_sl_atr": 2.0,
    "fp_trail_atr": 2.5,
    "fp_be_r": 1.0,
}


def _fp_signal_at(h, l, c, atr, i: int, params: dict, fp: dict) -> dict | None:
    if i < 235:
        return None
    a_i = fp_val(fp, "atr20", i, float(atr[i]))
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    e89 = fp_val(fp, "ema89", i, float("nan"))
    e89_p = fp_val(fp, "ema89", i - 3, float("nan"))
    e233 = fp_val(fp, "ema233", i, float("nan"))
    if not (np.isfinite(e89) and np.isfinite(e89_p) and np.isfinite(e233)):
        return None

    stkL = (e89 > e233) and (e89 > e89_p) and (float(c[i]) > e233)
    stkS = (e89 < e233) and (e89 < e89_p) and (float(c[i]) < e233)
    if not (stkL or stkS):
        return None

    dp = fp_val(fp, "d_pct", i, 0.0)
    clv = fp_val(fp, "clv", i, 0.0)
    vpm = fp_val(fp, "vpm_rk", i, 0.0)
    min_dp = float(params.get("fp_min_dp", 5.0))
    min_clv = float(params.get("fp_min_clv", 0.25))
    use_zero = bool(params.get("fp_zero_branch", True))
    min_vpm = float(params.get("fp_min_vpm", 0.40))

    touch_L = stkL and float(l[i]) <= e89 + 0.25 * a_i and float(c[i]) > e89 and dp > min_dp and clv > min_clv
    touch_S = stkS and float(h[i]) >= e89 - 0.25 * a_i and float(c[i]) < e89 and dp < -min_dp and clv < -min_clv
    zero_L = use_zero and stkL and fp_flag(fp, "zero_bot", i) and vpm > min_vpm
    zero_S = use_zero and stkS and fp_flag(fp, "zero_top", i) and vpm > min_vpm

    side = 1 if (touch_L or zero_L) else (-1 if (touch_S or zero_S) else 0)
    if side == 0:
        return None

    tag = "S10_FP_ZERO" if (zero_L or zero_S) else "S10_FP_E89"
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
        "note": f"{tag}|d%={dp:+.1f}|vpm={vpm:.2f}",
    }


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    if i < 205 or i >= len(c):
        return None
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None:
        return _fp_signal_at(h, l, c, atr, i, params, fp)
    a_i = float(atr[i])
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None

    h_s, l_s, c_s = h[: i + 1], l[: i + 1], c[: i + 1]
    adx_min = float(params.get("adx_min", 22.0))
    if float(adx_arr(h_s, l_s, c_s, 14)[i]) < adx_min:
        return None

    rng = max(float(h[i] - l[i]), 1e-12)
    body_min = float(params.get("body_min", 0.30))
    if abs(float(c[i] - o[i])) / rng < body_min:
        return None

    ep = int(params.get("ema_period", 50))
    ef = ema_arr(c_s, ep)
    e200 = ema_arr(c_s, 200)
    tol = 0.25 * a_i

    side = 0
    rd = 0.0
    if ef[i] > e200[i] and ef[i] > ef[i - 5] and l[i] <= ef[i] + tol and c[i] > ef[i] and c[i] > o[i]:
        side = 1
        rd = max(float(c[i]) - (min(float(l[i]), float(ef[i])) - 0.2 * a_i), 1.2 * a_i)
    elif ef[i] < e200[i] and ef[i] < ef[i - 5] and h[i] >= ef[i] - tol and c[i] < ef[i] and c[i] < o[i]:
        side = -1
        rd = max((max(float(h[i]), float(ef[i])) + 0.2 * a_i) - float(c[i]), 1.2 * a_i)
    if side == 0:
        return None

    tp_r = float(params.get("tp_r", 3.5))
    return {
        "side": side, "rd": float(rd), "sl_abs": None, "tp_abs": None,
        "exit_r": tp_r, "note": f"S10_E{ep}",
    }
