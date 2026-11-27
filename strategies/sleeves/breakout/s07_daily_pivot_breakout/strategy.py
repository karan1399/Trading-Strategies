"""IG-S7: Daily Turning-Point Pivot Breakout (4H, Causal Stateless).

Confirms D1 swing highs/lows with k-day causal lag (known strictly after day d+k closes),
tracks nearest un-broken levels, and fires on the FIRST 4H close-through aligned with
4H EMA50/EMA200 trend filter. Structural stop = sl_atr * ATR(14), TP = tp_r * R.
"""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import adx_arr, ema_arr, er_arr
from strategies.core.fp_features import fp_get, fp_val


DEFAULT_PARAMS = {
    "swing_k": 2,
    "sl_atr": 2.0,
    "tp_r": 2.5,
    "be_r": None,
    "trend_ema50_200": True,
    "adx_min": 0.0,
    "er_min": 0.0,
    "max_levels": 3,
    "fp_mode": None,
}


def _fp_gate_pass(c, i: int, side: int, fp: dict | None) -> bool:
    if fp is None:
        return True
    e233 = fp_val(fp, "ema233", i, float("nan"))
    poc = fp_val(fp, "poc", i, float("nan"))
    clv, dp = fp_val(fp, "clv", i, 0.0), fp_val(fp, "d_pct", i, 0.0)
    bi, si = int(fp_val(fp, "buy_imb", i, 0.0)), int(fp_val(fp, "sell_imb", i, 0.0))
    if side == 1:
        return np.isfinite(e233) and float(c[i]) > e233 and clv > 0.25 and dp > 0.0 and bi >= si and (not np.isfinite(poc) or float(c[i]) >= poc)
    return np.isfinite(e233) and float(c[i]) < e233 and clv < -0.25 and dp < 0.0 and si >= bi and (not np.isfinite(poc) or float(c[i]) <= poc)


def _d1_bars_from_arrays(h: np.ndarray, l: np.ndarray, c: np.ndarray, times_min: np.ndarray):
    """Vectorized D1 aggregation of closed bars 0..i with bar-end index per UTC day."""
    days = times_min // 1440
    bounds = np.flatnonzero(days[1:] != days[:-1])
    starts = np.insert(bounds + 1, 0, 0)
    ends = np.append(bounds, len(c) - 1)
    d_highs = np.maximum.reduceat(h, starts)
    d_lows = np.minimum.reduceat(l, starts)
    return days[starts], d_highs, d_lows, ends


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    if i < 205 or i >= len(c):
        return None
    a_i = float(atr[i])
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None

    # Slice strictly 0..i to guarantee prefix == full causality
    h_s, l_s, c_s, tm_s = h[: i + 1], l[: i + 1], c[: i + 1], times_min[: i + 1]
    adx_min = float(params.get("adx_min", 0.0) or 0.0)
    if adx_min > 0.0:
        if float(adx_arr(h_s, l_s, c_s, 14)[i]) < adx_min:
            return None
    er_min = float(params.get("er_min", 0.0) or 0.0)
    if er_min > 0.0:
        if float(er_arr(c_s, 14)[i]) < er_min:
            return None
    e50 = ema_arr(c_s, 50)
    e200 = ema_arr(c_s, 200)
    c_prev, c_cur = float(c_s[i - 1]), float(c_s[i])

    trend_f = bool(params.get("trend_ema50_200", True))
    up_ok = (e50[i] > e200[i] and c_cur > e200[i]) if trend_f else (c_cur > e200[i])
    dn_ok = (e50[i] < e200[i] and c_cur < e200[i]) if trend_f else (c_cur < e200[i])
    if not ((up_ok and c_cur > c_prev) or (dn_ok and c_cur < c_prev)):
        return None

    k = int(params.get("swing_k", 3))
    max_levels = int(params.get("max_levels", 3))
    sl_atr = float(params.get("sl_atr", 2.0))
    tp_r = float(params.get("tp_r", 3.5))

    uniq_days, dh, dl, d_end = _d1_bars_from_arrays(h_s, l_s, c_s, tm_s)
    # Only days strictly completed before today's UTC day (exclude last day if forming; k lag protects too)
    n_days = len(uniq_days) - 1
    if n_days < 2 * k + 2:
        return None

    active_highs = []
    active_lows = []
    for j in range(k, n_days - k):
        conf_bar = d_end[j + k]
        if conf_bar >= i:
            continue
        first_live = conf_bar + 1
        win_h = dh[j - k : j + k + 1]
        win_l = dl[j - k : j + k + 1]
        if dh[j] == float(np.max(win_h)):
            lvl = float(dh[j])
            # Unbroken if no close between first_live..i-1 exceeded lvl
            if first_live < i:
                if float(np.max(c_s[first_live:i])) <= lvl:
                    active_highs.append(lvl)
            else:
                active_highs.append(lvl)
        if dl[j] == float(np.min(win_l)):
            lvl = float(dl[j])
            if first_live < i:
                if float(np.min(c_s[first_live:i])) >= lvl:
                    active_lows.append(lvl)
            else:
                active_lows.append(lvl)

    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    fp_active = (use_fp is True or (use_fp is None and fp is not None)) and fp is not None
    if up_ok and c_cur > c_prev:
        near_h = sorted(x for x in active_highs if x >= c_prev)[:max_levels]
        for lvl in near_h:
            if c_prev <= lvl < c_cur and (not fp_active or _fp_gate_pass(c_s, i, 1, fp)):
                rd = max(sl_atr * (fp_val(fp, "atr20", i, a_i) if fp_active else a_i), 1e-9)
                return {
                    "side": 1, "rd": float(rd), "sl_abs": None, "tp_abs": None,
                    "exit_r": 0.0 if fp_active else tp_r,
                    "be_r": 1.0 if fp_active else None,
                    "trail_atr": 2.5 if fp_active else 0.0,
                    "step_lock": fp_active,
                    "note": f"S7{'_FP' if fp_active else ''}_pivH@{lvl:.4g}",
                }
    if dn_ok and c_cur < c_prev:
        near_l = sorted((x for x in active_lows if x <= c_prev), reverse=True)[:max_levels]
        for lvl in near_l:
            if c_prev >= lvl > c_cur and (not fp_active or _fp_gate_pass(c_s, i, -1, fp)):
                rd = max(sl_atr * (fp_val(fp, "atr20", i, a_i) if fp_active else a_i), 1e-9)
                return {
                    "side": -1, "rd": float(rd), "sl_abs": None, "tp_abs": None,
                    "exit_r": 0.0 if fp_active else tp_r,
                    "be_r": 1.0 if fp_active else None,
                    "trail_atr": 2.5 if fp_active else 0.0,
                    "step_lock": fp_active,
                    "note": f"S7{'_FP' if fp_active else ''}_pivL@{lvl:.4g}",
                }
    return None
