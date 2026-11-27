"""IG-S2: Connors 2-Bar RSI(2) Mean Reversion + ADX Regime Cap (H1/H4).

Enters trend-aligned pullbacks using Connors cumulative 2-bar RSI(2), ADX ceiling
(preventing crash-catching during extreme blow-offs), rising/falling EMA200 slope,
and reversal close confirmation (c[i] > c[i-1] / c[i] < c[i-1]).
"""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import adx_arr, ema_arr, rsi_wilder_arr
from strategies.core.fp_features import fp_flag, fp_get, fp_val


DEFAULT_PARAMS = {
    "connors_rev_adx35": True,
    "rsi_lo": 10.0,
    "adx_max": 35.0,
    "sl_atr": 1.5,
    "tp_r": 2.5,
    "max_hold_bars": 30,
    "fp_mode": None,
    "fp_rsi_lo": 15.0,
    "fp_vpin_max": 0.65,
    "fp_sl_atr": 2.0,
    "fp_tp_r": 3.5,
    "fp_be_r": 1.0,
}


def _fp_signal_at(h, l, c, atr, i: int, params: dict, fp: dict) -> dict | None:
    if i < 235:
        return None
    a_i = fp_val(fp, "atr20", i, float(atr[i]))
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    r2 = fp_val(fp, "rsi2", i, float("nan"))
    e233 = fp_val(fp, "ema233", i, float("nan"))
    vpin = fp_val(fp, "vpin_rank", i, 1.0)
    if not (np.isfinite(r2) and np.isfinite(e233)) or vpin >= float(params.get("fp_vpin_max", 0.65)):
        return None

    dp = fp_val(fp, "d_pct", i, 0.0)
    clv = fp_val(fp, "clv", i, 0.0)
    rsi_thr = float(params.get("fp_rsi_lo", 15.0))
    of_L = fp_flag(fp, "cg_abs_b", i) or fp_flag(fp, "cg_exh_b", i) or fp_flag(fp, "cg_div_b", i) or (dp > 5.0 and clv > 0.25)
    of_S = fp_flag(fp, "cg_abs_s", i) or fp_flag(fp, "cg_exh_s", i) or fp_flag(fp, "cg_div_s", i) or (dp < -5.0 and clv < -0.25)

    side = 0
    if r2 < rsi_thr and float(c[i]) > e233 and of_L:
        side = 1
    elif r2 > (100.0 - rsi_thr) and float(c[i]) < e233 and of_S:
        side = -1
    if side == 0:
        return None

    rd = max(float(params.get("fp_sl_atr", 2.0)) * a_i, 1e-9)
    return {
        "side": side,
        "rd": float(rd),
        "sl_abs": None,
        "tp_abs": None,
        "exit_r": float(params.get("fp_tp_r", 3.5)),
        "be_r": float(params.get("fp_be_r", 1.0)),
        "trail_atr": 0.0,
        "step_lock": False,
        "max_hold": 48,
        "note": f"S2_FP_RSI2={r2:.1f}|d%={dp:+.1f}|VPIN={vpin:.2f}",
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
    r2 = rsi_wilder_arr(c_s, 2)
    e200 = ema_arr(c_s, 200)
    rsi_lo = float(params.get("rsi_lo", 10.0))
    use_connors = bool(params.get("connors_rev_adx35", True))

    side = 0
    if not use_connors:
        if r2[i] < rsi_lo and c_s[i] > e200[i]:
            side = 1
        elif r2[i] > (100.0 - rsi_lo) and c_s[i] < e200[i]:
            side = -1
    else:
        adx_max = float(params.get("adx_max", 35.0))
        if float(adx_arr(h_s, l_s, c_s, 14)[i]) > adx_max:
            return None
        cum2 = 0.5 * (float(r2[i]) + float(r2[i - 1]))
        if cum2 < rsi_lo + 5.0 and c_s[i] > e200[i] and e200[i] > e200[i - 5] and c_s[i] > c_s[i - 1]:
            side = 1
        elif cum2 > 100.0 - (rsi_lo + 5.0) and c_s[i] < e200[i] and e200[i] < e200[i - 5] and c_s[i] < c_s[i - 1]:
            side = -1
    if side == 0:
        return None

    sl_atr = float(params.get("sl_atr", 1.5))
    tp_r = float(params.get("tp_r", 2.5))
    rd = max(sl_atr * a_i, 1e-9)
    return {
        "side": side, "rd": float(rd), "sl_abs": None, "tp_abs": None,
        "exit_r": tp_r, "note": f"S2_C2R={r2[i]:.1f}",
    }
