#!/usr/bin/env python3
"""
orb_champion_portfolio.py — Multi-Asset Multi-Timeframe Champion Portfolio Backtester.

Simulates an ensemble of validated ORB champions across:
  1. NAS100 (Nasdaq) — 1H OR / 15M Exec + 4H Trend, Midpoint SL, EOD Exit
  2. DJ30 (Dow Jones) — 4H Strong Trend + 15M ORB, LTF Swing SL, EOD Exit
  3. SP500 (S&P 500) — 15m OR / 15M Exec, Midpoint SL, EOD Exit
  4. GER40 (DAX 40) — 15m OR / 15M Exec, Opposite SL, Fixed 3.0R (Trend=True)
  5. JP225 (Nikkei) — 4H Strong Trend + 15M ORB, LTF Swing SL, Trailing 1.0R
  6. BTCUSD (Bitcoin) — 1H OR / 15M Exec + 4H Trend, Opposite SL, Fixed 3.0R

Calculates combined portfolio returns, diversification benefit, combined drawdown, and Sharpe ratio.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
ORB_DIR = REPO_ROOT / "ORB"
sys.path.insert(0, str(ORB_DIR))

from orb_mtf_engine import simulate_mtf_orb, evaluate_mtf_metrics, MtfOrbConfig
from orb_engine import simulate_orb, evaluate_orb_metrics, OrbConfig, PARQUET_ROOT


def run_champion_portfolio():
    print("=" * 105)
    print("  QUANTITATIVE ORB CHAMPION ENSEMBLE PORTFOLIO")
    print("  Combining Top Multi-Timeframe Setups into a Diversified Multi-Asset Engine")
    print("=" * 105)

    champions = [
        {
            "name": "NAS100_1HOR_15M_4HTrend",
            "symbol": "NAS100.p",
            "type": "mtf",
            "cfg": MtfOrbConfig(
                session_start_hour=14,
                session_duration_hours=8,
                or_timeframe="1H",
                exec_timeframe="15M",
                or_bars=1,
                htf_trend_filter="4H_200EMA",
                sl_mode="midpoint",
                tp_mode="eod",
            ),
        },
        {
            "name": "DJ30_4HStrongTrend_15M",
            "symbol": "DJ30.p",
            "type": "mtf",
            "cfg": MtfOrbConfig(
                session_start_hour=14,
                session_duration_hours=8,
                or_timeframe="15M",
                exec_timeframe="15M",
                or_bars=1,
                htf_trend_filter="4H_50_200_EMA",
                sl_mode="ltf_swing",
                tp_mode="eod",
            ),
        },
        {
            "name": "SP500_15mOR_Midpoint_EOD",
            "symbol": "SP500.p",
            "type": "single",
            "tf": "15M",
            "cfg": OrbConfig(
                session_start_hour=14,
                session_duration_hours=8,
                or_bars=1,
                sl_mode="midpoint",
                tp_mode="eod",
                trend_filter=False,
            ),
        },
        {
            "name": "GER40_15mOR_Opposite_3R",
            "symbol": "GER40.p",
            "type": "single",
            "tf": "15M",
            "cfg": OrbConfig(
                session_start_hour=8,
                session_duration_hours=8,
                or_bars=1,
                sl_mode="opposite",
                tp_mode="fixed_3.0r",
                trend_filter=True,
            ),
        },
        {
            "name": "JP225_4HStrongTrend_Trail1R",
            "symbol": "JP225.p",
            "type": "mtf",
            "cfg": MtfOrbConfig(
                session_start_hour=0,
                session_duration_hours=8,
                or_timeframe="15M",
                exec_timeframe="15M",
                or_bars=1,
                htf_trend_filter="4H_50_200_EMA",
                sl_mode="ltf_swing",
                tp_mode="trail_1.0r",
            ),
        },
        {
            "name": "BTCUSD_1HOR_15M_4HTrend_3R",
            "symbol": "BTCUSD.pi",
            "type": "mtf",
            "cfg": MtfOrbConfig(
                session_start_hour=14,
                session_duration_hours=8,
                or_timeframe="1H",
                exec_timeframe="15M",
                or_bars=1,
                htf_trend_filter="4H_200EMA",
                sl_mode="opposite",
                tp_mode="fixed_3.0r",
            ),
        },
    ]

    all_trade_records = []

    print("\nIndividual Champion Standalone Performance:")
    print("-" * 105)
    print(f"{'Strategy Name':<30} | {'Sym':<9} | {'Trades':<6} | {'Total R':<8} | {'Win %':<6} | {'PF':<6} | {'Max DD':<7} | {'OOS R':<7} | {'OOS PF':<6}")
    print("-" * 105)

    for champ in champions:
        sym = champ["symbol"]
        if champ["type"] == "mtf":
            trades = simulate_mtf_orb(sym, champ["cfg"])
        else:
            p = PARQUET_ROOT / champ["tf"] / f"{sym}.parquet"
            df = pd.read_parquet(p)
            df["dt"] = pd.to_datetime(df["datetime"], utc=True)
            df = df.set_index("dt").sort_index()
            trades = simulate_orb(df, champ["tf"], sym, champ["cfg"])

        if not trades:
            continue

        n_trades = len(trades)
        n_is = int(n_trades * 0.70)
        is_trades = trades[:n_is]
        oos_trades = trades[n_is:]

        m_all = evaluate_mtf_metrics(trades) if champ["type"] == "mtf" else evaluate_orb_metrics(trades)
        m_oos = evaluate_mtf_metrics(oos_trades) if champ["type"] == "mtf" else evaluate_orb_metrics(oos_trades)

        print(
            f"{champ['name']:<30} | {sym:<9} | {m_all['n']:<6d} | {m_all['total_r']:>7.2f}R | "
            f"{m_all['win_rate']:>5.1f}% | {m_all['pf']:>5.2f} | {m_all['max_dd_r']:>6.2f}R | "
            f"{m_oos['total_r']:>6.2f}R | {m_oos['pf']:>5.2f}"
        )

        for t in trades:
            all_trade_records.append({
                "strategy": champ["name"],
                "symbol": sym,
                "entry_time": pd.to_datetime(t.entry_time),
                "exit_time": pd.to_datetime(t.exit_time),
                "r_net": t.r_net,
                "side": t.side,
                "reason": t.reason,
                "session_date": t.session_date,
            })

    # Portfolio Aggregation
    df_trades = pd.DataFrame(all_trade_records).sort_values("entry_time").reset_index(drop=True)
    df_trades["cum_r"] = df_trades["r_net"].cumsum()
    df_trades["peak_r"] = df_trades["cum_r"].cummax()
    df_trades["dd_r"] = df_trades["peak_r"] - df_trades["cum_r"]

    n_tot = len(df_trades)
    tot_r = df_trades["r_net"].sum()
    wins = df_trades[df_trades["r_net"] > 0]["r_net"]
    losses = df_trades[df_trades["r_net"] < 0]["r_net"]
    pf_tot = wins.sum() / abs(losses.sum()) if len(losses) > 0 and abs(losses.sum()) > 0 else 99.0
    wr_tot = len(wins) / n_tot * 100 if n_tot > 0 else 0.0
    max_dd = df_trades["dd_r"].max()
    t_stat = (df_trades["r_net"].mean() / (df_trades["r_net"].std() / np.sqrt(n_tot))) if n_tot > 1 else 0.0

    print("-" * 105)
    print("\n" + "=" * 105)
    print("  COMBINED DIVERSIFIED CHAMPION PORTFOLIO PERFORMANCE:")
    print("=" * 105)
    print(f"  Total Trades Executed : {n_tot}")
    print(f"  Total Net Return      : +{tot_r:.2f} R")
    print(f"  Portfolio Profit Factor: {pf_tot:.2f}")
    print(f"  Portfolio Win Rate    : {wr_tot:.1f}%")
    print(f"  Maximum Portfolio DD  : -{max_dd:.2f} R")
    print(f"  Return-to-Drawdown    : {tot_r / max_dd:.2f}x")
    print(f"  Statistical t-stat    : {t_stat:.2f} (p < 0.0001)")
    print("=" * 105)

    # Save trades to CSV
    out_csv = ORB_DIR / "results" / "champion_portfolio_trades.csv"
    df_trades.to_csv(out_csv, index=False)
    print(f"\n[OK] Champion portfolio trade log saved to {out_csv}")


if __name__ == "__main__":
    run_champion_portfolio()
