#!/usr/bin/env python3
"""
orb_mtf_sweep.py — Multi-Timeframe (MTF) ORB Comprehensive Sweep Engine.

Sweeps Multi-Timeframe Architectures across 18 Curated Assets:
  - Arch 1: 4H Macro Trend Filter (4H 200 EMA / 4H 50+200 EMA) + 15M ORB execution
  - Arch 2: 1H Opening Range (1st hour) + 15M Intra-Hour Precision Trigger
  - Arch 3: 1H Opening Range (1st hour) + 5M Ultra-Precision Trigger
  - Arch 4: Pre-Session Volatility Compression Ratio (VCR) Filter
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

from orb_mtf_engine import simulate_mtf_orb, evaluate_mtf_metrics, MtfOrbConfig, PARQUET_ROOT


def run_mtf_sweep():
    print("=" * 105)
    print("  MULTI-TIMEFRAME (MTF) ORB QUANTITATIVE SWEEP")
    print("  Evaluating Multi-Timeframe Architectures across Curated Assets under Real Spreads")
    print("=" * 105)

    assets = [
        # Indices
        ("NAS100.p", "Indices", 14),
        ("SP500.p", "Indices", 14),
        ("DJ30.p", "Indices", 14),
        ("GER40.p", "Indices", 8),
        ("UK100.p", "Indices", 8),
        ("JP225.p", "Indices", 0),
        # Metals & Energy
        ("XAUUSD.pi", "Commodity_Metals", 14),
        ("XAGUSD.pi", "Commodity_Metals", 14),
        ("USWTI.p", "Commodity_Energy", 14),
        # Crypto
        ("BTCUSD.pi", "Crypto", 14),
        ("BTCUSD.pi", "Crypto", 0),
        ("ETHUSD.pi", "Crypto", 14),
        ("SOLUSD.p", "Crypto", 14),
        # Forex
        ("EURUSD.pi", "Forex", 8),
        ("GBPUSD.pi", "Forex", 8),
        ("USDJPY.pi", "Forex", 0),
        ("GBPJPY.pi", "Forex", 8),
    ]

    # Test Architectures:
    # (name, or_tf, exec_tf, or_bars, htf_trend, vcr_filter)
    architectures = [
        # Arch 1: 4H Macro Trend + 15M ORB (15m OR window)
        ("Arch1_4HTrend_15mOR_15MExec", "15M", "15M", 1, "4H_200EMA", False),
        ("Arch1_4HStrongTrend_15mOR_15MExec", "15M", "15M", 1, "4H_50_200_EMA", False),
        
        # Arch 1b: 4H Macro Trend + 15M ORB (30m OR window)
        ("Arch1b_4HTrend_30mOR_15MExec", "15M", "15M", 2, "4H_200EMA", False),

        # Arch 2: 1H Opening Range (first 60m) + 15M Intra-Hour Trigger
        ("Arch2_1HOR_15MExec_NoHTF", "1H", "15M", 1, "none", False),
        ("Arch2_1HOR_15MExec_4HTrend", "1H", "15M", 1, "4H_200EMA", False),

        # Arch 3: 1H Opening Range (first 60m) + 5M Ultra-Precision Trigger
        ("Arch3_1HOR_5MExec_NoHTF", "1H", "5M", 1, "none", False),
        ("Arch3_1HOR_5MExec_4HTrend", "1H", "5M", 1, "4H_200EMA", False),

        # Arch 4: Pre-Session Volatility Compression (VCR) + 15M ORB
        ("Arch4_VCR_15mOR_15MExec", "15M", "15M", 1, "none", True),
        ("Arch4_VCR_4HTrend_15mOR_15MExec", "15M", "15M", 1, "4H_200EMA", True),
    ]

    sl_modes = ["midpoint", "opposite", "ltf_swing"]
    tp_modes = ["fixed_2.0r", "fixed_3.0r", "trail_1.0r", "eod"]

    rows = []

    for sym, cluster, session_hour in assets:
        for arch_name, or_tf, exec_tf, or_bars, htf_trend, vcr in architectures:
            # Check file presence
            exec_p = PARQUET_ROOT / exec_tf / f"{sym}.parquet"
            if not exec_p.exists():
                continue

            for sl_m in sl_modes:
                for tp_m in tp_modes:
                    cfg = MtfOrbConfig(
                        session_start_hour=session_hour,
                        session_duration_hours=8,
                        or_timeframe=or_tf,
                        exec_timeframe=exec_tf,
                        or_bars=or_bars,
                        htf_trend_filter=htf_trend,
                        vcr_filter=vcr,
                        max_vcr=0.80,
                        sl_mode=sl_m,
                        tp_mode=tp_m,
                        stretch_atr_mult=0.05,
                    )

                    try:
                        trades = simulate_mtf_orb(sym, cfg)
                    except Exception as e:
                        continue

                    if not trades or len(trades) < 12:
                        continue

                    n_trades = len(trades)
                    n_is = int(n_trades * 0.70)
                    is_trades = trades[:n_is]
                    oos_trades = trades[n_is:]

                    m_all = evaluate_mtf_metrics(trades)
                    m_is = evaluate_mtf_metrics(is_trades)
                    m_oos = evaluate_mtf_metrics(oos_trades)

                    rows.append({
                        "symbol": sym,
                        "cluster": cluster,
                        "session_utc": session_hour,
                        "arch": arch_name,
                        "or_tf": or_tf,
                        "exec_tf": exec_tf,
                        "or_bars": or_bars,
                        "htf_trend": htf_trend,
                        "vcr": vcr,
                        "sl_mode": sl_m,
                        "tp_mode": tp_m,
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

    df_res = pd.DataFrame(rows)
    print(f"\nTotal MTF Configurations Evaluated: {len(df_res)}")

    out_results = ORB_DIR / "results"
    out_results.mkdir(parents=True, exist_ok=True)
    df_res.to_csv(out_results / "orb_mtf_sweep_full.csv", index=False)

    # Qualified Setups: OOS PF >= 1.25, OOS N >= 6, OOS R > 0, IS PF >= 1.15
    qual = df_res[(df_res["is_pf"] >= 1.15) & (df_res["oos_pf"] >= 1.25) & (df_res["oos_n"] >= 6) & (df_res["oos_r"] > 0)].copy()
    qual = qual.sort_values("oos_r", ascending=False)
    qual.to_csv(out_results / "orb_mtf_qualified.csv", index=False)

    print("\n" + "=" * 105)
    print(f"  TOP VALIDATED MULTI-TIMEFRAME ORB STRATEGIES (OOS PF >= 1.25, OOS N >= 6): {len(qual)} setups")
    print("=" * 105)
    cols = ["symbol", "cluster", "arch", "sl_mode", "tp_mode", "is_r", "is_pf", "oos_r", "oos_pf", "oos_n", "oos_t", "all_r", "all_pf"]
    print(qual[cols].head(35).to_string(index=False))

    # Architecture summary
    print("\n" + "=" * 105)
    print("  ARCHITECTURE BREAKDOWN:")
    print("=" * 105)
    arch_agg = df_res.groupby("arch").agg(
        setups=("symbol", "count"),
        pos_oos=("oos_r", lambda x: (x > 0).sum()),
        avg_is_pf=("is_pf", "mean"),
        avg_oos_pf=("oos_pf", "mean"),
        total_oos_r=("oos_r", "sum")
    ).reset_index()
    print(arch_agg.to_string(index=False))

    print(f"\n[OK] MTF Sweep complete. Results saved to {out_results / 'orb_mtf_qualified.csv'}")


if __name__ == "__main__":
    run_mtf_sweep()
