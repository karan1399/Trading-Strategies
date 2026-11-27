"""S03: HTF EMA(233) P4 Cluster-Stall Rejection (4H, Causal Stateless)."""
from __future__ import annotations

import numpy as np
from strategies.core.indicators import ema_arr
from strategies.core.fp_features import fp_get, fp_val

DEFAULT_PARAMS = {
    "ema_period": 233,
    "pattern": "P4_cluster",
    "slope_gate": True,
    "regime_gate": False,
    "exit_r": 2.5,
    "fp_mode": None,
    "fp_min_imb": 2,
    "fp_min_clv": 0.25,
    "fp_sl_atr": 2.5,
    "fp_tp_r": 3.5,
    "fp_be_r": 1.0,
}


def _fp_signal_at(h, l, c, atr, i: int, params: dict, fp: dict) -> dict | None:
    if i < 235:
        return None
    a_i = fp_val(fp, "atr20", i, float(atr[i]))
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    e_cur = fp_val(fp, "ema233", i, float("nan"))
    e_prev = fp_val(fp, "ema233", i - 2, float("nan"))
    if not (np.isfinite(e_cur) and np.isfinite(e_prev)):
        return None

    clv = fp_val(fp, "clv", i, 0.0)
    bi = int(fp_val(fp, "buy_imb", i, 0.0))
    si = int(fp_val(fp, "sell_imb", i, 0.0))
    min_imb = int(params.get("fp_min_imb", 2))
    min_clv = float(params.get("fp_min_clv", 0.25))

    side = 0
    if float(l[i]) <= e_cur + 0.30 * a_i and float(c[i]) > e_cur and float(c[i - 2]) > e_prev and bi >= min_imb and clv > min_clv:
        side = 1
    elif float(h[i]) >= e_cur - 0.30 * a_i and float(c[i]) < e_cur and float(c[i - 2]) < e_prev and si >= min_imb and clv < -min_clv:
        side = -1
    if side == 0:
        return None

    rd = max(float(params.get("fp_sl_atr", 2.5)) * a_i, 1e-9)
    return {
        "side": side,
        "rd": float(rd),
        "sl_abs": None,
        "tp_abs": None,
        "exit_r": float(params.get("fp_tp_r", 3.5)),
        "be_r": float(params.get("fp_be_r", 1.0)),
        "trail_atr": 0.0,
        "step_lock": False,
        "max_hold": 72,
        "note": f"S3_FP_E233|imb={bi if side == 1 else si}|clv={clv:+.2f}",
    }


def _cluster_break_at(o, h, l, c, atr, i: int, level: float, side_bias: int):
    if i < 3:
        return 0.0, 0.0
    tol = max(1e-5, 0.20 * float(atr[i]))
    ch, cl, cc = h[i - 3:i], l[i - 3:i], c[i - 3:i]
    if side_bias == 1:
        touches = int((cl >= level - tol).sum())
        if touches >= 2 and np.min(cc) >= level - 0.15 * float(atr[i]) and c[i] > np.max(ch):
            struct = float(np.min(cl)) - 0.20 * float(atr[i])
            return struct, max(float(c[i]) - struct, 1.2 * float(atr[i]))
    else:
        touches = int((ch <= level + tol).sum())
        if touches >= 2 and np.max(cc) <= level + 0.15 * float(atr[i]) and c[i] < np.min(cl):
            struct = float(np.max(ch)) + 0.20 * float(atr[i])
            return struct, max(struct - float(c[i]), 1.2 * float(atr[i]))
    return 0.0, 0.0


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    """Evaluate causal P4 cluster stall & breakout around EMA(period) at closed bar i."""
    period = int(params.get("ema_period", 233))
    if i < period + 10 or i < 5 or i >= len(c):
        return None
    if h[i] <= l[i] or not np.isfinite(atr[i]) or atr[i] <= 0.0:
        return None
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None:
        return _fp_signal_at(h, l, c, atr, i, params, fp)
    ef = ema_arr(c[: i + 1], period)
    e = float(ef[i])
    if e <= 0.0:
        return None
    rising = e > float(ef[i - 5])
    falling = e < float(ef[i - 5])
    if params.get("slope_gate", False) and not (rising or falling):
        return None
    sb = 1 if rising else (-1 if falling else 0)
    if sb == 0:
        return None
    _, rd = _cluster_break_at(o, h, l, c, atr, i, e, sb)
    if rd <= 0.0:
        return None
    if params.get("regime_gate", False):
        es = ema_arr(c[: i + 1], 233)
        if sb == 1 and not c[i] > es[i]:
            return None
        if sb == -1 and not c[i] < es[i]:
            return None
    return {
        "side": sb,
        "rd": float(rd),
        "sl_abs": None,
        "tp_abs": None,
        "exit_r": float(params.get("exit_r", 2.5)),
        "note": f"P4_cluster/EMA{period}",
    }
