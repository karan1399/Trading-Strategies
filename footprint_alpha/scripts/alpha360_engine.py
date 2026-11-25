#!/usr/bin/env python3
"""Alpha360: 360-degree exploration of the HTF EMA-rejection concept.

Generalizes the validated 89/233 baseline (htf_ema_rejection/) along every axis:
  EMA pairs   - 11 canonical (fast, slow) combos: Fibonacci ladder (8/21 ...
                144/377, incl. the 89/233 baseline) + classics (20/50, 50/100,
                50/200, 100/200)
  reject mode - 'fast'  : pullback rejection at the FAST EMA, slow EMA = regime
                'slow'  : deep rejection at the SLOW EMA, fast>slow = regime
                'band'  : rejection of the fast/slow EMA *band* when the two are
                          compressed (<= band_k * ATR apart) - the literal
                          reading of the original 89/233 chart images
  patterns    - P1 wick-kiss, P2 two-bar, P3 pierce-fail, P4 cluster-stall
                (baseline set) + P5 inside-bar, P6 engulfing (new)
  gates       - slope (5-bar EMA slope) and regime exposed as cheap post-masks
                so the 4 on/off combos share one signal computation

Reuses the corrected dfib_engine simulator: next-open entry, structural SL,
fixed-RR / trailing exits (BE lock +1R, trail from +1.5R), 6-bar cooldown,
adverse-first intrabar sequencing, real broker spread charged both sides.
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "/home/z/my-project/scripts")
from dfib_engine import load_symbol, wilder_atr, ema, list_symbols  # noqa: E402

PAIRS = [
    (8, 21), (13, 34), (21, 55), (34, 89), (55, 144), (89, 233), (144, 377),  # Fib ladder
    (20, 50), (50, 100), (50, 200), (100, 200),                              # classics
]
REJECTS = ("fast", "slow", "band")
PATTERNS = ("P1_wick", "P2_two_bar", "P3_pierce", "P4_cluster", "P5_inside", "P6_engulf")
TOL_K = 0.20          # EMA touch tolerance in ATR
BAND_K = 0.75         # fast/slow compression for 'band' mode, in ATR


def _cluster_break(h, l, c, atr, i, lo, hi, side_bias):
    """3-bar cluster dwelling at the band [lo, hi]; breakaway with the trend."""
    if i < 3:
        return 0.0, 0.0
    tol = max(1e-5, 0.20 * atr[i])
    ch, cl, cc = h[i - 3:i], l[i - 3:i], c[i - 3:i]
    if side_bias == 1:  # bullish: cluster above band, break UP
        touches = int((cl >= lo - tol).sum())
        if touches >= 2 and np.min(cc) >= lo - 0.15 * atr[i] and c[i] > np.max(ch):
            struct = np.min(cl) - 0.20 * atr[i]
            return struct, max(c[i] - struct, 1.2 * atr[i])
    else:               # bearish: cluster below band, break DOWN
        touches = int((ch <= hi + tol).sum())
        if touches >= 2 and np.max(cc) <= hi + 0.15 * atr[i] and c[i] < np.min(cl):
            struct = np.max(ch) + 0.20 * atr[i]
            return struct, max(struct - c[i], 1.2 * atr[i])
    return 0.0, 0.0


def precompute_pair(df, fast, slow, reject, pattern, wick_ratio=0.35,
                    cooldown_start=20, ef=None, es=None, atr=None):
    """Signals for one (pair, reject, pattern) with gates exposed as masks.

    ef/es/atr: optional precomputed EMA / ATR arrays (per-symbol caches).

    Returns (sides, setups, rds, slope_ok, regime_ok):
      sides/rds  - int8 per-bar side, structural risk distance
      slope_ok   - +1 bar if rejection-EMA slope up over 5 bars, -1 if down
      regime_ok  - +1 bar if trend regime long, -1 if short
    """
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    if atr is None:
        atr = wilder_atr(h, l, c, 14)
    if ef is None:
        ef = ema(c, fast)
    if es is None:
        es = ema(c, slow)
    n = len(c)
    start = max(cooldown_start, slow + 10)
    sides = np.zeros(n, np.int8)
    setups = [""] * n
    rds = np.zeros(n)
    slope_ok = np.zeros(n, np.int8)
    regime_ok = np.zeros(n, np.int8)

    for i in range(start, n - 1):
        if ef[i] <= 0 or es[i] <= 0:
            continue
        e_f, e_s = ef[i], es[i]
        # rejection level(s) for this mode
        if reject == "fast":
            lo = hi = e_f
        elif reject == "slow":
            lo = hi = e_s
        else:  # band: only when compressed
            if abs(e_f - e_s) > BAND_K * atr[i]:
                continue
            lo, hi = min(e_f, e_s), max(e_f, e_s)
        e = (lo + hi) / 2.0 if lo != hi else lo

        # slope on the rejection EMA (fast for 'fast', slow otherwise)
        if reject == "fast":
            rising, falling = e_f > ef[i - 5], e_f < ef[i - 5]
        else:
            rising, falling = e_s > es[i - 5], e_s < es[i - 5]
        slope_ok[i] = 1 if rising else (-1 if falling else 0)
        # regime: fast-reject -> price vs slow EMA; slow/band -> fast vs slow EMA
        if reject == "fast":
            regime_ok[i] = 1 if c[i] > e_s else (-1 if c[i] < e_s else 0)
        else:
            regime_ok[i] = 1 if e_f > e_s else (-1 if e_f < e_s else 0)

        tol = TOL_K * atr[i]
        side = 0
        sl = rd = 0.0
        if pattern == "P1_wick":
            rng = max(1e-6, h[i] - l[i])
            body_top, body_bot = max(o[i], c[i]), min(o[i], c[i])
            lw, uw = (body_bot - l[i]) / rng, (h[i] - body_top) / rng
            if rising and l[i] <= hi + tol and c[i] >= lo - tol and lw >= wick_ratio and c[i] > o[i]:
                side = 1
            elif falling and h[i] >= lo - tol and c[i] <= hi + tol and uw >= wick_ratio and c[i] < o[i]:
                side = -1
            if side:
                sl = min(l[i], lo) - tol if side == 1 else max(h[i], hi) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P2_two_bar":
            if i < 1:
                continue
            mid1 = (o[i - 1] + c[i - 1]) / 2.0
            if rising and l[i - 1] <= hi + tol and c[i] > e and c[i] > o[i] and c[i] > mid1:
                side = 1
            elif falling and h[i - 1] >= lo - tol and c[i] < e and c[i] < o[i] and c[i] < mid1:
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P3_pierce":
            if i < 1:
                continue
            if rising and c[i - 1] < lo - tol and c[i] > hi + tol and c[i] > o[i]:
                side = 1
            elif falling and c[i - 1] > hi + tol and c[i] < lo - tol and c[i] < o[i]:
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P4_cluster":
            bias = 1 if rising else (-1 if falling else 0)
            sl, rd = _cluster_break(h, l, c, atr, i, lo, hi, bias)
            if rd > 0 and ((sl < c[i] and rising) or (sl > c[i] and falling)):
                side = 1 if rising else -1
            else:
                side = 0
            rd = max(rd, 1.2 * atr[i]) if side else 0.0
        elif pattern == "P5_inside":
            if i < 1:
                continue
            rng1 = max(1e-6, h[i - 1] - l[i - 1])
            if (rising and l[i - 1] <= hi + tol and h[i] <= h[i - 1] and l[i] >= l[i - 1]
                    and c[i] > l[i - 1] + 0.5 * rng1 and c[i] > o[i]):
                side = 1
            elif (falling and h[i - 1] >= lo - tol and h[i] <= h[i - 1] and l[i] >= l[i - 1]
                    and c[i] < l[i - 1] + 0.5 * rng1 and c[i] < o[i]):
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P6_engulf":
            if i < 1:
                continue
            body_lo1, body_hi1 = min(o[i - 1], c[i - 1]), max(o[i - 1], c[i - 1])
            if (rising and l[i - 1] <= hi + tol and c[i - 1] < o[i - 1]
                    and c[i] > o[i] and o[i] <= body_lo1 and c[i] >= body_hi1):
                side = 1
            elif (falling and h[i - 1] >= lo - tol and c[i - 1] > o[i - 1]
                    and c[i] < o[i] and o[i] >= body_hi1 and c[i] <= body_lo1):
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        if side == 0:
            continue
        sides[i] = side
        setups[i] = f"{pattern}_EMA{fast}/{slow}_{reject}_{'L' if side == 1 else 'S'}"
        rds[i] = rd
    return sides, setups, rds, slope_ok, regime_ok


def apply_gates(sides, rds, slope_ok, regime_ok, slope, regime):
    """Zero out signals whose gate masks disagree with the side.

    Returns (sides, rds); the caller reuses the original setups array - a
    gate-removed bar has side 0, so its stale label is never read by simulate.
    """
    if not slope and not regime:
        return sides, rds
    keep = np.ones(len(sides), bool)
    if slope:
        keep &= slope_ok == sides
    if regime:
        keep &= regime_ok == sides
    if keep.all():
        return sides, rds
    return np.where(keep, sides, 0).astype(np.int8), np.where(keep, rds, 0.0)
