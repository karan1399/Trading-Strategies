#!/usr/bin/env python3
"""HTF EMA 89/233 trend-continuation rejection engine.

Spec from the user's 4 chart images (XAUUSD H1, red EMA ~89, yellow EMA ~233):
  P1  wick-kiss      - single candle touches EMA from the trend side, rejection
                       wick >= wick_ratio of range, closes back on the trend side
  P2  two-bar        - bar i-1 tests the EMA, bar i closes back beyond it past
                       bar i-1's midpoint (their TWO_BAR semantics)
  P3  pierce-fail    - bar i-1 CLOSES beyond the EMA, bar i closes back through
                       it against the pierce (fakeout sweep) -> trend-direction entry
  P4  cluster stall  - 3-bar cluster dwelling at the EMA (>=2 touches), breaks
                       away with the trend (their CLUSTER semantics)

Filters:
  slope_gate  - EMA slope over the last 5 bars must agree with the trade side
  regime_gate - price must be on the trend side of the SLOW EMA (233) at signal

Reuse from dfib_engine: data loading, digits, ATR, simulate (next-open entry,
structural SL, fixed-RR/trailing exits, cooldowns, adverse-first sequencing,
real spread costs both sides), metrics, IS/OOS split.
"""
from __future__ import annotations

import sys

import numpy as np

from pathlib import Path
DIR = Path(__file__).resolve().parent
if str(DIR) not in sys.path:
    sys.path.insert(0, str(DIR))
from dfib_engine import load_symbol, wilder_atr, ema, simulate, SimCfg, metrics, split_is_oos

PATTERNS = ("P1_wick", "P2_two_bar", "P3_pierce", "P4_cluster")
FAST = 89
SLOW = 233


def _cluster_break(o, h, l, c, atr, i, level, side_bias):
    """Their 3-bar cluster semantics ported: >=2 touches in last 3 bars, closes
    held within 0.15 ATR of the level, bar i breaks the cluster extreme."""
    if i < 3:
        return 0.0, 0.0
    tol = max(1e-5, 0.20 * atr[i])
    ch, cl, cc = h[i - 3:i], l[i - 3:i], c[i - 3:i]
    if side_bias == 1:  # bullish: cluster above level, break UP
        touches = int((cl >= level - tol).sum())
        if touches >= 2 and np.min(cc) >= level - 0.15 * atr[i] and c[i] > np.max(ch):
            struct = np.min(cl) - 0.20 * atr[i]
            return struct, max(c[i] - struct, 1.2 * atr[i])
    else:  # bearish: cluster below level, break DOWN
        touches = int((ch <= level + tol).sum())
        if touches >= 2 and np.max(cc) <= level + 0.15 * atr[i] and c[i] < np.min(cl):
            struct = np.max(ch) + 0.20 * atr[i]
            return struct, max(struct - c[i], 1.2 * atr[i])
    return 0.0, 0.0


def precompute_htf(df, symbol, tf, period, pattern, slope_gate=True, regime_gate=False,
                   wick_ratio=0.35, cooldown_start=20):
    """Signal arrays for one configuration. Returns (sides, setups, risk_dists)."""
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    atr = wilder_atr(h, l, c, 14)
    ef = ema(c, period)
    start = max(cooldown_start, period + 10)
    if regime_gate:
        es = ema(c, SLOW)
        start = max(start, SLOW + 10)
    n = len(c)
    sides = np.zeros(n, np.int8)
    setups = [""] * n
    rds = np.zeros(n)
    tol_k = 0.20  # touch tolerance in ATR

    for i in range(start, n - 1):
        e = ef[i]
        if e <= 0:
            continue
        rising = e > ef[i - 5] if i >= 5 else False
        falling = e < ef[i - 5] if i >= 5 else False
        if slope_gate and not (rising or falling):
            continue
        tol = tol_k * atr[i]

        side = 0
        if pattern == "P1_wick":
            rng = max(1e-6, h[i] - l[i])
            body_top, body_bot = max(o[i], c[i]), min(o[i], c[i])
            lw, uw = (body_bot - l[i]) / rng, (h[i] - body_top) / rng
            if rising and l[i] <= e + tol and c[i] >= e - tol and lw >= wick_ratio and c[i] > o[i]:
                side = 1
            elif falling and h[i] >= e - tol and c[i] <= e + tol and uw >= wick_ratio and c[i] < o[i]:
                side = -1
            if side:
                sl = min(l[i], e) - tol if side == 1 else max(h[i], e) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P2_two_bar":
            if i < 1:
                continue
            mid1 = (o[i - 1] + c[i - 1]) / 2.0
            if rising and l[i - 1] <= ef[i - 1] + tol and c[i] > ef[i] + 0.0 and c[i] > o[i] and c[i] > mid1:
                side = 1
            elif falling and h[i - 1] >= ef[i - 1] - tol and c[i] < ef[i] and c[i] < o[i] and c[i] < mid1:
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P3_pierce":
            if i < 1:
                continue
            # pierce beyond EMA then fail back through it, trend-aligned
            if rising and c[i - 1] < ef[i - 1] - tol and c[i] > ef[i] + tol and c[i] > o[i]:
                side = 1
            elif falling and c[i - 1] > ef[i - 1] + tol and c[i] < ef[i] - tol and c[i] < o[i]:
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rd = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P4_cluster":
            struct, rd = _cluster_break(o, h, l, c, atr, i, e, 1 if rising else (-1 if falling else 0))
            if rd > 0 and ((struct < c[i] and rising) or (struct > c[i] and falling)):
                side = 1 if rising else -1
                sl = struct
                rd = max(rd, 1.2 * atr[i])
        if side == 0:
            continue
        if regime_gate:
            es_i = es[i]
            if side == 1 and not c[i] > es_i:
                continue
            if side == -1 and not c[i] < es_i:
                continue
        sides[i] = side
        setups[i] = f"{pattern}_EMA{period}_{'L' if side == 1 else 'S'}"
        rds[i] = rd
    return sides, setups, rds
