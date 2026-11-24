#!/usr/bin/env python3
"""
orb_mtf_engine.py — Multi-Timeframe Quantitative Opening Range Breakout (MTF-ORB) Engine.

Architectures Supported:
  1. MTF-Trend: 4H Macro Trend Filter (50/200 EMA + Price vs EMA) merged causally with 15M/5M ORB.
  2. HTF-OR / LTF-Exec: 1H Opening Range (established during first session hour) executed on 15M or 5M closed bars.
  3. Pre-Session Volatility Compression Filter (VCR): Asian/Pre-session range relative to ATR.
  4. Adaptive Structural Stop: 1H Midpoint vs 15M Swing Pivot vs Full Opposite.
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
class MtfOrbConfig:
    session_start_hour: int = 14       # UTC hour (14=NY, 8=London, 0=Tokyo)
    session_duration_hours: int = 8
    or_timeframe: str = "1H"           # Timeframe defining the Opening Range ("1H" or "15M")
    exec_timeframe: str = "15M"        # Timeframe used for breakout execution ("15M" or "5M")
    or_bars: int = 1                   # Number of bars on or_timeframe (e.g. 1 bar on 1H = 1st hour)
    htf_trend_filter: str = "4H_200EMA"# "none", "4H_200EMA", "4H_50_200_EMA", "1H_200EMA"
    vcr_filter: bool = False           # Require pre-session volatility compression
    max_vcr: float = 0.80              # Pre-session range / ATR ratio <= 0.80
    sl_mode: str = "midpoint"          # "midpoint", "opposite", "ltf_swing", "atr_1.2"
    tp_mode: str = "fixed_3.0r"        # "fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r", "eod"
    stretch_atr_mult: float = 0.05     # Buffer beyond OR High/Low
    max_trades_per_day: int = 1


@dataclass
class MtfOrbTrade:
    entry_idx: int
    exit_idx: int
    entry_time: str
    exit_time: str
    symbol: str
    or_tf: str
    exec_tf: str
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


_DATA_CACHE: dict = {}

def prepare_multi_timeframe_data(
    symbol: str,
    exec_tf: str = "15M",
    htf_trend: str = "4H_200EMA",
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """
    Loads execution timeframe parquet and higher timeframe parquet,
    computes HTF indicators, and causally aligns HTF signals onto LTF bars using merge_asof.
    Cached in memory for high-speed parameter sweeps.
    """
    cache_key = (symbol, exec_tf, htf_trend)
    if cache_key in _DATA_CACHE:
        df_exec_cached, df_htf_cached = _DATA_CACHE[cache_key]
        return df_exec_cached.copy(), df_htf_cached

    exec_path = PARQUET_ROOT / exec_tf / f"{symbol}.parquet"
    if not exec_path.exists():
        return pd.DataFrame(), None

    df_exec = pd.read_parquet(exec_path)
    df_exec["dt"] = pd.to_datetime(df_exec["datetime"], utc=True)
    df_exec = df_exec.sort_values("dt").reset_index(drop=True)

    if htf_trend == "none":
        _DATA_CACHE[cache_key] = (df_exec, None)
        return df_exec.copy(), None

    htf_tf = "4H" if "4H" in htf_trend else "1H"
    htf_path = PARQUET_ROOT / htf_tf / f"{symbol}.parquet"
    if not htf_path.exists():
        _DATA_CACHE[cache_key] = (df_exec, None)
        return df_exec.copy(), None

    df_htf = pd.read_parquet(htf_path)
    df_htf["dt"] = pd.to_datetime(df_htf["datetime"], utc=True)
    df_htf = df_htf.sort_values("dt").reset_index(drop=True)

    # Compute HTF indicators
    htf_c = df_htf["close"].values
    df_htf["htf_ema50"] = ema(htf_c, 50)
    df_htf["htf_ema200"] = ema(htf_c, 200)
    df_htf["htf_bullish"] = (df_htf["close"] > df_htf["htf_ema200"])
    if "50_200" in htf_trend:
        df_htf["htf_bullish"] = df_htf["htf_bullish"] & (df_htf["htf_ema50"] > df_htf["htf_ema200"])
        df_htf["htf_bearish"] = (df_htf["close"] < df_htf["htf_ema200"]) & (df_htf["htf_ema50"] < df_htf["htf_ema200"])
    else:
        df_htf["htf_bearish"] = (df_htf["close"] < df_htf["htf_ema200"])

    # Causal merge: A bar at time T only sees the HTF bar that completed at or prior to T.
    # In MT5, a 4H bar timestamp is the open of that bar. Its close happens at timestamp + 4H.
    # To prevent lookahead, we shift HTF signals by 1 bar!
    df_htf_shifted = df_htf[["dt", "htf_bullish", "htf_bearish", "htf_ema200"]].copy()
    df_htf_shifted["htf_bullish"] = df_htf_shifted["htf_bullish"].shift(1).fillna(False)
    df_htf_shifted["htf_bearish"] = df_htf_shifted["htf_bearish"].shift(1).fillna(False)
    df_htf_shifted["htf_ema200"] = df_htf_shifted["htf_ema200"].shift(1).bfill()

    # Merge asof backward
    df_exec = pd.merge_asof(
        df_exec,
        df_htf_shifted,
        on="dt",
        direction="backward",
    )

    _DATA_CACHE[cache_key] = (df_exec, df_htf)
    return df_exec.copy(), df_htf


def simulate_mtf_orb(
    symbol: str,
    cfg: MtfOrbConfig,
) -> list[MtfOrbTrade]:
    df_exec, df_htf = prepare_multi_timeframe_data(
        symbol=symbol,
        exec_tf=cfg.exec_timeframe,
        htf_trend=cfg.htf_trend_filter,
    )
    if df_exec.empty or len(df_exec) < 200:
        return []

    times = df_exec["dt"].values
    n = len(df_exec)
    c = df_exec["close"].values
    h = df_exec["high"].values
    l = df_exec["low"].values
    o = df_exec["open"].values
    spreads = df_exec["spread"].values if "spread" in df_exec else np.zeros(n)
    digits = int(df_exec["digits"].iloc[0]) if "digits" in df_exec else 2
    point = 10.0 ** (-digits)

    # ATR on execution timeframe
    atr = wilder_atr(h, l, c, period=14)

    # HTF trend columns if available
    has_htf = "htf_bullish" in df_exec.columns
    htf_bull = df_exec["htf_bullish"].values if has_htf else np.ones(n, dtype=bool)
    htf_bear = df_exec["htf_bearish"].values if has_htf else np.ones(n, dtype=bool)

    dt_series = pd.DatetimeIndex(df_exec["dt"])
    hours = dt_series.hour
    dates = dt_series.date

    unique_dates = np.unique(dates)
    date_indices = {}
    for idx, d in enumerate(dates):
        if d not in date_indices:
            date_indices[d] = []
        date_indices[d].append(idx)

    trades = []

    # Opening Range window duration in minutes
    # e.g. if or_timeframe == "1H", duration is 60 mins.
    # If or_timeframe == "15M", duration is 15 or 30 mins.
    if cfg.or_timeframe == "1H":
        or_duration_minutes = 60 * cfg.or_bars
    elif cfg.or_timeframe == "15M":
        or_duration_minutes = 15 * cfg.or_bars
    elif cfg.or_timeframe == "30M":
        or_duration_minutes = 30 * cfg.or_bars
    else:
        or_duration_minutes = 60

    for d in unique_dates:
        idxs = date_indices[d]
        if len(idxs) < 10:
            continue

        # Find session start bar on exec timeframe
        session_start_idx = None
        for idx in idxs:
            if hours[idx] >= cfg.session_start_hour:
                session_start_idx = idx
                break

        if session_start_idx is None:
            continue

        session_start_time = dt_series[session_start_idx]
        or_cutoff_time = session_start_time + pd.Timedelta(minutes=or_duration_minutes)

        # Identify bars forming the Opening Range
        or_bar_idxs = [idx for idx in idxs if session_start_time <= dt_series[idx] < or_cutoff_time]
        if not or_bar_idxs:
            continue

        # Opening Range High, Low, Mid
        or_h = np.max(h[or_bar_idxs])
        or_l = np.min(l[or_bar_idxs])
        or_mid = 0.5 * (or_h + or_l)
        or_range = or_h - or_l

        last_or_bar_idx = or_bar_idxs[-1]
        curr_atr = atr[last_or_bar_idx]
        if curr_atr <= 0 or or_range <= 0:
            continue

        # Optional Pre-Session Volatility Compression Ratio (VCR)
        if cfg.vcr_filter:
            # Check 4-hour pre-session range (session_start - 4 hours to session_start)
            pre_start_time = session_start_time - pd.Timedelta(hours=4)
            pre_idxs = [idx for idx in idxs if pre_start_time <= dt_series[idx] < session_start_time]
            if len(pre_idxs) >= 4:
                pre_range = np.max(h[pre_idxs]) - np.min(l[pre_idxs])
                vcr = pre_range / curr_atr
                if vcr > cfg.max_vcr:
                    continue  # Market was noisy/expanded pre-session, skip

        buffer = cfg.stretch_atr_mult * curr_atr
        long_trigger = or_h + buffer
        short_trigger = or_l - buffer

        trades_today = 0
        exec_start_idx = last_or_bar_idx + 1

        # Scan execution bars
        for i in range(exec_start_idx, idxs[-1] + 1):
            if i >= n - 1 or trades_today >= cfg.max_trades_per_day:
                break
            # Session expiration (e.g. 8 hours after start)
            if hours[i] >= (cfg.session_start_hour + cfg.session_duration_hours):
                break

            sig_long = False
            sig_short = False

            # Long breakout: close > long_trigger, bullish candle
            if c[i] > long_trigger and c[i] > o[i]:
                if not has_htf or htf_bull[i]:
                    sig_long = True

            # Short breakout: close < short_trigger, bearish candle
            elif c[i] < short_trigger and c[i] < o[i]:
                if not has_htf or htf_bear[i]:
                    sig_short = True

            if not (sig_long or sig_short):
                continue

            # Enter on next bar open
            entry_idx = i + 1
            if entry_idx >= n:
                break

            entry_time = str(dt_series[entry_idx])
            raw_entry = o[entry_idx]
            spread_pt = spreads[entry_idx] * point
            side = "BUY" if sig_long else "SELL"

            if side == "BUY":
                entry_price = raw_entry + spread_pt
                if cfg.sl_mode == "opposite":
                    sl = or_l
                elif cfg.sl_mode == "midpoint":
                    sl = or_mid
                elif cfg.sl_mode == "ltf_swing":
                    # Swing low of last 3 bars prior to breakout
                    lookback_start = max(session_start_idx, i - 3)
                    sl = np.min(l[lookback_start:i + 1]) - 0.2 * curr_atr
                else:  # atr_1.2
                    sl = entry_price - 1.2 * curr_atr
            else:
                entry_price = raw_entry
                if cfg.sl_mode == "opposite":
                    sl = or_h
                elif cfg.sl_mode == "midpoint":
                    sl = or_mid
                elif cfg.sl_mode == "ltf_swing":
                    lookback_start = max(session_start_idx, i - 3)
                    sl = np.max(h[lookback_start:i + 1]) + 0.2 * curr_atr
                else:
                    sl = entry_price + 1.2 * curr_atr

            risk_dist = abs(entry_price - sl)
            if risk_dist <= 0:
                continue

            # Target TP
            target_r = 3.0
            if "2.0r" in cfg.tp_mode:
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

            curr_sl = sl
            be_locked = False
            trail_dist = 1.0 * risk_dist
            trade_done = False
            exit_idx = entry_idx
            exit_price = entry_price
            reason = "EOD"

            max_hold = min(n - 1, entry_idx + 48)  # up to 48 exec bars
            for j in range(entry_idx, max_hold):
                exit_spread_pt = spreads[j] * point
                bar_h = h[j]
                bar_l = l[j]
                bar_c = c[j]

                # Check EOD / Session End
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

            if side == "BUY":
                r_net = (exit_price - entry_price) / risk_dist
            else:
                r_net = (entry_price - (exit_price + exit_spread_pt)) / risk_dist

            trades.append(
                MtfOrbTrade(
                    entry_idx=entry_idx,
                    exit_idx=exit_idx,
                    entry_time=entry_time,
                    exit_time=str(dt_series[exit_idx]),
                    symbol=symbol,
                    or_tf=cfg.or_timeframe,
                    exec_tf=cfg.exec_timeframe,
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
            break

    return trades


def evaluate_mtf_metrics(trades: list[MtfOrbTrade]) -> dict:
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
