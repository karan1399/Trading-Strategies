"""Causal O(N) footprint + OHLC feature extraction for strategies s01..s11.

All rolling windows, EMAs, VWAP Z-scores, VPIN, VPM velocity ranks, and variance
ratios use strictly backward-looking closed-bar data up to bar `i` (zero lookahead).
"""
from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from strategies.core.indicators import adx_arr, ema_arr, rsi_wilder_arr, williams_r_arr


def build_fp_context(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """Build causal numpy feature dictionary (`extra['fp']`) from a bar DataFrame.

    Accepts DataFrames with columns `o/h/l/c` (or `open/high/low/close`) and optional
    footprint columns (`d_pct`/`delta_pct`, `clv`, `buy_imb`, `sell_imb`, `zero_top`,
    `zero_bot`, `poc`, `vpin_rank`, `vpm_rk`, `vwz48`, `cg_*`).
    """
    n = len(df)
    o = df["o"].to_numpy(dtype=float) if "o" in df else df["open"].to_numpy(dtype=float)
    h = df["h"].to_numpy(dtype=float) if "h" in df else df["high"].to_numpy(dtype=float)
    l = df["l"].to_numpy(dtype=float) if "l" in df else df["low"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float) if "c" in df else df["close"].to_numpy(dtype=float)
    s_c = pd.Series(c)
    s_h = pd.Series(h)
    s_l = pd.Series(l)

    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    if n > 0:
        tr[0] = h[0] - l[0]
    atr20 = pd.Series(tr).rolling(20).mean().to_numpy(dtype=float)

    def _arr(col: str, alt: str | None = None, default: float | bool = 0.0, dtype=float) -> np.ndarray:
        if col in df:
            return df[col].to_numpy(dtype=dtype)
        if alt and alt in df:
            return df[alt].to_numpy(dtype=dtype)
        return np.full(n, default, dtype=dtype)

    v = _arr("v", "volume", default=0.0)
    delta = _arr("delta", default=0.0)
    if "d_pct" in df:
        d_pct = df["d_pct"].to_numpy(dtype=float)
    elif "delta_pct" in df:
        d_pct = df["delta_pct"].to_numpy(dtype=float)
    else:
        d_pct = np.where(v > 0.0, delta / np.maximum(v, 1e-12) * 100.0, 0.0)

    if "clv" in df:
        clv = df["clv"].to_numpy(dtype=float)
    else:
        rng = h - l
        clv = np.where(rng > 0.0, ((c - l) - (h - c)) / np.where(rng > 0.0, rng, 1.0), 0.0)

    if "vpin_rank" in df:
        vpin_rank = df["vpin_rank"].to_numpy(dtype=float)
    else:
        s_v = pd.Series(v)
        vpin12 = pd.Series(np.abs(delta)).rolling(12).sum() / s_v.rolling(12).sum().replace(0, np.nan)
        vpin_rank = vpin12.rolling(100).rank(pct=True).to_numpy(dtype=float)

    if "vpm_rk" in df:
        vpm_rk = df["vpm_rk"].to_numpy(dtype=float)
    else:
        dur_m = _arr("dur_m", default=60.0)
        vpm_rk = (pd.Series(v) / pd.Series(np.maximum(dur_m, 1.0))).rolling(50).rank(pct=True).to_numpy(dtype=float)

    if "vwz48" in df:
        vwz48 = df["vwz48"].to_numpy(dtype=float)
    else:
        s_v = pd.Series(v)
        vw = (s_c * s_v).rolling(48).sum() / s_v.rolling(48).sum().replace(0, np.nan)
        sd = s_c.rolling(48).std().replace(0, np.nan)
        vwz48 = ((s_c - vw) / sd).to_numpy(dtype=float)

    with np.errstate(divide="ignore", invalid="ignore"):
        r1 = np.log(s_c / s_c.shift(1))
        r4 = np.log(s_c / s_c.shift(4))
        vr4 = (r4.rolling(40).var() / (4.0 * r1.rolling(40).var().replace(0, np.nan))).to_numpy(dtype=float)

    out: dict[str, np.ndarray] = {
        "o": o, "h": h, "l": l, "c": c,
        "atr20": atr20,
        "ema89": _arr("ema89", default=np.nan) if "ema89" in df else ema_arr(c, 89),
        "ema233": _arr("ema233", default=np.nan) if "ema233" in df else ema_arr(c, 233),
        "rsi2": rsi_wilder_arr(c, 2),
        "wr14": williams_r_arr(h, l, c, 14),
        "adx14": adx_arr(h, l, c, 14),
        "d_pct": d_pct,
        "clv": clv,
        "buy_imb": _arr("buy_imb", "buy_imbalances", default=0.0),
        "sell_imb": _arr("sell_imb", "sell_imbalances", default=0.0),
        "zero_top": _arr("zero_top", default=False, dtype=bool),
        "zero_bot": _arr("zero_bot", default=False, dtype=bool),
        "poc": _arr("poc", default=np.nan),
        "vpin_rank": vpin_rank,
        "vpm_rk": vpm_rk,
        "vwz48": vwz48,
        "vr4": vr4,
    }
    for tk in ("abs", "div", "exh", "trap", "sdiv"):
        out[f"cg_{tk}_b"] = _arr(f"cg_{tk}_b", default=False, dtype=bool)
        out[f"cg_{tk}_s"] = _arr(f"cg_{tk}_s", default=False, dtype=bool)
    return out


def fp_get(extra: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return footprint context dictionary from `extra` if present."""
    if not extra:
        return None
    fp = extra.get("fp")
    return fp if isinstance(fp, dict) else None


def fp_val(fp: dict[str, Any], key: str, i: int, default: float = 0.0) -> float:
    """Safely read scalar float from precomputed `fp[key][i]` with fallback."""
    arr = fp.get(key)
    if arr is None or i < 0 or i >= len(arr):
        return default
    v = float(arr[i])
    return v if np.isfinite(v) else default


def fp_flag(fp: dict[str, Any], key: str, i: int) -> bool:
    """Safely read scalar bool from precomputed `fp[key][i]`."""
    arr = fp.get(key)
    if arr is None or i < 0 or i >= len(arr):
        return False
    return bool(arr[i])
