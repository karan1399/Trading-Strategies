"""S11: London Open OR30 Opening-Range Breakout (15M, Causal Stateless)."""
from __future__ import annotations

import time
import numpy as np
from strategies.core.indicators import adx_arr, ema_arr
from strategies.core.fp_features import fp_get, fp_val

DEFAULT_PARAMS = {
    "anchor_utc": "08:00",
    "or_minutes": 30,
    "signal_window_hours": 6.0,
    "htf_trend": True,
    "htf_tf": "H4",
    "htf_ema": 200,
    "sl_mode": "opposite",
    "max_or_atr": 2.0,
    "adx_min": 0.0,
    "exit_r": 3.0,
    "fp_mode": None,
    "fp_min_dp": 3.0,
    "fp_min_clv": 0.30,
    "fp_min_imb": 2,
    "fp_min_vpm": 0.45,
}


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    """Evaluate causal OR30 breakout aligned with optional H4 EMA trend at bar i."""
    if i < 2 or i >= len(c):
        return None
    ah, am = str(params.get("anchor_utc", "14:30")).split(":")
    anchor = int(ah) * 60 + int(am)
    or_min = int(params.get("or_minutes", 30))
    nbars = or_min // 15
    day = time.strftime("%Y-%m-%d", time.gmtime(times_min[i] * 60))
    day_start = None
    j = i
    while j >= 0 and time.strftime("%Y-%m-%d", time.gmtime(times_min[j] * 60)) == day:
        day_start = j
        j -= 1
    if day_start is None:
        return None
    day_ts = [times_min[k] for k in range(day_start, i + 1)]
    anchor_idx = [k for k, t in enumerate(day_ts) if anchor <= (t % 1440) < anchor + or_min]
    if len(anchor_idx) < nbars:
        return None
    bars_in_day = list(range(day_start, i + 1))
    or_bars = [bars_in_day[k] for k in anchor_idx]
    if i < or_bars[-1]:
        return None
    or_high = max(float(h[k]) for k in or_bars)
    or_low = min(float(l[k]) for k in or_bars)
    sig_end = anchor + int(float(params.get("signal_window_hours", 6.0)) * 60)
    if (times_min[i] % 1440) > sig_end:
        return None
    a_i = float(atr[i]) if i < len(atr) else 0.0
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    max_oa = float(params.get("max_or_atr", 99.0) or 99.0)
    if max_oa < 90.0 and ((or_high - or_low) / a_i) > max_oa:
        return None
    adx_min = float(params.get("adx_min", 0.0) or 0.0)
    if adx_min > 0.0 and float(adx_arr(h[: i + 1], l[: i + 1], c[: i + 1], 14)[i]) < adx_min:
        return None
    side = 1 if (c[i] > or_high and c[i] >= o[i]) else (-1 if (c[i] < or_low and c[i] <= o[i]) else 0)
    if side == 0:
        return None
    htf_c_4h = (extra or {}).get("htf_c4")
    if params.get("htf_trend", False) and htf_c_4h is not None and len(htf_c_4h) > int(params.get("htf_ema", 200)):
        e4 = ema_arr(htf_c_4h, int(params.get("htf_ema", 200)))
        if (side == 1 and not c[i] > e4[-1]) or (side == -1 and not c[i] < e4[-1]):
            return None
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None and i >= 235:
        e89, e89_p = fp_val(fp, "ema89", i, float("nan")), fp_val(fp, "ema89", i - 3, float("nan"))
        e233 = fp_val(fp, "ema233", i, float("nan"))
        clv, dp = fp_val(fp, "clv", i, 0.0), fp_val(fp, "d_pct", i, 0.0)
        bi, si = int(fp_val(fp, "buy_imb", i, 0.0)), int(fp_val(fp, "sell_imb", i, 0.0))
        vpm = fp_val(fp, "vpm_rk", i, 0.0)
        if not (np.isfinite(e89) and np.isfinite(e89_p) and np.isfinite(e233)):
            return None
        if side == 1 and not (e89 > e233 and e89 > e89_p and c[i] > e233 and clv > float(params.get("fp_min_clv", 0.30)) and dp > float(params.get("fp_min_dp", 3.0)) and bi >= int(params.get("fp_min_imb", 2)) and vpm > float(params.get("fp_min_vpm", 0.45))):
            return None
        if side == -1 and not (e89 < e233 and e89 < e89_p and c[i] < e233 and clv < -float(params.get("fp_min_clv", 0.30)) and dp < -float(params.get("fp_min_dp", 3.0)) and si >= int(params.get("fp_min_imb", 2)) and vpm > float(params.get("fp_min_vpm", 0.45))):
            return None
        return {
            "side": side,
            "rd": float(2.0 * fp_val(fp, "atr20", i, a_i)),
            "sl_abs": None,
            "tp_abs": None,
            "exit_r": 0.0,
            "be_r": 1.0,
            "trail_atr": 2.5,
            "step_lock": True,
            "max_hold": 96,
            "note": f"S11_FP_OR{or_min}_{day}|vpm={vpm:.2f}",
        }
    sl_mode = str(params.get("sl_mode", "midpoint")).lower()
    sl_val = float(or_low if side == 1 else or_high) if sl_mode == "opposite" else float((or_high + or_low) / 2.0)
    return {
        "side": side,
        "rd": None,
        "sl_abs": sl_val,
        "tp_abs": None,
        "exit_r": float(params.get("exit_r", 2.0)),
        "note": f"OR{or_min}_{day}",
    }
