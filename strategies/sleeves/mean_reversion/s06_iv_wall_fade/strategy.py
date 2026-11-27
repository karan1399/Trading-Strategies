"""S06: Session-Anchored 2-SD IV Wall Fade (15M, Causal Stateless)."""
from __future__ import annotations

import math
import time
import numpy as np
from strategies.core.fp_features import fp_flag, fp_get, fp_val

DEFAULT_PARAMS = {
    "vol_lam": 0.94,
    "sd_wall": 2.0,
    "target_m": 0.5,
    "buffer_atr": 1.0,
    "skip_first_bars": 1,
    "broker_utc_offset": 2,
    "fp_mode": None,
    "fp_vwz_min": 2.0,
    "fp_vpin_max": 0.60,
    "fp_sl_atr": 2.5,
    "fp_tp_r": 5.0,
    "fp_be_r": 1.2,
}


def _broker_day(ts_utc_min: float, offset_h: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((ts_utc_min + offset_h * 60) * 60))


def ewma_sigma(c: np.ndarray, lam: float = 0.94) -> np.ndarray:
    """RiskMetrics per-bar sigma of log returns (causal)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.diff(np.log(np.maximum(c, 1e-12)), prepend=np.log(max(float(c[0]), 1e-12)))
    lr[0] = 0.0
    lr = np.nan_to_num(lr, nan=0.0, posinf=0.0, neginf=0.0)
    var = np.zeros(len(c))
    alpha = 1.0 - lam
    k = max(1, len(c) // 20)
    v = float((lr[:k] ** 2).mean()) if len(c) > 20 else float(lr[0] ** 2)
    for idx in range(len(c)):
        v = alpha * lr[idx] ** 2 + (1.0 - alpha) * v
        var[idx] = v
    return np.sqrt(np.maximum(var, 0.0))


def session_spans_from(times_min: np.ndarray, offset_h: int) -> list[float]:
    """Completed-session span list in hours (causal; excludes running session)."""
    if len(times_min) < 10:
        return []
    days = np.array([_broker_day(t, offset_h) for t in times_min])
    spans, i = [], 0
    n = len(days)
    while i < n:
        j = i
        while j + 1 < n and days[j + 1] == days[i]:
            j += 1
        if j + 1 < n:
            spans.append((times_min[j] - times_min[i]) / 60.0 + 0.25)
        i = j + 1
    return spans[-10:]


def _fp_signal_at(c, atr, i: int, params: dict, fp: dict) -> dict | None:
    if i < 60:
        return None
    a_i = fp_val(fp, "atr20", i, float(atr[i]))
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    vwz = fp_val(fp, "vwz48", i, float("nan"))
    vpin = fp_val(fp, "vpin_rank", i, 1.0)
    if not np.isfinite(vwz) or vpin >= float(params.get("fp_vpin_max", 0.60)):
        return None
    dp = fp_val(fp, "d_pct", i, 0.0)
    clv = fp_val(fp, "clv", i, 0.0)
    z_thr = float(params.get("fp_vwz_min", 2.0))
    ok_L = dp > 0.0 or clv > 0.20 or fp_flag(fp, "zero_bot", i) or fp_flag(fp, "cg_exh_b", i) or fp_flag(fp, "cg_abs_b", i)
    ok_S = dp < 0.0 or clv < -0.20 or fp_flag(fp, "zero_top", i) or fp_flag(fp, "cg_exh_s", i) or fp_flag(fp, "cg_abs_s", i)
    side = 1 if (vwz < -z_thr and ok_L) else (-1 if (vwz > z_thr and ok_S) else 0)
    if side == 0:
        return None
    rd = max(float(params.get("fp_sl_atr", 2.5)) * a_i, 1e-9)
    return {
        "side": side,
        "rd": float(rd),
        "sl_abs": None,
        "tp_abs": None,
        "exit_r": float(params.get("fp_tp_r", 5.0)),
        "be_r": float(params.get("fp_be_r", 1.2)),
        "trail_atr": 0.0,
        "step_lock": False,
        "max_hold": 48,
        "note": f"S6_FP_VWZ={vwz:+.2f}|VPIN={vpin:.2f}",
    }


def signal_at(o, h, l, c, times_min, atr, i: int, params: dict, extra=None) -> dict | None:
    """Fade touches outside broker-day 2.0-SD EWMA expected move walls closing back inside."""
    if i < 20 or i >= len(c):
        return None
    fp = fp_get(extra)
    use_fp = params.get("fp_mode")
    if (use_fp is True or (use_fp is None and fp is not None)) and fp is not None:
        return _fp_signal_at(c, atr, i, params, fp)
    offset = int(params.get("broker_utc_offset", 2))
    lam = float(params.get("vol_lam", 0.94))
    ext = extra or {}
    sig_series = ext.get("sig_series")
    if sig_series is None:
        sig_series = ewma_sigma(c[: i + 1], lam)
    session_spans = ext.get("session_spans")
    if session_spans is None:
        session_spans = session_spans_from(times_min[: i + 1], offset)

    sid = _broker_day(times_min[i], offset)
    start = i
    while start > 0 and _broker_day(times_min[start - 1], offset) == sid:
        start -= 1
    if i == start or (i - start) < int(params.get("skip_first_bars", 1)):
        return None
    sig_bar = float(sig_series[start - 1]) if start - 1 >= 0 else 0.0
    if not np.isfinite(sig_bar) or sig_bar <= 0.0:
        return None
    sess_open = float(o[start])
    exp_h = float(np.median(session_spans)) if len(session_spans) >= 3 else 24.0
    em = sess_open * sig_bar * math.sqrt(max(exp_h, 0.25) * 4.0)
    if not np.isfinite(em) or em <= 0.0:
        return None
    sd_wall = float(params.get("sd_wall", 2.0))
    up, dn = sess_open + sd_wall * em, sess_open - sd_wall * em
    atr14 = float(atr[i])
    if atr14 <= 0.0:
        return None
    side, wall = 0, 0.0
    if h[i] >= up and not (c[i] > up):
        side, wall = -1, up
    elif l[i] <= dn and not (c[i] < dn):
        side, wall = 1, dn
    if side == 0:
        return None
    buf = float(params.get("buffer_atr", 1.0)) * atr14
    sl_abs = wall + buf if side == -1 else wall - buf
    tp_abs = (
        sess_open - float(params.get("target_m", 0.5)) * em
        if side == -1
        else sess_open + float(params.get("target_m", 0.5)) * em
    )
    return {
        "side": side,
        "rd": None,
        "sl_abs": float(sl_abs),
        "tp_abs": float(tp_abs),
        "exit_r": None,
        "note": f"wall{sid}",
        "em": float(em),
    }
