#!/usr/bin/env python3
"""Shared helpers for alpha360 stages B/C: symbol families, confluence masks,
RSI, ATR-regime ratio, cross-asset confirmation."""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import ema, wilder_atr


_INDEX = ("AU200", "AUS200", "CHINA50", "CHINAH", "DJ30", "FR40", "GER40", "HK50",
          "JP225", "NAS100", "NETH25", "SP500", "STOXX50", "SWISS20", "UK100",
          "US30", "US500", "USTEC", "USDX", "DXY")
_METALS = ("XAG", "XPT", "XPD", "COPPER", "ALUMINIUM", "LEAD", "NICKEL")
_ENERGY = ("OIL", "GAS", "BRENT", "BRN", "XTI", "XBR")
_USD_MAJORS = ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF")


def classify_family(symbol: str) -> str:
    s = symbol.upper()
    if s.startswith("XAU") or s.startswith("GAU"):
        return "gold"
    if s.startswith(_METALS):
        return "metals_other"
    if s.startswith(_INDEX):
        return "index"
    if s.startswith(_ENERGY):
        return "energy"
    if s.endswith(".P"):
        return "crypto"
    if s.endswith("JPY.PI") or s.endswith("JPY"):
        return "jpy"
    if s.startswith(_USD_MAJORS):
        return "usd_major"
    return "fx_cross"


def wilder_rsi(c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    out = np.zeros(n)
    if n <= period + 1:
        return out
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag, al = gain[:period].mean(), loss[:period].mean()
    out[period] = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else 99.0))
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + gain[i - 1]) / period
        al = (al * (period - 1) + loss[i - 1]) / period
        out[i] = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else 99.0))
    return out


def confluence_masks(df: pd.DataFrame, symbol: str, tf: str,
                     driver_slopes: dict | None = None) -> dict[str, np.ndarray]:
    """Per-bar boolean masks (AND overlays). driver_slopes maps driver symbol ->
    pd.Series of +1/-1 EMA50 slope aligned by timestamp (cross-asset confirm)."""
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    v = df["tick_volume"].to_numpy(float)
    n = len(c)
    atr = wilder_atr(h, l, c, 14)
    rsi = wilder_rsi(c, 14)
    masks = {}

    ok = (rsi > 0) & (atr > 0)
    longs = ok & (rsi >= 40) & (rsi <= 65)
    shorts = ok & (rsi >= 35) & (rsi <= 60)
    masks["rsi"] = (longs, shorts)

    # ATR regime: current ATR vs trailing-500-bar median (0.7x .. 1.6x)
    med = pd.Series(atr).rolling(500, min_periods=100).median().to_numpy()
    ratio = np.where(med > 0, atr / med, np.nan)
    okr = np.nan_to_num(ratio, nan=0.0)
    m = (okr >= 0.7) & (okr <= 1.6)
    masks["atr"] = (m, m)

    # volume surge: tick_volume >= 1.2 x SMA20
    sma = pd.Series(v).rolling(20, min_periods=5).mean().to_numpy()
    mv = np.nan_to_num(v / np.maximum(sma, 1e-9), nan=0.0) >= 1.2
    masks["vol"] = (mv, mv)

    # session: London+NY. 1H -> open hour 7..16 UTC; 4H -> open hour in {8, 12}
    hrs = df.index.hour.to_numpy()
    ms = (hrs >= 7) & (hrs <= 16) if tf == "1H" else np.isin(hrs, (8, 12))
    masks["session"] = (ms, ms)

    # cross-asset driver confirm
    if driver_slopes:
        for name, slope in driver_slopes.items():
            aligned = slope.reindex(df.index, method="ffill").to_numpy(float)
            good = np.nan_to_num(aligned, nan=0.0)
            masks[name] = (good > 0, good < 0)
    return masks


def driver_slope_series(tf: str, driver: str, period: int = 50) -> pd.Series | None:
    """+1/-1 EMA slope series for a driver symbol, indexed by its timestamps."""
    from dfib_engine import load_symbol
    try:
        d = load_symbol(tf, driver)
    except Exception:
        return None
    c = d["close"].to_numpy(float)
    e = ema(c, period)
    slope = np.zeros(len(c))
    slope[period + 5:] = np.where(e[period + 5:] > e[period:-5], 1,
                                  np.where(e[period + 5:] < e[period:-5], -1, 0))
    return pd.Series(slope, index=d.index)


def apply_overlay(sides, rds, masks_long, masks_short):
    """AND the side-appropriate mask into the signal arrays. Returns (sides, rds);
    reuse the original setups array - masked bars have side 0 and are never read."""
    keep = np.where(sides > 0, masks_long, np.where(sides < 0, masks_short, True))
    return (np.where(keep, sides, 0).astype(np.int8), np.where(keep, rds, 0.0))
