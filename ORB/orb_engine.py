#!/usr/bin/env python3
"""
orb_engine.py — Core Quantitative Opening Range Breakout (ORB) Backtesting Engine.

Features:
  - Supports arbitrary Opening Range definitions:
      * 15M ORB (first 15m or 30m of session)
      * 1H ORB (first 1h of session)
      * 5M ORB (first 5m, 15m, 30m of session)
  - Configurable Session Openings:
      * London Open (07:00 / 08:00 UTC)
      * New York Open (13:00 / 14:00 UTC)
      * Asian / Tokyo Open (00:00 / 01:00 UTC)
  - Quant Filter Layers:
      * Opening Range Volatility Ratio (ORVR): OR_Range / ATR_14 <= max_orvr (avoids exhausted opens)
      * Macro Trend Filter: 200 EMA alignment (Longs above EMA, Shorts below EMA)
      * Candle Direction & Momentum Confirmation
      * Buffer / Stretch parameter
  - Risk & Execution Discipline:
      * Real broker spreads charged on entry & exit
      * Closed-bar causal trigger -> execution on next bar open
      * Stop Loss: OPPOSITE (OR Low/High), MIDPOINT (OR Mid), or ATR-based
      * Take Profit: Fixed (1.5R, 2R, 3R, 4R), EOD (End-of-Session Close), or Trailing
      * Max 1 trade per day/session
      * 70% In-Sample / 30% Out-of-Sample chronological split
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET_ROOT = REPO_ROOT / "historical_parquet"


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = np.zeros(n)
    if n < period:
        return tr
    atr[period - 1] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(period, n):
        atr[i] = atr[i - 1] * (1.0 - alpha) + tr[i] * alpha
    return atr


def ema(arr: np.ndarray, span: int) -> np.ndarray:
    n = len(arr)
    out = np.zeros(n)
    if n == 0:
        return out
    out[0] = arr[0]
    alpha = 2.0 / (span + 1.0)
    for i in range(1, n):
        out[i] = out[i - 1] + alpha * (arr[i] - out[i - 1])
    return out


@dataclass
class OrbConfig:
    session_start_hour: int = 14       # UTC hour when session begins (e.g. 14 for NY, 8 for London)
    session_duration_hours: int = 8    # How many hours the trading session lasts
    or_bars: int = 2                   # Number of bars defining the opening range (e.g. 2 bars on 15M = 30M ORB)
    sl_mode: str = "midpoint"          # "opposite" (OR opposite side), "midpoint" (OR mid), "atr" (1.2 * ATR)
    tp_mode: str = "fixed_3.0r"        # "fixed_1.5r", "fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r", "eod"
    trend_filter: bool = True          # Require price > EMA200 for Long, < EMA200 for Short
    ema_period: int = 200
    max_orvr: float = 1.2              # Max allowed ratio of OR_range to ATR_14 (filters over-expanded opens)
    stretch_atr_mult: float = 0.05     # Breakout buffer beyond OR High/Low (in ATR units)
    max_trades_per_day: int = 1


@dataclass
class OrbTrade:
    entry_idx: int
    exit_idx: int
    entry_time: str
    exit_time: str
    symbol: str
    tf: str
    side: str
    entry_price: float
    exit_price: float
    sl: float
    tp: float
    risk_dist: float
    r_net: float
    reason: str
    bars_held: int
    session_date: str


def simulate_orb(
    df: pd.DataFrame,
    tf: str,
    symbol: str,
    cfg: OrbConfig,
) -> list[OrbTrade]:
    n = len(df)
    if n < cfg.ema_period + 50:
        return []

    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    o = df["open"].values
    times = df.index
    spreads = df["spread"].values if "spread" in df else np.zeros(n)

    digits = int(df["digits"].iloc[0]) if "digits" in df else 2
    point = 10.0 ** (-digits)

    # Technical indicators
    atr = wilder_atr(h, l, c, period=14)
    trend_ema = ema(c, cfg.ema_period) if cfg.trend_filter else np.zeros(n)

    # Dates and hours
    hours = times.hour
    dates = times.date

    trades = []
    
    # Process day-by-day
    unique_dates = np.unique(dates)

    # Map dates to bar index ranges
    date_indices = {}
    for idx, d in enumerate(dates):
        if d not in date_indices:
            date_indices[d] = []
        date_indices[d].append(idx)

    for d in unique_dates:
        idxs = date_indices[d]
        if len(idxs) < cfg.or_bars + 2:
            continue

        # Find the session start bar
        session_start_idx = None
        for idx in idxs:
            if hours[idx] >= cfg.session_start_hour:
                session_start_idx = idx
                break

        if session_start_idx is None:
            continue

        # Check if we have enough bars for the opening range
        or_end_idx = session_start_idx + cfg.or_bars
        if or_end_idx >= n or dates[or_end_idx - 1] != d:
            continue

        # Calculate Opening Range
        or_h = np.max(h[session_start_idx:or_end_idx])
        or_l = np.min(l[session_start_idx:or_end_idx])
        or_mid = 0.5 * (or_h + or_l)
        or_range = or_h - or_l

        curr_atr = atr[or_end_idx - 1]
        if curr_atr <= 0 or or_range <= 0:
            continue

        # Volatility compression check (ORVR)
        orvr = or_range / curr_atr
        if orvr > cfg.max_orvr:
            continue  # Open is already over-exhausted

        buffer = cfg.stretch_atr_mult * curr_atr
        long_trigger = or_h + buffer
        short_trigger = or_l - buffer

        # Find session end limit
        session_end_hour = (cfg.session_start_hour + cfg.session_duration_hours) % 24
        
        trades_today = 0
        in_trade = False

        # Scan subsequent bars in session
        for i in range(or_end_idx, len(idxs) + idxs[0]):
            if i >= n - 1 or trades_today >= cfg.max_trades_per_day:
                break
            if hours[i] >= (cfg.session_start_hour + cfg.session_duration_hours) and cfg.session_start_hour + cfg.session_duration_hours <= 24:
                break  # Session window ended

            sig_long = False
            sig_short = False

            # Long breakout: close breaks above OR High + buffer, bullish candle
            if c[i] > long_trigger and c[i] > o[i]:
                if not cfg.trend_filter or c[i] > trend_ema[i]:
                    sig_long = True

            # Short breakout: close breaks below OR Low - buffer, bearish candle
            elif c[i] < short_trigger and c[i] < o[i]:
                if not cfg.trend_filter or c[i] < trend_ema[i]:
                    sig_short = True

            if not (sig_long or sig_short):
                continue

            # Valid breakout at bar i close -> Enter at bar i+1 open
            entry_idx = i + 1
            if entry_idx >= n:
                break

            entry_time = str(times[entry_idx])
            raw_entry = o[entry_idx]
            spread_pt = spreads[entry_idx] * point
            side = "BUY" if sig_long else "SELL"

            if side == "BUY":
                entry_price = raw_entry + spread_pt
                if cfg.sl_mode == "opposite":
                    sl = or_l
                elif cfg.sl_mode == "midpoint":
                    sl = or_mid
                else:  # atr
                    sl = entry_price - 1.2 * curr_atr
            else:
                entry_price = raw_entry
                if cfg.sl_mode == "opposite":
                    sl = or_h
                elif cfg.sl_mode == "midpoint":
                    sl = or_mid
                else:
                    sl = entry_price + 1.2 * curr_atr

            risk_dist = abs(entry_price - sl)
            if risk_dist <= 0:
                continue

            # Determine TP
            target_r = 3.0
            if "1.5r" in cfg.tp_mode:
                target_r = 1.5
            elif "2.0r" in cfg.tp_mode:
                target_r = 2.0
            elif "3.0r" in cfg.tp_mode:
                target_r = 3.0
            elif "4.0r" in cfg.tp_mode:
                target_r = 4.0

            is_trail = "trail" in cfg.tp_mode
            is_eod = (cfg.tp_mode == "eod")

            if not (is_trail or is_eod):
                tp = (entry_price + target_r * risk_dist) if side == "BUY" else (entry_price - target_r * risk_dist)
            else:
                tp = None

            # Simulate forward to exit
            curr_sl = sl
            be_locked = False
            trail_dist = 1.0 * risk_dist
            trade_done = False
            exit_idx = entry_idx
            exit_price = entry_price
            reason = "EOD"

            # Max hold up to session end or 24 bars
            max_hold = min(n - 1, entry_idx + 32)
            for j in range(entry_idx, max_hold):
                exit_spread_pt = spreads[j] * point
                bar_h = h[j]
                bar_l = l[j]
                bar_c = c[j]

                # Check End of Day / Session close
                if is_eod and (dates[j] != d or hours[j] >= (cfg.session_start_hour + cfg.session_duration_hours)):
                    exit_idx = j
                    exit_price = bar_c
                    reason = "EOD_EXIT"
                    trade_done = True
                    break

                if side == "BUY":
                    if bar_l <= curr_sl:
                        exit_idx = j
                        exit_price = curr_sl
                        reason = "SL_HIT"
                        trade_done = True
                        break

                    if not (is_trail or is_eod) and bar_h >= tp:
                        exit_idx = j
                        exit_price = tp
                        reason = "TP_HIT"
                        trade_done = True
                        break

                    if is_trail:
                        if not be_locked and bar_h >= entry_price + 1.0 * risk_dist:
                            curr_sl = entry_price + 0.1 * risk_dist
                            be_locked = True
                        if bar_h >= entry_price + 1.5 * risk_dist:
                            new_sl = bar_c - trail_dist
                            if new_sl > curr_sl:
                                curr_sl = new_sl

                else:  # SELL
                    if (bar_h + exit_spread_pt) >= curr_sl:
                        exit_idx = j
                        exit_price = curr_sl
                        reason = "SL_HIT"
                        trade_done = True
                        break

                    if not (is_trail or is_eod) and (bar_l + exit_spread_pt) <= tp:
                        exit_idx = j
                        exit_price = tp
                        reason = "TP_HIT"
                        trade_done = True
                        break

                    if is_trail:
                        if not be_locked and bar_l <= entry_price - 1.0 * risk_dist:
                            curr_sl = entry_price - 0.1 * risk_dist
                            be_locked = True
                        if bar_l <= entry_price - 1.5 * risk_dist:
                            new_sl = bar_c + trail_dist
                            if new_sl < curr_sl:
                                curr_sl = new_sl

            if not trade_done:
                exit_idx = max_hold
                exit_price = c[max_hold]
                reason = "TIME_EXPIRED"

            # Calculate Net R
            if side == "BUY":
                r_net = (exit_price - entry_price) / risk_dist
            else:
                r_net = (entry_price - (exit_price + exit_spread_pt)) / risk_dist

            trades.append(
                OrbTrade(
                    entry_idx=entry_idx,
                    exit_idx=exit_idx,
                    entry_time=entry_time,
                    exit_time=str(times[exit_idx]),
                    symbol=symbol,
                    tf=tf,
                    side=side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    sl=sl,
                    tp=tp if tp else 0.0,
                    risk_dist=risk_dist,
                    r_net=r_net,
                    reason=reason,
                    bars_held=exit_idx - entry_idx,
                    session_date=str(d),
                )
            )
            trades_today += 1
            break  # Move to next session

    return trades


def evaluate_orb_metrics(trades: list[OrbTrade]) -> dict:
    if not trades:
        return {"n": 0, "total_r": 0.0, "pf": 0.0, "win_rate": 0.0, "avg_r": 0.0, "max_dd_r": 0.0, "t_stat": 0.0}
    r = np.array([t.r_net for t in trades])
    n = len(r)
    total_r = r.sum()
    wins = r[r > 0]
    losses = r[r < 0]
    win_rate = len(wins) / n if n > 0 else 0.0
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and abs(losses.sum()) > 0 else (99.0 if len(wins) > 0 else 0.0)
    avg_r = total_r / n if n > 0 else 0.0
    cum = np.cumsum(r)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = dd.max() if len(dd) > 0 else 0.0
    t_stat = (np.mean(r) / (np.std(r, ddof=1) / np.sqrt(n))) if n > 1 and np.std(r, ddof=1) > 0 else 0.0
    return {
        "n": n,
        "total_r": round(float(total_r), 2),
        "pf": round(float(pf), 2),
        "win_rate": round(float(win_rate * 100), 1),
        "avg_r": round(float(avg_r), 3),
        "max_dd_r": round(float(max_dd), 2),
        "t_stat": round(float(t_stat), 2),
    }
