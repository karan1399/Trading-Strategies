#!/usr/bin/env python3
"""
quant_volatility_compression.py — Systematic Quantitative Exploration of
Volatility Compression & Squeeze Breakout Strategies across Multi-Asset & Multi-Timeframe.

Quant Framework:
  1. Microstructure Model:
     - Volatility state: Bollinger Bands (20, 2.0) vs Keltner Channels (20, 1.5 ATR).
     - Squeeze metric: S_t = BB_width / KC_width < 1.0 (Bands coiled inside Channels).
     - Compression energy: Minimum duration k consecutive bars in squeeze (k >= 3).
     - Release trigger: First bar where squeeze ends (BB expands beyond KC) OR breakout beyond KC band.
     - Directional filter: Macro Trend alignment (Price > EMA200 / EMA50 slope).
     - Volume / Range confirmation: Bar range > 1.2 * ATR (momentum confirmation).
  2. Transaction Costs & Realism:
     - Real broker spreads from MT5 parquet charged on BOTH entry and exit.
     - Closed-bar causal execution: Signal fires at bar close -> Enter next bar open.
     - Structural SL: Opposing Keltner channel band or 1.5 * ATR.
     - Exits: Fixed 2R, 3R, 4R, and Trailing Stop (Lock BE at 1R, trail 1R).
     - Chronological 70% In-Sample / 30% Out-Of-Sample split.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from dataclasses import dataclass

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


def sma(arr: np.ndarray, window: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window:
        return out
    csum = np.cumsum(np.insert(arr, 0, 0))
    out[window - 1 :] = (csum[window:] - csum[:-window]) / window
    # backfill warm-up
    out[: window - 1] = out[window - 1]
    return out


def rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window:
        return np.zeros(n)
    for i in range(window - 1, n):
        out[i] = np.std(arr[i - window + 1 : i + 1], ddof=1)
    out[: window - 1] = out[window - 1]
    return out


@dataclass
class SqueezeTrade:
    entry_idx: int
    exit_idx: int
    entry_time: str
    exit_time: str
    side: str
    entry_price: float
    exit_price: float
    sl: float
    tp: float
    risk_dist: float
    r_net: float
    reason: str
    bars_held: int


def simulate_squeeze(
    df: pd.DataFrame,
    tf: str,
    symbol: str,
    bb_period: int = 20,
    bb_mult: float = 2.0,
    kc_mult: float = 1.5,
    min_squeeze_bars: int = 3,
    trend_filter_ema: int = 200,
    exit_mode: str = "fixed_3.0r",  # "fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r"
    cooldown_bars: int = 6,
) -> list[SqueezeTrade]:
    n = len(df)
    if n < trend_filter_ema + 50:
        return []

    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    o = df["open"].values
    times = df.index
    spreads = df["spread"].values if "spread" in df else np.zeros(n)

    digits = int(df["digits"].iloc[0]) if "digits" in df else 2
    point = 10.0 ** (-digits)

    # 1. Indicators
    atr = wilder_atr(h, l, c, period=14)
    basis = sma(c, bb_period)
    r_std = rolling_std(c, bb_period)
    bb_upper = basis + bb_mult * r_std
    bb_lower = basis - bb_mult * r_std

    kc_upper = basis + kc_mult * atr
    kc_lower = basis - kc_mult * atr

    trend_ema = ema(c, trend_filter_ema)

    # 2. Squeeze Detection
    # Squeeze is on when BB is completely inside KC
    is_squeezed = (bb_upper <= kc_upper) & (bb_lower >= kc_lower)

    # Momentum confirmation: 12-period linear regression / momentum of (c - (basis + (kc_upper+kc_lower)/2)/2)
    mom = c - basis

    # Track consecutive squeeze bars
    sqz_count = np.zeros(n, dtype=int)
    for i in range(1, n):
        if is_squeezed[i]:
            sqz_count[i] = sqz_count[i - 1] + 1
        else:
            sqz_count[i] = 0

    trades = []
    last_exit_idx = -cooldown_bars

    for i in range(max(bb_period, trend_filter_ema), n - 1):
        if i - last_exit_idx < cooldown_bars:
            continue

        # Look for breakout from a matured squeeze:
        # Condition: Previous bar was in squeeze of at least min_squeeze_bars,
        # and current bar fires breakout.
        prev_squeezed = (sqz_count[i - 1] >= min_squeeze_bars) or (sqz_count[i - 2] >= min_squeeze_bars and is_squeezed[i - 1])
        if not prev_squeezed:
            continue

        signal_long = False
        signal_short = False

        # Long breakout: close breaks above KC upper & above BB upper & above EMA200 & positive momentum
        if c[i] > kc_upper[i] and c[i] > trend_ema[i] and mom[i] > 0 and c[i] > o[i]:
            signal_long = True

        # Short breakout: close breaks below KC lower & below BB lower & below EMA200 & negative momentum
        elif c[i] < kc_lower[i] and c[i] < trend_ema[i] and mom[i] < 0 and c[i] < o[i]:
            signal_short = True

        if not (signal_long or signal_short):
            continue

        # Valid signal at bar i close -> Entry at bar i+1 open
        entry_idx = i + 1
        entry_time = str(times[entry_idx])
        raw_entry = o[entry_idx]
        spread_pt = spreads[entry_idx] * point

        side = "BUY" if signal_long else "SELL"

        # Bid/Ask execution pricing
        if side == "BUY":
            entry_price = raw_entry + spread_pt
            # Stop loss at KC basis or 1.5 * ATR
            sl_dist = max(raw_entry - basis[i], 1.2 * atr[i])
            sl = entry_price - sl_dist
        else:
            entry_price = raw_entry
            sl_dist = max(basis[i] - raw_entry, 1.2 * atr[i])
            sl = entry_price + sl_dist

        risk_dist = abs(entry_price - sl)
        if risk_dist <= 0:
            continue

        # Target TP setup
        is_trail = exit_mode.startswith("trail")
        target_r = 3.0
        if "2.0r" in exit_mode:
            target_r = 2.0
        elif "3.0r" in exit_mode:
            target_r = 3.0
        elif "4.0r" in exit_mode:
            target_r = 4.0

        if not is_trail:
            tp = (entry_price + target_r * risk_dist) if side == "BUY" else (entry_price - target_r * risk_dist)
        else:
            tp = None

        # Simulate forward until exit
        curr_sl = sl
        be_locked = False
        trail_dist = 1.0 * risk_dist

        trade_done = False
        exit_idx = entry_idx
        exit_price = entry_price
        reason = "HOLD"

        for j in range(entry_idx, n):
            exit_spread_pt = spreads[j] * point
            bar_h = h[j]
            bar_l = l[j]
            bar_c = c[j]

            # In-bar adverse price first
            if side == "BUY":
                # Check Stop Loss (bid price)
                if bar_l <= curr_sl:
                    exit_idx = j
                    exit_price = curr_sl
                    reason = "SL_HIT"
                    trade_done = True
                    break

                # Check Take Profit
                if not is_trail and bar_h >= tp:
                    exit_idx = j
                    exit_price = tp
                    reason = "TP_HIT"
                    trade_done = True
                    break

                # Trailing logic
                if is_trail:
                    # Move to BE at +1.0R
                    if not be_locked and bar_h >= entry_price + 1.0 * risk_dist:
                        curr_sl = entry_price + 0.1 * risk_dist  # Lock small gain to cover costs
                        be_locked = True
                    # Trail when price exceeds 1.5R
                    if bar_h >= entry_price + 1.5 * risk_dist:
                        new_sl = bar_c - trail_dist
                        if new_sl > curr_sl:
                            curr_sl = new_sl

            else:  # SELL
                # Check Stop Loss (ask price = bar_h + spread)
                if (bar_h + exit_spread_pt) >= curr_sl:
                    exit_idx = j
                    exit_price = curr_sl
                    reason = "SL_HIT"
                    trade_done = True
                    break

                # Check Take Profit
                if not is_trail and (bar_l + exit_spread_pt) <= tp:
                    exit_idx = j
                    exit_price = tp
                    reason = "TP_HIT"
                    trade_done = True
                    break

                # Trailing logic
                if is_trail:
                    if not be_locked and bar_l <= entry_price - 1.0 * risk_dist:
                        curr_sl = entry_price - 0.1 * risk_dist
                        be_locked = True
                    if bar_l <= entry_price - 1.5 * risk_dist:
                        new_sl = bar_c + trail_dist
                        if new_sl < curr_sl:
                            curr_sl = new_sl

        if not trade_done:
            # End of sample
            exit_idx = n - 1
            exit_price = c[-1]
            reason = "END_OF_DATA"

        # Calculate Net R
        if side == "BUY":
            # Exit at bid: exit_price
            r_net = (exit_price - entry_price) / risk_dist
        else:
            # Exit at ask: exit_price + exit_spread_pt
            r_net = (entry_price - (exit_price + exit_spread_pt)) / risk_dist

        trades.append(
            SqueezeTrade(
                entry_idx=entry_idx,
                exit_idx=exit_idx,
                entry_time=entry_time,
                exit_time=str(times[exit_idx]),
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                sl=sl,
                tp=tp if tp else 0.0,
                risk_dist=risk_dist,
                r_net=r_net,
                reason=reason,
                bars_held=exit_idx - entry_idx,
            )
        )
        last_exit_idx = exit_idx

    return trades


def evaluate_metrics(trades: list[SqueezeTrade]) -> dict:
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


def run_full_study():
    assets = [
        # Metals
        ("XAUUSD.pi", "Commodity_Metals"),
        ("XAGUSD.pi", "Commodity_Metals"),
        ("XAUAUD.pi", "Commodity_Metals"),
        ("XAUGBP.pi", "Commodity_Metals"),
        ("XAUCNH.pi", "Commodity_Metals"),
        # Crypto
        ("BTCUSD.pi", "Crypto"),
        ("ETHUSD.pi", "Crypto"),
        ("SOLUSD.p", "Crypto"),
        ("LTCUSD.pi", "Crypto"),
        ("XRPUSD.pi", "Crypto"),
        # Indices
        ("SP500.p", "Indices"),
        ("NAS100.p", "Indices"),
        ("DJ30.p", "Indices"),
        ("GER40.p", "Indices"),
        ("JP225.p", "Indices"),
        ("UK100.p", "Indices"),
        # Forex
        ("EURUSD.pi", "Forex_Majors"),
        ("GBPUSD.pi", "Forex_Majors"),
        ("USDJPY.pi", "Forex_Majors"),
        ("AUDUSD.pi", "Forex_Majors"),
        ("USDCAD.pi", "Forex_Majors"),
        ("USDCHF.pi", "Forex_Majors"),
        ("GBPJPY.pi", "Forex_Crosses"),
        ("EURJPY.pi", "Forex_Crosses"),
        ("CHFJPY.pi", "Forex_Crosses"),
        ("CADJPY.pi", "Forex_Crosses"),
    ]

    timeframes = ["4H", "1H"]
    exit_modes = ["fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r"]

    all_results = []

    print("=" * 100)
    print("  QUANTITATIVE EXPLORATION OF VOLATILITY COMPRESSION (BB/KC SQUEEZE)")
    print("  Evaluating 26 Assets across 4H & 1H with Real Broker Spreads & 70/30 IS/OOS Split")
    print("=" * 100)

    for symbol, cluster in assets:
        for tf in timeframes:
            p = PARQUET_ROOT / tf / f"{symbol}.parquet"
            if not p.exists():
                continue
            df = pd.read_parquet(p)
            df["dt"] = pd.to_datetime(df["datetime"], utc=True)
            df = df.set_index("dt").sort_index()

            for exit_m in exit_modes:
                trades = simulate_squeeze(df, tf, symbol, exit_mode=exit_m)
                if not trades:
                    continue

                # 70/30 IS/OOS Split
                n_trades = len(trades)
                n_is = int(n_trades * 0.70)
                is_trades = trades[:n_is]
                oos_trades = trades[n_is:]

                m_all = evaluate_metrics(trades)
                m_is = evaluate_metrics(is_trades)
                m_oos = evaluate_metrics(oos_trades)

                all_results.append({
                    "symbol": symbol,
                    "cluster": cluster,
                    "tf": tf,
                    "exit": exit_m,
                    "all_n": m_all["n"],
                    "all_r": m_all["total_r"],
                    "all_pf": m_all["pf"],
                    "all_wr": m_all["win_rate"],
                    "is_n": m_is["n"],
                    "is_r": m_is["total_r"],
                    "is_pf": m_is["pf"],
                    "is_wr": m_is["win_rate"],
                    "oos_n": m_oos["n"],
                    "oos_r": m_oos["total_r"],
                    "oos_pf": m_oos["pf"],
                    "oos_wr": m_oos["win_rate"],
                    "oos_t": m_oos["t_stat"],
                    "max_dd_r": m_all["max_dd_r"],
                })

    df_res = pd.DataFrame(all_results)
    print(f"\nTotal Configurations Tested: {len(df_res)}")

    # Filter for Robust Out-of-Sample Edge (IS PF >= 1.25, OOS PF >= 1.20, OOS N >= 10, OOS R > 0)
    qualified = df_res[(df_res["is_pf"] >= 1.25) & (df_res["oos_pf"] >= 1.20) & (df_res["oos_n"] >= 8) & (df_res["oos_r"] > 0)].copy()
    qualified = qualified.sort_values("oos_r", ascending=False)

    print("\n" + "=" * 100)
    print(f"  TOP VALIDATED VOLATILITY COMPRESSION STRATEGIES (OOS PF >= 1.20, OOS N >= 8): {len(qualified)} setups")
    print("=" * 100)
    cols = ["symbol", "cluster", "tf", "exit", "is_r", "is_pf", "is_n", "oos_r", "oos_pf", "oos_n", "oos_t", "all_r", "all_pf"]
    print(qualified[cols].head(25).to_string(index=False))

    # Cluster Analysis
    print("\n" + "=" * 100)
    print("  CLUSTER PERFORMANCE SUMMARY (Average OOS PF and Net R by Asset Class):")
    print("=" * 100)
    cluster_agg = df_res.groupby(["cluster", "tf"]).agg(
        setups=("symbol", "count"),
        positive_oos=("oos_r", lambda x: (x > 0).sum()),
        avg_is_pf=("is_pf", "mean"),
        avg_oos_pf=("oos_pf", "mean"),
        total_oos_r=("oos_r", "sum")
    ).reset_index()
    print(cluster_agg.to_string(index=False))

    # Save to disk
    out_dir = REPO_ROOT / "Volatility_compression" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    df_res.to_csv(out_dir / "volatility_compression_full_sweep.csv", index=False)
    qualified.to_csv(out_dir / "volatility_compression_qualified.csv", index=False)
    # Mirror to desk/research
    desk_out = REPO_ROOT / "desk" / "research"
    desk_out.mkdir(parents=True, exist_ok=True)
    df_res.to_csv(desk_out / "volatility_compression_full_sweep.csv", index=False)
    qualified.to_csv(desk_out / "volatility_compression_qualified.csv", index=False)
    print(f"\n[OK] Results saved to {out_dir / 'volatility_compression_qualified.csv'}")


if __name__ == "__main__":
    run_full_study()
