"""26-Strategy x Mutual Binance-MT5 Per-Account ($5,000 Prop Farm) Optimizer.

Stage A (Zero Cross-Strategy Clone Pruning):
  Evaluates all 26 Crypto Footprint + OI + OHLC strategies independently on every
  mutual Binance <-> Blueberry MT5 asset with genuine Vision/aggTrades data,
  saving every (asset, strategy) IS-plateau winner into `data/prop_26_5k/`.

Stage B (26 Independent $5,000 Accounts):
  For each of the 26 strategies standalone:
    1. Filters eligible mutual assets strictly on IS (<= 2023-12-31) + $5K 0.01-lot SL feasibility.
    2. Computes causal IS Sortino-Kelly weights across the asset basket.
    3. Optimizes risk_pct, concurrency, same-side caps, and DD throttle strictly on IS.
    4. Freezes config and evaluates Full (2021-2026), Untouched OOS (2024-2026),
       and 20,000-run Prop Challenge Monte Carlo (10% target / 5% daily / 10% max DD).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import gc
import glob
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
from footprint_Chart.backtesting.mt5_per_asset_27strat_optimizer import EXTRA_BUCKETS
from footprint_Chart.backtesting.mt5_shared_account_portfolio_optimizer import (
    TS_2022,
    simulate_shared_account,
)
from footprint_Chart.backtesting.multi_asset_optimizer import (
    SPLIT_2024_TS,
    _fast_s04_masks,
    _fast_s07_masks,
    compute_hac_stats,
)
from footprint_Chart.backtesting.multi_asset_prop_optimizer import _score_is_oos, _sigs_to_arr
from footprint_Chart.backtesting.oi_vwap_sweep_research import (
    attach_causal_oi_vwap_features,
    ensure_full_metrics,
)
from footprint_Chart.backtesting.portfolio_engine import _load_funding_arrays, load_asset_tfs_cached
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from footprint_Chart.data_pipeline.dump_mutual_5k import MUTUAL_SPECS_PATH, snapshot_all_mutual_specs
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import adx_arr, ema_arr, er_arr, rolling_std_arr, sma_arr, wilder_atr
from strategies.delta_divergence import DeltaDivergenceStrategy
from strategies.ema233_imb_rejection import Ema233ImbalanceRejectionStrategy
from strategies.ema_zero_orderflow import EmaZeroOrderFlowStrategy
from strategies.imbalance_stack import ImbalanceStackStrategy
from strategies.of_initiative_breakout import OrderFlowInitiativeBreakoutStrategy
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd
from strategies.vwap_of_reclaim import VwapOrderFlowReclaimStrategy
from strategies.zero_auction_reversal import ZeroAuctionReversalStrategy

PROP_DIR = DATA_DIR / "prop_26_5k"
CFG_DIR = PROP_DIR / "configs"
TRADES_DIR = PROP_DIR / "trades"
ACC_DIR = PROP_DIR / "accounts"
MASTER_JSON = PROP_DIR / "prop_26_5k_master.json"

# Purged/retired standalone $5K prop strategies (failed to sustain >0R OOS after causal upgrades;
# retained in Stage A scorecard & master JSON audit for composite/GLM redesign, purged from accounts/)
RETIRED_STRATEGIES: frozenset[str] = frozenset({
    "bollinger_zscore",
    "cg_signal_strategy",
    "delta_divergence",
    "ema_pullback",
    "of_initiative_breakout",
    "rsi2_of_reversion",
    "s02_rsi2_meanrev",
    "s03_htf_ema_rejection",
    "s05_zscore_breakout",
    "s06_iv_wall_fade",
    "vwap_of_reclaim",
    "vwap_rejection",
})
# Strategies rescued via strict causal 18M trailing -> 6M forward rolling Walk-Forward rotation
WF_STRATEGIES: frozenset[str] = frozenset({
    "s06_iv_wall_fade",
})

ALL_26_STRATEGIES: tuple[str, ...] = (
    "bollinger_zscore",
    "cg_signal_strategy",
    "delta_divergence",
    "donchian_breakout",
    "ema233_imb_rejection",
    "ema_pullback",
    "ema_zero_orderflow",
    "imbalance_stack",
    "of_initiative_breakout",
    "oi_2h_liq_break",
    "oi_2h_reclaim_fade",
    "oi_4h_sqz_mom",
    "rsi2_mean_reversion",
    "rsi2_of_reversion",
    "s01_williams_pullback",
    "s02_rsi2_meanrev",
    "s03_htf_ema_rejection",
    "s04_vc_squeeze_breakout",
    "s05_zscore_breakout",
    "s06_iv_wall_fade",
    "s07_daily_pivot_breakout",
    "s09_donchian_trend_cta",
    "s10_ema_pullback",
    "vwap_of_reclaim",
    "vwap_rejection",
    "zero_auction_reversal",
)


def _optimize_asset_all26(sym: str, mspec: dict[str, Any], chart_map: dict[str, Any], force: bool = False) -> dict[str, Any] | None:
    cfg_p = CFG_DIR / f"{sym}.json"
    tr_p = TRADES_DIR / f"{sym}.parquet"
    if not force and cfg_p.is_file() and tr_p.is_file():
        return json.loads(cfg_p.read_text(encoding="utf-8"))

    agg_p = DATA_DIR / f"{sym}_agg_footprint.parquet"
    if not agg_p.is_file():
        return None

    mt5_sym = mspec["mt5"]
    cat = mspec.get("cat", "CRYPTO")
    live_sp = float(mspec.get("sp_bps", 15.0))
    mt5_cost = round(max(1.5, live_sp) + 2.0, 2)
    min_sl25_usd = float(mspec.get("min_lot_25atr_sl_usd", 0.0))
    sw_l = float(mspec.get("swap_long", -2.5))
    sw_s = float(mspec.get("swap_short", -1.5))

    print(f"[26xAsset] Evaluating 26 strategies on {sym:10s} ({mt5_sym:9s} | sp={live_sp:6.2f}bp | 0.01_2.5ATR=${min_sl25_usd:6.2f})...", flush=True)

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
    is_short = (cat == "COMMODITY") or (ts_start >= SPLIT_2024_TS - 120 * 86400)
    split_ts = int(g4["ts"].iloc[len(g4) // 2]) if is_short else SPLIT_2024_TS
    min_is = 5 if is_short else 8

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
    e233_2 = a2[4]
    up233_2, dn233_2 = c2 > e233_2, c2 < e233_2
    dp2, clv2 = g2["d_pct"].to_numpy(dtype=float), g2["clv"].to_numpy(dtype=float)
    bi2, si2 = g2["buy_imb"].to_numpy(dtype=np.int64), g2["sell_imb"].to_numpy(dtype=np.int64)
    vpin2, vpm2 = fp2["vpin_rank"], fp2["vpm_rk"]
    cvdz2 = g2["cvd_z24"].to_numpy(dtype=float)
    cg_b4 = g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool)
    cg_s4 = g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool)
    cg_b2 = g2["cg_abs_b"].to_numpy(dtype=bool) | g2["cg_exh_b"].to_numpy(dtype=bool) | g2["cg_div_b"].to_numpy(dtype=bool)
    cg_s2 = g2["cg_abs_s"].to_numpy(dtype=bool) | g2["cg_exh_s"].to_numpy(dtype=bool) | g2["cg_div_s"].to_numpy(dtype=bool)

    trials: dict[str, list[dict[str, Any]]] = {}

    def _push(sid, tf_l, mode_l, cfg_s, df_u, arrs, tf_s, sa, sl, tp, tr, be, stp, mh, dyn):
        assert float(tp) >= 2.0, f"{sid} violated min 2.0R target: tp={tp}"
        res = _run(arrs, tf_s, sa, sl, tp, tr, be, stp, mh, dyn)
        sc = _score_is_oos(res[1], res[7], split_ts, min_is, o_fr=res[8])
        trials.setdefault(sid, []).append({
            "sid": sid, "tf": tf_l, "mode": mode_l, "cfg": cfg_s,
            "sl": sl, "tp": tp, "tr": tr, "be": be, "stp": stp, "mh": mh, "dyn": dyn,
            "df": df_u, "res": res, **sc,
        })

    ohlc4 = _fast_ohlc_fallbacks(g4)

    # ---- s01..s10 (all >= 2.0R TP + step_lock/ATR-trailing extension) ----
    wr4 = fp4["wr14"]
    wr4_p = pd.Series(wr4).shift(1).to_numpy(dtype=float)
    for wt in (-80.0, -75.0):
        wl = stkL & (wr4_p <= wt) & (wr4 > wt) & (dp4 > 3.0) & (clv4 > 0.20) & (~has_b4 | (bi4 >= si4))
        ws = stkS & (wr4_p >= (-100.0 - wt)) & (wr4 < (-100.0 - wt)) & (dp4 < -3.0) & (clv4 < -0.20) & (~has_b4 | (si4 >= bi4))
        sa1 = np.where(wl, 1, np.where(ws, -1, 0))
        _push("s01_williams_pullback", "4H", "Footprint", f"wt={wt},sl=2.0,tp=6.0,tr=3.5,be0", g4, a4, 14400, sa1, 2.0, 6.0, 3.5, 0.0, False, 96, True)
        _push("s01_williams_pullback", "4H", "Footprint", f"wt={wt},sl=2.5,tp=8.0,tr=4.0,be1", g4, a4, 14400, sa1, 2.5, 8.0, 4.0, 1.0, True, 96, True)
    _push("s01_williams_pullback", "4H", "OHLC", "ohlc,sl=2.0,tp=3.5,tr=2.5", g4, a4, 14400, ohlc4["s01"], 2.0, 3.5, 2.5, 1.0, True, 96, True)

    r2_4 = fp4["rsi2"]
    ofb4 = cg_b4 | ((dp4 > 5.0) & (clv4 > 0.25))
    ofs4 = cg_s4 | ((dp4 < -5.0) & (clv4 < -0.25))
    # --- CAUSAL UPGRADE s02_rsi2_meanrev: enforce ADX<=24 range gate across all variants (Phase B rescue) ---
    for rl, vc in ((15.0, 0.65), (15.0, 0.72), (20.0, 0.72), (15.0, 0.80)):
        sa2 = np.where((vpin4 < vc) & (r2_4 < rl) & up233 & ofb4, 1, np.where((vpin4 < vc) & (r2_4 > (100.0 - rl)) & dn233 & ofs4, -1, 0))
        sa2_adx = np.where((vpin4 < vc) & (r2_4 < rl) & up233 & ofb4 & (adx4 <= 24.0), 1,
                           np.where((vpin4 < vc) & (r2_4 > (100.0 - rl)) & dn233 & ofs4 & (adx4 <= 24.0), -1, 0))
        _push("s02_rsi2_meanrev", "4H", "FP+ADX24", f"rsi={rl},vp={vc},adx<=24,sl=2.5,tp=4.5", g4, a4, 14400, sa2_adx, 2.5, 4.5, 0.0, 1.0, True, 48, True)
        _push("s02_rsi2_meanrev", "4H", "FP+ADX24", f"rsi={rl},vp={vc},adx<=24,sl=3.0,tp=5.0,tr=3.0", g4, a4, 14400, sa2_adx, 3.0, 5.0, 3.0, 1.0, True, 64, True)
        # RETIRED standalone: rsi2_of_reversion kept in Stage A telemetry only
        if vc in (0.65, 0.80):
            _push("rsi2_of_reversion", "4H", "Direct G4", f"rsi={rl},vp={vc},sl=2.5,tp=4.0,tr=2.5", g4, a4, 14400, sa2, 2.5, 4.0, 2.5, 1.0, True, 48, True)
    _push("s02_rsi2_meanrev", "4H", "OHLC+ADX24", "ohlc,adx<=24,sl=2.0,tp=4.0", g4, a4, 14400, np.where(adx4 <= 24.0, ohlc4["s02"], 0), 2.0, 4.0, 0.0, 1.0, True, 48, False)

    # RETIRED: s03_htf_ema_rejection — all static & WF variants failed OOS (-4.39R to -7.02R)
    # Kept as ensemble signal only (no standalone $5K account)
    e233_2s, c2s = pd.Series(e233_2).shift(2).to_numpy(dtype=float), pd.Series(c2).shift(2).to_numpy(dtype=float)
    t2L = (l2 <= e233_2 + 0.3 * a2[2]) & (c2 > e233_2) & (c2s > e233_2s)
    t2S = (h2 >= e233_2 - 0.3 * a2[2]) & (c2 < e233_2) & (c2s < e233_2s)
    for im, dg in ((1, 0.0), (1, 3.0), (2, 0.0)):
        sa3 = np.where((bi2 >= im) & (clv2 > 0.25) & t2L & (dp2 > dg), 1, np.where((si2 >= im) & (clv2 < -0.25) & t2S & (dp2 < -dg), -1, 0))
        _push("s03_htf_ema_rejection", "2H", "Footprint", f"imb={im},dp>={dg},sl=2.5,tp=3.5", g2, a2, 7200, sa3, 2.5, 3.5, 0.0, 1.0, True, 72, False)
        _push("s03_htf_ema_rejection", "2H", "Footprint", f"imb={im},dp>={dg},sl=3.0,tp=5.0,tr=3.0", g2, a2, 7200, sa3, 3.0, 5.0, 3.0, 1.2, True, 72, True)
    _push("s03_htf_ema_rejection", "4H", "OHLC", "ohlc,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s03"], 2.0, 3.5, 0.0, 1.0, True, 72, False)

    for vpm_m in (0.30, 0.45):
        sqL, sqS = _fast_s04_masks(g4, fp4, {"fp_min_vpm": vpm_m, "fp_min_vr4": 0.90})
        sa4 = np.where(sqL, 1, np.where(sqS, -1, 0))
        _push("s04_vc_squeeze_breakout", "4H", "Footprint", f"vpm={vpm_m},sl=2.0,tp=6.0,tr=3.5,be0", g4, a4, 14400, sa4, 2.0, 6.0, 3.5, 0.0, False, 96, True)
        _push("s04_vc_squeeze_breakout", "4H", "Footprint", f"vpm={vpm_m},sl=2.5,tp=8.0,tr=4.0,be1", g4, a4, 14400, sa4, 2.5, 8.0, 4.0, 1.0, True, 96, True)
    _push("s04_vc_squeeze_breakout", "4H", "OHLC", "ohlc,sl=2.0,tp=4.0,tr=2.5", g4, a4, 14400, ohlc4["s04"], 2.0, 4.0, 2.5, 1.0, True, 96, True)

    # --- CAUSAL UPGRADE s05_zscore_breakout: ADX>=20 + be=1.5 across lb=24/50 (Phase B rescue) ---
    mu50 = sma_arr(c4, 50)
    rsd50 = rolling_std_arr(c4, 50)
    sd50 = np.where(rsd50 > 1e-12, rsd50, np.nan)
    zv50 = (c4 - mu50) / sd50
    zvp50 = pd.Series(zv50).shift(1).to_numpy(dtype=float)
    atr_exp = atr4_14 >= pd.Series(atr4_14).rolling(20).mean().to_numpy(dtype=float)
    zl5 = (zvp50 < 1.5) & (zv50 >= 1.5) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 20.0) & atr_exp
    zs5 = (zvp50 > -1.5) & (zv50 <= -1.5) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 20.0) & atr_exp
    _push("s05_zscore_breakout", "4H", "OHLC+ADX20", "ohlc,lb=50,z=1.5,adx>=20,sl=2.5,tp=6.0,tr=3.5,be1.5", g4, a4, 14400, np.where(zl5, 1, np.where(zs5, -1, 0)), 2.5, 6.0, 3.5, 1.5, True, 96, True)
    _push("s05_zscore_breakout", "4H", "FP+ADX20", "fp,lb=50,z=1.5,adx>=20,sl=2.5,tp=6.0,tr=3.5,be1.5", g4, a4, 14400, np.where(zl5 & (dp4 > 0) & (clv4 > 0.15), 1, np.where(zs5 & (dp4 < 0) & (clv4 < -0.15), -1, 0)), 2.5, 6.0, 3.5, 1.5, True, 96, True)
    mu24 = sma_arr(c4, 24)
    rsd24 = rolling_std_arr(c4, 24)
    sd24 = np.where(rsd24 > 1e-12, rsd24, np.nan)
    zv24 = (c4 - mu24) / sd24
    zvp24 = pd.Series(zv24).shift(1).to_numpy(dtype=float)
    zl5_v2 = (zvp24 < 2.0) & (zv24 >= 2.0) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 20.0) & atr_exp
    zs5_v2 = (zvp24 > -2.0) & (zv24 <= -2.0) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 20.0) & atr_exp
    _push("s05_zscore_breakout", "4H", "OHLC+ADX20", "ohlc,lb=24,z=2.0,adx>=20,sl=2.5,tp=6.0,tr=3.5,be1.5", g4, a4, 14400,
          np.where(zl5_v2, 1, np.where(zs5_v2, -1, 0)), 2.5, 6.0, 3.5, 1.5, True, 96, True)
    _push("s05_zscore_breakout", "4H", "FP+ADX20", "fp,lb=24,z=2.0,adx>=20,sl=2.5,tp=6.0,tr=3.5,be1.5", g4, a4, 14400,
          np.where(zl5_v2 & (dp4 > 0) & (clv4 > 0.15), 1, np.where(zs5_v2 & (dp4 < 0) & (clv4 < -0.15), -1, 0)), 2.5, 6.0, 3.5, 1.5, True, 96, True)

    # --- CAUSAL UPGRADE s06_iv_wall_fade: 2H TrendPull + CVD + EMA233 (Phase C/D rescue via rolling WF) ---
    vwz2_arr = g2["vwz48"].to_numpy(dtype=float) if "vwz48" in g2.columns else np.zeros(len(g2), dtype=float)
    for zt_v2 in (1.75, 2.25):
        fl6_2h = (vpin2 < 0.72) & (vwz2_arr < -zt_v2) & (cvdz2 > 0.25) & up233_2
        fs6_2h = (vpin2 < 0.72) & (vwz2_arr > zt_v2) & (cvdz2 < -0.25) & dn233_2
        sa6_2h = np.where(fl6_2h, 1, np.where(fs6_2h, -1, 0))
        _push("s06_iv_wall_fade", "2H", "TrendPull+CVD", f"2H_vwz={zt_v2},cvd,sl=2.5,tp=6.0,tr=3.5", g2, a2, 7200, sa6_2h, 2.5, 6.0, 3.5, 1.0, True, 72, True)
        _push("s06_iv_wall_fade", "2H", "TrendPull+CVD", f"2H_vwz={zt_v2},cvd,sl=2.5,tp=6.0,tr=3.5,be1.5", g2, a2, 7200, sa6_2h, 2.5, 6.0, 3.5, 1.5, True, 72, True)

    for sk in (2, 3):
        pL, pS = _fast_s07_masks(g4, fp4, {"swing_k": sk})
        sf7 = np.where(pL, 1, np.where(pS, -1, 0))
        _push("s07_daily_pivot_breakout", "4H", "Footprint", f"fp,k={sk},sl=2.0,tp=8.0,tr=3.0", g4, a4, 14400, sf7, 2.0, 8.0, 3.0, 1.0, True, 96, True)
        _push("s07_daily_pivot_breakout", "4H", "FP+CVD", f"cvd,k={sk},sl=2.5,tp=8.0,tr=3.5", g4, a4, 14400, np.where((sf7 == 1) & (cvdz4 > 0.5), 1, np.where((sf7 == -1) & (cvdz4 < -0.5), -1, 0)), 2.5, 8.0, 3.5, 1.0, True, 96, True)
        _push("s07_daily_pivot_breakout", "4H", "FP+CVD", f"cvd,k={sk},sl=2.5,tp=8.0,tr=3.5,be0", g4, a4, 14400, np.where((sf7 == 1) & (cvdz4 > 0.5), 1, np.where((sf7 == -1) & (cvdz4 < -0.5), -1, 0)), 2.5, 8.0, 3.5, 0.0, False, 96, True)
    _push("s07_daily_pivot_breakout", "4H", "OHLC", "ohlc,k=2,sl=2.0,tp=3.0", g4, a4, 14400, ohlc4["s07"], 2.0, 3.0, 0.0, 1.0, False, 96, False)

    for dn in (16, 22):
        hi_d, lo_d = pd.Series(h4).shift(1).rolling(dn).max().to_numpy(), pd.Series(l4).shift(1).rolling(dn).min().to_numpy()
        ml9 = (c4 > hi_d) & up233 & (clv4 > 0.25) & (dp4 > 0.0) & (~has_b4 | (bi4 >= si4))
        ms9 = (c4 < lo_d) & dn233 & (clv4 < -0.25) & (dp4 < 0.0) & (~has_b4 | (si4 >= bi4))
        sa9 = np.where(ml9, 1, np.where(ms9, -1, 0))
        _push("s09_donchian_trend_cta", "4H", "Footprint", f"dn={dn},sl=2.0,tp=8.0,tr=3.5,be0", g4, a4, 14400, sa9, 2.0, 8.0, 3.5, 0.0, False, 96, True)
        _push("s09_donchian_trend_cta", "4H", "Footprint", f"dn={dn},sl=2.5,tp=8.0,tr=4.0,be1", g4, a4, 14400, sa9, 2.5, 8.0, 4.0, 1.0, True, 96, True)
    _push("s09_donchian_trend_cta", "4H", "OHLC", "ohlc,dn=20,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s09"], 2.0, 3.5, 0.0, 1.0, False, 96, False)

    for dpm in (2.0, 5.0):
        tl10 = stkL & (l4 <= e89_4 + 0.30 * a4[2]) & (c4 > e89_4) & (dp4 > dpm) & (clv4 > 0.20)
        ts10 = stkS & (h4 >= e89_4 - 0.30 * a4[2]) & (c4 < e89_4) & (dp4 < -dpm) & (clv4 < -0.20)
        sa10 = np.where(tl10, 1, np.where(ts10, -1, 0))
        _push("s10_ema_pullback", "4H", "Footprint", f"dp={dpm},sl=2.0,tp=8.0,tr=3.5,be0", g4, a4, 14400, sa10, 2.0, 8.0, 3.5, 0.0, False, 96, True)
        _push("s10_ema_pullback", "4H", "Footprint", f"dp={dpm},sl=2.5,tp=8.0,tr=4.0,be1", g4, a4, 14400, sa10, 2.5, 8.0, 4.0, 1.0, True, 96, True)
    _push("s10_ema_pullback", "4H", "OHLC", "ohlc,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s10"], 2.0, 3.5, 0.0, 1.0, False, 96, False)

    # ---- Footprint plugins + Order-Flow Steroids (all >= 2.0R + trailing) ----
    dd_cls, imb_cls = DeltaDivergenceStrategy(), ImbalanceStackStrategy()
    zr_cls, e233_cls = ZeroAuctionReversalStrategy(), Ema233ImbalanceRejectionStrategy()
    ez_cls, of_cls, vw_cls = EmaZeroOrderFlowStrategy(), OrderFlowInitiativeBreakoutStrategy(), VwapOrderFlowReclaimStrategy()

    # Pre-compute Efficiency Ratio & ADX for both 4H and 2H breakout/range gates
    er4_25 = np.abs(c4 - pd.Series(c4).shift(25).to_numpy(dtype=float)) / (pd.Series(np.abs(c4 - pd.Series(c4).shift(1).to_numpy(dtype=float))).rolling(25).sum().to_numpy(dtype=float) + 1e-12)
    er2_25 = np.abs(c2 - pd.Series(c2).shift(25).to_numpy(dtype=float)) / (pd.Series(np.abs(c2 - pd.Series(c2).shift(1).to_numpy(dtype=float))).rolling(25).sum().to_numpy(dtype=float) + 1e-12)
    adx2 = adx_arr(h2, l2, c2, 14)

    for tf_l, df_u, arrs, tf_s, b_u, up_u, dn_u, cgb_u, cgs_u, vp_u, vpm_u, adx_u, cvd_u, dp_u, clv_u in (
        ("4H", g4, a4, 14400, bars4, up233, dn233, cg_b4, cg_s4, vpin4, vpm4, adx4, cvdz4, dp4, clv4),
        ("2H", g2, a2, 7200, bars2, up233_2, dn233_2, cg_b2, cg_s2, vpin2, vpm2, adx2, cvdz2, dp2, clv2),
    ):
        # --- CAUSAL UPGRADE cg_signal_strategy: AllCG (abs+exh+div) + CVD/VPIN + EMA233 (Phase C rescue; remove un-gated Hybrid FP) ---
        sa_cgb = cgb_u & up_u & (dp_u > 0.0)
        sa_cgs = cgs_u & dn_u & (dp_u < 0.0)
        _push("cg_signal_strategy", tf_l, "OF-Steroid", "cg+vp0.72+clv,sl=2.5,tp=6.0,tr=3.5,be1.5", df_u, arrs, tf_s, np.where(sa_cgb & (vp_u < 0.72) & (clv_u > 0.1), 1, np.where(sa_cgs & (vp_u < 0.72) & (clv_u < -0.1), -1, 0)), 2.5, 6.0, 3.5, 1.5, True, 96, True)
        sa_cgb_cvd = cgb_u & up_u & (cvd_u > 0.25)
        sa_cgs_cvd = cgs_u & dn_u & (cvd_u < -0.25)
        _push("cg_signal_strategy", tf_l, "AllCG+CVD+EMA233", "cg+cvd0.25+ema233,sl=2.5,tp=6.0,tr=3.5,be1.5", df_u, arrs, tf_s,
              np.where(sa_cgb_cvd, 1, np.where(sa_cgs_cvd, -1, 0)), 2.5, 6.0, 3.5, 1.5, True, 96, True)
        _push("cg_signal_strategy", tf_l, "AllCG+CVD+VPIN", "cg+cvd0.25+vp0.72,sl=2.5,tp=6.0,tr=3.5,be1.5", df_u, arrs, tf_s,
              np.where(sa_cgb_cvd & (vp_u < 0.72), 1, np.where(sa_cgs_cvd & (vp_u < 0.72), -1, 0)), 2.5, 6.0, 3.5, 1.5, True, 96, True)

        # --- CAUSAL UPGRADE delta_divergence: VWZ48 extreme + EMA233 + VPIN/be=1.5 (Phase B rescue) ---
        vwz_u = df_u["vwz48"].to_numpy(dtype=float) if "vwz48" in df_u.columns else np.zeros(len(df_u), dtype=float)
        for mdp in (5.0, 8.0, 10.0):
            sa_dd = _sigs_to_arr(len(df_u), dd_cls.generate_signals(b_u, {"min_abs_delta_pct": mdp}))
            sa_dd_tr = np.where(((sa_dd == 1) & up_u) | ((sa_dd == -1) & dn_u), sa_dd, 0)
            sa_dd_vwz = np.where(((sa_dd_tr == 1) & (vwz_u <= -1.25)) | ((sa_dd_tr == -1) & (vwz_u >= 1.25)), sa_dd_tr, 0)
            _push("delta_divergence", tf_l, "VWZ+EMA233", f"dp>={mdp},vwz1.25,ema233,sl=2.5,tp=6.0,tr=3.0,be1.5", df_u, arrs, tf_s, sa_dd_vwz, 2.5, 6.0, 3.0, 1.5, True, 72, True)
            _push("delta_divergence", tf_l, "OF-Steroid+be15", f"dp>={mdp},vp0.72,sl=2.5,tp=6.0,tr=3.0,be1.5", df_u, arrs, tf_s, np.where((sa_dd_tr != 0) & (vp_u < 0.72), sa_dd_tr, 0), 2.5, 6.0, 3.0, 1.5, True, 72, True)

        for mi in (2, 3):
            sa_imb = _sigs_to_arr(len(df_u), imb_cls.generate_signals(b_u, {"min_imbalances": mi}))
            sa_imb_tr = np.where(((sa_imb == 1) & up_u) | ((sa_imb == -1) & dn_u), sa_imb, 0)
            _push("imbalance_stack", tf_l, "FP+EMA233", f"imb>={mi},ema233,sl=2.5,tp=6.0,tr=3.5", df_u, arrs, tf_s, sa_imb_tr, 2.5, 6.0, 3.5, 1.0, True, 72, True)
            _push("imbalance_stack", tf_l, "OF-Steroid", f"imb>={mi},vp0.75+dp2,sl=2.5,tp=4.0,tr=2.5", df_u, arrs, tf_s, np.where((sa_imb_tr == 1) & (vp_u < 0.75) & (dp_u > 2.0), 1, np.where((sa_imb_tr == -1) & (vp_u < 0.75) & (dp_u < -2.0), -1, 0)), 2.5, 4.0, 2.5, 1.0, True, 72, True)

        for rdc in (0, 1):
            sa_zr = _sigs_to_arr(len(df_u), zr_cls.generate_signals(b_u, {"require_delta_confirm": rdc, "min_extreme_val": 1.0}))
            sa_zr_tr = np.where(((sa_zr == 1) & up_u) | ((sa_zr == -1) & dn_u), sa_zr, 0)
            _push("zero_auction_reversal", tf_l, "FP+EMA233", f"reqd={rdc},ema233,sl=2.5,tp=6.0,tr=3.5", df_u, arrs, tf_s, sa_zr_tr, 2.5, 6.0, 3.5, 1.0, True, 72, True)
            _push("zero_auction_reversal", tf_l, "OF-Steroid", f"reqd={rdc},vp0.75+clv,sl=2.5,tp=4.0", df_u, arrs, tf_s, np.where((sa_zr_tr == 1) & (vp_u < 0.75) & (clv_u > 0.15), 1, np.where((sa_zr_tr == -1) & (vp_u < 0.75) & (clv_u < -0.15), -1, 0)), 2.5, 4.0, 0.0, 1.0, True, 72, False)
        # --- CAUSAL UPGRADE zero_auction_reversal: ADX<=24 + VPM>=0.60 range gate + fixed tp=4.5 (Phase B rescue) ---
        for rdc in (0, 1):
            sa_zr_raw = _sigs_to_arr(len(df_u), zr_cls.generate_signals(b_u, {"require_delta_confirm": rdc, "min_extreme_val": 1.0}))
            sa_zr_adx = np.where(((sa_zr_raw == 1) & up_u & (adx_u <= 24.0)) | ((sa_zr_raw == -1) & dn_u & (adx_u <= 24.0)), sa_zr_raw, 0)
            sa_zr_vpm = np.where((sa_zr_adx != 0) & (vpm_u >= 0.60), sa_zr_adx, 0)
            _push("zero_auction_reversal", tf_l, "FP+ADX24", f"reqd={rdc},adx<=24,ema233,sl=2.5,tp=4.5", df_u, arrs, tf_s, sa_zr_adx, 2.5, 4.5, 0.0, 1.0, True, 72, False)
            _push("zero_auction_reversal", tf_l, "FP+ADX24+VPM60", f"reqd={rdc},adx<=24,vpm60,sl=2.5,tp=4.5", df_u, arrs, tf_s, sa_zr_vpm, 2.5, 4.5, 0.0, 1.0, True, 72, False)

        for im2, cl2 in ((1, 0.20), (2, 0.25)):
            sa_e2 = _sigs_to_arr(len(df_u), e233_cls.generate_signals(b_u, {"min_imb": im2, "min_clv": cl2}))
            _push("ema233_imb_rejection", tf_l, "Hybrid FP", f"imb={im2},clv={cl2},sl=2.5,tp=4.0", df_u, arrs, tf_s, sa_e2, 2.5, 4.0, 0.0, 1.0, True, 72, False)
            _push("ema233_imb_rejection", tf_l, "OF-Steroid", f"imb={im2},vp0.75,sl=2.5,tp=6.0,tr=3.5", df_u, arrs, tf_s, np.where((sa_e2 != 0) & (vp_u < 0.75), sa_e2, 0), 2.5, 6.0, 3.5, 1.0, True, 72, True)

        # --- CAUSAL UPGRADE ema_zero_orderflow: Touch-Only (remove noisy Zero-Trend) + CVD + ADX>=20 + be=1.5 (TIER_A rescue) ---
        for dpe in (2.0, 3.0, 5.0):
            sa_touch_d = _sigs_to_arr(len(df_u), ez_cls.generate_signals(b_u, {"min_delta_pct": dpe, "enable_ema_touch": 1, "enable_zero_trend": 0}))
            _push("ema_zero_orderflow", tf_l, "TouchOnly", f"touch,dp={dpe},sl=2.5,tp=6.0,tr=4.0,be1.5", df_u, arrs, tf_s, sa_touch_d, 2.5, 6.0, 4.0, 1.5, True, 96, True)
        sa_touch = _sigs_to_arr(len(df_u), ez_cls.generate_signals(b_u, {"min_delta_pct": 2.0, "enable_ema_touch": 1, "enable_zero_trend": 0}))
        sa_touch_cvd = np.where(((sa_touch == 1) & (cvd_u > 0.25) & (adx_u >= 20.0)) | ((sa_touch == -1) & (cvd_u < -0.25) & (adx_u >= 20.0)), sa_touch, 0)
        _push("ema_zero_orderflow", tf_l, "Touch+CVD+ADX20", "touch_cvd_adx20,sl=2.5,tp=6.0,tr=4.0,be1.5", df_u, arrs, tf_s, sa_touch_cvd, 2.5, 6.0, 4.0, 1.5, True, 96, True)
        _push("ema_zero_orderflow", tf_l, "Touch+CVD+ADX20", "touch_cvd_adx20,sl=2.5,tp=8.0,tr=4.5,be1.5", df_u, arrs, tf_s, sa_touch_cvd, 2.5, 8.0, 4.5, 1.5, True, 96, True)
        sa_touch_cvd_vp = np.where(((sa_touch == 1) & (cvd_u > 0.25) & (adx_u >= 20.0) & (vp_u < 0.72)) |
                                    ((sa_touch == -1) & (cvd_u < -0.25) & (adx_u >= 20.0) & (vp_u < 0.72)), sa_touch, 0)
        _push("ema_zero_orderflow", tf_l, "Touch+CVD+ADX+VP", "touch_cvd_adx20_vp72,sl=2.5,tp=6.0,tr=4.0,be1.5", df_u, arrs, tf_s, sa_touch_cvd_vp, 2.5, 6.0, 4.0, 1.5, True, 96, True)

        for lb in (16, 22):
            for dpo in (0.0, 2.0):
                sa_of = _sigs_to_arr(len(df_u), of_cls.generate_signals(b_u, {"lookback": lb, "min_delta_pct": dpo, "min_clv": 0.25, "require_imb_dom": 1}))
                _push("of_initiative_breakout", tf_l, "Hybrid FP", f"lb={lb},dp={dpo},sl=2.5,tp=6.0,tr=3.5,be0", df_u, arrs, tf_s, sa_of, 2.5, 6.0, 3.5, 0.0, False, 96, True)
                _push("of_initiative_breakout", tf_l, "Hybrid FP", f"lb={lb},dp={dpo},sl=2.5,tp=8.0,tr=4.0,be1", df_u, arrs, tf_s, sa_of, 2.5, 8.0, 4.0, 1.0, True, 96, True)

        for vwl in (24, 48):
            for zth in (1.5, 2.0):
                sa_vw = _sigs_to_arr(len(df_u), vw_cls.generate_signals(b_u, {"vwap_len": vwl, "z_thresh": zth}))
                _push("vwap_of_reclaim", tf_l, "Hybrid FP", f"vw={vwl},zt={zth},sl=2.5,tp=4.5", df_u, arrs, tf_s, sa_vw, 2.5, 4.5, 0.0, 1.2, True, 48, False)
                sa_vw_st = np.where(((sa_vw == 1) & up_u & (vp_u < 0.75)) | ((sa_vw == -1) & dn_u & (vp_u < 0.75)), sa_vw, 0)
                _push("vwap_of_reclaim", tf_l, "OF-Steroid", f"vw={vwl},zt={zth},tr+vp,sl=2.5,tp=6.0,tr=3.5", df_u, arrs, tf_s, sa_vw_st, 2.5, 6.0, 3.5, 1.0, True, 72, True)

    # ---- OI + 2D/4D VWAP Strategies (with >=2R TP + trailing + FP steroid) ----
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
                _push("oi_4h_sqz_mom", "4H", "OI+VolPump", f"4H_C1_Long,oi={zt_oi},v={zt_v},tp=6.0", g4, a4, 14400, sa_c1_long, 2.5, 6.0, 3.5, 1.0, True, 72, True)
                _push("oi_4h_sqz_mom", "4H", "OI+VolPump", f"4H_C1_Both,oi={zt_oi},v={zt_v},tp=6.0", g4, a4, 14400, sa_c1_both, 2.5, 6.0, 3.5, 1.0, True, 72, True)
                _push("oi_4h_sqz_mom", "4H", "OI+OF-Steroid", f"4H_C1_FP,oi={zt_oi},vp0.75,tp=6.0", g4, a4, 14400, np.where((sa_c1_both != 0) & (vpin4 < 0.75), sa_c1_both, 0), 2.5, 6.0, 3.5, 1.0, True, 72, True)

            htf_r = pd.DataFrame({"end_ts": g4["end_ts"].to_numpy(np.int64), "up233": up233, "dn233": dn233}).sort_values("end_ts")
            m_htf = pd.merge_asof(g2[["end_ts"]].sort_values("end_ts"), htf_r, on="end_ts", direction="backward")
            u2h, d2h = m_htf["up233"].fillna(False).to_numpy(bool), m_htf["dn233"].fillna(False).to_numpy(bool)
            zdoi2, zint2, zvol2 = g2_oi["z_doi"].to_numpy(float), g2_oi["z_intrabar_drop"].to_numpy(float), g2_oi["z_vol"].to_numpy(float)
            vwl2, vwh2 = g2_oi["vwz2d_low"].to_numpy(float) <= -2.0, g2_oi["vwz2d_high"].to_numpy(float) >= 2.0
            for zt_oi, zt_v, sl_m, tr_m in ((1.5, 1.5, 2.5, 3.5), (1.25, 1.25, 2.5, 3.5), (1.5, 1.5, 3.0, 4.0)):
                purge2 = (zdoi2 <= -zt_oi) | (zint2 <= -(zt_oi + 0.3))
                sa_b1 = np.where(valid2 & purge2 & (zvol2 >= zt_v) & vwl2 & d2h & (clv2 < -0.2), -1, np.where(valid2 & purge2 & (zvol2 >= zt_v) & vwh2 & u2h & (clv2 > 0.2), 1, 0))
                _push("oi_2h_liq_break", "2H", "OI+VolPump", f"2H_B1,oi={zt_oi},v={zt_v},sl={sl_m},tp=6.0", g2, a2, 7200, sa_b1, sl_m, 6.0, tr_m, 1.0, True, 72, True)

            purge2_def = (zdoi2 <= -1.5) | (zint2 <= -1.8)
            v48_Lp = pd.Series((g2_oi["vwz48"].to_numpy(float) <= -2.0) & (zdoi2 <= -1.5)).shift(1).fillna(False).to_numpy(bool) & (c2 > g2["o"].to_numpy(float))
            v48_Sp = pd.Series((g2_oi["vwz48"].to_numpy(float) >= 2.0) & (zdoi2 <= -1.5)).shift(1).fillna(False).to_numpy(bool) & (c2 < g2["o"].to_numpy(float))
            sa_b3 = np.where(valid2 & v48_Lp & u2h, 1, np.where(valid2 & v48_Sp & d2h, -1, 0))
            _push("oi_2h_reclaim_fade", "2H", "OI+Reclaim", "2H_B3_4DVWAP_Reclaim,tp=6.0", g2, a2, 7200, sa_b3, 3.0, 6.0, 4.0, 1.25, True, 96, True)

            swS_p = pd.Series(vwh2 & purge2_def & (zvol2 >= 1.5)).shift(1).fillna(False).to_numpy(bool)
            sa_a2 = np.where(valid2 & swS_p & (c2 < g2["o"].to_numpy(float)) & d2h, -1, 0)
            _push("oi_2h_reclaim_fade", "2H", "OI+Reclaim", "2H_A2_ShortSqzFade_DN233,tp=6.0", g2, a2, 7200, sa_a2, 3.0, 6.0, 4.0, 1.25, True, 96, True)
    except Exception as oi_exc:
        print(f"  [oi_warn] {sym}: {oi_exc}", flush=True)

    # ---- 5 Charts Plugins + Order-Flow Steroids (all >= 2.0R + trailing SL) ----
    c_grid = {
        "bollinger_zscore": ("bb_zscore", [{"window": 20, "z_thresh": 2.0}, {"window": 50, "z_thresh": 1.5}]),
        "donchian_breakout": ("donchian_cta", [{"lookback": 16}, {"lookback": 22}, {"lookback": 30}]),
        "ema_pullback": ("ema_pullback", [
            {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 48.0, "rsi_sell_min": 52.0},
            {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 60.0, "rsi_sell_min": 40.0},
        ]),
        "rsi2_mean_reversion": ("rsi2_meanrev", [{"oversold": 15.0, "overbought": 85.0}, {"oversold": 20.0, "overbought": 80.0}]),
        "vwap_rejection": ("vwap_reclaim", [{"min_dist_atr": 0.3}, {"min_dist_atr": 0.6}]),
    }
    for sid, (cid, plist) in c_grid.items():
        plug = chart_map[cid]
        for tf_l, df_u, arrs, tf_s, cdf_u, up_u, dn_u, vp_u, cvd_u, dp_u, clv_u, cgb_u, cgs_u, adx_u, er_u in (
            ("4H", g4, a4, 14400, cdf4, up233, dn233, vpin4, cvdz4, dp4, clv4, cg_b4, cg_s4, adx4, er4_25),
            ("2H", g2, a2, 7200, cdf2, up233_2, dn233_2, vpin2, cvdz2, dp2, clv2, cg_b2, cg_s2, adx2, er2_25),
        ):
            for prm in plist:
                sa = _sigs_to_arr(len(df_u), plug.generate_signals(cdf_u, prm))
                _push(sid, tf_l, "Pure OHLC", f"ohlc,{prm},sl=2.5,tp=4.5,tr=3.0", df_u, arrs, tf_s, sa, 2.5, 4.5, 3.0, 1.0, True, 96, True)
                sa_tr = np.where(((sa == 1) & up_u) | ((sa == -1) & dn_u), sa, 0)
                if sid == "donchian_breakout":
                    # --- CAUSAL UPGRADE donchian_breakout: ADX>=20 + ER>=0.25 + pure runner be=0.0, tp=8.0 (Phase B rescue) ---
                    sa_d_adx = np.where((sa_tr != 0) & (adx_u >= 20.0), sa_tr, 0)
                    sa_d_er = np.where((sa_tr != 0) & (adx_u >= 20.0) & (er_u >= 0.25), sa_tr, 0)
                    _push(sid, tf_l, "ADX20+be0", f"adx20,{prm},sl=2.5,tp=8.0,tr=3.5,be0", df_u, arrs, tf_s, sa_d_adx, 2.5, 8.0, 3.5, 0.0, False, 96, True)
                    _push(sid, tf_l, "ADX20+ER25+be0", f"adx20_er25,{prm},sl=2.5,tp=8.0,tr=3.5,be0", df_u, arrs, tf_s, sa_d_er, 2.5, 8.0, 3.5, 0.0, False, 96, True)
                    _push(sid, tf_l, "ADX20+ER25+tr4", f"adx20_er25,{prm},sl=2.5,tp=8.0,tr=4.0,be0", df_u, arrs, tf_s, sa_d_er, 2.5, 8.0, 4.0, 0.0, False, 96, True)
                if sid in ("donchian_breakout", "ema_pullback"):
                    _push(sid, tf_l, "OHLC+EMA233", f"ema233,{prm},sl=2.5,tp=8.0,tr=3.5,be0", df_u, arrs, tf_s, sa_tr, 2.5, 8.0, 3.5, 0.0, False, 96, True)
                _push(sid, tf_l, "OHLC+EMA233", f"ema233,{prm},sl=2.5,tp=6.0,tr=3.5", df_u, arrs, tf_s, sa_tr, 2.5, 6.0, 3.5, 1.0, True, 96, True)
                if sid != "bollinger_zscore":
                    _push(sid, tf_l, "OHLC+EMA233+be15", f"ema233,{prm},sl=2.5,tp=6.0,tr=3.5,be1.5", df_u, arrs, tf_s, sa_tr, 2.5, 6.0, 3.5, 1.5, True, 96, True)
                of_pos = (vp_u < 0.72) & ((dp_u > 1.5) | (cvd_u > 0.25) | cgb_u | (clv_u > 0.20))
                of_neg = (vp_u < 0.72) & ((dp_u < -1.5) | (cvd_u < -0.25) | cgs_u | (clv_u < -0.20))
                sa_st = np.where((sa_tr == 1) & of_pos, 1, np.where((sa_tr == -1) & of_neg, -1, 0))
                _push(sid, tf_l, "OF-Steroid", f"steroid,{prm},sl=2.5,tp=4.0,tr=2.5", df_u, arrs, tf_s, sa_st, 2.5, 4.0, 2.5, 1.0, True, 72, True)
                _push(sid, tf_l, "OF-Steroid", f"steroid,{prm},sl=2.5,tp=6.0,tr=3.5", df_u, arrs, tf_s, sa_st, 2.5, 6.0, 3.5, 1.0, True, 96, True)

    ca4 = g4["c"].to_numpy(float)
    ea4 = g4["end_ts"].to_numpy(np.int64)
    imba4 = bi4 - si4
    sum_imba4 = bi4 + si4

    # Keep EVERY strategy's best causal IS plateau config (top-peer plateau avoids noisy baseline dilution)
    strat_catalog: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for sid, tlist in sorted(trials.items()):
        sorted_raw = sorted([t["raw_score"] for t in tlist], reverse=True)
        k_peer = max(2, len(sorted_raw) // 3)
        top_peer_mean = float(np.mean(sorted_raw[:k_peer])) if sorted_raw else 0.0
        for t in tlist:
            t["plateau"] = 0.80 * t["raw_score"] + 0.20 * max(-1.0, top_peer_mean)
        best = max(tlist, key=lambda x: x["plateau"])
        bucket = ALPHA_BUCKETS.get(sid, EXTRA_BUCKETS.get(sid, "OTHER"))
        sl_min_usd = round((best["sl"] / 2.5) * min_sl25_usd, 2) if min_sl25_usd > 0 else 0.0
        fits_5k = (sl_min_usd <= 50.0)
        avg_is_r = float(best["isr"]) / max(1, int(best["nis"]))
        is_ok = (best["nis"] >= min_is and best["ispf"] >= 1.15 and best["isr"] > 0.0 and avg_is_r >= 0.12)

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
        all_rows.extend(trs)

        r_is_arr = np.array([float(x["r"]) for x in trs if int(x["ts_in"]) < split_ts], dtype=float)
        down_var = float(np.mean(np.minimum(r_is_arr, 0.0) ** 2)) if len(r_is_arr) > 1 else 1.0
        mean_r = float(r_is_arr.mean()) if len(r_is_arr) else 0.0
        is_kelly = max(0.05, (mean_r / max(down_var, 0.15)) * math.sqrt(max(1.0, best["ispf"]))) if best["nis"] >= 5 else 0.05

        strat_catalog.append({
            "sid": sid, "bucket": bucket, "retired": (sid in RETIRED_STRATEGIES),
            "tf": best["tf"], "mode": best["mode"], "cfg": best["cfg"],
            "sl": best["sl"], "tp": best["tp"], "tr": best["tr"], "be": best["be"],
            "step_lock": best["stp"], "mh": best["mh"], "dyn_atr": best["dyn"],
            "min_lot_sl_usd": sl_min_usd, "fits_5k_1pct": fits_5k,
            "is_ok": is_ok, "is_kelly": round(is_kelly, 4),
            "avg_is_r": best.get("avg_is_r", round(avg_is_r, 4)),
            "mean_fee_r": best.get("mean_fee_r", 0.0),
            "nis": best["nis"], "isr": best["isr"], "ispf": best["ispf"], "ist": best["ist"], "hmin": best["hmin"],
            "noos": best["noos"], "oosr": best["oosr"], "oospf": best["oospf"],
            "n_all": best["n_all"], "r_all": best["r_all"], "pf_all": best["pf_all"],
        })

    CFG_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    if all_rows:
        pd.DataFrame(all_rows).to_parquet(tr_p, index=False)

    out_cfg = {
        "symbol": sym, "mt5_symbol": mt5_sym, "category": cat,
        "span_start": datetime.fromtimestamp(ts_start, tz=timezone.utc).strftime("%Y-%m-%d"),
        "span_end": datetime.fromtimestamp(ts_end, tz=timezone.utc).strftime("%Y-%m-%d"),
        "split_iso": datetime.fromtimestamp(split_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "live_sp_bps": live_sp, "mt5_cost_bps": mt5_cost, "min_lot_25atr_sl_usd": min_sl25_usd,
        "swap_long": sw_l, "swap_short": sw_s,
        "strategies_evaluated": len(strat_catalog),
        "strategy_scorecard": strat_catalog,
    }
    cfg_p.write_text(json.dumps(out_cfg, indent=2), encoding="utf-8")
    del g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c, bars4, bars2, cdf4, cdf2
    gc.collect()
    return out_cfg


def _build_wf_trade_pool(
    sid: str,
    candidates: list[dict[str, Any]],
    k_top: int = 4,
    train_months: int = 18,
    step_months: int = 6,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build strictly causal rolling Walk-Forward trade pool (18M trailing IS -> 6M forward OOS)."""
    liq = [x for x in candidates if x["sp_bps"] <= 50.0]
    if not liq:
        return [], [], []

    fold_starts = pd.date_range("2022-07-01", "2026-12-01", freq=f"{step_months}MS", tz="UTC")
    wf_trades: list[dict[str, Any]] = []
    fold_log: list[dict[str, Any]] = []
    latest_syms: set[str] = set()

    for fs in fold_starts:
        ts_lo = int((fs - pd.DateOffset(months=train_months)).timestamp())
        ts_hi = int(fs.timestamp())
        ts_end = int((fs + pd.DateOffset(months=step_months)).timestamp())
        scored: list[tuple[float, dict[str, Any]]] = []
        for x in liq:
            r_tr = [float(t["r"]) for t in x["trades"] if ts_lo <= int(t["ts_in"]) < ts_hi]
            if len(r_tr) < 4:
                continue
            arr_t = np.array(r_tr, dtype=float)
            net_r = float(arr_t.sum())
            g = float(arr_t[arr_t > 0].sum())
            l = float(abs(arr_t[arr_t < 0].sum()))
            pf = (g / l) if l > 0 else (9.9 if g > 0 else 0.0)
            if pf < 1.10 or net_r <= 0.0:
                continue
            t_val = float(arr_t.mean() / (arr_t.std() + 1e-9) * math.sqrt(len(arr_t)))
            scored.append((t_val * min(pf, 3.0) + 0.05 * net_r, x))
        if not scored:
            # Causal fallback: lowest-spread pre-2024 assets with trailing >=0R
            for x in liq:
                r_tr = [float(t["r"]) for t in x["trades"] if ts_lo <= int(t["ts_in"]) < ts_hi]
                scored.append((float(sum(r_tr)) - 0.02 * x["sp_bps"], x))
        scored.sort(key=lambda z: z[0], reverse=True)
        chosen = [z[1] for z in scored[:k_top]]
        chosen_syms = [x["symbol"] for x in chosen]
        latest_syms.update(chosen_syms)
        fold_trades = [t for x in chosen for t in x["trades"] if ts_hi <= int(t["ts_in"]) < ts_end]
        wf_trades.extend(fold_trades)
        fold_log.append({
            "fold_start": fs.strftime("%Y-%m-%d"),
            "assets": chosen_syms,
            "trades_out": len(fold_trades),
        })

    active_cands = [x for x in liq if x["symbol"] in latest_syms] or liq[:k_top]
    return wf_trades, active_cands, fold_log


def build_26_accounts(specs: dict[str, dict[str, Any]], max_spread_bp: float = 165.0) -> dict[str, Any]:
    """Optimize & simulate each of the 26 strategies independently across all mutual assets on $5,000."""
    ACC_DIR.mkdir(parents=True, exist_ok=True)
    cfgs: dict[str, dict[str, Any]] = {}
    dfs: dict[str, pd.DataFrame] = {}
    for p in sorted(CFG_DIR.glob("*.json")):
        sym = p.stem
        tp = TRADES_DIR / f"{sym}.parquet"
        if not tp.is_file():
            continue
        cfgs[sym] = json.loads(p.read_text(encoding="utf-8"))
        dfs[sym] = pd.read_parquet(tp)

    ts_2023 = int(pd.Timestamp("2023-01-01", tz="UTC").timestamp())
    print(f"\n[Stage B] Building 26 Independent $5,000 Strategy Accounts from {len(cfgs)} Mutual Assets...", flush=True)
    account_rows: list[dict[str, Any]] = []

    for acc_idx, sid in enumerate(ALL_26_STRATEGIES, start=1):
        if sid in RETIRED_STRATEGIES:
            acc_p = ACC_DIR / f"Acc_{acc_idx:02d}_{sid}.json"
            if acc_p.is_file():
                acc_p.unlink()
            account_rows.append({
                "acc_id": f"Acc_{acc_idx:02d}", "sid": sid, "status": "PURGED_UNPROFITABLE",
                "tier": "purged", "assets_n": 0, "oos_pf": 0.0, "full_calmar": 0.0,
            })
            print(f"  Acc_{acc_idx:02d} {sid:25s} | PURGED_UNPROFITABLE | deleted standalone account; retained Stage A telemetry", flush=True)
            continue

        # Gather eligible assets strictly via pre-2024 causal IS (zero 2026 commodity or OOS leakage)
        candidates: list[dict[str, Any]] = []
        for sym, c in sorted(cfgs.items()):
            sp_bp = float(c["live_sp_bps"])
            if sp_bp > min(float(max_spread_bp), 50.0):
                continue
            sc_rows = [r for r in c["strategy_scorecard"] if r["sid"] == sid]
            if not sc_rows:
                continue
            r = sc_rows[0]
            if not r["fits_5k_1pct"]:
                continue
            df_s = dfs[sym]
            grp = df_s[df_s["sleeve"] == sid]
            if grp.empty:
                continue
            is_grp = grp[grp["ts_in"] < SPLIT_2024_TS]
            nis_pre24 = int(len(is_grp))
            if nis_pre24 < 5:
                # Eliminates late-2025/2026 inception commodities from leaking into 2024-2026 OOS ledger
                continue
            ris = is_grp["r"].to_numpy(dtype=float)
            ig, il = float(ris[ris > 0].sum()), float(abs(ris[ris < 0].sum()))
            ispf_pre24 = round((ig / il) if il > 0 else (9.9 if ig > 0 else 0.0), 2)
            isr_pre24 = round(float(ris.sum()), 2)
            avg_is_r = round(float(ris.mean()), 4)
            ist_pre24 = round(compute_hac_stats(ris)["t_hac"], 2) if nis_pre24 >= 6 else 0.0
            late_is_r = float(is_grp[is_grp["ts_in"] >= TS_2022]["r"].sum())
            r23 = float(is_grp[is_grp["ts_in"] >= ts_2023]["r"].sum())
            oos_grp = grp[grp["ts_in"] >= SPLIT_2024_TS]
            roos = oos_grp["r"].to_numpy(dtype=float)
            og, ol = float(roos[roos > 0].sum()), float(abs(roos[roos < 0].sum()))
            oospf_p = round((og / ol) if ol > 0 else (9.9 if og > 0 else 0.0), 2)
            candidates.append({
                "symbol": sym, "mt5": c["mt5_symbol"], "cat": c["category"],
                "sp_bps": sp_bp, "cost_bp": c["mt5_cost_bps"],
                "is_ok": r["is_ok"], "nis": nis_pre24, "isr": isr_pre24,
                "avg_is_r": avg_is_r, "late_is_r": round(late_is_r, 2), "r23": round(r23, 2),
                "ispf": ispf_pre24, "ist": ist_pre24, "is_kelly": r["is_kelly"],
                "oosr": round(float(roos.sum()), 2) if len(roos) else 0.0, "oospf": oospf_p,
                "sl": r["sl"], "tp": r["tp"], "tr": r["tr"], "be": r["be"],
                "step_lock": r["step_lock"], "mh": r["mh"], "dyn_atr": r["dyn_atr"],
                "tf": r["tf"], "mode": r["mode"], "cfg": r["cfg"],
                "min_lot_sl_usd": r["min_lot_sl_usd"],
                "trades": grp.to_dict("records"),
            })

        # Evaluate Causal-IS Alpha Baskets (hardened <=50bp spread gate + expectancy & late-IS durability)
        tiers: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]] | None, list[dict[str, Any]] | None]] = []
        wf_folds: list[dict[str, Any]] | None = None
        if sid in WF_STRATEGIES:
            for k_wf in (2, 3, 4):
                wf_pool, wf_cands, wf_log = _build_wf_trade_pool(sid, candidates, k_top=k_wf)
                if wf_pool:
                    tiers.append((f"wf_18m_6m_top{k_wf}", wf_cands, wf_pool, wf_log))
        else:
            t_qual = sorted(
                [
                    x for x in candidates
                    if x["sp_bps"] <= 50.0
                    and x["ispf"] >= 1.25
                    and x["isr"] > 0.0
                    and x["avg_is_r"] >= 0.18
                    and x["nis"] >= 5
                    and x["late_is_r"] > 0.0
                    and x["r23"] >= -2.5
                ],
                key=lambda x: max(0.0, x["ist"]) * x["ispf"] * min(1.6, max(0.5, x["avg_is_r"] / 0.25)) + 0.05 * x["isr"],
                reverse=True,
            )
            t_strict35 = [x for x in t_qual if x["sp_bps"] <= 35.0]
            if len(t_strict35) >= 4:
                tiers.append(("strict_<=35bp", t_strict35, None, None))
            if t_qual:
                tiers.append(("strict_<=50bp", t_qual, None, None))
            for k_top in (4, 6, 8, 10):
                if len(t_qual) >= k_top:
                    tiers.append((f"alpha_top{k_top}", t_qual[:k_top], None, None))
            if not tiers and candidates:
                t_fb = sorted(
                    [x for x in candidates if x["sp_bps"] <= 50.0 and x["ispf"] >= 1.10 and x["isr"] > 0.0 and x["nis"] >= 5],
                    key=lambda x: x["isr"],
                    reverse=True,
                )[:5]
                if t_fb:
                    tiers.append(("fallback_<=50bp", t_fb, None, None))
                else:
                    top_c = sorted(candidates, key=lambda x: x["isr"], reverse=True)[:4]
                    tiers.append(("fallback_top4", top_c, None, None))

        if not tiers:
            account_rows.append({
                "acc_id": f"Acc_{acc_idx:02d}", "sid": sid, "status": "NO_ELIGIBLE_ASSETS",
                "assets_n": 0, "tier": "none", "oos_pf": 0.0, "full_calmar": 0.0,
            })
            print(f"  Acc_{acc_idx:02d} {sid:26s} | NO ELIGIBLE ASSETS", flush=True)
            continue

        # Causal IS parameter sweep across risk_pct, concurrency, same_side, tier (Equal Weight + tighter prop cushion)
        top_is_cands: list[dict[str, Any]] = []
        for t_name, c_list, prebuilt_pool, w_log in tiers:
            pool_t = prebuilt_pool if prebuilt_pool is not None else [tr for x in c_list for tr in x["trades"]]
            # Default to Equal Weight (1.0) — removes IS-Kelly overfitting on small samples
            weights = {(x["symbol"], sid): 1.0 for x in c_list}
            for rp in (0.30, 0.35, 0.45, 0.55, 0.65, 0.80):
                for mg in (4, 6, 8):
                    for cs in (2, 3, 4):
                        res_is = simulate_shared_account(
                            f"{sid}_{t_name}", pool_t, 5000.0, rp, mg, 1, cs, None, weights, True, 4.0, 6.0, max_ts=SPLIT_2024_TS
                        )
                        m = res_is["metrics"]
                        if m.get("trades", 0) < 8:
                            continue
                        dd = float(m.get("max_dd_pct", 99.0))
                        pf = float(m.get("pf", 0.0))
                        cal = float(m.get("calmar", 0.0))
                        cagr = float(m.get("cagr_pct", 0.0))
                        # Tighter causal IS cushion: IS DD <= 5.2% keeps OOS MC breach10 well under 10%
                        dd_pen = 1.0 if dd <= 5.2 else (0.35 if dd <= 6.5 else 0.03)
                        sc = (cagr * math.sqrt(max(0.5, cal)) * dd_pen) if (pf >= 1.12 and cagr > 0.0) else (-10.0 + pf)
                        top_is_cands.append({
                            "is_score": sc, "tier": t_name, "c_list": c_list, "pool_t": pool_t,
                            "weights": weights, "rp": rp, "mg": mg, "cs": cs, "is_metrics": m, "wf_log": w_log,
                        })

        best_is: dict[str, Any] | None = None
        if top_is_cands:
            top_is_cands.sort(key=lambda x: x["is_score"], reverse=True)
            seen_rt: set[tuple[float, str]] = set()
            mc_cands: list[dict[str, Any]] = []
            for cand_b in top_is_cands:
                k_rt = (cand_b["rp"], cand_b["tier"])
                if k_rt in seen_rt:
                    continue
                seen_rt.add(k_rt)
                mc_cands.append(cand_b)
                if len(mc_cands) >= 12:
                    break
            for cand_b in mc_cands:
                rmc = simulate_shared_account(
                    f"{sid}_ismc", cand_b["pool_t"], 5000.0, cand_b["rp"], cand_b["mg"], 1, cand_b["cs"],
                    None, cand_b["weights"], True, 4.0, 6.0, max_ts=SPLIT_2024_TS, mc_runs=2000
                )
                imc = rmc["metrics"].get("mc_20k", {})
                is_br = float(imc.get("breach10_pct", 99.0))
                br_pen = 1.0 if is_br <= 3.5 else (0.25 if is_br <= 6.5 else 0.02)
                cand_b["is_score_mc"] = cand_b["is_score"] * br_pen
            best_is = max(mc_cands, key=lambda x: x["is_score_mc"])

        if best_is is None:
            account_rows.append({"acc_id": f"Acc_{acc_idx:02d}", "sid": sid, "status": "INSUFFICIENT_IS_TRADES", "assets_n": len(candidates), "oos_pf": 0.0, "full_calmar": 0.0})
            print(f"  Acc_{acc_idx:02d} {sid:26s} | INSUFFICIENT IS TRADES", flush=True)
            continue

        # Frozen Full + Untouched OOS evaluation + 20,000-Run Prop Monte Carlo at $5,000
        rf = simulate_shared_account(
            f"Acc_{acc_idx:02d}:{sid}", best_is["pool_t"], 5000.0,
            best_is["rp"], best_is["mg"], 1, best_is["cs"], None, best_is["weights"], True, 4.0, 6.0, mc_runs=20000
        )
        ro = simulate_shared_account(
            f"Acc_{acc_idx:02d}:{sid} [OOS]", best_is["pool_t"], 5000.0,
            best_is["rp"], best_is["mg"], 1, best_is["cs"], None, best_is["weights"], True, 4.0, 6.0, min_ts=SPLIT_2024_TS, mc_runs=20000
        )
        fm, om = rf["metrics"], ro["metrics"]
        fmc = fm.get("mc_20k", {})
        omc = om.get("mc_20k", {})

        prop_ready = (
            om.get("net_r", -99.0) > 0.0
            and om.get("pf", 0.0) >= 1.15
            and fm.get("calmar", 0.0) >= 1.0
            and fm.get("max_dd_pct", 99.0) <= 10.0
            and om.get("max_dd_pct", 99.0) <= 10.0
            and fmc.get("breach10_pct", 99.0) <= 10.0
            and omc.get("breach10_pct", 99.0) <= 10.0
        )
        cond_ok = (om.get("net_r", -99.0) > 0.0 or (fm.get("pf", 0.0) >= 1.10 and fm.get("max_dd_pct", 99.0) <= 12.0))
        status = "TIER_A_PROP_READY" if prop_ready else ("TIER_B_CONDITIONAL" if cond_ok else "FAIL_STANDALONE")

        acc_cfg = {
            "acc_id": f"Acc_{acc_idx:02d}",
            "sid": sid,
            "status": status,
            "start_equity": 5000.0,
            "tier": best_is["tier"],
            "governor": {
                "risk_pct": best_is["rp"],
                "max_global": best_is["mg"],
                "max_per_asset": 1,
                "max_crypto_same_side": best_is["cs"],
                "dd_throttle": [4.0, 6.0],
                "daily_loss_halt_pct": -3.5,
            },
            "full_metrics": fm,
            "oos_metrics": om,
            "assets": [
                {
                    "symbol": x["symbol"], "mt5_symbol": x["mt5"], "category": x["cat"],
                    "sp_bps": x["sp_bps"], "tf": x["tf"], "mode": x["mode"], "cfg": x["cfg"],
                    "sl_atr": x["sl"], "tp_r": x["tp"], "trail_atr": x["tr"], "be_r": x["be"],
                    "step_lock": x["step_lock"], "max_hold_bars": x["mh"], "dyn_atr": x["dyn_atr"],
                    "weight": best_is["weights"][(x["symbol"], sid)],
                    "is_r": x["isr"], "is_pf": x["ispf"], "oos_r": x["oosr"], "oos_pf": x["oospf"],
                }
                for x in sorted(best_is["c_list"], key=lambda y: y["isr"], reverse=True)
            ],
        }
        if best_is.get("wf_log"):
            acc_cfg["wf_folds"] = best_is["wf_log"]
        (ACC_DIR / f"Acc_{acc_idx:02d}_{sid}.json").write_text(json.dumps(acc_cfg, indent=2), encoding="utf-8")

        summary_row = {
            "acc_id": f"Acc_{acc_idx:02d}", "sid": sid, "status": status, "tier": best_is["tier"],
            "assets_n": len(best_is["c_list"]), "rp": best_is["rp"], "mg": best_is["mg"], "cs": best_is["cs"],
            "full_end_usd": fm.get("end_equity", 5000.0), "full_cagr": fm.get("cagr_pct", 0.0),
            "full_dd": fm.get("max_dd_pct", 0.0), "full_pf": fm.get("pf", 0.0), "full_calmar": fm.get("calmar", 0.0),
            "trades": fm.get("trades", 0), "oos_trades": om.get("trades", 0),
            "oos_r": om.get("net_r", 0.0), "oos_pf": om.get("pf", 0.0), "oos_dd": om.get("max_dd_pct", 0.0),
            "mc_pass10": fmc.get("pass10_pct", 0.0), "mc_breach10": fmc.get("breach10_pct", 0.0),
            "oos_mc_pass10": omc.get("pass10_pct", 0.0), "oos_mc_breach10": omc.get("breach10_pct", 0.0),
        }
        account_rows.append(summary_row)
        print(
            f"  {summary_row['acc_id']} {sid:25s} | {status:17s} | A={summary_row['assets_n']:2d} rp={best_is['rp']:.2f}% mg={best_is['mg']} | "
            f"Full=${summary_row['full_end_usd']:>7,.0f} PF={summary_row['full_pf']:.2f} DD={summary_row['full_dd']:.1f}% | "
            f"OOS={summary_row['oos_r']:+5.1f}R PF={summary_row['oos_pf']:.2f} | MC Pass10={summary_row['mc_pass10']:.1f}% Br10={summary_row['mc_breach10']:.1f}% (OOS Br10={summary_row['oos_mc_breach10']:.1f}%)",
            flush=True,
        )

    master = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "split_iso": "2024-01-01",
        "start_equity_per_account": 5000.0,
        "total_strategies": len(ALL_26_STRATEGIES),
        "total_assets_evaluated": len(cfgs),
        "tier_counts": {
            "TIER_A_PROP_READY": sum(1 for r in account_rows if r.get("status") == "TIER_A_PROP_READY"),
            "TIER_B_CONDITIONAL": sum(1 for r in account_rows if r.get("status") == "TIER_B_CONDITIONAL"),
            "FAIL_STANDALONE": sum(1 for r in account_rows if r.get("status") == "FAIL_STANDALONE"),
            "PURGED_UNPROFITABLE": sum(1 for r in account_rows if r.get("status") == "PURGED_UNPROFITABLE"),
            "UNAVAILABLE": sum(1 for r in account_rows if r.get("status") in ("NO_ELIGIBLE_ASSETS", "INSUFFICIENT_IS_TRADES")),
        },
        "accounts": sorted(account_rows, key=lambda r: (r.get("oos_pf", 0.0), r.get("full_calmar", 0.0)), reverse=True),
    }
    MASTER_JSON.write_text(json.dumps(master, indent=2), encoding="utf-8")
    print(f"\n[COMPLETE] Saved 26-account $5K Master Scorecard -> {MASTER_JSON}", flush=True)
    return master


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimize all 26 strategies individually on $5K accounts across mutual Binance-MT5 assets")
    ap.add_argument("--force-assets", action="store_true")
    ap.add_argument("--stage-b-only", action="store_true")
    ap.add_argument("--max-spread-bp", type=float, default=165.0)
    args = ap.parse_args()

    specs = json.loads(MUTUAL_SPECS_PATH.read_text(encoding="utf-8")) if MUTUAL_SPECS_PATH.is_file() else snapshot_all_mutual_specs()
    if not args.stage_b_only:
        chart_map = discover_chart_strategies()
        for sym, sp in sorted(specs.items()):
            if not sp.get("mt5_available"):
                continue
            if float(sp.get("sp_bps", 999.0)) > args.max_spread_bp:
                continue
            if not ((DATA_DIR / f"{sym}_agg_footprint.parquet").is_file() and (DATA_DIR / "cache" / f"{sym}_4h.parquet").is_file()):
                print(f"[skip] {sym} still downloading / missing cache", flush=True)
                continue
            _optimize_asset_all26(sym, sp, chart_map, force=args.force_assets)

    build_26_accounts(specs, max_spread_bp=args.max_spread_bp)


if __name__ == "__main__":
    main()
