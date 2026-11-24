#!/usr/bin/env python3
"""
orb_sweep.py — Multi-Asset & Multi-Timeframe ORB Quantitative Sweep Engine.

Evaluates Opening Range Breakouts across:
  - 18 Curated Assets (Indices, Commodities/Metals, Crypto, FX Majors/Crosses)
  - Timeframes: 15M, 1H, 5M
  - Opening Range Windows: 15-minute, 30-minute, 1-hour
  - Sessions: New York Open (14:00 UTC), London Open (08:00 UTC), Tokyo/Asian Open (00:00 UTC)
  - Stop Loss Modes: Midpoint, Opposite
  - Exit Modes: Fixed 2R, Fixed 3R, Fixed 4R, Trailing 1R, End-of-Day (EOD)
  - Multi-Timeframe Combination: 4H Trend alignment + 15M ORB execution
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

from orb_engine import simulate_orb, evaluate_orb_metrics, OrbConfig, PARQUET_ROOT


def run_orb_sweep():
    print("=" * 100)
    print("  ORB QUANTITATIVE SWEEP — MULTI-ASSET & MULTI-TIMEFRAME EVALUATION")
    print("  Testing 18 Curated Assets on historical_parquet with Real Spreads & 70/30 IS/OOS Split")
    print("=" * 100)

    assets = [
        # Indices (Natural ORB fit)
        ("NAS100.p", "Indices", 14),   # NY Open
        ("SP500.p", "Indices", 14),    # NY Open
        ("DJ30.p", "Indices", 14),     # NY Open
        ("GER40.p", "Indices", 8),     # London Open
        ("UK100.p", "Indices", 8),     # London Open
        ("JP225.p", "Indices", 0),     # Tokyo Open
        # Metals & Energy
        ("XAUUSD.pi", "Commodity_Metals", 14),  # NY Open
        ("XAUAUD.pi", "Commodity_Metals", 14),  # NY Open
        ("XAGUSD.pi", "Commodity_Metals", 14),  # NY Open
        ("USWTI.p", "Commodity_Energy", 14),    # NY Open
        # Crypto
        ("BTCUSD.pi", "Crypto", 14),   # NY Open
        ("BTCUSD.pi", "Crypto", 0),    # Daily UTC Open
        ("ETHUSD.pi", "Crypto", 14),   # NY Open
        ("SOLUSD.p", "Crypto", 14),    # NY Open
        # Forex
        ("EURUSD.pi", "Forex", 8),     # London Open
        ("GBPUSD.pi", "Forex", 8),     # London Open
        ("USDJPY.pi", "Forex", 0),     # Tokyo Open
        ("GBPJPY.pi", "Forex", 8),     # London Open
    ]

    # Timeframe configurations
    # (tf, or_bars, desc)
    tf_configs = [
        ("15M", 1, "15m_OR"),     # 15-minute Opening Range
        ("15M", 2, "30m_OR"),     # 30-minute Opening Range (first two 15M bars)
        ("15M", 4, "60m_OR"),     # 1-hour Opening Range (first four 15M bars)
        ("1H", 1, "60m_OR_1H"),   # 1-hour Opening Range on 1H bars
        ("5M", 3, "15m_OR_5M"),   # 15-minute Opening Range on 5M bars
    ]

    sl_modes = ["midpoint", "opposite"]
    tp_modes = ["fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r", "eod"]

    rows = []

    for sym, cluster, session_hour in assets:
        for tf, or_bars, or_desc in tf_configs:
            p = PARQUET_ROOT / tf / f"{sym}.parquet"
            if not p.exists():
                continue

            try:
                df = pd.read_parquet(p)
                df["dt"] = pd.to_datetime(df["datetime"], utc=True)
                df = df.set_index("dt").sort_index()
            except Exception:
                continue

            for sl_m in sl_modes:
                for tp_m in tp_modes:
                    for trend in [True, False]:
                        cfg = OrbConfig(
                            session_start_hour=session_hour,
                            session_duration_hours=8,
                            or_bars=or_bars,
                            sl_mode=sl_m,
                            tp_mode=tp_m,
                            trend_filter=trend,
                            ema_period=200,
                            max_orvr=1.2,
                            stretch_atr_mult=0.05,
                        )

                        trades = simulate_orb(df, tf, sym, cfg)
                        if not trades or len(trades) < 15:
                            continue

                        n_trades = len(trades)
                        n_is = int(n_trades * 0.70)
                        is_trades = trades[:n_is]
                        oos_trades = trades[n_is:]

                        m_all = evaluate_orb_metrics(trades)
                        m_is = evaluate_orb_metrics(is_trades)
                        m_oos = evaluate_orb_metrics(oos_trades)

                        rows.append({
                            "symbol": sym,
                            "cluster": cluster,
                            "session_utc": session_hour,
                            "tf": tf,
                            "or_window": or_desc,
                            "sl_mode": sl_m,
                            "tp_mode": tp_m,
                            "trend_filter": trend,
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

    df_sweep = pd.DataFrame(rows)
    print(f"\nTotal Configurations Evaluated: {len(df_sweep)}")

    out_results = ORB_DIR / "results"
    out_results.mkdir(parents=True, exist_ok=True)
    df_sweep.to_csv(out_results / "orb_sweep_full.csv", index=False)

    # Filter for Validated Out-Of-Sample Setups (IS PF >= 1.25, OOS PF >= 1.20, OOS N >= 8, OOS R > 0)
    qual = df_sweep[(df_sweep["is_pf"] >= 1.25) & (df_sweep["oos_pf"] >= 1.20) & (df_sweep["oos_n"] >= 8) & (df_sweep["oos_r"] > 0)].copy()
    qual = qual.sort_values("oos_r", ascending=False)

    qual.to_csv(out_results / "orb_qualified.csv", index=False)

    print("\n" + "=" * 100)
    print(f"  TOP VALIDATED ORB STRATEGIES (OOS PF >= 1.20, OOS N >= 8): {len(qual)} setups")
    print("=" * 100)
    cols = ["symbol", "cluster", "tf", "or_window", "sl_mode", "tp_mode", "trend_filter", "is_r", "is_pf", "oos_r", "oos_pf", "oos_n", "oos_t", "all_r", "all_pf"]
    print(qual[cols].head(30).to_string(index=False))

    # Cluster Summary
    print("\n" + "=" * 100)
    print("  CLUSTER PERFORMANCE SUMMARY FOR ORB:")
    print("=" * 100)
    cluster_agg = df_sweep.groupby(["cluster", "tf"]).agg(
        setups=("symbol", "count"),
        pos_oos=("oos_r", lambda x: (x > 0).sum()),
        avg_is_pf=("is_pf", "mean"),
        avg_oos_pf=("oos_pf", "mean"),
        total_oos_r=("oos_r", "sum")
    ).reset_index()
    print(cluster_agg.to_string(index=False))

    print(f"\n[OK] ORB Sweep complete. Results saved to {out_results / 'orb_qualified.csv'}")


if __name__ == "__main__":
    run_orb_sweep()
