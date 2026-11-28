"""Stage 1: MT5 True-Spread Per-Asset x 27-Strategy Optimizer & Parameter Vault.

Evaluates ALL 27 distinct strategies individually per MT5-tradable asset (`18` Crypto + `6`
Commodities = `24` assets) at ACTUAL live Blueberry Markets MT5 spreads (+2.0 bps raw
commission/slippage buffer), strictly under Causal In-Sample (<= 2023-12-31 for Crypto,
chronological 50% IS for Commodities):
  - Tests strategy-specific variants + wide-ATR MT5 stop/trail grids per asset
  - Explicitly selects or neglects each strategy per asset on IS plateau + HAC t-stat
  - Prunes redundant IS clones (|corr_IS| <= 0.75) per asset
  - Saves per-asset optimized configuration JSON (`mt5_portfolio/configs/<SYM>.json`)
  - Saves 1m-exact trade ledger with causal `vOF` multiplier (`mt5_portfolio/trades/<SYM>.parquet`)
    for Stage-2 Shared-Account Multi-Asset Portfolio Optimization.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import gc
import json
import math
from pathlib import Path
import time
from typing import Any

import numpy as np
import pandas as pd

from Charts.core.strategy_base import discover_strategies as discover_chart_strategies
from footprint_Chart.backtesting.btc_all25_optimizer_v2 import (
    _df_to_bars,
    _fast_ohlc_fallbacks,
    _prep_arrs,
    _res_to_dicts,
    _to_chart_df,
)
from footprint_Chart.backtesting.btc_quant_pareto_optimizer import ALPHA_BUCKETS
from footprint_Chart.backtesting.fast_kernels import sim_1m_trades_nb
from footprint_Chart.backtesting.multi_asset_optimizer import (
    SPLIT_2024_TS,
    _fast_s04_masks,
    _fast_s07_masks,
    compute_hac_stats,
)
from footprint_Chart.backtesting.multi_asset_prop_optimizer import (
    _mt5_h4_commodity_crosscheck,
    _score_is_oos,
    _sigs_to_arr,
)
from footprint_Chart.backtesting.oi_vwap_sweep_research import (
    attach_causal_oi_vwap_features,
    ensure_full_metrics,
)
from footprint_Chart.backtesting.portfolio_engine import (
    _load_funding_arrays,
    load_asset_tfs_cached,
)
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from footprint_Chart.data_pipeline.dump_26_prop_universe import SPECS_26_PATH
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import adx_arr, ema_arr, rolling_std_arr, sma_arr, wilder_atr
from strategies.delta_divergence import DeltaDivergenceStrategy
from strategies.ema233_imb_rejection import Ema233ImbalanceRejectionStrategy
from strategies.ema_zero_orderflow import EmaZeroOrderFlowStrategy
from strategies.imbalance_stack import ImbalanceStackStrategy
from strategies.of_initiative_breakout import OrderFlowInitiativeBreakoutStrategy
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd
from strategies.vwap_of_reclaim import VwapOrderFlowReclaimStrategy
from strategies.zero_auction_reversal import ZeroAuctionReversalStrategy

MT5_DIR = DATA_DIR / "mt5_portfolio"
CFG_DIR = MT5_DIR / "configs"
TRADES_DIR = MT5_DIR / "trades"
STAGE1_SUMMARY = MT5_DIR / "stage1_summary.json"

# Exact verified mapping from cached Binance datasets -> Blueberry Markets MT5 symbol names
MT5_UNIVERSE: list[dict[str, str]] = [
    # Tight/Medium-Spread Crypto
    {"binance": "ETHUSDT", "mt5": "ETHUSD.pi", "cat": "CRYPTO"},
    {"binance": "BTCUSDT", "mt5": "BTCUSD.pi", "cat": "CRYPTO"},
    {"binance": "ZECUSDT", "mt5": "ZECUSD.p", "cat": "CRYPTO"},
    {"binance": "XMRUSDT", "mt5": "XMRUSD.p", "cat": "CRYPTO"},
    {"binance": "BCHUSDT", "mt5": "BCHUSD.p", "cat": "CRYPTO"},
    {"binance": "AAVEUSDT", "mt5": "AVEUSD.p", "cat": "CRYPTO"},
    # Wide-Spread Crypto (honestly tested at true MT5 spread so pure math decides)
    {"binance": "XRPUSDT", "mt5": "XRPUSD.pi", "cat": "CRYPTO"},
    {"binance": "AVAXUSDT", "mt5": "AVXUSD.p", "cat": "CRYPTO"},
    {"binance": "TRXUSDT", "mt5": "TRXUSD.p", "cat": "CRYPTO"},
    {"binance": "UNIUSDT", "mt5": "UNIUSD.p", "cat": "CRYPTO"},
    {"binance": "BNBUSDT", "mt5": "BNBUSD.p", "cat": "CRYPTO"},
    {"binance": "SOLUSDT", "mt5": "SOLUSD.p", "cat": "CRYPTO"},
    {"binance": "LTCUSDT", "mt5": "LTCUSD.pi", "cat": "CRYPTO"},
    {"binance": "LINKUSDT", "mt5": "LNKUSD.p", "cat": "CRYPTO"},
    {"binance": "ADAUSDT", "mt5": "ADAUSD.p", "cat": "CRYPTO"},
    {"binance": "DOTUSDT", "mt5": "DOTUSD.pi", "cat": "CRYPTO"},
    {"binance": "DOGEUSDT", "mt5": "DOGUSD.p", "cat": "CRYPTO"},
    {"binance": "FILUSDT", "mt5": "FILUSD.p", "cat": "CRYPTO"},
    # Commodities (Footprint + Multi-Year MT5 H4 Cross-Check)
    {"binance": "XAUUSDT", "mt5": "XAUUSD.pi", "cat": "COMMODITY"},
    {"binance": "COPPERUSDT", "mt5": "COPPER.p", "cat": "COMMODITY"},
    {"binance": "CLUSDT", "mt5": "USWTI.p", "cat": "COMMODITY"},
    {"binance": "XAGUSDT", "mt5": "XAGUSD.pi", "cat": "COMMODITY"},
    {"binance": "XPTUSDT", "mt5": "XPTUSD.pi", "cat": "COMMODITY"},
    {"binance": "XPDUSDT", "mt5": "XPDUSD.pi", "cat": "COMMODITY"},
]

EXTRA_BUCKETS = {
    "cg_signal_strategy": "MEANREV",
    "zero_auction_reversal": "MEANREV",
    "ema233_imb_rejection": "MEANREV",
    "ema_zero_orderflow": "PULLBACK",
    "vwap_of_reclaim": "MEANREV",
}


def fetch_live_mt5_specs() -> dict[str, dict[str, Any]]:
    """Query live Blueberry Markets MT5 spreads & swap rates cleanly, with fallback cache."""
    cached = json.loads(SPECS_26_PATH.read_text(encoding="utf-8")) if SPECS_26_PATH.is_file() else {}
    out: dict[str, dict[str, Any]] = {}
    mt5_mod = None
    connected = False
    try:
        import MetaTrader5 as mt5  # type: ignore

        mt5_mod = mt5
        connected = bool(mt5.initialize())
    except Exception:
        connected = False

    try:
        for row in MT5_UNIVERSE:
            b, ms = row["binance"], row["mt5"]
            sp_bps = float(cached.get(b, {}).get("sp_bps") or 10.0)
            sw_l, sw_s = 0.0, 0.0
            if connected and mt5_mod is not None:
                inf = mt5_mod.symbol_info(ms)
                if inf is not None and inf.bid > 0 and inf.ask >= inf.bid:
                    mid = 0.5 * (inf.bid + inf.ask)
                    sp_bps = round(((inf.ask - inf.bid) / mid) * 10000.0, 2)
                    sw_l = float(inf.swap_long)
                    sw_s = float(inf.swap_short)
            # Add +2.0 bps realistic raw commission/execution slippage buffer on top of live bid-ask spread
            eff_cost_bps = round(max(1.5, sp_bps) + 2.0, 2)
            out[b] = {
                "mt5_symbol": ms,
                "live_sp_bps": round(sp_bps, 2),
                "mt5_cost_bps": eff_cost_bps,
                "swap_long": sw_l,
                "swap_short": sw_s,
                "source": "live_mt5" if connected else "cached_specs",
            }
    finally:
        if connected and mt5_mod is not None:
            mt5_mod.shutdown()
    return out


def optimize_asset_mt5(row: dict[str, Any], mspec: dict[str, Any], chart_map: dict[str, Any]) -> dict[str, Any]:
    sym = row["binance"]
    mt5_sym = row["mt5"]
    cat = row["cat"]
    mt5_cost = float(mspec["mt5_cost_bps"])
    live_sp = float(mspec["live_sp_bps"])
    print(f"\n>>> [Stage 1 MT5] Optimizing {sym} -> {mt5_sym} ({cat} | LiveSp={live_sp:.2f}bp | ModelCost={mt5_cost:.2f}bp)...", flush=True)

    g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = load_asset_tfs_cached(sym, DATA_DIR, adaptive_tick=True)
    m1_ts_c = np.ascontiguousarray(m1_ts, dtype=np.int64)
    m1_o_c = np.ascontiguousarray(m1_o, dtype=np.float64)
    m1_h_c = np.ascontiguousarray(m1_h, dtype=np.float64)
    m1_l_c = np.ascontiguousarray(m1_l, dtype=np.float64)
    m1_c_c = np.ascontiguousarray(m1_c, dtype=np.float64)
    f_ts, f_rt = _load_funding_arrays(sym, DATA_DIR)
    fts_c = np.ascontiguousarray(f_ts, dtype=np.int64) if len(f_ts) else np.empty(0, dtype=np.int64)
    frt_c = np.ascontiguousarray(f_rt, dtype=np.float64) if len(f_rt) else np.empty(0, dtype=np.float64)

    a4, a2 = _prep_arrs(g4), _prep_arrs(g2)
    fp4, fp2 = build_fp_context(g4), build_fp_context(g2)
    bars4, bars2 = _df_to_bars(g4), _df_to_bars(g2)
    cdf4, cdf2 = _to_chart_df(g4), _to_chart_df(g2)

    ts_start, ts_end = int(g4["ts"].iloc[0]), int(g4["end_ts"].iloc[-1])
    span_days = max(1.0, (ts_end - ts_start) / 86400.0)
    is_short = (cat == "COMMODITY") or (ts_start >= SPLIT_2024_TS - 120 * 86400)
    split_ts = int(g4["ts"].iloc[len(g4) // 2]) if is_short else SPLIT_2024_TS
    min_is = 5 if is_short else 12

    def _run(arrs, tf_sec, side_arr, sl, tp, tr, be, stp, mh, dyn):
        return sim_1m_trades_nb(
            arrs[0], arrs[1], arrs[2], arrs[3], arrs[4], arrs[5],
            np.ascontiguousarray(side_arr, dtype=np.int8),
            m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c,
            int(tf_sec), float(sl), float(tp), float(tr), float(be), 0.05, bool(stp), int(mh), 1, bool(dyn),
            True, float(live_sp), False, 0.0, 0.0, 5, -2.5, -1.5, fts_c, frt_c,
        )

    c4, h4, l4 = a4[5], g4["h"].to_numpy(dtype=float), g4["l"].to_numpy(dtype=float)
    e89_4, e233_4 = a4[3], a4[4]
    up233, dn233 = c4 > e233_4, c4 < e233_4
    e89_s3 = pd.Series(e89_4).shift(3).to_numpy(dtype=float)
    stkL = (e89_4 > e233_4) & (e89_4 > e89_s3) & up233
    stkS = (e89_4 < e233_4) & (e89_4 < e89_s3) & dn233
    dp4, clv4 = g4["d_pct"].to_numpy(dtype=float), g4["clv"].to_numpy(dtype=float)
    bi4, si4 = g4["buy_imb"].to_numpy(dtype=np.int64), g4["sell_imb"].to_numpy(dtype=np.int64)
    has_b4 = (bi4 + si4) > 0
    vpin4, vpm4 = fp4["vpin_rank"], fp4["vpm_rk"]
    e50_4, e200_4 = ema_arr(c4, 50), ema_arr(c4, 200)
    adx4, atr4_14 = adx_arr(h4, l4, c4, 14), wilder_atr(h4, l4, c4, 14)
    cvdz4, vwz4 = g4["cvd_z24"].to_numpy(dtype=float), g4["vwz48"].to_numpy(dtype=float)
    zbot4, ztop4 = g4["zero_bot"].to_numpy(dtype=bool), g4["zero_top"].to_numpy(dtype=bool)

    c2, h2, l2 = a2[5], g2["h"].to_numpy(dtype=float), g2["l"].to_numpy(dtype=float)
    e89_2, e233_2 = a2[3], a2[4]
    up233_2, dn233_2 = c2 > e233_2, c2 < e233_2
    e89_2s3 = pd.Series(e89_2).shift(3).to_numpy(dtype=float)
    stkL2 = (e89_2 > e233_2) & (e89_2 > e89_2s3) & up233_2
    stkS2 = (e89_2 < e233_2) & (e89_2 < e89_2s3) & dn233_2
    dp2, clv2 = g2["d_pct"].to_numpy(dtype=float), g2["clv"].to_numpy(dtype=float)
    bi2, si2 = g2["buy_imb"].to_numpy(dtype=np.int64), g2["sell_imb"].to_numpy(dtype=np.int64)
    vpin2 = fp2["vpin_rank"]

    trials: dict[str, list[dict[str, Any]]] = {}

    def _push(sid, tf_l, mode_l, cfg_s, df_u, arrs, tf_s, sa, sl, tp, tr, be, stp, mh, dyn):
        res = _run(arrs, tf_s, sa, sl, tp, tr, be, stp, mh, dyn)
        sc = _score_is_oos(res[1], res[7], split_ts, min_is, exit_available_ts=res[2] + 60)
        trials.setdefault(sid, []).append({
            "sid": sid, "tf": tf_l, "mode": mode_l, "cfg": cfg_s,
            "sl": sl, "tp": tp, "tr": tr, "be": be, "mh": mh,
            "df": df_u, "res": res, **sc,
        })

    ohlc4 = _fast_ohlc_fallbacks(g4)

    # ---- Live Sleeves s01..s10 (including wider ATR stop options to absorb MT5 CFD spreads cleanly) ----
    wr4 = fp4["wr14"]
    wr4_p = pd.Series(wr4).shift(1).to_numpy(dtype=float)
    for wt in (-80.0, -75.0):
        wl = stkL & (wr4_p <= wt) & (wr4 > wt) & (dp4 > 3.0) & (clv4 > 0.20) & (~has_b4 | (bi4 >= si4))
        ws = stkS & (wr4_p >= (-100.0 - wt)) & (wr4 < (-100.0 - wt)) & (dp4 < -3.0) & (clv4 < -0.20) & (~has_b4 | (si4 >= bi4))
        sa1 = np.where(wl, 1, np.where(ws, -1, 0))
        _push("s01_williams_pullback", "4H", "Footprint", f"wt={wt},sl=2.0,tr=3.5", g4, a4, 14400, sa1, 2.0, 0.0, 3.5, 0.0, True, 96, True)
        _push("s01_williams_pullback", "4H", "Footprint", f"wt={wt},sl=2.5,tr=4.0", g4, a4, 14400, sa1, 2.5, 0.0, 4.0, 1.0, True, 96, True)
    _push("s01_williams_pullback", "4H", "OHLC", "ohlc,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s01"], 2.0, 3.5, 0.0, 1.0, False, 96, False)

    r2_4 = fp4["rsi2"]
    ofb4 = g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool) | ((dp4 > 5.0) & (clv4 > 0.25))
    ofs4 = g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool) | ((dp4 < -5.0) & (clv4 < -0.25))
    for rl, vc in ((15.0, 0.65), (20.0, 0.65), (15.0, 0.80)):
        sa2 = np.where((vpin4 < vc) & (r2_4 < rl) & up233 & ofb4, 1, np.where((vpin4 < vc) & (r2_4 > (100.0 - rl)) & dn233 & ofs4, -1, 0))
        _push("s02_rsi2_meanrev", "4H", "Footprint", f"rsi={rl},vp={vc},sl=2.5,tp=4.0", g4, a4, 14400, sa2, 2.5, 4.0, 0.0, 1.0, True, 48, True)
        _push("s02_rsi2_meanrev", "4H", "Footprint", f"rsi={rl},vp={vc},sl=3.0,tp=5.0", g4, a4, 14400, sa2, 3.0, 5.0, 0.0, 1.0, True, 64, True)
        _push("rsi2_of_reversion", "4H", "Direct G4", f"rsi={rl},vp={vc},sl=2.5,tp=4.0", g4, a4, 14400, sa2, 2.5, 4.0, 0.0, 1.0, True, 48, True)
    _push("s02_rsi2_meanrev", "4H", "OHLC", "ohlc,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s02"], 2.0, 3.5, 0.0, 1.0, False, 48, False)

    e233_2s, c2s = pd.Series(e233_2).shift(2).to_numpy(dtype=float), pd.Series(c2).shift(2).to_numpy(dtype=float)
    t2L = (l2 <= e233_2 + 0.3 * a2[2]) & (c2 > e233_2) & (c2s > e233_2s)
    t2S = (h2 >= e233_2 - 0.3 * a2[2]) & (c2 < e233_2) & (c2s < e233_2s)
    for im, dg in ((1, 0.0), (1, 3.0), (2, 0.0)):
        sa3 = np.where((bi2 >= im) & (clv2 > 0.25) & t2L & (dp2 > dg), 1, np.where((si2 >= im) & (clv2 < -0.25) & t2S & (dp2 < -dg), -1, 0))
        _push("s03_htf_ema_rejection", "2H", "Footprint", f"imb={im},dp>={dg},sl=2.5,tp=3.5", g2, a2, 7200, sa3, 2.5, 3.5, 0.0, 1.0, False, 72, False)
        _push("s03_htf_ema_rejection", "2H", "Footprint", f"imb={im},dp>={dg},sl=3.0,tp=4.5", g2, a2, 7200, sa3, 3.0, 4.5, 0.0, 1.2, False, 72, False)
    _push("s03_htf_ema_rejection", "4H", "OHLC", "ohlc,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s03"], 2.0, 3.5, 0.0, 1.0, False, 72, False)

    for vpm_m in (0.30, 0.45):
        sqL, sqS = _fast_s04_masks(g4, fp4, {"fp_min_vpm": vpm_m, "fp_min_vr4": 0.90})
        sa4 = np.where(sqL, 1, np.where(sqS, -1, 0))
        _push("s04_vc_squeeze_breakout", "4H", "Footprint", f"vpm={vpm_m},sl=2.0,tr=3.5", g4, a4, 14400, sa4, 2.0, 0.0, 3.5, 0.0, True, 96, True)
        _push("s04_vc_squeeze_breakout", "4H", "Footprint", f"vpm={vpm_m},sl=2.5,tr=4.0", g4, a4, 14400, sa4, 2.5, 0.0, 4.0, 1.0, True, 96, True)
    _push("s04_vc_squeeze_breakout", "4H", "OHLC", "ohlc,sl=2.0,tp=4.0", g4, a4, 14400, ohlc4["s04"], 2.0, 4.0, 0.0, 1.0, False, 96, False)

    mu50 = sma_arr(c4, 50)
    sd50 = np.where(rolling_std_arr(c4, 50) > 1e-12, rolling_std_arr(c4, 50), np.nan)
    zv50 = (c4 - mu50) / sd50
    zvp50 = pd.Series(zv50).shift(1).to_numpy(dtype=float)
    atr_exp = atr4_14 >= pd.Series(atr4_14).rolling(20).mean().to_numpy(dtype=float)
    zl5 = (zvp50 < 1.5) & (zv50 >= 1.5) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 18.0) & atr_exp
    zs5 = (zvp50 > -1.5) & (zv50 <= -1.5) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 18.0) & atr_exp
    _push("s05_zscore_breakout", "4H", "OHLC", "ohlc,lb=50,z=1.5,sl=2.0,tp=3.5", g4, a4, 14400, np.where(zl5, 1, np.where(zs5, -1, 0)), 2.0, 3.5, 0.0, 1.0, False, 96, False)
    _push("s05_zscore_breakout", "4H", "Footprint", "fp,lb=50,z=1.5,sl=2.5,tr=3.5", g4, a4, 14400, np.where(zl5 & (dp4 > 0) & (clv4 > 0.15), 1, np.where(zs5 & (dp4 < 0) & (clv4 < -0.15), -1, 0)), 2.5, 0.0, 3.5, 0.0, False, 96, True)

    for zt_v, vc in ((1.75, 0.75), (2.25, 0.60)):
        fl6 = (vpin4 < vc) & (vwz4 < -zt_v) & ((dp4 > 0.0) | (clv4 > 0.20) | zbot4)
        fs6 = (vpin4 < vc) & (vwz4 > zt_v) & ((dp4 < 0.0) | (clv4 < -0.20) | ztop4)
        sa6 = np.where(fl6, 1, np.where(fs6, -1, 0))
        _push("s06_iv_wall_fade", "4H", "Footprint", f"vwz={zt_v},vp={vc},sl=2.5,tp=4.5", g4, a4, 14400, sa6, 2.5, 4.5, 0.0, 1.2, False, 48, False)
        _push("s06_iv_wall_fade", "4H", "Footprint", f"vwz={zt_v},vp={vc},sl=3.0,tp=5.0", g4, a4, 14400, sa6, 3.0, 5.0, 0.0, 1.2, False, 64, False)

    for sk in (2, 3):
        pL, pS = _fast_s07_masks(g4, fp4, {"swing_k": sk})
        sf7 = np.where(pL, 1, np.where(pS, -1, 0))
        _push("s07_daily_pivot_breakout", "4H", "Footprint", f"fp,k={sk},sl=2.0,tr=3.0", g4, a4, 14400, sf7, 2.0, 0.0, 3.0, 1.0, True, 96, True)
        _push("s07_daily_pivot_breakout", "4H", "FP+CVD", f"cvd,k={sk},sl=2.5,tr=3.5", g4, a4, 14400, np.where((sf7 == 1) & (cvdz4 > 0.5), 1, np.where((sf7 == -1) & (cvdz4 < -0.5), -1, 0)), 2.5, 0.0, 3.5, 1.0, True, 96, True)
    _push("s07_daily_pivot_breakout", "4H", "OHLC", "ohlc,k=2,sl=2.0,tp=3.0", g4, a4, 14400, ohlc4["s07"], 2.0, 3.0, 0.0, 1.0, False, 96, False)

    for dn in (16, 22):
        hi_d, lo_d = pd.Series(h4).shift(1).rolling(dn).max().to_numpy(), pd.Series(l4).shift(1).rolling(dn).min().to_numpy()
        ml9 = (c4 > hi_d) & up233 & (clv4 > 0.25) & (dp4 > 0.0) & (~has_b4 | (bi4 >= si4))
        ms9 = (c4 < lo_d) & dn233 & (clv4 < -0.25) & (dp4 < 0.0) & (~has_b4 | (si4 >= bi4))
        sa9 = np.where(ml9, 1, np.where(ms9, -1, 0))
        _push("s09_donchian_trend_cta", "4H", "Footprint", f"dn={dn},sl=2.0,tr=3.5", g4, a4, 14400, sa9, 2.0, 0.0, 3.5, 0.0, False, 96, True)
        _push("s09_donchian_trend_cta", "4H", "Footprint", f"dn={dn},sl=2.5,tr=4.0", g4, a4, 14400, sa9, 2.5, 0.0, 4.0, 1.0, False, 96, True)
    _push("s09_donchian_trend_cta", "4H", "OHLC", "ohlc,dn=20,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s09"], 2.0, 3.5, 0.0, 1.0, False, 96, False)

    for dpm in (2.0, 5.0):
        tl10 = stkL & (l4 <= e89_4 + 0.30 * a4[2]) & (c4 > e89_4) & (dp4 > dpm) & (clv4 > 0.20)
        ts10 = stkS & (h4 >= e89_4 - 0.30 * a4[2]) & (c4 < e89_4) & (dp4 < -dpm) & (clv4 < -0.20)
        sa10 = np.where(tl10, 1, np.where(ts10, -1, 0))
        _push("s10_ema_pullback", "4H", "Footprint", f"dp={dpm},sl=2.0,tr=3.5", g4, a4, 14400, sa10, 2.0, 0.0, 3.5, 0.0, False, 96, True)
        _push("s10_ema_pullback", "4H", "Footprint", f"dp={dpm},sl=2.5,tr=4.0", g4, a4, 14400, sa10, 2.5, 0.0, 4.0, 1.0, False, 96, True)
    _push("s10_ema_pullback", "4H", "OHLC", "ohlc,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s10"], 2.0, 3.5, 0.0, 1.0, False, 96, False)

    # ---- Footprint Plugins ----
    cg_b = (g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool)) & stkL & (dp4 > 0.0)
    cg_s = (g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool)) & stkS & (dp4 < 0.0)
    _push("cg_signal_strategy", "4H", "Hybrid FP", "cgstk,sl=2.5,tr=3.5", g4, a4, 14400, np.where(cg_b, 1, np.where(cg_s, -1, 0)), 2.5, 0.0, 3.5, 0.0, False, 96, True)

    dd_cls, imb_cls = DeltaDivergenceStrategy(), ImbalanceStackStrategy()
    zr_cls, e233_cls = ZeroAuctionReversalStrategy(), Ema233ImbalanceRejectionStrategy()
    ez_cls, of_cls, vw_cls = EmaZeroOrderFlowStrategy(), OrderFlowInitiativeBreakoutStrategy(), VwapOrderFlowReclaimStrategy()

    for tf_l, df_u, arrs, tf_s, b_u, up_u, dn_u in (("4H", g4, a4, 14400, bars4, up233, dn233), ("2H", g2, a2, 7200, bars2, up233_2, dn233_2)):
        sa_dd = _sigs_to_arr(len(df_u), dd_cls.generate_signals(b_u, {"min_abs_delta_pct": 10.0}))
        _push("delta_divergence", tf_l, "FP+EMA233", "dp>=10,ema233,sl=2.5,tp=3.5", df_u, arrs, tf_s, np.where(((sa_dd == 1) & up_u) | ((sa_dd == -1) & dn_u), sa_dd, 0), 2.5, 3.5, 0.0, 1.0, False, 48, False)

        sa_imb = _sigs_to_arr(len(df_u), imb_cls.generate_signals(b_u, {"min_imbalances": 3}))
        _push("imbalance_stack", tf_l, "Pure FP", "imb>=3,sl=2.5,tp=3.5", df_u, arrs, tf_s, sa_imb, 2.5, 3.5, 0.0, 1.0, False, 48, False)
        _push("imbalance_stack", tf_l, "FP+EMA233", "imb>=3,ema233,sl=2.5,tr=3.5", df_u, arrs, tf_s, np.where(((sa_imb == 1) & up_u) | ((sa_imb == -1) & dn_u), sa_imb, 0), 2.5, 0.0, 3.5, 0.0, False, 72, True)

        sa_zr = _sigs_to_arr(len(df_u), zr_cls.generate_signals(b_u, {"require_delta_confirm": 1, "min_extreme_val": 1.0}))
        _push("zero_auction_reversal", tf_l, "FP+EMA233", "reqd=1,ema233,sl=2.5,tr=3.5", df_u, arrs, tf_s, np.where(((sa_zr == 1) & up_u) | ((sa_zr == -1) & dn_u), sa_zr, 0), 2.5, 0.0, 3.5, 0.0, False, 72, True)

        sa_e2 = _sigs_to_arr(len(df_u), e233_cls.generate_signals(b_u, {"min_imb": 2, "min_clv": 0.25}))
        _push("ema233_imb_rejection", tf_l, "Hybrid FP", "imb=2,clv=0.25,sl=2.5,tp=4.0", df_u, arrs, tf_s, sa_e2, 2.5, 4.0, 0.0, 1.0, False, 72, False)

        sa_ez = _sigs_to_arr(len(df_u), ez_cls.generate_signals(b_u, {"min_delta_pct": 3.0, "enable_ema_touch": 1, "enable_zero_trend": 1}))
        _push("ema_zero_orderflow", tf_l, "Hybrid FP", "dp=3.0,sl=2.5,tr=4.5", df_u, arrs, tf_s, sa_ez, 2.5, 0.0, 4.5, 0.0, False, 96, True)

        for lb in (16, 22):
            sa_of = _sigs_to_arr(len(df_u), of_cls.generate_signals(b_u, {"lookback": lb, "min_delta_pct": 2.0, "min_clv": 0.25, "require_imb_dom": 1}))
            _push("of_initiative_breakout", tf_l, "Hybrid FP", f"lb={lb},dp=2,sl=2.5,tr=3.5", df_u, arrs, tf_s, sa_of, 2.5, 0.0, 3.5, 0.0, False, 96, True)

        sa_vw = _sigs_to_arr(len(df_u), vw_cls.generate_signals(b_u, {"vwap_len": 48, "z_thresh": 2.0}))
        _push("vwap_of_reclaim", tf_l, "Hybrid FP", "zt=2.0,sl=2.5,tp=4.5", df_u, arrs, tf_s, sa_vw, 2.5, 4.5, 0.0, 1.2, False, 48, False)

    # ---- 3 Independent OI + 2D/4D VWAP + VolPump Strategies ----
    try:
        oi_start_d = date(2025, 11, 1) if cat == "COMMODITY" else date(2021, 12, 1)
        m_df = ensure_full_metrics(sym, start_d=oi_start_d)
        if not m_df.empty and len(m_df) >= 200:
            g4_oi = attach_causal_oi_vwap_features(g4, m_df, bar_hours=4)
            g2_oi = attach_causal_oi_vwap_features(g2, m_df, bar_hours=2)
            oi_t0 = int(m_df["ts"].iloc[0]) + (24 * 7200 if cat == "COMMODITY" else 48 * 7200)
            valid4 = g4_oi["ts"].to_numpy(dtype=np.int64) >= oi_t0
            valid2 = g2_oi["ts"].to_numpy(dtype=np.int64) >= oi_t0

            zdoi4, zint4, zvol4 = g4_oi["z_doi"].to_numpy(float), g4_oi["z_intrabar_drop"].to_numpy(float), g4_oi["z_vol"].to_numpy(float)
            vwl4, vwh4 = g4_oi["vwz2d_low"].to_numpy(float) <= -2.0, g4_oi["vwz2d_high"].to_numpy(float) >= 2.0

            for zt_oi, zt_v in ((1.5, 1.5), (1.25, 1.25)):
                p4 = (zdoi4 <= -zt_oi) | (zint4 <= -(zt_oi + 0.3))
                sa_c1_long = np.where(valid4 & vwh4 & p4 & (zvol4 >= zt_v) & up233, 1, 0)
                sa_c1_both = np.where(valid4 & vwh4 & p4 & (zvol4 >= zt_v) & up233 & (clv4 > 0.1), 1, np.where(valid4 & vwl4 & p4 & (zvol4 >= zt_v) & dn233 & (clv4 < -0.1), -1, 0))
                _push("oi_4h_sqz_mom", "4H", "OI+VolPump", f"4H_C1_Long,oi={zt_oi},v={zt_v}", g4, a4, 14400, sa_c1_long, 2.5, 0.0, 3.5, 1.0, False, 72, True)
                _push("oi_4h_sqz_mom", "4H", "OI+VolPump", f"4H_C1_Both,oi={zt_oi},v={zt_v}", g4, a4, 14400, sa_c1_both, 2.5, 0.0, 3.5, 1.0, False, 72, True)

            htf_r = pd.DataFrame({"end_ts": g4["end_ts"].to_numpy(np.int64), "up233": up233, "dn233": dn233}).sort_values("end_ts")
            m_htf = pd.merge_asof(g2[["end_ts"]].sort_values("end_ts"), htf_r, on="end_ts", direction="backward")
            u2h, d2h = m_htf["up233"].fillna(False).to_numpy(bool), m_htf["dn233"].fillna(False).to_numpy(bool)
            zdoi2, zint2, zvol2 = g2_oi["z_doi"].to_numpy(float), g2_oi["z_intrabar_drop"].to_numpy(float), g2_oi["z_vol"].to_numpy(float)
            vwl2, vwh2 = g2_oi["vwz2d_low"].to_numpy(float) <= -2.0, g2_oi["vwz2d_high"].to_numpy(float) >= 2.0

            for zt_oi, zt_v, sl_m, tr_m in ((1.5, 1.5, 2.5, 3.5), (1.25, 1.25, 2.5, 3.5), (1.5, 1.5, 3.0, 4.0)):
                purge2 = (zdoi2 <= -zt_oi) | (zint2 <= -(zt_oi + 0.3))
                sa_b1 = np.where(valid2 & purge2 & (zvol2 >= zt_v) & vwl2 & d2h & (clv2 < -0.2), -1, np.where(valid2 & purge2 & (zvol2 >= zt_v) & vwh2 & u2h & (clv2 > 0.2), 1, 0))
                _push("oi_2h_liq_break", "2H", "OI+VolPump", f"2H_B1,oi={zt_oi},v={zt_v},sl={sl_m}", g2, a2, 7200, sa_b1, sl_m, 0.0, tr_m, 1.0, False, 72, True)

            purge2_def = (zdoi2 <= -1.5) | (zint2 <= -1.8)
            v48_Lp = pd.Series((g2_oi["vwz48"].to_numpy(float) <= -2.0) & (zdoi2 <= -1.5)).shift(1).fillna(False).to_numpy(bool) & (c2 > g2["o"].to_numpy(float))
            v48_Sp = pd.Series((g2_oi["vwz48"].to_numpy(float) >= 2.0) & (zdoi2 <= -1.5)).shift(1).fillna(False).to_numpy(bool) & (c2 < g2["o"].to_numpy(float))
            sa_b3 = np.where(valid2 & v48_Lp & u2h, 1, np.where(valid2 & v48_Sp & d2h, -1, 0))
            _push("oi_2h_reclaim_fade", "2H", "OI+Reclaim", "2H_B3_4DVWAP_Reclaim", g2, a2, 7200, sa_b3, 3.0, 0.0, 4.0, 1.5, False, 96, True)

            swS_p = pd.Series(vwh2 & purge2_def & (zvol2 >= 1.5)).shift(1).fillna(False).to_numpy(bool)
            sa_a2 = np.where(valid2 & swS_p & (c2 < g2["o"].to_numpy(float)) & d2h, -1, 0)
            _push("oi_2h_reclaim_fade", "2H", "OI+Reclaim", "2H_A2_ShortSqzFade_DN233", g2, a2, 7200, sa_a2, 3.0, 0.0, 4.0, 1.5, False, 96, True)
    except Exception as oi_exc:
        print(f"  [oi_warn] {sym}: {oi_exc}", flush=True)

    # ---- 5 Charts Plugins ----
    c_grid = {
        "bollinger_zscore": ("bb_zscore", [{"window": 20, "z_thresh": 2.0}, {"window": 50, "z_thresh": 1.5}]),
        "donchian_breakout": ("donchian_cta", [{"lookback": 22}, {"lookback": 30}]),
        "ema_pullback": ("ema_pullback", [
            {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 48.0, "rsi_sell_min": 52.0},
            {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 60.0, "rsi_sell_min": 40.0},
        ]),
        "rsi2_mean_reversion": ("rsi2_meanrev", [{"oversold": 15.0, "overbought": 85.0}]),
        "vwap_rejection": ("vwap_reclaim", [{"min_dist_atr": 0.3}]),
    }
    for sid, (cid, plist) in c_grid.items():
        plug = chart_map[cid]
        for tf_l, df_u, arrs, tf_s, cdf_u, up_u, dn_u in (("4H", g4, a4, 14400, cdf4, up233, dn233), ("2H", g2, a2, 7200, cdf2, up233_2, dn233_2)):
            for prm in plist:
                sa = _sigs_to_arr(len(df_u), plug.generate_signals(cdf_u, prm))
                _push(sid, tf_l, "Pure OHLC", f"ohlc,{prm},sl=2.5,tr=3.5", df_u, arrs, tf_s, sa, 2.5, 0.0, 3.5, 0.0, False, 96, True)
                if sid != "rsi2_mean_reversion":
                    sa_tr = np.where(((sa == 1) & up_u) | ((sa == -1) & dn_u), sa, 0)
                    _push(sid, tf_l, "OHLC+EMA233", f"ema233,{prm},sl=2.5,tr=4.0", df_u, arrs, tf_s, sa_tr, 2.5, 0.0, 4.0, 0.0, False, 96, True)

    # ---- Precompute Causal vOF arrays on g4 for Stage 2 zero-lookahead lookup ----
    ca4 = g4["c"].to_numpy(float)
    ea4 = g4["end_ts"].to_numpy(np.int64)
    imba4 = bi4 - si4
    sum_imba4 = bi4 + si4

    # ---- Causal IS Plateau Optimization per Strategy + Explicit Neglect Reasons ----
    strat_catalog: list[dict[str, Any]] = []
    qual_trades: dict[str, list[dict[str, Any]]] = {}

    for sid, tlist in sorted(trials.items()):
        raw_arr = np.array([t["raw_score"] for t in tlist], dtype=float)
        for idx_t, t in enumerate(tlist):
            om = (float(raw_arr.sum() - raw_arr[idx_t]) / (len(raw_arr) - 1)) if len(raw_arr) > 1 else raw_arr[idx_t]
            t["plateau"] = 0.65 * t["raw_score"] + 0.35 * max(-1.0, om)
        best = max(tlist, key=lambda x: x["plateau"])

        neglect_reasons = []
        if best["nis"] < min_is:
            neglect_reasons.append(f"low_IS_n({best['nis']}<{min_is})")
        if best["ispf"] < 1.15:
            neglect_reasons.append(f"weak_IS_PF({best['ispf']:.2f}<1.15)")
        if best["isr"] <= (2.0 if is_short else 4.0):
            neglect_reasons.append(f"low_IS_R({best['isr']:.1f})")
        if best["ist"] < 0.65:
            neglect_reasons.append(f"weak_t_hac({best['ist']:.2f}<0.65)")
        if best["hmin"] <= -4.5:
            neglect_reasons.append(f"unstable_half({best['hmin']:.1f}R)")

        is_qualified = len(neglect_reasons) == 0
        bucket = ALPHA_BUCKETS.get(sid, EXTRA_BUCKETS.get(sid, "OTHER"))

        strat_catalog.append({
            "sid": sid, "bucket": bucket, "tf": best["tf"], "mode": best["mode"], "cfg": best["cfg"],
            "sl": best["sl"], "tp": best["tp"], "tr": best["tr"], "be": best["be"], "mh": best["mh"],
            "status": "QUALIFIED" if is_qualified else "NEGLECTED",
            "neglect_reason": "; ".join(neglect_reasons) if neglect_reasons else "",
            "nis": best["nis"], "isr": best["isr"], "ispf": best["ispf"], "ist": best["ist"], "hmin": best["hmin"],
            "noos": best["noos"], "oosr": best["oosr"], "oospf": best["oospf"],
            "n_all": best["n_all"], "r_all": best["r_all"], "pf_all": best["pf_all"],
        })

        if is_qualified:
            trs = _res_to_dicts(best["df"], sid, f"{best['tf']} {best['mode']}", best["res"])
            for tr in trs:
                idx_r = max(0, int(np.searchsorted(ea4, int(tr["ts_in"]), side="right")) - 1)
                s_sign = 1 if tr["side"] == "BUY" else -1
                imb_v = int(imba4[idx_r]) if sum_imba4[idx_r] > 0 else s_sign
                vof_mult = vol_of_conviction_risk_usd(
                    ca4[max(0, idx_r - 245) : idx_r + 1], "H4", s_sign,
                    float(dp4[idx_r]), imb_v, float(vpin4[idx_r]),
                    1.0, 0.65, 1.25, target_ann_vol=0.55, lookback=240,
                )
                tr["symbol"] = sym
                tr["mt5_symbol"] = mt5_sym
                tr["category"] = cat
                tr["bucket"] = bucket
                tr["tf"] = best["tf"]
                tr["mode"] = best["mode"]
                tr["cfg"] = best["cfg"]
                tr["vof_mult"] = round(float(vof_mult), 4)
                tr["mt5_cost_bps"] = mt5_cost
            qual_trades[sid] = trs

    # ---- Prune Exact Clones Strictly on IS (|corr_IS| <= 0.72) ----
    qual_list = sorted([r for r in strat_catalog if r["status"] == "QUALIFIED"], key=lambda x: x["ist"] * x["ispf"], reverse=True)
    is_dm: dict[str, dict[int, float]] = {}
    all_d = set()
    for r in qual_list:
        dm: dict[int, float] = {}
        for t in qual_trades[r["sid"]]:
            if t["ts_in"] < split_ts:
                dk = int(t["ts_out"] // 86400)
                dm[dk] = dm.get(dk, 0.0) + float(t["r"])
                all_d.add(dk)
        is_dm[r["sid"]] = dm

    picked_sids: list[str] = []
    if len(all_d) >= 8 and len(qual_list) >= 2:
        mat = pd.DataFrame({r["sid"]: [is_dm[r["sid"]].get(d, 0.0) for d in sorted(all_d)] for r in qual_list}).fillna(0.0)
        cmat = mat.corr()
        for r in qual_list:
            s = r["sid"]
            if any(abs(float(cmat.loc[s, p])) > 0.72 for p in picked_sids):
                r["status"] = "CLONE_PRUNED"
                r["neglect_reason"] = "IS_corr > 0.72 with higher-ranked strategy"
                continue
            picked_sids.append(s)
    else:
        picked_sids = [r["sid"] for r in qual_list]

    # Save 1m-exact trade parquet for ALL non-neglected/picked strategies on this asset
    kept_rows = [t for s in picked_sids for t in qual_trades[s]]
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    trades_pq = TRADES_DIR / f"{sym}.parquet"
    if kept_rows:
        pd.DataFrame(kept_rows).to_parquet(trades_pq, index=False)
    elif trades_pq.is_file():
        trades_pq.unlink()

    mt5_chk = _mt5_h4_commodity_crosscheck(sym, mt5_cost) if cat == "COMMODITY" else {}

    out_cfg = {
        "symbol": sym, "mt5_symbol": mt5_sym, "category": cat,
        "span_days": round(span_days, 1),
        "span_start": datetime.fromtimestamp(ts_start, tz=timezone.utc).strftime("%Y-%m-%d"),
        "span_end": datetime.fromtimestamp(ts_end, tz=timezone.utc).strftime("%Y-%m-%d"),
        "split_iso": datetime.fromtimestamp(split_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "live_sp_bps": live_sp, "mt5_cost_bps": mt5_cost,
        "swap_long": mspec["swap_long"], "swap_short": mspec["swap_short"],
        "total_strategies_tested": len(strat_catalog),
        "qualified_kept": len(picked_sids),
        "neglected_or_pruned": len(strat_catalog) - len(picked_sids),
        "selected_sids": picked_sids,
        "kept_trades_count": len(kept_rows),
        "strategy_scorecard": strat_catalog,
        "mt5_h4_crosscheck": mt5_chk,
    }
    (CFG_DIR / f"{sym}.json").write_text(json.dumps(out_cfg, indent=2), encoding="utf-8")

    # Quick console summary
    oos_rs = sum(r["oosr"] for r in qual_list if r["sid"] in picked_sids)
    print(f"    -> {sym:10s} ({mt5_sym:10s}) | Kept={len(picked_sids):2d}/{len(strat_catalog)} strats | Trades={len(kept_rows):4d} | SumIS_R={sum(r['isr'] for r in qual_list if r['sid'] in picked_sids):+6.1f}R | SumOOS_R={oos_rs:+6.1f}R", flush=True)

    del g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c, bars4, bars2, cdf4, cdf2
    gc.collect()
    return out_cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 1: MT5 Per-Asset x 27-Strategy True-Spread Optimizer")
    ap.add_argument("--symbols", type=str, default="", help="Optional comma-separated Binance symbol filter")
    ap.add_argument("--skip-existing", action="store_true", help="Skip symbols with existing configs/<SYM>.json")
    args = ap.parse_args()

    t0 = time.time()
    MT5_DIR.mkdir(parents=True, exist_ok=True)
    mspecs = fetch_live_mt5_specs()
    chart_map = discover_chart_strategies()
    sym_filter = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    results: list[dict[str, Any]] = []
    for row in MT5_UNIVERSE:
        sym = row["binance"]
        if sym_filter is not None and sym not in sym_filter:
            cfg_p = CFG_DIR / f"{sym}.json"
            if cfg_p.is_file():
                results.append(json.loads(cfg_p.read_text(encoding="utf-8")))
            continue
        if args.skip_existing and (CFG_DIR / f"{sym}.json").is_file():
            results.append(json.loads((CFG_DIR / f"{sym}.json").read_text(encoding="utf-8")))
            continue
        res = optimize_asset_mt5(row, mspecs[sym], chart_map)
        results.append(res)

    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "total_assets": len(results),
        "assets": [
            {
                "symbol": r["symbol"], "mt5_symbol": r["mt5_symbol"], "category": r["category"],
                "mt5_cost_bps": r["mt5_cost_bps"], "kept": r["qualified_kept"],
                "neglected": r["neglected_or_pruned"], "trades": r["kept_trades_count"],
                "selected_sids": r["selected_sids"],
            }
            for r in results
        ],
    }
    STAGE1_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[Stage 1 Complete in {summary['elapsed_sec']}s] Saved {len(results)} asset configs & trade ledgers -> {MT5_DIR}", flush=True)


if __name__ == "__main__":
    main()
