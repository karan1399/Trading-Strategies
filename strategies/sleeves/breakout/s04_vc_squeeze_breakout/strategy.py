"""S04: Volatility Compression Squeeze Breakout (4H, Causal Stateless)."""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import adx_arr, ema_arr, sma_arr, wilder_atr
from strategies.core.fp_features import fp_get, fp_val

DEFAULT_PARAMS = {
    "bb_len": 20,
    "bb_dev": 2.0,
    "kc_len": 20,
    "kc_mult": 1.5,
    "min_squeeze_bars": 3,
    "trend_ema": 200,
    "adx_min": 0.0,
    "sl_basis": True,
    "sl_atr_floor": 1.2,
    "exit_r": 4.0,
    "fp_mode": None,
    "fp_min_vpm": 0.40,
    "fp_min_vr4": 0.95,
}


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    """Detect Bollinger Band inside Keltner Channel squeeze release aligned with EMA200."""
    if i < max(int(params.get("trend_ema", 200)), 60) + 10 or i >= len(c):
        return None
    atr14 = float(atr[i])
    if not np.isfinite(atr14) or atr14 <= 0.0:
        return None
    adx_min = float(params.get("adx_min", 0.0) or 0.0)
    if adx_min > 0.0 and float(adx_arr(h[: i + 1], l[: i + 1], c[: i + 1], 14)[i]) < adx_min:
        return None
    bb_len, dev = int(params.get("bb_len", 20)), float(params.get("bb_dev", 2.0))
    mult = float(params.get("kc_mult", 1.5))
    seg_c = c[: i + 1]
    sma = sma_arr(seg_c, bb_len)
    need = int(params.get("min_squeeze_bars", 3))
    j0 = i - need
    if j0 < bb_len:
        return None
    sd = np.zeros(i + 1)
    for k in range(max(bb_len - 1, j0), i + 1):
        sd[k] = seg_c[k - bb_len + 1: k + 1].std(ddof=0)
    bb_up, bb_dn = sma + dev * sd, sma - dev * sd
    atr_kc = wilder_atr(h[: i + 1], l[: i + 1], seg_c, 14)
    kc_up, kc_dn = sma + mult * atr_kc, sma - mult * atr_kc
    if not all(bb_up[k] < kc_up[k] and bb_dn[k] > kc_dn[k] for k in range(j0, i)):
        return None
    ema200 = ema_arr(seg_c, int(params.get("trend_ema", 200)))
    ema50 = ema_arr(seg_c, 50)
    bullish, bearish = c[i] >= o[i], c[i] <= o[i]
    side = 0
    if c[i] > bb_up[i] and bullish and c[i] > ema200[i] and ema50[i] >= ema200[i]:
        side = 1
    elif c[i] < bb_dn[i] and bearish and c[i] < ema200[i] and ema50[i] <= ema200[i]:
        side = -1
    if side == 0:
        return None
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None:
        e89, e89_p = fp_val(fp, "ema89", i, float("nan")), fp_val(fp, "ema89", i - 3, float("nan"))
        e233 = fp_val(fp, "ema233", i, float("nan"))
        dp, clv = fp_val(fp, "d_pct", i, 0.0), fp_val(fp, "clv", i, 0.0)
        bi, si = int(fp_val(fp, "buy_imb", i, 0.0)), int(fp_val(fp, "sell_imb", i, 0.0))
        vpm, vr4 = fp_val(fp, "vpm_rk", i, 0.0), fp_val(fp, "vr4", i, 0.0)
        if not (np.isfinite(e89) and np.isfinite(e89_p) and np.isfinite(e233)):
            return None
        if side == 1 and not (e89 > e233 and e89 > e89_p and c[i] > e233 and clv > 0.30 and dp > 3.0 and bi >= si and vpm > float(params.get("fp_min_vpm", 0.40)) and vr4 > float(params.get("fp_min_vr4", 0.95))):
            return None
        if side == -1 and not (e89 < e233 and e89 < e89_p and c[i] < e233 and clv < -0.30 and dp < -3.0 and si >= bi and vpm > float(params.get("fp_min_vpm", 0.40)) and vr4 > float(params.get("fp_min_vr4", 0.95))):
            return None
        return {
            "side": side,
            "rd": float(2.0 * fp_val(fp, "atr20", i, atr14)),
            "sl_abs": None,
            "tp_abs": None,
            "exit_r": 0.0,
            "be_r": 1.0,
            "trail_atr": 2.5,
            "step_lock": True,
            "max_hold": 96,
            "note": f"S4_FP_squeeze{need}|vr4={vr4:.2f}",
        }
    basis = float(sma[i])
    rd = abs(float(c[i]) - basis) if (params.get("sl_basis", True) and basis > 0.0) else 0.0
    rd = max(rd, float(params.get("sl_atr_floor", 1.2)) * atr14, 1e-9)
    return {
        "side": side,
        "rd": float(rd),
        "sl_abs": None,
        "tp_abs": None,
        "exit_r": float(params.get("exit_r", 4.0)),
        "note": f"squeeze{need}bb",
    }
