"""Shared deterministic technical & order-flow indicators for strategy sleeves."""
from __future__ import annotations

from numba import njit
import numpy as np


@njit(cache=True, nogil=True)
def _ema_nb(c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    out = np.zeros(n, dtype=np.float64)
    if n < period or period <= 0:
        return out
    s = 0.0
    for k in range(period):
        s += c[k]
    out[period - 1] = s / period
    a = 2.0 / (period + 1.0)
    om = 1.0 - a
    for i in range(period, n):
        out[i] = a * c[i] + om * out[i - 1]
    return out


def ema_arr(c: np.ndarray, period: int) -> np.ndarray:
    return _ema_nb(np.asarray(c, dtype=np.float64), int(period))


def sma_arr(c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan, dtype=float)
    if n < period or period <= 0:
        return out
    cs = np.cumsum(np.insert(c.astype(float), 0, 0.0))
    out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def rolling_std_arr(c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan, dtype=float)
    if n < period or period <= 1:
        return out
    c_f = c.astype(float)
    cs = np.cumsum(np.insert(c_f, 0, 0.0))
    cs2 = np.cumsum(np.insert(c_f * c_f, 0, 0.0))
    mu = (cs[period:] - cs[:-period]) / period
    var = np.maximum(0.0, (cs2[period:] - cs2[:-period]) / period - mu * mu)
    out[period - 1:] = np.sqrt(var)
    return out


@njit(cache=True, nogil=True)
def _wilder_atr_nb(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    atr = np.zeros(n, dtype=np.float64)
    if n <= period:
        return atr
    tr = np.empty(n, dtype=np.float64)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        d1 = h[i] - l[i]
        d2 = abs(h[i] - c[i - 1])
        d3 = abs(l[i] - c[i - 1])
        tr[i] = d1 if d1 >= d2 and d1 >= d3 else (d2 if d2 >= d3 else d3)
    s = 0.0
    for i in range(period):
        s += tr[i]
    atr[period - 1] = s / period
    pm1 = period - 1
    for i in range(period, n):
        atr[i] = (atr[i - 1] * pm1 + tr[i]) / period
    return atr


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    return _wilder_atr_nb(
        np.asarray(h, dtype=np.float64),
        np.asarray(l, dtype=np.float64),
        np.asarray(c, dtype=np.float64),
        int(period),
    )


def williams_r_arr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan, dtype=float)
    if n < period or period <= 0:
        return out
    for i in range(period - 1, n):
        hh = float(np.max(h[i - period + 1: i + 1]))
        ll = float(np.min(l[i - period + 1: i + 1]))
        if hh - ll > 1e-12:
            out[i] = -100.0 * (hh - float(c[i])) / (hh - ll)
    return out


@njit(cache=True, nogil=True)
def _rsi_wilder_nb(c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    out = np.full(n, 50.0, dtype=np.float64)
    if n < 2 or period <= 0:
        return out
    alpha = 1.0 / period
    om = 1.0 - alpha
    au = 0.0
    ad = 0.0
    for i in range(1, n):
        d = c[i] - c[i - 1]
        u = d if d > 0.0 else 0.0
        dn = -d if d < 0.0 else 0.0
        au = alpha * u + om * au
        ad = alpha * dn + om * ad
        if ad <= 1e-12:
            out[i] = 100.0 if au > 1e-12 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + au / ad)
    return out


def rsi_wilder_arr(c: np.ndarray, period: int = 2) -> np.ndarray:
    return _rsi_wilder_nb(np.asarray(c, dtype=np.float64), int(period))


@njit(cache=True, nogil=True)
def _adx_nb(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    adx = np.zeros(n, dtype=np.float64)
    if n < 2 * period + 2 or period <= 0:
        return adx
    tr = np.zeros(n, dtype=np.float64)
    pdm = np.zeros(n, dtype=np.float64)
    mdm = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0.0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0.0) else 0.0
        d1 = h[i] - l[i]
        d2 = abs(h[i] - c[i - 1])
        d3 = abs(l[i] - c[i - 1])
        tr[i] = d1 if d1 >= d2 and d1 >= d3 else (d2 if d2 >= d3 else d3)
    str_ = 0.0
    sp = 0.0
    sm = 0.0
    for i in range(1, period + 1):
        str_ += tr[i]
        sp += pdm[i]
        sm += mdm[i]
    dx = np.zeros(n, dtype=np.float64)
    if str_ > 1e-12:
        pdi = 100.0 * sp / str_
        mdi = 100.0 * sm / str_
        if pdi + mdi > 1e-12:
            dx[period] = 100.0 * abs(pdi - mdi) / (pdi + mdi)
    for i in range(period + 1, n):
        str_ = str_ - (str_ / period) + tr[i]
        sp = sp - (sp / period) + pdm[i]
        sm = sm - (sm / period) + mdm[i]
        if str_ > 1e-12:
            pdi = 100.0 * sp / str_
            mdi = 100.0 * sm / str_
            if pdi + mdi > 1e-12:
                dx[i] = 100.0 * abs(pdi - mdi) / (pdi + mdi)
    s_dx = 0.0
    for i in range(period, 2 * period):
        s_dx += dx[i]
    adx[2 * period - 1] = s_dx / period
    pm1 = period - 1
    for i in range(2 * period, n):
        adx[i] = (adx[i - 1] * pm1 + dx[i]) / period
    return adx


def adx_arr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    """Causal Wilder ADX(period) matching research nb_adx."""
    return _adx_nb(
        np.asarray(h, dtype=np.float64),
        np.asarray(l, dtype=np.float64),
        np.asarray(c, dtype=np.float64),
        int(period),
    )


def er_arr(c: np.ndarray, period: int = 14) -> np.ndarray:
    """Causal Kaufman Efficiency Ratio |close[i] - close[i-p]| / sum(|diff|)."""
    n = len(c)
    out = np.zeros(n, dtype=float)
    if n <= period or period <= 0:
        return out
    diffs = np.abs(np.diff(c.astype(float), prepend=float(c[0])))
    diffs[0] = 0.0
    cs_v = np.cumsum(diffs)
    for i in range(period, n):
        vol_sum = float(cs_v[i] - cs_v[i - period])
        if vol_sum > 1e-12:
            out[i] = abs(float(c[i] - c[i - period])) / vol_sum
    return out


def rolling_max_prev(arr: np.ndarray, period: int) -> np.ndarray:
    """Strictly causal previous-period rolling high (excludes index i)."""
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    for i in range(period, n):
        out[i] = float(np.max(arr[i - period : i]))
    return out


def rolling_min_prev(arr: np.ndarray, period: int) -> np.ndarray:
    """Strictly causal previous-period rolling low (excludes index i)."""
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    for i in range(period, n):
        out[i] = float(np.min(arr[i - period : i]))
    return out
