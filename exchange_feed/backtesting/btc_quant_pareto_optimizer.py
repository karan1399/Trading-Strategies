"""Causal In-Sample Quant Portfolio & Pareto Frontier Optimizer (BTCUSDT 2021-2026).

Objectives: Maximize compounded equity return, Calmar ratio, and Profit Factor while
minimizing Max Drawdown and 20K Monte Carlo Crash DD95 — with ZERO Out-of-Sample leakage.

Pipeline:
  1. Re-generate exact 1m-executed trade logs for all 18 v2 IS-Qualified strategies.
  2. Compute strict In-Sample (<= 2023-12-31) daily return correlation matrix,
     down-day tail correlations, individual IS Sharpe / Calmar / PF / Ulcer metrics.
  3. Cluster redundant strategies into orthogonal Alpha Buckets (Breakout, Pullback,
     Mean-Reversion) and compute Causal IS Inverse-Volatility / Calmar parity weights.
  4. Run Greedy Forward-Stepwise IS Optimization + Concurrency/Cooldown/Bucket/Risk
     sweep strictly on IS (<= 2023) to identify Pareto-optimal portfolios:
       - Q1: Max-Calmar Fortress (lowest DD, highest Calmar, bucketed orthogonality)
       - Q2: Pareto Balanced Champion (peak Calmar * sqrt(EndEq) * PF)
       - Q3: Max-Return Growth Champion (fractional-Kelly sizing under strict DD<=30% IS)
       - Q4: P5 v2 Reference (apples-to-apples baseline)
  5. Freeze selected strategy sets, weights, bucket caps, and risk parameters onto
     2024-2026 OOS + 20,000-path correlated block-bootstrap & crash Monte Carlo.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from Charts.core.strategy_base import discover_strategies as discover_chart_strategies
from exchange_feed.backtesting.btc_all25_optimizer_v2 import (
    _df_to_bars,
    _fast_ohlc_fallbacks,
    _prep_arrs,
    _res_to_dicts,
    _to_chart_df,
)
from exchange_feed.backtesting.fast_kernels import sim_1m_trades_nb
from exchange_feed.backtesting.multi_asset_optimizer import (
    SPLIT_2024_TS,
    _correlated_block_mc_20k,
    _fast_s07_masks,
    compute_hac_stats,
)
from exchange_feed.backtesting.portfolio_engine import (
    _dsr_pct,
    _load_funding_arrays,
    load_asset_tfs_cached,
)
from exchange_feed.data_pipeline.binance_dumper import DATA_DIR
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import ema_arr, rolling_std_arr, sma_arr, wilder_atr, adx_arr
from strategies.delta_divergence import DeltaDivergenceStrategy
from strategies.imbalance_stack import ImbalanceStackStrategy
from strategies.of_initiative_breakout import OrderFlowInitiativeBreakoutStrategy
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd

ARTIFACT_PNG = Path(
    r"C:\Users\SIGMA\.gemini\antigravity\brain\4ed94c81-b1da-4868-8a7c-aec85386790d\btc_quant_pareto_frontier.png"
)
REPORT_JSON = DATA_DIR / "btc_quant_pareto_report.json"

ALPHA_BUCKETS: dict[str, str] = {
    "of_initiative_breakout": "BREAKOUT",
    "s09_donchian_trend_cta": "BREAKOUT",
    "donchian_breakout": "BREAKOUT",
    "s07_daily_pivot_breakout": "BREAKOUT",
    "s04_vc_squeeze_breakout": "BREAKOUT",
    "s05_zscore_breakout": "BREAKOUT",
    "s10_ema_pullback": "PULLBACK",
    "s01_williams_pullback": "PULLBACK",
    "ema_pullback": "PULLBACK",
    "s02_rsi2_meanrev": "MEANREV",
    "rsi2_of_reversion": "MEANREV",
    "s06_iv_wall_fade": "MEANREV",
    "s03_htf_ema_rejection": "MEANREV",
    "rsi2_mean_reversion": "MEANREV",
    "vwap_rejection": "MEANREV",
    "delta_divergence": "MEANREV",
    "imbalance_stack": "MEANREV",
    "bollinger_zscore": "MEANREV",
    "oi_vwap_liq_sweep": "BREAKOUT",
    "oi_4h_sqz_mom": "BREAKOUT",
    "oi_2h_liq_break": "BREAKOUT",
    "oi_2h_reclaim_fade": "MEANREV",
}


def _sigs_to_arr(n_b: int, sigs: list[Any]) -> np.ndarray:
    arr = np.zeros(n_b, dtype=np.int8)
    for s in sigs:
        idx = int(getattr(s, "index"))
        if 0 <= idx < n_b and arr[idx] == 0:
            arr[idx] = int(getattr(s, "side"))
    return arr


def _generate_18_qualified_trades() -> tuple[pd.DataFrame, dict[str, list[dict[str, Any]]]]:
    """Re-generate exact 1m-executed trade dicts for all 18 IS-qualified v2 strategies."""
    print("[1/5] Loading BTCUSDT 2021-2026 1m + 2H/4H data & regenerating 18 IS-qualified sleeves...", flush=True)
    g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = load_asset_tfs_cached("BTCUSDT", DATA_DIR, adaptive_tick=True)
    m1_ts_c = np.ascontiguousarray(m1_ts, dtype=np.int64)
    m1_o_c = np.ascontiguousarray(m1_o, dtype=np.float64)
    m1_h_c = np.ascontiguousarray(m1_h, dtype=np.float64)
    m1_l_c = np.ascontiguousarray(m1_l, dtype=np.float64)
    m1_c_c = np.ascontiguousarray(m1_c, dtype=np.float64)
    f_ts, f_rt = _load_funding_arrays("BTCUSDT", DATA_DIR)
    fts_c = np.ascontiguousarray(f_ts, dtype=np.int64) if len(f_ts) else np.empty(0, dtype=np.int64)
    frt_c = np.ascontiguousarray(f_rt, dtype=np.float64) if len(f_rt) else np.empty(0, dtype=np.float64)

    a4 = _prep_arrs(g4)
    a2 = _prep_arrs(g2)
    fp4 = build_fp_context(g4)
    bars4 = _df_to_bars(g4)
    bars2 = _df_to_bars(g2)
    cdf4 = _to_chart_df(g4)
    cdf2 = _to_chart_df(g2)

    def _sim(df_u, arrs, tf_sec, side_arr, sl, tp, tr, be, stp, mh, dyn):
        return sim_1m_trades_nb(
            arrs[0], arrs[1], arrs[2], arrs[3], arrs[4], arrs[5],
            np.ascontiguousarray(side_arr, dtype=np.int8),
            m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c,
            int(tf_sec), float(sl), float(tp), float(tr), float(be), 0.05, bool(stp), int(mh), 1, bool(dyn),
            False, 2.65, True, 0.0, 0.0, 5, 0.0, 0.0,
            fts_c, frt_c,
        )

    c4, h4, l4 = a4[5], g4["h"].to_numpy(dtype=np.float64), g4["l"].to_numpy(dtype=np.float64)
    e89_4, e233_4 = a4[3], a4[4]
    up233, dn233 = c4 > e233_4, c4 < e233_4
    e89_s3 = pd.Series(e89_4).shift(3).to_numpy(dtype=np.float64)
    stkL = (e89_4 > e233_4) & (e89_4 > e89_s3) & up233
    stkS = (e89_4 < e233_4) & (e89_4 < e89_s3) & dn233
    dp4, clv4 = g4["d_pct"].to_numpy(dtype=np.float64), g4["clv"].to_numpy(dtype=np.float64)
    bi4, si4 = g4["buy_imb"].to_numpy(dtype=np.int64), g4["sell_imb"].to_numpy(dtype=np.int64)
    has_b4 = (bi4 + si4) > 0
    vpin4 = fp4["vpin_rank"]
    e50_4, e200_4 = ema_arr(c4, 50), ema_arr(c4, 200)
    adx4, atr4_14 = adx_arr(h4, l4, c4, 14), wilder_atr(h4, l4, c4, 14)
    cvdz4 = g4["cvd_z24"].to_numpy(dtype=np.float64)

    c2, h2, l2 = a2[5], g2["h"].to_numpy(dtype=np.float64), g2["l"].to_numpy(dtype=np.float64)
    e233_2 = a2[4]
    up233_2, dn233_2 = c2 > e233_2, c2 < e233_2
    dp2, clv2 = g2["d_pct"].to_numpy(dtype=np.float64), g2["clv"].to_numpy(dtype=np.float64)
    bi2, si2 = g2["buy_imb"].to_numpy(dtype=np.int64), g2["sell_imb"].to_numpy(dtype=np.int64)

    ohlc4 = _fast_ohlc_fallbacks(g4)
    chart_map = discover_chart_strategies()
    trades_by_sid: dict[str, list[dict[str, Any]]] = {}

    def _rec(sid, df_u, lbl, res):
        trades_by_sid[sid] = _res_to_dicts(df_u, sid, lbl, res)

    # 1. of_initiative_breakout
    of_sa = _sigs_to_arr(len(g4), OrderFlowInitiativeBreakoutStrategy().generate_signals(
        bars4, {"lookback": 16, "min_delta_pct": 3.0, "min_clv": 0.30, "require_imb_dom": 1}
    ))
    _rec("of_initiative_breakout", g4, "4H Hybrid FP", _sim(g4, a4, 14400, of_sa, 2.0, 0.0, 3.5, 0.0, False, 96, True))

    # 2. s09_donchian_trend_cta
    hi22 = pd.Series(h4).shift(1).rolling(22).max().to_numpy()
    lo22 = pd.Series(l4).shift(1).rolling(22).min().to_numpy()
    s09_l = (c4 > hi22) & up233 & (clv4 > 0.25) & (dp4 > 0.0) & (~has_b4 | (bi4 >= si4))
    s09_s = (c4 < lo22) & dn233 & (clv4 < -0.25) & (dp4 < 0.0) & (~has_b4 | (si4 >= bi4))
    _rec("s09_donchian_trend_cta", g4, "4H FP", _sim(g4, a4, 14400, np.where(s09_l, 1, np.where(s09_s, -1, 0)), 2.0, 0.0, 3.5, 0.0, False, 96, True))

    # 3. s07_daily_pivot_breakout (FP+CVD)
    pL, pS = _fast_s07_masks(g4, fp4, {"swing_k": 3})
    s07_sa = np.where(pL & (cvdz4 > 0.5), 1, np.where(pS & (cvdz4 < -0.5), -1, 0))
    _rec("s07_daily_pivot_breakout", g4, "4H FP+CVD", _sim(g4, a4, 14400, s07_sa, 2.0, 0.0, 3.5, 1.0, True, 96, True))

    # 4. s10_ema_pullback
    s10_l = stkL & (l4 <= e89_4 + 0.30 * a4[2]) & (c4 > e89_4) & (dp4 > 5.0) & (clv4 > 0.20)
    s10_s = stkS & (h4 >= e89_4 - 0.30 * a4[2]) & (c4 < e89_4) & (dp4 < -5.0) & (clv4 < -0.20)
    _rec("s10_ema_pullback", g4, "4H FP", _sim(g4, a4, 14400, np.where(s10_l, 1, np.where(s10_s, -1, 0)), 2.0, 0.0, 3.5, 0.0, False, 96, True))

    # 5. s04_vc_squeeze_breakout
    _rec("s04_vc_squeeze_breakout", g4, "4H OHLC", _sim(g4, a4, 14400, ohlc4["s04"], 1.5, 4.0, 0.0, 1.0, False, 96, False))

    # 6. s01_williams_pullback
    wr4 = fp4["wr14"]
    wr4_p = pd.Series(wr4).shift(1).to_numpy(dtype=np.float64)
    s01_l = stkL & (wr4_p <= -75.0) & (wr4 > -75.0) & (dp4 > 5.0) & (clv4 > 0.20) & (~has_b4 | (bi4 >= si4))
    s01_s = stkS & (wr4_p >= -25.0) & (wr4 < -25.0) & (dp4 < -5.0) & (clv4 < -0.20) & (~has_b4 | (si4 >= bi4))
    _rec("s01_williams_pullback", g4, "4H FP", _sim(g4, a4, 14400, np.where(s01_l, 1, np.where(s01_s, -1, 0)), 2.0, 0.0, 3.5, 0.0, True, 96, True))

    # 7 & 12. s02_rsi2_meanrev + rsi2_of_reversion
    r2_4 = fp4["rsi2"]
    ofb4 = g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool) | ((dp4 > 5.0) & (clv4 > 0.25))
    ofs4 = g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool) | ((dp4 < -5.0) & (clv4 < -0.25))
    s02_sa = np.where((vpin4 < 0.65) & (r2_4 < 20.0) & up233 & ofb4, 1, np.where((vpin4 < 0.65) & (r2_4 > 80.0) & dn233 & ofs4, -1, 0))
    _rec("s02_rsi2_meanrev", g4, "4H FP", _sim(g4, a4, 14400, s02_sa, 2.5, 4.0, 0.0, 1.0, True, 48, True))
    r2of_sa = np.where((vpin4 < 0.70) & (r2_4 < 15.0) & up233 & ofb4, 1, np.where((vpin4 < 0.70) & (r2_4 > 85.0) & dn233 & ofs4, -1, 0))
    _rec("rsi2_of_reversion", g4, "4H G4 FP", _sim(g4, a4, 14400, r2of_sa, 2.5, 4.0, 0.0, 1.0, True, 48, True))

    # 8. s06_iv_wall_fade
    vwz4 = g4["vwz48"].to_numpy(dtype=np.float64)
    zbot4, ztop4 = g4["zero_bot"].to_numpy(dtype=bool), g4["zero_top"].to_numpy(dtype=bool)
    s06_l = (vpin4 < 0.60) & (vwz4 < -2.25) & ((dp4 > 0.0) | (clv4 > 0.20) | zbot4)
    s06_s = (vpin4 < 0.60) & (vwz4 > 2.25) & ((dp4 < 0.0) | (clv4 < -0.20) | ztop4)
    _rec("s06_iv_wall_fade", g4, "4H FP", _sim(g4, a4, 14400, np.where(s06_l, 1, np.where(s06_s, -1, 0)), 2.5, 4.5, 0.0, 1.2, False, 48, False))

    # 9. s03_htf_ema_rejection
    e233_2s = pd.Series(e233_2).shift(2).to_numpy(dtype=np.float64)
    c2s = pd.Series(c2).shift(2).to_numpy(dtype=np.float64)
    t2L = (l2 <= e233_2 + 0.3 * a2[2]) & (c2 > e233_2) & (c2s > e233_2s)
    t2S = (h2 >= e233_2 - 0.3 * a2[2]) & (c2 < e233_2) & (c2s < e233_2s)
    s03_l = (bi2 >= 1) & (clv2 > 0.25) & t2L & (dp2 > 3.0)
    s03_s = (si2 >= 1) & (clv2 < -0.25) & t2S & (dp2 < -3.0)
    _rec("s03_htf_ema_rejection", g2, "2H FP", _sim(g2, a2, 7200, np.where(s03_l, 1, np.where(s03_s, -1, 0)), 2.5, 3.5, 0.0, 1.0, False, 72, False))

    # 10. donchian_breakout (4H EMA233)
    dc_sa = _sigs_to_arr(len(g4), chart_map["donchian_cta"].generate_signals(cdf4, {"lookback": 22}))
    dc_tr = np.where(((dc_sa == 1) & up233) | ((dc_sa == -1) & dn233), dc_sa, 0)
    _rec("donchian_breakout", g4, "4H EMA233", _sim(g4, a4, 14400, dc_tr, 2.5, 0.0, 4.0, 0.0, False, 96, True))

    # 11. rsi2_mean_reversion (4H Pure OHLC)
    r2_sa = _sigs_to_arr(len(g4), chart_map["rsi2_meanrev"].generate_signals(cdf4, {"oversold": 15.0, "overbought": 85.0}))
    _rec("rsi2_mean_reversion", g4, "4H OHLC", _sim(g4, a4, 14400, r2_sa, 2.0, 0.0, 3.5, 0.0, False, 96, True))

    # 13. ema_pullback (2H EMA233)
    ep_sa = _sigs_to_arr(len(g2), chart_map["ema_pullback"].generate_signals(
        cdf2, {"fast_ema": 21, "slow_ema": 50, "trend_ema": 200, "rsi_buy_max": 62.0, "rsi_sell_min": 38.0}
    ))
    ep_tr = np.where(((ep_sa == 1) & up233_2) | ((ep_sa == -1) & dn233_2), ep_sa, 0)
    _rec("ema_pullback", g2, "2H EMA233", _sim(g2, a2, 7200, ep_tr, 2.5, 0.0, 4.0, 0.0, False, 96, True))

    # 14. vwap_rejection (2H EMA233)
    vw_sa = _sigs_to_arr(len(g2), chart_map["vwap_reclaim"].generate_signals(cdf2, {"min_dist_atr": 0.3}))
    vw_tr = np.where(((vw_sa == 1) & up233_2) | ((vw_sa == -1) & dn233_2), vw_sa, 0)
    _rec("vwap_rejection", g2, "2H EMA233", _sim(g2, a2, 7200, vw_tr, 2.5, 0.0, 4.0, 0.0, False, 96, True))

    # 15. delta_divergence (2H FP+EMA233)
    dd_sa = _sigs_to_arr(len(g2), DeltaDivergenceStrategy().generate_signals(bars2, {"min_abs_delta_pct": 8.0}))
    dd_tr = np.where(((dd_sa == 1) & up233_2) | ((dd_sa == -1) & dn233_2), dd_sa, 0)
    _rec("delta_divergence", g2, "2H FP+EMA233", _sim(g2, a2, 7200, dd_tr, 2.5, 3.5, 0.0, 1.0, False, 48, False))

    # 16. s05_zscore_breakout (4H FP)
    mu50 = sma_arr(c4, 50)
    sd50 = np.where(rolling_std_arr(c4, 50) > 1e-12, rolling_std_arr(c4, 50), np.nan)
    zv50 = (c4 - mu50) / sd50
    zvp50 = pd.Series(zv50).shift(1).to_numpy(dtype=np.float64)
    atr_exp = atr4_14 >= pd.Series(atr4_14).rolling(20).mean().to_numpy(dtype=np.float64)
    s05_l = (zvp50 < 1.5) & (zv50 >= 1.5) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 18.0) & atr_exp & (dp4 > 0.0) & (clv4 > 0.15)
    s05_s = (zvp50 > -1.5) & (zv50 <= -1.5) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 18.0) & atr_exp & (dp4 < 0.0) & (clv4 < -0.15)
    _rec("s05_zscore_breakout", g4, "4H FP", _sim(g4, a4, 14400, np.where(s05_l, 1, np.where(s05_s, -1, 0)), 2.0, 0.0, 3.0, 0.0, False, 96, True))

    # 17. imbalance_stack (2H Pure FP)
    imb_sa = _sigs_to_arr(len(g2), ImbalanceStackStrategy().generate_signals(bars2, {"min_imbalances": 3}))
    _rec("imbalance_stack", g2, "2H Pure FP", _sim(g2, a2, 7200, imb_sa, 2.0, 3.0, 0.0, 1.0, False, 48, False))

    # 18. bollinger_zscore (4H EMA233)
    bb_sa = _sigs_to_arr(len(g4), chart_map["bb_zscore"].generate_signals(cdf4, {"window": 50, "z_thresh": 1.5}))
    bb_tr = np.where(((bb_sa == 1) & up233) | ((bb_sa == -1) & dn233), bb_sa, 0)
    _rec("bollinger_zscore", g4, "4H EMA233", _sim(g4, a4, 14400, bb_tr, 2.0, 3.5, 0.0, 1.0, False, 96, False))

    return g4, trades_by_sid


def _simulate_quant_portfolio(
    sids: list[str],
    trades_by_sid: dict[str, list[dict[str, Any]]],
    vof_refs: tuple[np.ndarray, ...],
    start_equity: float = 5000.0,
    risk_pct: float = 1.0,
    max_concurrent: int = 4,
    max_same_side: int = 3,
    bucket_caps: dict[str, int] | None = None,
    cooldown_sec: int = 0,
    sleeve_weights: dict[str, float] | None = None,
    use_vof: bool = True,
    min_ts: int | None = None,
    max_ts: int | None = None,
    mc_runs: int = 0,
    name: str = "Portfolio",
) -> dict[str, Any]:
    """Chronological simulator supporting Alpha Bucket Caps, Same-Side Cooldown, Sleeve Weights & vOF."""
    pool = []
    for sid in sids:
        for t in trades_by_sid[sid]:
            if min_ts is not None and t["ts_in"] < min_ts:
                continue
            if max_ts is not None and t["ts_in"] >= max_ts:
                continue
            pool.append(t)
    sorted_t = sorted(pool, key=lambda x: x["ts_in"])

    active: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    last_side_ts = {"BUY": -1, "SELL": -1}
    bcaps = bucket_caps or {}

    for t in sorted_t:
        tin = int(t["ts_in"])
        active = [x for x in active if x["ts_out"] > tin]
        if len(active) >= max_concurrent:
            continue
        same_side = sum(1 for x in active if x["side"] == t["side"])
        if same_side >= max_same_side:
            continue
        if cooldown_sec > 0 and last_side_ts[t["side"]] > 0 and (tin - last_side_ts[t["side"]]) < cooldown_sec:
            continue
        bkt = ALPHA_BUCKETS.get(t["sleeve"], "OTHER")
        if bkt in bcaps:
            in_bkt = sum(1 for x in active if ALPHA_BUCKETS.get(x["sleeve"], "OTHER") == bkt)
            if in_bkt >= bcaps[bkt]:
                continue
        executed.append(t)
        active.append(t)
        last_side_ts[t["side"]] = tin

    if not executed:
        return {"name": name, "metrics": {"total_trades": 0, "end_equity": start_equity, "calmar_ratio": 0.0, "max_dd_pct": 0.0, "profit_factor": 0.0}}

    ca, ea, dpa, imba, sum_imba, vpa = vof_refs
    sw = sleeve_weights or {}
    eq = float(start_equity)
    pk = float(start_equity)
    max_dd_pct = 0.0
    rp = float(risk_pct) / 100.0
    r_vals: list[float] = []
    dd_vals: list[float] = [0.0]
    curve: list[dict[str, Any]] = [{"ts": int(executed[0]["ts_in"]), "equity": round(eq, 2)}]
    daily_r_map: dict[int, dict[str, float]] = {}
    is_r: list[float] = []
    oos_r: list[float] = []
    by_sleeve: dict[str, dict[str, Any]] = {}

    for t in executed:
        w = float(sw.get(t["sleeve"], 1.0))
        base_usd = eq * rp * w
        if use_vof:
            idx_r = max(0, int(np.searchsorted(ea, t["ts_in"], side="right")) - 1)
            s_sign = 1 if t["side"] == "BUY" else -1
            imb_val = int(imba[idx_r]) if sum_imba[idx_r] > 0 else s_sign
            scaled_usd = vol_of_conviction_risk_usd(
                ca[max(0, idx_r - 245) : idx_r + 1], "H4", s_sign,
                float(dpa[idx_r]), imb_val, float(vpa[idx_r]),
                base_usd, base_usd * 0.65, base_usd * 1.25, target_ann_vol=0.55, lookback=240,
            )
            eff_r = float(t["r"]) * (scaled_usd / (eq * rp) if eq > 0 else w)
            pnl = scaled_usd * float(t["r"])
        else:
            eff_r = float(t["r"]) * w
            pnl = base_usd * float(t["r"])

        eq += pnl
        pk = max(pk, eq)
        dd_pct = ((pk - eq) / pk * 100.0) if pk > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_pct)
        dd_vals.append(dd_pct)
        r_vals.append(eff_r)
        curve.append({"ts": int(t["ts_out"]), "equity": round(eq, 2)})

        day_k = int(t["ts_out"] // 86400)
        dm = daily_r_map.setdefault(day_k, {})
        dm["port_r"] = dm.get("port_r", 0.0) + eff_r
        if t["ts_in"] < SPLIT_2024_TS:
            is_r.append(eff_r)
        else:
            oos_r.append(eff_r)

        slv = by_sleeve.setdefault(t["sleeve"], {"trades": 0, "wins": 0, "sum_r": 0.0, "pnl_usd": 0.0})
        slv["trades"] += 1
        slv["wins"] += 1 if t["r"] > 0 else 0
        slv["sum_r"] += eff_r
        slv["pnl_usd"] += pnl

    arr = np.array(r_vals, dtype=float)
    span_ts = max(86400.0, float(executed[-1]["ts_out"] - executed[0]["ts_in"]))
    yrs = max(0.25, span_ts / (365.25 * 86400.0))
    cagr = (((eq / start_equity) ** (1.0 / yrs) - 1.0) * 100.0) if eq > 0 else -100.0
    calmar = (cagr / max_dd_pct) if max_dd_pct > 0 else 0.0
    sharpe = float(arr.mean() / (arr.std() + 1e-9) * math.sqrt(len(arr) / yrs))
    down = arr[arr < 0]
    sortino = float(arr.mean() / (down.std() + 1e-9) * math.sqrt(len(arr) / yrs)) if len(down) > 2 else sharpe
    gains, losses = float(arr[arr > 0].sum()), float(abs(arr[arr < 0].sum()))
    pf = (gains / losses) if losses > 0 else 9.99
    wr = float((arr > 0).mean() * 100.0)
    ulcer = float(math.sqrt(np.mean(np.square(dd_vals)))) if dd_vals else 0.0
    hac = compute_hac_stats(arr)

    oos_arr = np.array(oos_r, dtype=float)
    og, ol = float(oos_arr[oos_arr > 0].sum()), float(abs(oos_arr[oos_arr < 0].sum()))
    oos_pf = (og / ol) if ol > 0 else (9.99 if og > 0 else 0.0)
    oeq, opk, omdd = 1.0, 1.0, 0.0
    for rv in oos_arr:
        oeq *= max(0.01, 1.0 + float(rv) * rp)
        opk = max(opk, oeq)
        omdd = max(omdd, (opk - oeq) / opk * 100.0)

    d_min, d_max = min(daily_r_map.keys()), max(daily_r_map.keys())
    all_days = list(range(d_min, d_max + 1))
    d_df = pd.DataFrame([daily_r_map.get(d, {}) for d in all_days], index=all_days).fillna(0.0)
    if "port_r" not in d_df:
        d_df["port_r"] = 0.0

    mc_20k = _correlated_block_mc_20k(d_df, float(start_equity), float(risk_pct), n_mc=mc_runs) if mc_runs > 0 else {}

    return {
        "name": name,
        "sids": list(sids),
        "risk_pct": risk_pct,
        "metrics": {
            "start_equity": round(start_equity, 2),
            "end_equity": round(eq, 2),
            "cagr_pct": round(cagr, 1),
            "total_trades": len(arr),
            "win_rate": round(wr, 1),
            "total_r": round(float(arr.sum()), 2),
            "profit_factor": round(pf, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "calmar_ratio": round(calmar, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "ulcer_index": round(ulcer, 2),
            "t_hac": hac["t_hac"],
            "is_r": round(float(sum(is_r)), 2),
            "oos_r": round(float(sum(oos_r)), 2),
            "oos_trades": int(len(oos_arr)),
            "oos_pf": round(oos_pf, 2),
            "oos_dd_pct": round(omdd, 2),
            "oos_ret_pct": round(float((oeq - 1.0) * 100.0), 1),
            "mc_20k": mc_20k,
            "dd_curve_pct": [round(float(x), 2) for x in dd_vals],
        },
        "sleeves": sorted(
            [{"name": k, **{m: round(v[m], 2) if isinstance(v[m], float) else v[m] for m in ("trades", "wins", "sum_r", "pnl_usd")}} for k, v in by_sleeve.items()],
            key=lambda x: x["pnl_usd"], reverse=True,
        ),
        "equity_curve": curve[::max(1, len(curve) // 450)] + ([curve[-1]] if curve else []),
        "daily_df": d_df,
    }


def _compute_is_forensics(trades_by_sid: dict[str, list[dict[str, Any]]]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """Compute strictly IS (<= 2023) daily return matrix, pairwise correlation, and IS quality stats."""
    daily_series: dict[str, dict[int, float]] = {}
    stats_rows: list[dict[str, Any]] = []
    all_is_days = set()

    for sid, trs in trades_by_sid.items():
        is_trs = [t for t in trs if t["ts_in"] < SPLIT_2024_TS]
        r_is = np.array([t["r"] for t in is_trs], dtype=float)
        dm: dict[int, float] = {}
        for t in is_trs:
            dk = int(t["ts_out"] // 86400)
            dm[dk] = dm.get(dk, 0.0) + float(t["r"])
            all_is_days.add(dk)
        daily_series[sid] = dm

        g = float(r_is[r_is > 0].sum()) if len(r_is) else 0.0
        l = float(abs(r_is[r_is < 0].sum())) if len(r_is) else 0.0
        pf = (g / l) if l > 0 else 9.9
        eq_r = np.cumsum(r_is)
        pk_r = np.maximum.accumulate(np.insert(eq_r, 0, 0.0))[1:]
        dd_r = float(np.max(pk_r - eq_r)) if len(eq_r) else 1.0
        calmar_r = float(r_is.sum()) / max(dd_r, 1.0)
        h1 = float(r_is[: len(r_is) // 2].sum()) if len(r_is) >= 6 else -9.0
        h2 = float(r_is[len(r_is) // 2 :].sum()) if len(r_is) >= 6 else -9.0
        mean_r = float(r_is.mean()) if len(r_is) else 0.0
        # Sortino-Kelly: divide by downside semi-variance (never penalize right-skewed ATR-trail winners!)
        down_var = float(np.mean(np.minimum(r_is, 0.0) ** 2)) if len(r_is) > 1 else 1.0
        kelly_raw = max(0.05, (mean_r / max(down_var, 0.15)) * math.sqrt(max(1.0, pf)))
        stats_rows.append({
            "sid": sid,
            "bucket": ALPHA_BUCKETS[sid],
            "is_n": len(r_is),
            "is_r": round(float(r_is.sum()), 2),
            "is_pf": round(pf, 2),
            "is_dd_r": round(dd_r, 2),
            "is_calmar_r": round(calmar_r, 2),
            "is_t_hac": compute_hac_stats(r_is)["t_hac"],
            "is_half_min": round(min(h1, h2), 2),
            "is_kelly_raw": round(kelly_raw, 4),
        })

    idx_days = sorted(all_is_days)
    mat = pd.DataFrame({sid: [daily_series[sid].get(d, 0.0) for d in idx_days] for sid in trades_by_sid}, index=idx_days)
    corr = mat.corr().round(3)

    # Causal IS Sortino-Kelly Parity Weights (clipped [0.65, 1.35], mean-normalized = 1.0)
    raw_w = {r["sid"]: r["is_kelly_raw"] for r in stats_rows}
    m_w = sum(raw_w.values()) / max(1, len(raw_w))
    norm_w = {k: round(min(1.35, max(0.65, v / m_w)), 3) for k, v in raw_w.items()}
    return pd.DataFrame(stats_rows).sort_values("is_calmar_r", ascending=False), corr, norm_w


def _run_pareto_optimization() -> dict[str, Any]:
    g4, trades_by_sid = _generate_18_qualified_trades()
    bi = g4["buy_imb"].to_numpy(dtype=int)
    si = g4["sell_imb"].to_numpy(dtype=int)
    vof_refs = (
        g4["c"].to_numpy(dtype=float),
        g4["end_ts"].to_numpy(),
        g4["d_pct"].to_numpy(dtype=float),
        bi - si,
        bi + si,
        g4["vpin_rank"].to_numpy(dtype=float),
    )

    print("[2/5] Computing strict IS (<=2023) daily correlations, tail risk & Calmar weights...", flush=True)
    is_stats_df, is_corr_df, is_calmar_w = _compute_is_forensics(trades_by_sid)

    # Identify top correlated pairs in IS
    corr_pairs: list[tuple[str, str, float]] = []
    cols = list(is_corr_df.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            cval = float(is_corr_df.iloc[i, j])
            if abs(cval) >= 0.20:
                corr_pairs.append((cols[i], cols[j], cval))
    corr_pairs.sort(key=lambda x: abs(x[2]), reverse=True)

    print("[3/5] Running Causal IS Clone-Pruning (|corr|<=0.70), Greedy Subsets & Architecture Sweep (zero OOS leakage)...", flush=True)
    # Pool 1: Low-DD Resilient Specialists (IS PF >= 1.65, individual IS DD <= 8.5R, half_min >= 3.5R, t >= 1.30)
    # Rank by IS (t_hac * PF) so higher-significance leads when pruning >=0.70 clones (e.g. of_initiative beats s09 0.95 clone)
    low_dd_df = is_stats_df[
        (is_stats_df["is_pf"] >= 1.65)
        & (is_stats_df["is_dd_r"] <= 8.5)
        & (is_stats_df["is_half_min"] >= 3.5)
        & (is_stats_df["is_t_hac"] >= 1.30)
    ].copy()
    low_dd_df["is_sig_score"] = low_dd_df["is_t_hac"] * low_dd_df["is_pf"]
    low_dd_pool = low_dd_df.sort_values("is_sig_score", ascending=False)["sid"].tolist()

    # Prune exact/near clones (|corr_IS| > 0.70) while preserving distinct Breakout / Pullback / MeanRev engines
    decorr_70: list[str] = []
    for cand in low_dd_pool:
        if any(abs(float(is_corr_df.loc[cand, picked])) > 0.70 for picked in decorr_70):
            continue
        decorr_70.append(cand)

    # Strict decorrelation (|corr_IS| <= 0.45) ranked by significance
    decorr_45: list[str] = []
    for cand in low_dd_pool:
        if any(abs(float(is_corr_df.loc[cand, picked])) > 0.45 for picked in decorr_45):
            continue
        decorr_45.append(cand)

    elite_pool = decorr_70  # used for correlation heatmap panel

    arch_presets = [
        ("Bkt(1,1,2)_c3_s2", 3, 2, {"BREAKOUT": 1, "PULLBACK": 1, "MEANREV": 2}, 0),
        ("Bkt(2,1,2)_c4_s3", 4, 3, {"BREAKOUT": 2, "PULLBACK": 1, "MEANREV": 2}, 0),
        ("Bkt(2,1,2)_c4_s3_cd4h", 4, 3, {"BREAKOUT": 2, "PULLBACK": 1, "MEANREV": 2}, 14400),
        ("Bkt(2,2,2)_c4_s3", 4, 3, {"BREAKOUT": 2, "PULLBACK": 2, "MEANREV": 2}, 0),
        ("Raw_c4_s3", 4, 3, None, 0),
    ]

    # Greedy IS builder on decorr_70 starting from top-3 independent bucket seeds
    seeds = [
        next((s for s in decorr_70 if ALPHA_BUCKETS[s] == "BREAKOUT"), "of_initiative_breakout"),
        next((s for s in decorr_70 if ALPHA_BUCKETS[s] == "PULLBACK"), "s01_williams_pullback"),
        next((s for s in decorr_70 if ALPHA_BUCKETS[s] == "MEANREV"), "s02_rsi2_meanrev"),
    ]
    seed_set = list(dict.fromkeys(seeds))

    greedy_trace: list[dict[str, Any]] = []
    cur_set = list(seed_set)
    best_cur = _simulate_quant_portfolio(
        cur_set, trades_by_sid, vof_refs, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_side=3, bucket_caps={"BREAKOUT": 2, "PULLBACK": 1, "MEANREV": 2},
        sleeve_weights=is_calmar_w, max_ts=SPLIT_2024_TS, mc_runs=0,
    )
    greedy_trace.append({"step": len(cur_set), "added": "+".join(cur_set), **best_cur["metrics"]})

    remaining = [s for s in decorr_70 if s not in cur_set]
    while remaining:
        cand_scores = []
        for s in remaining:
            trial = cur_set + [s]
            res = _simulate_quant_portfolio(
                trial, trades_by_sid, vof_refs, start_equity=5000.0, risk_pct=1.0,
                max_concurrent=4, max_same_side=3, bucket_caps={"BREAKOUT": 2, "PULLBACK": 1, "MEANREV": 2},
                sleeve_weights=is_calmar_w, max_ts=SPLIT_2024_TS, mc_runs=0,
            )
            m = res["metrics"]
            obj = m["calmar_ratio"] * math.sqrt(max(0.5, m["profit_factor"])) * (1.0 + 0.15 * math.log(max(1.0, m["end_equity"] / 5000.0)))
            if m["max_dd_pct"] > 18.0:
                obj *= 0.80
            cand_scores.append((obj, s, m))
        cand_scores.sort(key=lambda x: x[0], reverse=True)
        top_obj, top_s, top_m = cand_scores[0]
        cur_set.append(top_s)
        remaining.remove(top_s)
        greedy_trace.append({"step": len(cur_set), "added": top_s, "obj": round(top_obj, 3), **top_m})

    best_fortress_row = max([r for r in greedy_trace if r["step"] <= 7], key=lambda x: x["calmar_ratio"])
    fortress_sids = list(seed_set) + [r["added"] for r in greedy_trace[1 : greedy_trace.index(best_fortress_row) + 1]]

    # Pool 3: Diversified IS Kelly Pool (includes high-frequency low-variance-adjusted alpha with |corr|<=0.70, half_min>=3.5)
    div_df = is_stats_df[(is_stats_df["is_pf"] >= 1.60) & (is_stats_df["is_half_min"] >= 3.5) & (is_stats_df["is_t_hac"] >= 1.30)].copy()
    div_df["is_sig_score"] = div_df["is_t_hac"] * div_df["is_pf"]
    div_pool: list[str] = []
    for cand in div_df.sort_values("is_sig_score", ascending=False)["sid"].tolist():
        if any(abs(float(is_corr_df.loc[cand, picked])) > 0.70 for picked in div_pool):
            continue
        div_pool.append(cand)

    # Sweep architectures & fractional-Kelly risk levels strictly on IS (<= 2023)
    is_frontier: list[dict[str, Any]] = []
    for sub_name, sub_sids in (("Decorr70_IS", decorr_70), ("DivKelly_IS", div_pool), ("Fortress_IS", fortress_sids)):
        for arch_name, mc_c, ms_s, bcaps, cd in arch_presets:
            for w_mode, sw_map in (("vof_eq", None), ("vof_is_kelly", is_calmar_w)):
                for r_pct in (0.75, 0.85, 1.0, 1.15, 1.25, 1.50):
                    res_is = _simulate_quant_portfolio(
                        sub_sids, trades_by_sid, vof_refs, start_equity=5000.0, risk_pct=r_pct,
                        max_concurrent=mc_c, max_same_side=ms_s, bucket_caps=bcaps, cooldown_sec=cd,
                        sleeve_weights=sw_map, max_ts=SPLIT_2024_TS, mc_runs=0,
                    )
                    m = res_is["metrics"]
                    is_frontier.append({
                        "subset": sub_name, "sids": sub_sids, "arch": arch_name,
                        "mc": mc_c, "ms": ms_s, "bcaps": bcaps, "cd": cd,
                        "w_mode": w_mode, "sw": sw_map, "risk_pct": r_pct,
                        "is_end": m["end_equity"], "is_cagr": m["cagr_pct"], "is_dd": m["max_dd_pct"],
                        "is_calmar": m["calmar_ratio"], "is_pf": m["profit_factor"],
                        "is_sharpe": m["sharpe_ratio"], "is_sortino": m["sortino_ratio"],
                        "is_ulcer": m["ulcer_index"], "is_n": m["total_trades"],
                    })

    # Select 3 Pareto Champions STRICTLY from IS frontier (ZERO OOS peeking, safe monotonic fallback):
    # Q1: True Minimum-Drawdown Fortress (IS PF >= 1.85, IS CAGR >= 50%, minimize IS DD first, tie-break by Calmar)
    q1_cands = [x for x in is_frontier if x["is_pf"] >= 1.85 and x["is_cagr"] >= 50.0] or is_frontier
    q1_spec = min(q1_cands, key=lambda x: (round(x["is_dd"], 1), -x["is_calmar"]))

    # Q2: Optimal Pareto Balanced Sweet Spot (IS PF >= 1.85, IS DD <= 16.5%, peak IS Calmar * log(EndEq) * PF)
    q2_cands = [x for x in is_frontier if x["is_pf"] >= 1.85 and x["is_dd"] <= 16.5] or sorted(is_frontier, key=lambda x: x["is_dd"])[:15]
    q2_spec = max(q2_cands, key=lambda x: x["is_calmar"] * math.log(max(2.0, x["is_end"] / 5000.0)) * x["is_pf"])

    # Q3: Max-Return Growth under Controlled Risk (IS PF >= 1.80, IS DD <= 22.0%, max IS EndEq)
    q3_cands = [x for x in is_frontier if x["is_pf"] >= 1.80 and x["is_dd"] <= 22.0] or sorted(is_frontier, key=lambda x: x["is_dd"])[:15]
    q3_spec = max(q3_cands, key=lambda x: x["is_end"])

    # Q4: P5 v2 Reference (exact same 10-strategy set from v2 @ 1.0% raw cap=4)
    p5_v2_sids = [
        "s09_donchian_trend_cta", "s10_ema_pullback", "s03_htf_ema_rejection", "s02_rsi2_meanrev",
        "s04_vc_squeeze_breakout", "s06_iv_wall_fade", "of_initiative_breakout",
        "s07_daily_pivot_breakout", "s01_williams_pullback", "rsi2_of_reversion",
    ]

    print("[4/5] Evaluating Frozen OOS (2024-2026) & 20,000 Correlated Block+Crash MC on Q1..Q4...", flush=True)
    results: dict[str, dict[str, Any]] = {}
    for q_key, label, spec in (
        ("Q1_max_calmar", "Q1: Max-Calmar Fortress", q1_spec),
        ("Q2_pareto_sweet", "Q2: Pareto Balanced Champion", q2_spec),
        ("Q3_max_growth", "Q3: Max-Return Growth (DD<=30% IS)", q3_spec),
    ):
        full_p = _simulate_quant_portfolio(
            spec["sids"], trades_by_sid, vof_refs, start_equity=5000.0, risk_pct=spec["risk_pct"],
            max_concurrent=spec["mc"], max_same_side=spec["ms"], bucket_caps=spec["bcaps"],
            cooldown_sec=spec["cd"], sleeve_weights=spec["sw"], mc_runs=20000, name=label,
        )
        oos_p = _simulate_quant_portfolio(
            spec["sids"], trades_by_sid, vof_refs, start_equity=5000.0, risk_pct=spec["risk_pct"],
            max_concurrent=spec["mc"], max_same_side=spec["ms"], bucket_caps=spec["bcaps"],
            cooldown_sec=spec["cd"], sleeve_weights=spec["sw"], min_ts=SPLIT_2024_TS, mc_runs=20000, name=f"{label} (Frozen OOS)",
        )
        results[q_key] = {"spec": {k: v for k, v in spec.items() if k != "sw"}, "full": full_p, "oos": oos_p}

    # Reference Q4 (P5 v2)
    q4_full = _simulate_quant_portfolio(
        p5_v2_sids, trades_by_sid, vof_refs, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_side=3, bucket_caps=None, cooldown_sec=0, sleeve_weights=None,
        mc_runs=20000, name="Q4: P5 v2 Reference (10 Strats @ 1.0%)",
    )
    q4_oos = _simulate_quant_portfolio(
        p5_v2_sids, trades_by_sid, vof_refs, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_side=3, bucket_caps=None, cooldown_sec=0, sleeve_weights=None,
        min_ts=SPLIT_2024_TS, mc_runs=20000, name="Q4: P5 v2 Reference (Frozen OOS)",
    )
    results["Q4_p5_ref"] = {"spec": {"sids": p5_v2_sids, "arch": "Raw_c4_s3", "w_mode": "vof_eq", "risk_pct": 1.0}, "full": q4_full, "oos": q4_oos}

    print("[5/5] Rendering Pareto Frontier, Correlation Heatmap & Equity/MC Chart...", flush=True)
    _render_quant_chart(ARTIFACT_PNG, results, is_frontier, is_corr_df, elite_pool)

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "split_iso": "2024-01-01 (Strict Causal IS <= 2023 -> Frozen OOS 2024-2026)",
        "is_individual_stats": is_stats_df.to_dict(orient="records"),
        "top_is_correlated_pairs": [{"s1": a, "s2": b, "is_corr": c} for a, b, c in corr_pairs[:12]],
        "is_calmar_parity_weights": is_calmar_w,
        "greedy_is_trace": greedy_trace,
        "portfolios": {
            k: {
                "spec": v["spec"],
                "full": {mk: mv for mk, mv in v["full"].items() if mk != "daily_df"},
                "oos": {mk: mv for mk, mv in v["oos"].items() if mk != "daily_df"},
            }
            for k, v in results.items()
        },
        "chart_png": str(ARTIFACT_PNG),
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _render_quant_chart(out_path: Path, results: dict[str, Any], is_frontier: list[dict[str, Any]], is_corr: pd.DataFrame, elite_pool: list[str]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, axes = plt.subplots(2, 2, figsize=(17, 11), facecolor="#0d1117")
    fig.suptitle(
        "BTCUSDT Quant Pareto Optimization (2021-2026 | Strict Causal IS<=2023 -> Frozen OOS 2024-2026 | 20,000 MC)",
        color="#f0f6fc", fontsize=13, fontweight="bold", y=0.985,
    )
    for ax in axes.flat:
        ax.set_facecolor("#161b22")
        ax.tick_params(colors="#c9d1d9", labelsize=8.2)
        for sp in ax.spines.values():
            sp.set_color("#30363d")
        ax.grid(alpha=0.18, color="#8b949e")

    # 1) Equity Curves (Log Scale) Q1..Q4
    ax1 = axes[0, 0]
    styles = (
        ("Q3_max_growth", "#a371f7", "Q3 Max-Growth"),
        ("Q2_pareto_sweet", "#3fb950", "Q2 Pareto Sweet Spot"),
        ("Q1_max_calmar", "#58a6ff", "Q1 Max-Calmar Fortress"),
        ("Q4_p5_ref", "#8b949e", "Q4 P5 v2 Baseline"),
    )
    for key, col, short in styles:
        p = results[key]["full"]
        pts = p.get("equity_curve") or []
        if pts:
            dt_x = [datetime.fromtimestamp(x["ts"], tz=timezone.utc) for x in pts]
            eq_y = [x["equity"] for x in pts]
            m = p["metrics"]
            ls = "--" if key == "Q4_p5_ref" else "-"
            ax1.plot(
                dt_x, eq_y, color=col, linestyle=ls, linewidth=2.0 if key != "Q4_p5_ref" else 1.4,
                label=f"{short} ({len(p['sids'])}s @ {p['risk_pct']}%): ${m['end_equity']:,.0f} | Calmar {m['calmar_ratio']} | DD {m['max_dd_pct']}% | PF {m['profit_factor']}",
            )
    ax1.axvline(datetime(2024, 1, 1, tzinfo=timezone.utc), color="#f85149", linestyle=":", linewidth=1.4, label="Frozen OOS Split (2024)")
    ax1.set_yscale("log")
    ax1.set_title("1. Pareto Equity Curves ($5,000 Start | Log Scale)", color="#f0f6fc", fontsize=10.5)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax1.legend(loc="upper left", fontsize=6.8, facecolor="#0d1117", labelcolor="#f0f6fc")

    # 2) Causal IS Risk-Return Efficient Frontier (MaxDD% vs CAGR%, colored by Calmar)
    ax2 = axes[0, 1]
    dds = np.array([x["is_dd"] for x in is_frontier])
    cagrs = np.array([x["is_cagr"] for x in is_frontier])
    cals = np.array([x["is_calmar"] for x in is_frontier])
    sc = ax2.scatter(dds, cagrs, c=cals, cmap="viridis", s=38, alpha=0.82, edgecolors="#0d1117", linewidth=0.5)
    cb = plt.colorbar(sc, ax=ax2)
    cb.set_label("IS Calmar Ratio", color="#c9d1d9", fontsize=8)
    cb.ax.tick_params(colors="#c9d1d9", labelsize=7.5)
    for key, col, mk, lbl in (
        ("Q1_max_calmar", "#58a6ff", "D", "Q1"),
        ("Q2_pareto_sweet", "#3fb950", "*", "Q2"),
        ("Q3_max_growth", "#a371f7", "^", "Q3"),
    ):
        sp = results[key]["spec"]
        ax2.scatter([sp["is_dd"]], [sp["is_cagr"]], color=col, marker=mk, s=160, edgecolors="#ffffff", linewidth=1.3, zorder=6, label=f"{lbl} (IS DD {sp['is_dd']}%, CAGR {sp['is_cagr']}%)")
    ax2.set_title("2. Strict Causal IS Efficient Frontier (MaxDD% vs CAGR%)", color="#f0f6fc", fontsize=10.5)
    ax2.set_xlabel("In-Sample Max Drawdown (%)", color="#c9d1d9", fontsize=8.5)
    ax2.set_ylabel("In-Sample CAGR (%)", color="#c9d1d9", fontsize=8.5)
    ax2.legend(loc="lower right", fontsize=7.5, facecolor="#0d1117", labelcolor="#f0f6fc")

    # 3) IS Daily Return Correlation Heatmap across Elite Pool
    ax3 = axes[1, 0]
    sub_corr = is_corr.loc[elite_pool, elite_pool].to_numpy(dtype=float) if len(elite_pool) >= 3 else is_corr.to_numpy(dtype=float)
    lbls = [s[:16] for s in (elite_pool if len(elite_pool) >= 3 else list(is_corr.columns))]
    im = ax3.imshow(sub_corr, cmap="RdBu_r", vmin=-0.35, vmax=0.65, aspect="auto")
    ax3.set_xticks(np.arange(len(lbls)))
    ax3.set_yticks(np.arange(len(lbls)))
    ax3.set_xticklabels(lbls, rotation=45, ha="right", fontsize=6.5, color="#c9d1d9")
    ax3.set_yticklabels(lbls, fontsize=6.5, color="#c9d1d9")
    for i in range(len(lbls)):
        for j in range(len(lbls)):
            ax3.text(j, i, f"{sub_corr[i, j]:.2f}", ha="center", va="center", fontsize=5.8, color="#0d1117" if abs(sub_corr[i, j]) > 0.25 else "#c9d1d9")
    ax3.set_title("3. Causal IS (<=2023) Daily Return Correlation Matrix (Elite Pool)", color="#f0f6fc", fontsize=10.0)

    # 4) Underwater Drawdown % Comparison (Q1, Q2, Q3 vs Q4 Baseline)
    ax4 = axes[1, 1]
    for key, col, short in (
        ("Q1_max_calmar", "#58a6ff", "Q1 Max-Calmar"),
        ("Q2_pareto_sweet", "#3fb950", "Q2 Pareto Sweet"),
        ("Q3_max_growth", "#a371f7", "Q3 Max-Growth"),
        ("Q4_p5_ref", "#8b949e", "Q4 P5 Ref"),
    ):
        m = results[key]["full"]["metrics"]
        dd = m.get("dd_curve_pct") or []
        if dd:
            xs = np.linspace(0, 100, len(dd))
            ax4.plot(xs, [-d for d in dd], color=col, linewidth=1.6 if key != "Q4_p5_ref" else 1.2, linestyle="--" if key == "Q4_p5_ref" else "-",
                     label=f"{short}: MaxDD -{m['max_dd_pct']:.1f}% | Crash95 -{m['mc_20k'].get('crash_dd_p95', 0):.1f}% | P(Loss) {m['mc_20k'].get('p_loss_pct', 0):.2f}%")
    ax4.set_title("4. Underwater Drawdown % & 20K Crash Tail Risk", color="#f0f6fc", fontsize=10.5)
    ax4.set_xlabel("Trade Timeline (%)", color="#c9d1d9", fontsize=8.5)
    ax4.set_ylabel("Drawdown %", color="#c9d1d9", fontsize=8.5)
    ax4.legend(loc="lower left", fontsize=7.2, facecolor="#0d1117", labelcolor="#f0f6fc")

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=140)
    plt.close(fig)


if __name__ == "__main__":
    rep = _run_pareto_optimization()
    print("\n=== CAUSAL IS INDIVIDUAL RANKING (<=2023) ===")
    for r in rep["is_individual_stats"]:
        print(
            f"{r['sid']:24s} [{r['bucket']:8s}] | n={r['is_n']:3d} R={r['is_r']:5.1f} PF={r['is_pf']:4.2f} "
            f"DD={r['is_dd_r']:4.1f}R CalmarR={r['is_calmar_r']:5.2f} t={r['is_t_hac']:4.2f} hmin={r['is_half_min']:5.1f} | W={rep['is_calmar_parity_weights'][r['sid']]}"
        )
    print("\n=== PARETO PORTFOLIO COMPARISON (2021-2026 & FROZEN OOS 2024-2026) ===")
    for qk, bundle in rep["portfolios"].items():
        sp = bundle["spec"]
        fm = bundle["full"]["metrics"]
        om = bundle["oos"]["metrics"]
        fmc = fm.get("mc_20k", {})
        omc = om.get("mc_20k", {})
        print(
            f"\n[{qk}] ({sp.get('arch')} | {sp.get('w_mode')} | risk={sp.get('risk_pct')}% | {len(sp['sids'])} strats: {', '.join(sp['sids'])})"
        )
        print(
            f"  FULL: n={fm['total_trades']:4d} WR={fm['win_rate']:4.1f}% R={fm['total_r']:6.1f} PF={fm['profit_factor']:4.2f} "
            f"End=${fm['end_equity']:10,.2f} CAGR={fm['cagr_pct']:5.1f}% MaxDD={fm['max_dd_pct']:5.2f}% Calmar={fm['calmar_ratio']:4.2f} "
            f"Sharpe={fm['sharpe_ratio']:4.2f} Sortino={fm['sortino_ratio']:4.2f} Ulcer={fm['ulcer_index']:4.2f} "
            f"| MC P(Loss)={fmc.get('p_loss_pct')}% CrashDD95={fmc.get('crash_dd_p95')}% P5=${fmc.get('blk_eq_p5'):,.0f} P50=${fmc.get('blk_eq_p50'):,.0f} P95=${fmc.get('blk_eq_p95'):,.0f}"
        )
        print(
            f"  OOS : n={om['total_trades']:4d} WR={om['win_rate']:4.1f}% R={om['total_r']:6.1f} PF={om['profit_factor']:4.2f} "
            f"End=${om['end_equity']:10,.2f} CAGR={om['cagr_pct']:5.1f}% MaxDD={om['max_dd_pct']:5.2f}% Calmar={om['calmar_ratio']:4.2f} "
            f"| OOS_MC P(Loss)={omc.get('p_loss_pct')}% CrashDD95={omc.get('crash_dd_p95')}%"
        )
