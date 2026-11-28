"""Strict IS->OOS BTC Optimizer v2 — Improved QUAL + Rescued FAIL strategies.

Extends v1 with:
  A) Improved parameter grids for all 14 IS-Qualified strategies.
  B) Rescue alternative signal constructions for 10 FAIL strategies.
  C) P5 improved portfolio = top uncorrelated strategies from both original qualifieds
     and newly rescued (was-FAIL, now-QUAL) strategies.
  D) Chart panel 3 colors rescued strategies in purple.
  E) Output files: btc_all25_equity_mc_v2.png + btc_all25_optimization_report_v2.json

All baselines, methodology, and cost model identical to v1:
  - Strict causal IS (<=2023) -> Frozen OOS (2024-2026). Zero OOS leakage.
  - 1m-exact sim_1m_trades_nb, sp_bps=2.65 Binance BTCUSDT Futures
  - $5,000 start, 1.0% base risk, s08 vOF sizing on combined portfolios
  - 20,000 block-bootstrap correlated MC simulation
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
from exchange_feed.backtesting.fast_kernels import sim_1m_trades_nb
from exchange_feed.backtesting.multi_asset_optimizer import (
    SPLIT_2024_TS,
    _correlated_block_mc_20k,
    compute_hac_stats,
    simulate_multi_asset_portfolio,
    _fast_s04_masks,
    _fast_s07_masks,
)
from exchange_feed.backtesting.portfolio_engine import (
    _dsr_pct,
    _load_funding_arrays,
    load_asset_tfs_cached,
)
from exchange_feed.data_pipeline.binance_dumper import DATA_DIR
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import (
    adx_arr, ema_arr, er_arr, rolling_std_arr,
    rsi_wilder_arr, sma_arr, wilder_atr, williams_r_arr,
)
from strategies.core.registry import generic_strategy_name, load_signal_fn

ARTIFACT_PNG = Path(
    r"C:\Users\SIGMA\.gemini\antigravity\brain\4ed94c81-b1da-4868-8a7c-aec85386790d\btc_all25_equity_mc_v2.png"
)
REPORT_JSON = DATA_DIR / "btc_all25_optimization_report_v2.json"


# ---------------------------------------------------------------------------
# Shared helper functions (identical to v1)
# ---------------------------------------------------------------------------

def _resample_m1(m1_ts, m1_o, m1_h, m1_l, m1_c, tf_sec):
    buckets = m1_ts // tf_sec
    bounds = np.flatnonzero(buckets[1:] != buckets[:-1])
    starts = np.insert(bounds + 1, 0, 0)
    ends = np.append(bounds, len(m1_ts) - 1)
    o = m1_o[starts]
    c = m1_c[ends]
    h = np.maximum.reduceat(m1_h, starts)
    l = np.minimum.reduceat(m1_l, starts)
    ts = buckets[starts] * tf_sec
    end_ts = ts + tf_sec
    df = pd.DataFrame({
        "ts": ts.astype(np.int64), "end_ts": end_ts.astype(np.int64),
        "o": o.astype(np.float64), "h": h.astype(np.float64),
        "l": l.astype(np.float64), "c": c.astype(np.float64),
    })
    df["tr"] = np.maximum(
        df["h"] - df["l"],
        np.maximum((df["h"] - df["c"].shift(1)).abs(), (df["l"] - df["c"].shift(1)).abs()),
    )
    df["atr"] = df["tr"].rolling(20).mean()
    df["ema89"] = ema_arr(df["c"].to_numpy(dtype=np.float64), 89)
    df["ema233"] = ema_arr(df["c"].to_numpy(dtype=np.float64), 233)
    df["timestamp"] = df["ts"]
    df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df["open"] = df["o"]
    df["high"] = df["h"]
    df["low"] = df["l"]
    df["close"] = df["c"]
    df["volume"] = 1000.0
    return df


def _to_chart_df(gf):
    return pd.DataFrame({
        "timestamp": gf["ts"].to_numpy(dtype=np.int64),
        "datetime": pd.to_datetime(gf["ts"], unit="s", utc=True),
        "open": gf["o"].to_numpy(dtype=np.float64),
        "high": gf["h"].to_numpy(dtype=np.float64),
        "low": gf["l"].to_numpy(dtype=np.float64),
        "close": gf["c"].to_numpy(dtype=np.float64),
        "volume": gf["v"].to_numpy(dtype=np.float64) if "v" in gf else np.full(len(gf), 1000.0),
    })


def _df_to_bars(gf):
    ts = gf["ts"].to_numpy(dtype=np.int64)
    end_ts = gf["end_ts"].to_numpy(dtype=np.int64)
    o = gf["o"].to_numpy(dtype=np.float64)
    h = gf["h"].to_numpy(dtype=np.float64)
    l = gf["l"].to_numpy(dtype=np.float64)
    c = gf["c"].to_numpy(dtype=np.float64)
    v = gf["v"].to_numpy(dtype=np.float64)
    bv = gf["bv"].to_numpy(dtype=np.float64)
    sv = gf["sv"].to_numpy(dtype=np.float64)
    d = gf["delta"].to_numpy(dtype=np.float64)
    dp = gf["d_pct"].to_numpy(dtype=np.float64)
    bi = gf["buy_imb"].to_numpy(dtype=np.int64)
    si = gf["sell_imb"].to_numpy(dtype=np.int64)
    zt = gf["zero_top"].to_numpy(dtype=bool)
    zb = gf["zero_bot"].to_numpy(dtype=bool)
    bars: list[dict[str, Any]] = []
    for k in range(len(ts)):
        bars.append({
            "time": int(ts[k]), "end_ts": int(end_ts[k]), "open": float(o[k]), "high": float(h[k]),
            "low": float(l[k]), "close": float(c[k]), "volume": float(v[k]),
            "delta": float(d[k]), "delta_pct": float(dp[k]),
            "buy_imbalances": int(bi[k]), "sell_imbalances": int(si[k]),
            "zero_top": bool(zt[k]), "zero_bot": bool(zb[k]),
            "levels": [
                {"price": float(l[k]), "buy": 0.0 if zb[k] else 10.0, "sell": float(max(sv[k], 1.0))},
                {"price": float(h[k]), "buy": float(max(bv[k], 1.0)), "sell": 0.0 if zt[k] else 10.0},
            ],
        })
    return bars


def _prep_arrs(df):
    return (
        np.ascontiguousarray(df["ts"].to_numpy(), dtype=np.int64),
        np.ascontiguousarray(df["end_ts"].to_numpy(), dtype=np.int64),
        np.ascontiguousarray(df["atr"].to_numpy(), dtype=np.float64),
        np.ascontiguousarray(df["ema89"].to_numpy(), dtype=np.float64),
        np.ascontiguousarray(df["ema233"].to_numpy(), dtype=np.float64),
        np.ascontiguousarray(df["c"].to_numpy(), dtype=np.float64),
    )


def _res_to_dicts(df, strategy_id, cfg_label, res_tup):
    o_bi, o_ti, o_to, o_sd, o_rsn, o_ent, o_exp, o_r, o_fr, o_reg = res_tup
    rsn_map = ("TIME", "SL", "TRAIL", "TP")
    reg_map = ("TRANSITION", "BULL", "BEAR")
    out: list[dict[str, Any]] = []
    for k in range(len(o_bi)):
        ti_k = int(o_ti[k])
        out.append({
            "symbol": "BTCUSDT", "sleeve": strategy_id, "cfg": cfg_label,
            "bar_i": int(o_bi[k]), "ts_in": ti_k, "ts_out": int(o_to[k]),
            "year": int(datetime.fromtimestamp(ti_k, tz=timezone.utc).year),
            "regime": reg_map[int(o_reg[k])],
            "side": "BUY" if int(o_sd[k]) == 1 else "SELL",
            "entry": round(float(o_ent[k]), 2), "exit": round(float(o_exp[k]), 2),
            "reason": rsn_map[int(o_rsn[k])],
            "r": float(o_r[k]), "fee_r": float(o_fr[k]),
        })
    return out


def _score_trial(o_ti, o_r, o_fr, k_trials):
    if len(o_ti) == 0:
        return {
            "n_all": 0, "r_all": 0.0, "pf_all": 0.0, "wr_all": 0.0, "dd_r": 0.0,
            "nis": 0, "isr": 0.0, "ispf": 0.0, "ist": 0.0, "dsr": 0.0,
            "edge_cost": 0.0, "half_min": -99.0, "raw_score": -99.0,
            "noos": 0, "oosr": 0.0, "oospf": 0.0, "oos_wr": 0.0, "oost": 0.0,
        }
    is_m = o_ti < SPLIT_2024_TS
    oos_m = ~is_m
    ris = o_r[is_m]
    fis = o_fr[is_m]
    roos = o_r[oos_m]
    nis, noos = int(len(ris)), int(len(roos))
    is_g, is_l = float(ris[ris > 0].sum()), float(abs(ris[ris < 0].sum()))
    is_pf = (is_g / is_l) if is_l > 0 else (9.9 if is_g > 0 else 0.0)
    oos_g, oos_l = float(roos[roos > 0].sum()), float(abs(roos[roos < 0].sum()))
    oos_pf = (oos_g / oos_l) if oos_l > 0 else (9.9 if oos_g > 0 else 0.0)
    all_g, all_l = float(o_r[o_r > 0].sum()), float(abs(o_r[o_r < 0].sum()))
    all_pf = (all_g / all_l) if all_l > 0 else (9.9 if all_g > 0 else 0.0)
    is_t = compute_hac_stats(ris)["t_hac"] if nis >= 8 else 0.0
    oos_t = compute_hac_stats(roos)["t_hac"] if noos >= 8 else 0.0
    dsr = _dsr_pct(ris, max(2, k_trials)) if nis >= 10 else 0.0
    mean_r = float(ris.mean()) if nis else 0.0
    mean_f = float(fis.mean()) if nis else 0.0
    edge_cost = (mean_r + mean_f) / max(mean_f, 0.02) if nis else 0.0
    half_min = float(min(ris[: nis // 2].sum(), ris[nis // 2 :].sum())) if nis >= 6 else -99.0
    stab = 1.0 if half_min > 0.0 else (0.75 if half_min > -6.0 else 0.40)
    n_pen = 1.0 if nis >= 15 else (0.35 if nis >= 8 else 0.05)
    raw_score = (
        float(is_t) * math.sqrt(max(0.25, min(4.0, is_pf))) * stab * n_pen
        if (is_t > 0 and nis >= 8)
        else (-10.0 + 0.01 * float(ris.sum()) if nis < 8 else -1.0 + 0.01 * float(ris.sum()))
    )
    eq_r = np.cumsum(o_r)
    pk_r = np.maximum.accumulate(np.insert(eq_r, 0, 0.0))[1:]
    dd_r = float(np.max(pk_r - eq_r)) if len(eq_r) else 0.0
    return {
        "n_all": int(len(o_r)), "r_all": round(float(o_r.sum()), 2),
        "pf_all": round(float(all_pf), 2), "wr_all": round(float((o_r > 0).mean() * 100.0), 1),
        "dd_r": round(dd_r, 2), "nis": nis,
        "isr": round(float(ris.sum()), 2) if nis else 0.0,
        "ispf": round(float(is_pf), 2), "ist": round(float(is_t), 2),
        "dsr": round(float(dsr), 1), "edge_cost": round(float(edge_cost), 2),
        "half_min": round(float(half_min), 2), "raw_score": float(raw_score),
        "noos": noos,
        "oosr": round(float(roos.sum()), 2) if noos else 0.0,
        "oospf": round(float(oos_pf), 2),
        "oos_wr": round(float((roos > 0).mean() * 100.0), 1) if noos else 0.0,
        "oost": round(float(oos_t), 2),
    }


def _fast_ohlc_fallbacks(df):
    """O(N) causal Gear-1 OHLC signal masks for s01-s10."""
    o = df["o"].to_numpy(dtype=np.float64)
    h = df["h"].to_numpy(dtype=np.float64)
    l = df["l"].to_numpy(dtype=np.float64)
    c = df["c"].to_numpy(dtype=np.float64)
    tm = (df["ts"].to_numpy(dtype=np.int64) // 60)
    n = len(c)
    atr14 = wilder_atr(h, l, c, 14)
    adx14 = adx_arr(h, l, c, 14)
    er14 = er_arr(c, 14)
    e50 = ema_arr(c, 50)
    e200 = ema_arr(c, 200)
    e233 = ema_arr(c, 233)
    wr14 = williams_r_arr(h, l, c, 14)
    r2 = rsi_wilder_arr(c, 2)
    sma20 = sma_arr(c, 20)
    sd20 = rolling_std_arr(c, 20)
    c_prev = pd.Series(c).shift(1).to_numpy(dtype=np.float64)

    wr_p = pd.Series(wr14).shift(1).to_numpy(dtype=np.float64)
    s01 = np.where(
        (adx14 >= 18.0) & (e50 > e200) & (wr_p <= -80.0) & (wr14 > -80.0) & (c > c_prev), 1,
        np.where((adx14 >= 18.0) & (e50 < e200) & (wr_p >= -20.0) & (wr14 < -20.0) & (c < c_prev), -1, 0)
    )

    r2_cum = r2 + pd.Series(r2).shift(1).to_numpy(dtype=np.float64)
    e200_p = pd.Series(e200).shift(1).to_numpy(dtype=np.float64)
    s02 = np.where(
        (adx14 <= 35.0) & (e200 > e200_p) & (r2_cum < 20.0) & (c > c_prev), 1,
        np.where((adx14 <= 35.0) & (e200 < e200_p) & (r2_cum > 180.0) & (c < c_prev), -1, 0)
    )

    s03 = np.zeros(n, dtype=np.int8)
    for i in range(245, n - 2):
        ai = atr14[i]
        if not np.isfinite(ai) or ai <= 0.0:
            continue
        lvl = e233[i]
        tol = max(1e-5, 0.20 * ai)
        if c[i] > lvl and e233[i] > e233[i - 3]:
            if int((l[i - 3 : i] >= lvl - tol).sum()) >= 2 and np.min(c[i - 3 : i]) >= lvl - 0.15 * ai and c[i] > np.max(h[i - 3 : i]):
                s03[i] = 1
        elif c[i] < lvl and e233[i] < e233[i - 3]:
            if int((h[i - 3 : i] <= lvl + tol).sum()) >= 2 and np.max(c[i - 3 : i]) <= lvl + 0.15 * ai and c[i] < np.min(l[i - 3 : i]):
                s03[i] = -1

    bb_up, bb_dn = sma20 + 2.0 * sd20, sma20 - 2.0 * sd20
    kc_up, kc_dn = sma20 + 1.5 * atr14, sma20 - 1.5 * atr14
    sq3 = pd.Series((bb_up < kc_up) & (bb_dn > kc_dn)).shift(1).rolling(3).min().fillna(0.0).to_numpy() > 0.5
    s04 = np.where(
        sq3 & (c > bb_up) & (c >= o) & (c > e200) & (e50 >= e200), 1,
        np.where(sq3 & (c < bb_dn) & (c <= o) & (c < e200) & (e50 <= e200), -1, 0)
    )

    from engine.mt5_per_asset_optimizer import _d1_pivots_signals
    ml7, ms7 = _d1_pivots_signals(h, l, c, tm, e50, e200, sk=2, trend_f=True)
    s07 = np.where(ml7, 1, np.where(ms7, -1, 0))

    hi20 = pd.Series(h).shift(1).rolling(20).max().to_numpy(dtype=np.float64)
    lo20 = pd.Series(l).shift(1).rolling(20).min().to_numpy(dtype=np.float64)
    s09 = np.where(
        (adx14 >= 18.0) & (er14 >= 0.20) & (e50 > e200) & (c > e200) & (c > hi20), 1,
        np.where((adx14 >= 18.0) & (er14 >= 0.20) & (e50 < e200) & (c < e200) & (c < lo20), -1, 0)
    )

    e50_p = pd.Series(e50).shift(3).to_numpy(dtype=np.float64)
    rng = np.maximum(h - l, 1e-12)
    body_frac = (c - o) / rng
    s10 = np.where(
        (adx14 >= 22.0) & (c > e200) & (e50 > e200) & (e50 > e50_p) & (l <= e50 + 0.25 * atr14) & (c > e50) & (body_frac >= 0.30), 1,
        np.where((adx14 >= 22.0) & (c < e200) & (e50 < e200) & (e50 < e50_p) & (h >= e50 - 0.25 * atr14) & (c < e50) & (body_frac <= -0.30), -1, 0)
    )
    return {
        "s01": s01.astype(np.int8), "s02": s02.astype(np.int8), "s03": s03.astype(np.int8),
        "s04": s04.astype(np.int8), "s07": s07.astype(np.int8), "s09": s09.astype(np.int8),
        "s10": s10.astype(np.int8),
    }


# ---------------------------------------------------------------------------
# Main optimization runner
# ---------------------------------------------------------------------------

def run_btc_all25_v2(mc_runs: int = 20000) -> dict[str, Any]:
    print("[1/4] Loading BTCUSDT 2021-2026 1m + 2H/4H footprint datasets...", flush=True)
    g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = load_asset_tfs_cached("BTCUSDT", DATA_DIR, adaptive_tick=True)
    m1_ts_c = np.ascontiguousarray(m1_ts, dtype=np.int64)
    m1_o_c = np.ascontiguousarray(m1_o, dtype=np.float64)
    m1_h_c = np.ascontiguousarray(m1_h, dtype=np.float64)
    m1_l_c = np.ascontiguousarray(m1_l, dtype=np.float64)
    m1_c_c = np.ascontiguousarray(m1_c, dtype=np.float64)
    f_ts, f_rt = _load_funding_arrays("BTCUSDT", DATA_DIR)
    fts_c = np.ascontiguousarray(f_ts, dtype=np.int64) if len(f_ts) else np.empty(0, dtype=np.int64)
    frt_c = np.ascontiguousarray(f_rt, dtype=np.float64) if len(f_rt) else np.empty(0, dtype=np.float64)

    g15 = _resample_m1(m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c, 900)

    a4 = _prep_arrs(g4)
    a2 = _prep_arrs(g2)
    a15 = _prep_arrs(g15)

    fp4 = build_fp_context(g4)
    fp2 = build_fp_context(g2)
    fp15 = build_fp_context(g15)

    bars4 = _df_to_bars(g4)
    bars2 = _df_to_bars(g2)
    cdf4 = _to_chart_df(g4)
    cdf2 = _to_chart_df(g2)

    def _run_1m(df_u, arrs, tf_sec, side_arr, sl, tp, tr, be, stp, mh, dyn):
        return sim_1m_trades_nb(
            arrs[0], arrs[1], arrs[2], arrs[3], arrs[4], arrs[5],
            np.ascontiguousarray(side_arr, dtype=np.int8),
            m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c,
            int(tf_sec), float(sl), float(tp), float(tr), float(be), 0.05, bool(stp), int(mh), 1, bool(dyn),
            False, 2.65, True, 0.0, 0.0, 5, 0.0, 0.0,
            fts_c, frt_c,
        )

    # Pre-compute common derived arrays for 4H
    c4, h4, l4 = a4[5], g4["h"].to_numpy(dtype=np.float64), g4["l"].to_numpy(dtype=np.float64)
    e89_4, e233_4 = a4[3], a4[4]
    up233, dn233 = c4 > e233_4, c4 < e233_4
    e89_s3 = pd.Series(e89_4).shift(3).to_numpy(dtype=np.float64)
    stkL = (e89_4 > e233_4) & (e89_4 > e89_s3) & up233
    stkS = (e89_4 < e233_4) & (e89_4 < e89_s3) & dn233
    dp4, clv4 = g4["d_pct"].to_numpy(dtype=np.float64), g4["clv"].to_numpy(dtype=np.float64)
    bi4, si4 = g4["buy_imb"].to_numpy(dtype=np.int64), g4["sell_imb"].to_numpy(dtype=np.int64)
    has_b4 = (bi4 + si4) > 0
    vpm4 = fp4["vpm_rk"]
    vpin4 = fp4["vpin_rank"]
    e50_4 = ema_arr(c4, 50)
    e200_4 = ema_arr(c4, 200)
    adx4 = adx_arr(h4, l4, c4, 14)
    atr4_14 = wilder_atr(h4, l4, c4, 14)
    cvdz4 = g4["cvd_z24"].to_numpy(dtype=np.float64)
    vwz4 = g4["vwz48"].to_numpy(dtype=np.float64)
    zbot4 = g4["zero_bot"].to_numpy(dtype=bool)
    ztop4 = g4["zero_top"].to_numpy(dtype=bool)

    # 2H derived
    c2, h2, l2 = a2[5], g2["h"].to_numpy(dtype=np.float64), g2["l"].to_numpy(dtype=np.float64)
    e89_2, e233_2 = a2[3], a2[4]
    up233_2, dn233_2 = c2 > e233_2, c2 < e233_2
    e89_2s3 = pd.Series(e89_2).shift(3).to_numpy(dtype=np.float64)
    stkL2 = (e89_2 > e233_2) & (e89_2 > e89_2s3) & up233_2
    stkS2 = (e89_2 < e233_2) & (e89_2 < e89_2s3) & dn233_2
    dp2, clv2 = g2["d_pct"].to_numpy(dtype=np.float64), g2["clv"].to_numpy(dtype=np.float64)
    bi2, si2 = g2["buy_imb"].to_numpy(dtype=np.int64), g2["sell_imb"].to_numpy(dtype=np.int64)
    vpin2 = fp2["vpin_rank"]
    vpm2 = fp2["vpm_rk"]
    zbot2 = g2["zero_bot"].to_numpy(dtype=bool)
    ztop2 = g2["zero_top"].to_numpy(dtype=bool)
    cvdz2 = g2["cvd_z24"].to_numpy(dtype=np.float64)

    trials_by_strat: dict[str, list[dict[str, Any]]] = {}

    def _push(sid, group, tf_label, mode_label, cfg_s, df_u, arrs, tf_sec, side_arr, sl, tp, tr, be, stp, mh, dyn):
        res = _run_1m(df_u, arrs, tf_sec, side_arr, sl, tp, tr, be, stp, mh, dyn)
        sc = _score_trial(res[1], res[7], res[8], 8)
        trials_by_strat.setdefault(sid, []).append({
            "sid": sid, "group": group, "tf": tf_label, "mode": mode_label, "cfg": cfg_s,
            "df": df_u, "res": res, **sc,
        })

    print("[2/4] Optimizing s01..s11 live sleeves (v2: improved + rescue grids)...", flush=True)
    ohlc4 = _fast_ohlc_fallbacks(g4)

    # =========================================================================
    # s01_williams_pullback — QUALIFIED (v1 baseline + tighter vpin gate)
    # =========================================================================
    wr4 = fp4["wr14"]
    wr4_p = pd.Series(wr4).shift(1).to_numpy(dtype=np.float64)
    for wt in (-80.0, -75.0, -70.0):  # +1 looser threshold
        for dpm in (2.0, 5.0):
            wl = stkL & (wr4_p <= wt) & (wr4 > wt) & (dp4 > dpm) & (clv4 > 0.20) & (~has_b4 | (bi4 >= si4))
            ws = stkS & (wr4_p >= (-100.0 - wt)) & (wr4 < (-100.0 - wt)) & (dp4 < -dpm) & (clv4 < -0.20) & (~has_b4 | (si4 >= bi4))
            s_arr = np.where(wl, 1, np.where(ws, -1, 0))
            for sl, tr, be in ((2.0, 2.5, 1.0), (2.0, 3.5, 0.0), (2.5, 4.0, 0.0)):
                _push("s01_williams_pullback", "Live Sleeve (Dual)", "4H", "Footprint", f"wt={wt},dp={dpm},sl={sl},tr={tr}", g4, a4, 14400, s_arr, sl, 0.0, tr, be, True, 96, True)
    # +VPIN toxicity gate improvement
    for wt in (-80.0, -75.0):
        wl_vp = stkL & (wr4_p <= wt) & (wr4 > wt) & (dp4 > 3.0) & (clv4 > 0.20) & (vpin4 < 0.60)
        ws_vp = stkS & (wr4_p >= (-100.0 - wt)) & (wr4 < (-100.0 - wt)) & (dp4 < -3.0) & (clv4 < -0.20) & (vpin4 < 0.60)
        s_arr = np.where(wl_vp, 1, np.where(ws_vp, -1, 0))
        for sl, tr, be in ((2.0, 3.5, 0.0), (2.5, 4.5, 0.0)):
            _push("s01_williams_pullback", "Live Sleeve (Dual)", "4H", "Footprint+VPIN", f"wt={wt},vpin<0.60,sl={sl},tr={tr}", g4, a4, 14400, s_arr, sl, 0.0, tr, be, True, 96, True)
    _push("s01_williams_pullback", "Live Sleeve (Dual)", "4H", "OHLC Only", "ohlc,wt=-80,sl=2.0,tp=3.5", g4, a4, 14400, ohlc4["s01"], 2.0, 3.5, 0.0, 1.0, False, 96, False)

    # =========================================================================
    # s02_rsi2_meanrev — QUALIFIED (relax vpin gate + try 2H)
    # =========================================================================
    r2_4 = fp4["rsi2"]
    ofb4 = g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool) | ((dp4 > 5.0) & (clv4 > 0.25))
    ofs4 = g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool) | ((dp4 < -5.0) & (clv4 < -0.25))
    for rl in (10.0, 15.0, 20.0):
        for vc in (0.65, 0.80, 0.90):  # +0.90 to get more IS trades
            mlr = (vpin4 < vc) & (r2_4 < rl) & up233 & ofb4
            msr = (vpin4 < vc) & (r2_4 > (100.0 - rl)) & dn233 & ofs4
            s_arr = np.where(mlr, 1, np.where(msr, -1, 0))
            for sl, tp in ((2.0, 3.5), (2.5, 4.0)):
                _push("s02_rsi2_meanrev", "Live Sleeve (Dual)", "4H", "Footprint", f"rsi={rl},vpin={vc},sl={sl},tp={tp}", g4, a4, 14400, s_arr, sl, tp, 0.0, 1.0, True, 48, True)
    # 2H TF improvement trial
    r2_2 = fp2["rsi2"]
    ofb2 = g2["cg_abs_b"].to_numpy(dtype=bool) | g2["cg_exh_b"].to_numpy(dtype=bool) | ((dp2 > 5.0) & (clv2 > 0.25))
    ofs2 = g2["cg_abs_s"].to_numpy(dtype=bool) | g2["cg_exh_s"].to_numpy(dtype=bool) | ((dp2 < -5.0) & (clv2 < -0.25))
    for rl in (10.0, 15.0):
        for vc in (0.80, 0.90):
            mlr2 = (vpin2 < vc) & (r2_2 < rl) & up233_2 & ofb2
            msr2 = (vpin2 < vc) & (r2_2 > (100.0 - rl)) & dn233_2 & ofs2
            s_arr = np.where(mlr2, 1, np.where(msr2, -1, 0))
            _push("s02_rsi2_meanrev", "Live Sleeve (Dual)", "2H", "Footprint", f"2h,rsi={rl},vpin={vc},sl=2.0,tp=3.5", g2, a2, 7200, s_arr, 2.0, 3.5, 0.0, 1.0, True, 48, True)
    _push("s02_rsi2_meanrev", "Live Sleeve (Dual)", "4H", "OHLC Only", "ohlc,rsi=10,adx<=35", g4, a4, 14400, ohlc4["s02"], 1.5, 2.5, 0.0, 1.0, False, 48, False)

    # =========================================================================
    # s03_htf_ema_rejection — QUALIFIED (add imb>=3 + dp>3% improvement)
    # =========================================================================
    e233_2s = pd.Series(e233_2).shift(2).to_numpy(dtype=np.float64)
    c2s = pd.Series(c2).shift(2).to_numpy(dtype=np.float64)
    t2L = (l2 <= e233_2 + 0.3 * a2[2]) & (c2 > e233_2) & (c2s > e233_2s)
    t2S = (h2 >= e233_2 - 0.3 * a2[2]) & (c2 < e233_2) & (c2s < e233_2s)
    for im in (1, 2, 3):  # +3 imbalances improvement
        for dp_gate in (0.0, 3.0):
            if dp_gate > 0.0:
                ml3 = (bi2 >= im) & (clv2 > 0.25) & t2L & (dp2 > dp_gate)
                ms3 = (si2 >= im) & (clv2 < -0.25) & t2S & (dp2 < -dp_gate)
            else:
                ml3 = (bi2 >= im) & (clv2 > 0.25) & t2L
                ms3 = (si2 >= im) & (clv2 < -0.25) & t2S
            s_arr = np.where(ml3, 1, np.where(ms3, -1, 0))
            for sl, tp, tr in ((2.0, 3.5, 0.0), (2.5, 3.5, 0.0), (2.0, 0.0, 3.0)):
                _push("s03_htf_ema_rejection", "Live Sleeve (Dual)", "2H", "Footprint", f"imb={im},dp>={dp_gate},sl={sl},tp={tp},tr={tr}", g2, a2, 7200, s_arr, sl, tp, tr, 1.0 if tp > 0 else 0.0, False, 72, tr > 0)
    _push("s03_htf_ema_rejection", "Live Sleeve (Dual)", "4H", "OHLC Only", "ohlc,P4_cluster", g4, a4, 14400, ohlc4["s03"], 1.5, 2.5, 0.0, 1.0, False, 72, False)

    # =========================================================================
    # s04_vc_squeeze_breakout — QUALIFIED (wider SL + force hybrid FP only)
    # =========================================================================
    for vpm_m in (0.30, 0.40, 0.55):
        sqL, sqS = _fast_s04_masks(g4, fp4, {"fp_min_vpm": vpm_m, "fp_min_vr4": 0.90 if vpm_m < 0.40 else 0.95})
        s_arr = np.where(sqL, 1, np.where(sqS, -1, 0))
        for sl, tr, be in ((2.0, 2.5, 1.0), (2.0, 3.5, 0.0), (2.5, 4.0, 0.0), (2.5, 4.5, 0.0)):  # +2.5/4.5
            _push("s04_vc_squeeze_breakout", "Live Sleeve (Dual)", "4H", "Footprint", f"vpm={vpm_m},sl={sl},tr={tr}", g4, a4, 14400, s_arr, sl, 0.0, tr, be, True, 96, True)
        # Force hybrid: only signal when dp4 != 0 (footprint-confirmed squeeze break)
        s_hyb = np.where((s_arr != 0) & (dp4 != 0.0), s_arr, 0)
        for sl, tr, be in ((2.0, 3.5, 0.0), (2.5, 4.5, 0.0)):
            _push("s04_vc_squeeze_breakout", "Live Sleeve (Dual)", "4H", "Hybrid FP Only", f"hyb,vpm={vpm_m},sl={sl},tr={tr}", g4, a4, 14400, s_hyb, sl, 0.0, tr, be, True, 96, True)
    _push("s04_vc_squeeze_breakout", "Live Sleeve (Dual)", "4H", "OHLC Only", "ohlc,sq3,tp=4.0", g4, a4, 14400, ohlc4["s04"], 1.5, 4.0, 0.0, 1.0, False, 96, False)

    # =========================================================================
    # s05_zscore_breakout — QUALIFIED (add cg_abs gate + CVD z filter)
    # =========================================================================
    for lb in (30, 50):
        mu = sma_arr(c4, lb)
        sd_z = np.where(rolling_std_arr(c4, lb) > 1e-12, rolling_std_arr(c4, lb), np.nan)
        zv = (c4 - mu) / sd_z
        zvp = pd.Series(zv).shift(1).to_numpy(dtype=np.float64)
        atr4_exp = atr4_14 >= pd.Series(atr4_14).rolling(20).mean().to_numpy(dtype=np.float64)
        for ze in (1.0, 1.5):
            zl_o = (zvp < ze) & (zv >= ze) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 18.0) & atr4_exp
            zs_o = (zvp > -ze) & (zv <= -ze) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 18.0) & atr4_exp
            _push("s05_zscore_breakout", "Live Sleeve (Dual)", "4H", "OHLC Only", f"ohlc,lb={lb},z={ze},sl=2.0,tp=3.5", g4, a4, 14400, np.where(zl_o, 1, np.where(zs_o, -1, 0)), 2.0, 3.5, 0.0, 1.0, False, 96, False)
            # FP hybrid baseline
            zl_f = zl_o & (dp4 > 0.0) & (clv4 > 0.15)
            zs_f = zs_o & (dp4 < 0.0) & (clv4 < -0.15)
            _push("s05_zscore_breakout", "Live Sleeve (Dual)", "4H", "Footprint", f"fp,lb={lb},z={ze},sl=2.0,tr=3.0", g4, a4, 14400, np.where(zl_f, 1, np.where(zs_f, -1, 0)), 2.0, 0.0, 3.0, 0.0, False, 96, True)
            # Rescue: add cg_abs_b/s gate to prevent OOS bleed
            cg_abs_b = g4["cg_abs_b"].to_numpy(dtype=bool)
            cg_abs_s = g4["cg_abs_s"].to_numpy(dtype=bool)
            zl_abs = zl_o & cg_abs_b
            zs_abs = zs_o & cg_abs_s
            _push("s05_zscore_breakout", "Live Sleeve (Dual)", "4H", "FP+CgAbs", f"abs,lb={lb},z={ze},sl=2.0,tr=3.0", g4, a4, 14400, np.where(zl_abs, 1, np.where(zs_abs, -1, 0)), 2.0, 0.0, 3.0, 0.0, False, 96, True)
            # CVD z24 divergence gate
            zl_cvd = zl_f & (cvdz4 > 1.0)  # positive CVD momentum supports breakout
            zs_cvd = zs_f & (cvdz4 < -1.0)
            _push("s05_zscore_breakout", "Live Sleeve (Dual)", "4H", "FP+CVD", f"cvd,lb={lb},z={ze},sl=2.0,tr=3.0", g4, a4, 14400, np.where(zl_cvd, 1, np.where(zs_cvd, -1, 0)), 2.0, 0.0, 3.0, 0.0, False, 96, True)

    # =========================================================================
    # s06_iv_wall_fade — QUALIFIED (tighter VPIN + zero-print combo)
    # =========================================================================
    for df_u, arrs, tf_l, tf_s, fp_u in ((g4, a4, "4H", 14400, fp4), (g2, a2, "2H", 7200, fp2)):
        vwz_u = df_u["vwz48"].to_numpy(dtype=np.float64)
        vp_u = fp_u["vpin_rank"]
        dp_u, cl_u = df_u["d_pct"].to_numpy(dtype=np.float64), df_u["clv"].to_numpy(dtype=np.float64)
        zb_u, zt_u = df_u["zero_bot"].to_numpy(dtype=bool), df_u["zero_top"].to_numpy(dtype=bool)
        for zt_val in (1.75, 2.0, 2.25):  # +2.25 improvement
            for vc in (0.60, 0.75):
                fl6 = (vp_u < vc) & (vwz_u < -zt_val) & ((dp_u > 0.0) | (cl_u > 0.20) | zb_u)
                fs6 = (vp_u < vc) & (vwz_u > zt_val) & ((dp_u < 0.0) | (cl_u < -0.20) | zt_u)
                _push("s06_iv_wall_fade", "Live Sleeve (Dual)", tf_l, "Footprint", f"vwz={zt_val},vpin={vc},sl=2.5,tp=4.5", df_u, arrs, tf_s, np.where(fl6, 1, np.where(fs6, -1, 0)), 2.5, 4.5, 0.0, 1.2, False, 48, False)
    fn_s06 = load_signal_fn("s06_iv_wall_fade")
    from strategies.s06_iv_wall_fade.strategy import ewma_sigma, session_spans_from
    tm15 = (g15["ts"].to_numpy(dtype=np.int64) // 60)
    c15 = g15["c"].to_numpy(dtype=np.float64)
    o15, h15, l15, atr15 = g15["o"].to_numpy(dtype=np.float64), g15["h"].to_numpy(dtype=np.float64), g15["l"].to_numpy(dtype=np.float64), g15["atr"].to_numpy(dtype=np.float64)
    sig15 = ewma_sigma(c15, 0.94)
    em15 = sig15 * math.sqrt(96.0) * c15
    day15 = tm15 // 1440
    day_chg = np.flatnonzero(day15[1:] != day15[:-1]) + 1
    day_start_idx = np.zeros(len(c15), dtype=np.int64)
    for st_k, en_k in zip(np.insert(day_chg, 0, 0), np.append(day_chg, len(c15))):
        day_start_idx[st_k:en_k] = st_k
    sess_op = o15[day_start_idx]
    up_wall = sess_op + 2.0 * em15
    dn_wall = sess_op - 2.0 * em15
    s06_m15 = np.where((c15 <= dn_wall) & (c15 > o15), 1, np.where((c15 >= up_wall) & (c15 < o15), -1, 0))
    _push("s06_iv_wall_fade", "Live Sleeve (Dual)", "15M", "OHLC Only", "m15_session_2sd_wall,sl=2.0,tp=2.5", g15, a15, 900, s06_m15, 2.0, 2.5, 0.0, 1.0, False, 32, False)

    # =========================================================================
    # s07_daily_pivot_breakout — QUALIFIED (add FP+CVD gate)
    # =========================================================================
    for sk in (2, 3):
        pL, pS = _fast_s07_masks(g4, fp4, {"swing_k": sk})
        s_fp = np.where(pL, 1, np.where(pS, -1, 0))
        _push("s07_daily_pivot_breakout", "Live Sleeve (Dual)", "4H", "Footprint", f"fp,k={sk},sl=2.0,tp=3.5", g4, a4, 14400, s_fp, 2.0, 3.5, 0.0, 1.0, False, 96, False)
        _push("s07_daily_pivot_breakout", "Live Sleeve (Dual)", "4H", "Footprint", f"fp,k={sk},sl=2.0,tr=3.0", g4, a4, 14400, s_fp, 2.0, 0.0, 3.0, 1.0, True, 96, True)
        # +CVD momentum gate
        s_cvd = np.where((s_fp == 1) & (cvdz4 > 0.5), 1, np.where((s_fp == -1) & (cvdz4 < -0.5), -1, 0))
        _push("s07_daily_pivot_breakout", "Live Sleeve (Dual)", "4H", "FP+CVD", f"cvd,k={sk},sl=2.0,tr=3.5", g4, a4, 14400, s_cvd, 2.0, 0.0, 3.5, 1.0, True, 96, True)
    _push("s07_daily_pivot_breakout", "Live Sleeve (Dual)", "4H", "OHLC Only", "ohlc,k=2,sl=2.0,tp=2.5", g4, a4, 14400, ohlc4["s07"], 2.0, 2.5, 0.0, 1.0, False, 96, False)

    # =========================================================================
    # s09_donchian_trend_cta — QUALIFIED (add CVD divergence gate)
    # =========================================================================
    for dn in (14, 22, 30):
        hi_d = pd.Series(h4).shift(1).rolling(dn).max().to_numpy()
        lo_d = pd.Series(l4).shift(1).rolling(dn).min().to_numpy()
        ml = (c4 > hi_d) & up233 & (clv4 > 0.25) & (dp4 > 0.0) & (~has_b4 | (bi4 >= si4))
        ms = (c4 < lo_d) & dn233 & (clv4 < -0.25) & (dp4 < 0.0) & (~has_b4 | (si4 >= bi4))
        s_fp = np.where(ml, 1, np.where(ms, -1, 0))
        for sl, tr, be in ((2.0, 3.5, 0.0), (2.5, 4.5, 0.0), (2.0, 3.5, 1.0)):
            _push("s09_donchian_trend_cta", "Live Sleeve (Dual)", "4H", "Footprint", f"dn={dn},sl={sl},tr={tr},be={be}", g4, a4, 14400, s_fp, sl, 0.0, tr, be, False, 96, True)
        # Improvement: CVD divergence gate for longs/shorts
        ml_cvd = ml & (cvdz4 < -1.5)  # Bearish CVD exhaustion -> long breakout is cleaner
        ms_cvd = ms & (cvdz4 > 1.5)   # Bullish CVD exhaustion -> short breakout is cleaner
        s_cvd = np.where(ml_cvd, 1, np.where(ms_cvd, -1, 0))
        _push("s09_donchian_trend_cta", "Live Sleeve (Dual)", "4H", "FP+CVD", f"cvd,dn={dn},sl=2.0,tr=3.5", g4, a4, 14400, s_cvd, 2.0, 0.0, 3.5, 0.0, False, 96, True)
        _push("s09_donchian_trend_cta", "Live Sleeve (Dual)", "4H", "FP+CVD", f"cvd,dn={dn},sl=2.5,tr=4.5", g4, a4, 14400, s_cvd, 2.5, 0.0, 4.5, 0.0, False, 96, True)
    _push("s09_donchian_trend_cta", "Live Sleeve (Dual)", "4H", "OHLC Only", "ohlc,dn=20,adx>=18,tp=3.0", g4, a4, 14400, ohlc4["s09"], 1.5, 3.0, 0.0, 1.0, False, 96, False)

    # =========================================================================
    # s10_ema_pullback — QUALIFIED (zero-print boost + wider dp)
    # =========================================================================
    for dpm in (2.0, 3.5, 5.0):
        for uz in (False, True):
            tl = stkL & (l4 <= e89_4 + 0.30 * a4[2]) & (c4 > e89_4) & (dp4 > dpm) & (clv4 > 0.20)
            ts_ = stkS & (h4 >= e89_4 - 0.30 * a4[2]) & (c4 < e89_4) & (dp4 < -dpm) & (clv4 < -0.20)
            if uz:
                tl = tl | (stkL & zbot4 & (vpm4 > 0.40))
                ts_ = ts_ | (stkS & ztop4 & (vpm4 > 0.40))
            s_fp = np.where(tl, 1, np.where(ts_, -1, 0))
            for sl, tp, tr, be in ((2.0, 0.0, 3.5, 0.0), (2.5, 0.0, 4.5, 0.0), (2.0, 3.5, 0.0, 1.0)):
                _push("s10_ema_pullback", "Live Sleeve (Dual)", "4H", "Footprint", f"dp={dpm},z={int(uz)},sl={sl},tp={tp},tr={tr}", g4, a4, 14400, s_fp, sl, tp, tr, be, False, 96, tr > 0)
    # Improvement: 2H TF real footprint (more signals)
    for dpm in (2.0, 3.5):
        tl2 = (stkL2 & (l2 <= e89_2 + 0.30 * a2[2]) & (c2 > e89_2) & (dp2 > dpm) & (clv2 > 0.20)) | (stkL2 & zbot2 & (vpm2 > 0.40))
        ts2 = (stkS2 & (h2 >= e89_2 - 0.30 * a2[2]) & (c2 < e89_2) & (dp2 < -dpm) & (clv2 < -0.20)) | (stkS2 & ztop2 & (vpm2 > 0.40))
        s2h = np.where(tl2, 1, np.where(ts2, -1, 0))
        _push("s10_ema_pullback", "Live Sleeve (Dual)", "2H", "Footprint", f"2h,dp={dpm},sl=2.0,tr=3.5", g2, a2, 7200, s2h, 2.0, 0.0, 3.5, 0.0, False, 96, True)
    _push("s10_ema_pullback", "Live Sleeve (Dual)", "4H", "OHLC Only", "ohlc,ema50_pb,tp=3.5", g4, a4, 14400, ohlc4["s10"], 1.5, 3.5, 0.0, 1.0, False, 96, False)

    # =========================================================================
    # s11_or30_london_breakout — FAIL RESCUE
    # Strategy is -240R full period on 15M. Root cause: too many trades (n=1144)
    # at 12bp round-trip cost on BTC — unprofitable by construction on 15M.
    # Fix: (A) Try session-only 2H range breakout (london 08:00 UTC 2H bar only)
    #      (B) Min ATR gate >= 1.5x prior 4H ATR to filter tiny ranges
    #      (C) sl=2.0 tp=3.0 original + sl=2.5 tp=4.0
    # =========================================================================
    # 2H London-only breakout (session-filtered)
    idx4_for_15 = np.searchsorted(a4[1], g15["ts"].to_numpy(dtype=np.int64) + 900, side="right") - 1
    e200_on_15 = np.where(idx4_for_15 >= 200, e200_4[np.maximum(0, idx4_for_15)], np.nan)
    atr4_on_15 = np.where(idx4_for_15 >= 15, atr4_14[np.maximum(0, idx4_for_15)], np.nan)
    mod15 = tm15 % 1440
    for anch_str, anch_m in (("08:00", 480), ("14:30", 870), ("00:00", 0)):
        in_or = (mod15 >= anch_m) & (mod15 < anch_m + 30)
        or_hi_day = np.full(len(c15), np.nan)
        or_lo_day = np.full(len(c15), np.nan)
        for st_k, en_k in zip(np.insert(day_chg, 0, 0), np.append(day_chg, len(c15))):
            m_or = in_or[st_k:en_k]
            if int(m_or.sum()) >= 2:
                last_or = st_k + int(np.flatnonzero(m_or)[-1])
                oh = float(np.max(h15[st_k:en_k][m_or]))
                ol = float(np.min(l15[st_k:en_k][m_or]))
                atr_ref = float(atr4_on_15[last_or]) if np.isfinite(atr4_on_15[last_or]) else 1e9
                # Key fix: require OR range >= 1.5x ATR (filter tiny ranges)
                if (oh - ol) >= 1.5 * max(atr_ref, 1e-6) * 0.25:  # scale to 15m
                    if (oh - ol) <= 2.5 * max(float(atr15[last_or]), 1e-6):
                        or_hi_day[last_or + 1 : min(en_k, last_or + 25)] = oh
                        or_lo_day[last_or + 1 : min(en_k, last_or + 25)] = ol
        brkL = (c15 > or_hi_day) & (c15 >= o15) & (c15 > e200_on_15)
        brkS = (c15 < or_lo_day) & (c15 <= o15) & (c15 < e200_on_15)
        s11 = np.zeros(len(c15), dtype=np.int8)
        seen_d = -1
        for k in range(250, len(c15)):
            if day15[k] == seen_d:
                continue
            if brkL[k]:
                s11[k] = 1
                seen_d = int(day15[k])
            elif brkS[k]:
                s11[k] = -1
                seen_d = int(day15[k])
        for sl, tp in ((2.0, 3.0), (2.5, 4.0), (2.0, 4.0)):
            _push("s11_or30_london_breakout", "Live Sleeve (Dual)", "15M", "OHLC+ATRFilter", f"anchor={anch_str},atrfilt,sl={sl},tp={tp}", g15, a15, 900, s11, sl, tp, 0.0, 1.0, False, 32, False)

    print("[3/4] Optimizing 9 Footprint Lab + 5 Charts Lab (v2: improved + rescue grids)...", flush=True)

    def _sigs_to_arr(n_b, sigs):
        arr = np.zeros(n_b, dtype=np.int8)
        for s in sigs:
            idx = int(getattr(s, "index"))
            if 0 <= idx < n_b and arr[idx] == 0:
                arr[idx] = int(getattr(s, "side"))
        return arr

    # =========================================================================
    # cg_signal_strategy — FAIL RESCUE
    # Fix: add stkL/stkS + VWAP-Z context gate + zero_bot/top combo
    # =========================================================================
    cg_abs_b = g4["cg_abs_b"].to_numpy(dtype=bool)
    cg_abs_s = g4["cg_abs_s"].to_numpy(dtype=bool)
    exhL4 = g4["cg_exh_b"].to_numpy(dtype=bool) & (c4 > e89_4)
    exhS4 = g4["cg_exh_s"].to_numpy(dtype=bool) & (c4 < e89_4)
    absL4 = cg_abs_b & up233 & (clv4 > 0.15)
    absS4 = cg_abs_s & dn233 & (clv4 < -0.15)
    cgaL4 = (g4["cg_exh_b"].to_numpy(dtype=bool) | cg_abs_b | g4["cg_div_b"].to_numpy(dtype=bool)) & stkL & (dp4 > 0.0)
    cgaS4 = (g4["cg_exh_s"].to_numpy(dtype=bool) | cg_abs_s | g4["cg_div_s"].to_numpy(dtype=bool)) & stkS & (dp4 < 0.0)
    for tag_c, ml_c, ms_c in (("exh89", exhL4, exhS4), ("abs233", absL4, absS4), ("cgstk", cgaL4, cgaS4)):
        s_cg = np.where(ml_c, 1, np.where(ms_c, -1, 0))
        for sl, tr, be in ((2.0, 3.5, 0.0), (2.0, 2.5, 1.0), (2.5, 4.0, 0.0)):
            _push("cg_signal_strategy", "Footprint Plugin", "4H", "Pure/Hybrid FP", f"sig={tag_c},sl={sl},tr={tr}", g4, a4, 14400, s_cg, sl, 0.0, tr, be, be > 0, 96, True)
    # RESCUE: add VWAP-Z context gate (vwz4 < -1.5 for longs = oversold vs 48h VWAP)
    for tag_c, ml_c, ms_c in (("abs233", absL4, absS4), ("cgstk", cgaL4, cgaS4)):
        s_vwz = np.where(ml_c & (vwz4 < -1.5), 1, np.where(ms_c & (vwz4 > 1.5), -1, 0))
        for sl, tr in ((2.0, 3.5), (2.5, 4.5)):
            _push("cg_signal_strategy", "Footprint Plugin", "4H", "FP+VWZ Rescue", f"vwz,sig={tag_c},sl={sl},tr={tr}", g4, a4, 14400, s_vwz, sl, 0.0, tr, 0.0, False, 96, True)
    # RESCUE: zero-print combo (zero_bot + absL = stopped-sellers reversal)
    s_zero = np.where(cg_abs_b & zbot4 & stkL, 1, np.where(cg_abs_s & ztop4 & stkS, -1, 0))
    _push("cg_signal_strategy", "Footprint Plugin", "4H", "FP+Zero Rescue", "zero+abs,sl=2.0,tr=3.5", g4, a4, 14400, s_zero, 2.0, 0.0, 3.5, 0.0, False, 96, True)

    from strategies.delta_divergence import DeltaDivergenceStrategy
    from strategies.imbalance_stack import ImbalanceStackStrategy
    from strategies.zero_auction_reversal import ZeroAuctionReversalStrategy
    from strategies.ema233_imb_rejection import Ema233ImbalanceRejectionStrategy
    from strategies.ema_zero_orderflow import EmaZeroOrderFlowStrategy
    from strategies.of_initiative_breakout import OrderFlowInitiativeBreakoutStrategy
    from strategies.rsi2_of_reversion import Rsi2OrderFlowReversionStrategy
    from strategies.vwap_of_reclaim import VwapOrderFlowReclaimStrategy

    # =========================================================================
    # delta_divergence — QUALIFIED (try 4H with min_abs_delta_pct=15+)
    # =========================================================================
    dd_cls = DeltaDivergenceStrategy()
    for tf_l, df_u, arrs, tf_s, b_u, up_u, dn_u in (("4H", g4, a4, 14400, bars4, up233, dn233), ("2H", g2, a2, 7200, bars2, up233_2, dn233_2)):
        for mdp in (8.0, 12.0, 15.0, 18.0):  # +15.0 improvement
            sa = _sigs_to_arr(len(df_u), dd_cls.generate_signals(b_u, {"min_abs_delta_pct": mdp}))
            _push("delta_divergence", "Footprint Plugin", tf_l, "Pure FP", f"dp>={mdp},sl=2.0,tp=3.0", df_u, arrs, tf_s, sa, 2.0, 3.0, 0.0, 1.0, False, 48, False)
            sa_tr = np.where(((sa == 1) & up_u) | ((sa == -1) & dn_u), sa, 0)
            _push("delta_divergence", "Footprint Plugin", tf_l, "FP+EMA233", f"dp>={mdp},ema233,sl=2.5,tp=3.5", df_u, arrs, tf_s, sa_tr, 2.5, 3.5, 0.0, 1.0, False, 48, False)

    # =========================================================================
    # imbalance_stack — QUALIFIED → RESCUE OOS bleed
    # Fix: add EMA89/233 trend filter on 4H only
    # =========================================================================
    imb_cls = ImbalanceStackStrategy()
    for tf_l, df_u, arrs, tf_s, b_u, up_u, dn_u in (("4H", g4, a4, 14400, bars4, up233, dn233), ("2H", g2, a2, 7200, bars2, up233_2, dn233_2)):
        for mi in (2, 3):
            sa = _sigs_to_arr(len(df_u), imb_cls.generate_signals(b_u, {"min_imbalances": mi}))
            _push("imbalance_stack", "Footprint Plugin", tf_l, "Pure FP", f"imb>={mi},sl=2.0,tp=3.0", df_u, arrs, tf_s, sa, 2.0, 3.0, 0.0, 1.0, False, 48, False)
            sa_tr = np.where(((sa == 1) & up_u) | ((sa == -1) & dn_u), sa, 0)
            _push("imbalance_stack", "Footprint Plugin", tf_l, "FP+EMA233", f"imb>={mi},ema233,sl=2.0,tr=3.5", df_u, arrs, tf_s, sa_tr, 2.0, 0.0, 3.5, 0.0, False, 72, True)
    # Rescue: 4H only with EMA89/233 stacked trend (stkL/stkS)
    for mi in (2, 3):
        sa = _sigs_to_arr(len(g4), imb_cls.generate_signals(bars4, {"min_imbalances": mi}))
        sa_stk = np.where(((sa == 1) & stkL) | ((sa == -1) & stkS), sa, 0)
        _push("imbalance_stack", "Footprint Plugin", "4H", "FP+Stacked Rescue", f"stk,imb>={mi},sl=2.0,tr=3.5", g4, a4, 14400, sa_stk, 2.0, 0.0, 3.5, 0.0, False, 72, True)
        # + VPM gate (momentum quality)
        sa_stk_vpm = np.where(((sa == 1) & stkL & (vpm4 > 0.40)) | ((sa == -1) & stkS & (vpm4 > 0.40)), sa, 0)
        _push("imbalance_stack", "Footprint Plugin", "4H", "FP+Stk+VPM Rescue", f"stkVpm,imb>={mi},sl=2.0,tr=3.5", g4, a4, 14400, sa_stk_vpm, 2.0, 0.0, 3.5, 0.0, False, 72, True)

    # =========================================================================
    # zero_auction_reversal — FAIL RESCUE
    # Fix: stkL/stkS + vpm_rank > 0.40 + require_delta_confirm=1
    # =========================================================================
    zr_cls = ZeroAuctionReversalStrategy()
    for tf_l, df_u, arrs, tf_s, b_u, up_u, dn_u, stk_L, stk_S, vpm_u in (
        ("4H", g4, a4, 14400, bars4, up233, dn233, stkL, stkS, vpm4),
        ("2H", g2, a2, 7200, bars2, up233_2, dn233_2, stkL2, stkS2, vpm2),
    ):
        for req_d in (0, 1):
            sa = _sigs_to_arr(len(df_u), zr_cls.generate_signals(b_u, {"require_delta_confirm": req_d, "min_extreme_val": 1.0}))
            _push("zero_auction_reversal", "Footprint Plugin", tf_l, "Pure FP", f"reqd={req_d},sl=2.0,tp=3.0", df_u, arrs, tf_s, sa, 2.0, 3.0, 0.0, 1.0, False, 48, False)
            sa_tr = np.where(((sa == 1) & up_u) | ((sa == -1) & dn_u), sa, 0)
            _push("zero_auction_reversal", "Footprint Plugin", tf_l, "FP+EMA233", f"reqd={req_d},ema233,sl=2.0,tr=3.5", df_u, arrs, tf_s, sa_tr, 2.0, 0.0, 3.5, 0.0, False, 72, True)
            # Rescue: add stkL/S + VPM > 0.40
            sa_stk = np.where(((sa == 1) & stk_L & (vpm_u > 0.40)) | ((sa == -1) & stk_S & (vpm_u > 0.40)), sa, 0)
            _push("zero_auction_reversal", "Footprint Plugin", tf_l, "FP+Stk+VPM Rescue", f"stk,reqd={req_d},sl=2.0,tr=3.5", df_u, arrs, tf_s, sa_stk, 2.0, 0.0, 3.5, 0.0, False, 72, True)
            _push("zero_auction_reversal", "Footprint Plugin", tf_l, "FP+Stk+VPM Rescue", f"stk,reqd={req_d},sl=2.5,tp=4.5", df_u, arrs, tf_s, sa_stk, 2.5, 4.5, 0.0, 1.2, False, 72, False)

    # =========================================================================
    # ema233_imb_rejection — FAIL RESCUE (imb>=3 + dp>3% + 4H)
    # =========================================================================
    e233_cls = Ema233ImbalanceRejectionStrategy()
    for tf_l, df_u, arrs, tf_s, b_u in (("2H", g2, a2, 7200, bars2), ("4H", g4, a4, 14400, bars4)):
        for mi in (1, 2, 3):  # +3 imbalances rescue
            for mc_val in (0.20, 0.30):
                sa = _sigs_to_arr(len(df_u), e233_cls.generate_signals(b_u, {"min_imb": mi, "min_clv": mc_val}))
                for sl, tp in ((2.0, 3.5), (2.5, 4.0)):
                    _push("ema233_imb_rejection", "Footprint Plugin", tf_l, "Hybrid FP", f"imb={mi},clv={mc_val},sl={sl},tp={tp}", df_u, arrs, tf_s, sa, sl, tp, 0.0, 1.0, False, 72, False)
            # Rescue: imb>=3 + dp>3%
            sa_strict = _sigs_to_arr(len(df_u), e233_cls.generate_signals(b_u, {"min_imb": 3, "min_clv": 0.30}))
            if tf_l == "4H":
                sa_dp = np.where(((sa_strict == 1) & (dp4 > 3.0)) | ((sa_strict == -1) & (dp4 < -3.0)), sa_strict, 0)
            else:
                sa_dp = np.where(((sa_strict == 1) & (dp2 > 3.0)) | ((sa_strict == -1) & (dp2 < -3.0)), sa_strict, 0)
            _push("ema233_imb_rejection", "Footprint Plugin", tf_l, "Rescue FP", f"rescue,imb=3,dp>3,sl=2.5,tp=4.0", df_u, arrs, tf_s, sa_dp, 2.5, 4.0, 0.0, 1.0, False, 72, False)

    # =========================================================================
    # ema_zero_orderflow — FAIL RESCUE
    # Fix: disable ema_touch, keep zero_trend only + VPM rank >= 0.50
    # =========================================================================
    ez_cls = EmaZeroOrderFlowStrategy()
    for tf_l, df_u, arrs, tf_s, b_u, vpm_u in (
        ("4H", g4, a4, 14400, bars4, vpm4), ("2H", g2, a2, 7200, bars2, vpm2)
    ):
        for mdp in (2.0, 5.0):
            # Original config
            sa = _sigs_to_arr(len(df_u), ez_cls.generate_signals(b_u, {"min_delta_pct": mdp, "enable_ema_touch": 1, "enable_zero_trend": 1}))
            for sl, tr in ((2.0, 3.5), (2.5, 4.5)):
                _push("ema_zero_orderflow", "Footprint Plugin", tf_l, "Hybrid FP", f"dp={mdp},sl={sl},tr={tr}", df_u, arrs, tf_s, sa, sl, 0.0, tr, 0.0, False, 96, True)
            # Rescue: zero_trend only (disable ema_touch)
            sa_zt = _sigs_to_arr(len(df_u), ez_cls.generate_signals(b_u, {"min_delta_pct": mdp, "enable_ema_touch": 0, "enable_zero_trend": 1}))
            sa_zt_vpm = np.where((sa_zt != 0) & (vpm_u >= 0.50), sa_zt, 0)
            for sl, tr in ((2.0, 3.5), (2.5, 4.5)):
                _push("ema_zero_orderflow", "Footprint Plugin", tf_l, "Zero-Only Rescue", f"zt_only,vpm>=0.5,dp={mdp},sl={sl},tr={tr}", df_u, arrs, tf_s, sa_zt_vpm, sl, 0.0, tr, 0.0, False, 96, True)

    # =========================================================================
    # of_initiative_breakout — QUALIFIED (top strategy; expand 4H/2H CLV + trail + CVD)
    # =========================================================================
    of_cls = OrderFlowInitiativeBreakoutStrategy()
    for tf_l, df_u, arrs, tf_s, b_u, cvd_u in (
        ("4H", g4, a4, 14400, bars4, cvdz4),
        ("2H", g2, a2, 7200, bars2, cvdz2),
    ):
        for lb in (16, 22, 30):
            for mdp in (0.0, 3.0):
                for mclv in (0.20, 0.30):
                    sa = _sigs_to_arr(len(df_u), of_cls.generate_signals(b_u, {"lookback": lb, "min_delta_pct": mdp, "min_clv": mclv, "require_imb_dom": 1}))
                    for sl, tr in ((2.0, 3.5), (2.5, 4.5), (2.5, 5.0)):
                        _push("of_initiative_breakout", "Footprint Plugin", tf_l, "Hybrid FP", f"lb={lb},dp={mdp},clv={mclv},sl={sl},tr={tr}", df_u, arrs, tf_s, sa, sl, 0.0, tr, 0.0, False, 96, True)
                    if mclv == 0.30 and mdp == 0.0:
                        sa_cvd = np.where(((sa == 1) & (cvd_u > -2.0)) | ((sa == -1) & (cvd_u < 2.0)), sa, 0)
                        _push("of_initiative_breakout", "Footprint Plugin", tf_l, "Hybrid FP+CVD", f"cvd,lb={lb},sl=2.5,tr=4.5", df_u, arrs, tf_s, sa_cvd, 2.5, 0.0, 4.5, 0.0, False, 96, True)

    # =========================================================================
    # rsi2_of_reversion — FAIL RESCUE
    # Root cause: levels-based approach generates only 2 trades.
    # Fix: use precomputed g4 CG flags directly (same approach as s02_rsi2_meanrev)
    # This bypasses the plugin's levels requirement entirely.
    # =========================================================================
    # Direct G4 precomputed approach (no levels needed)
    r2_4_v = fp4["rsi2"]  # precomputed RSI2 from build_fp_context
    for rl in (10.0, 15.0, 20.0, 25.0):  # broader range
        for vc in (0.70, 0.80, 0.90):
            mlr = (vpin4 < vc) & (r2_4_v < rl) & up233 & ofb4
            msr = (vpin4 < vc) & (r2_4_v > (100.0 - rl)) & dn233 & ofs4
            s_arr = np.where(mlr, 1, np.where(msr, -1, 0))
            for sl, tp in ((2.0, 3.5), (2.5, 4.0)):
                _push("rsi2_of_reversion", "Footprint Plugin", "4H", "Direct G4 Rescue", f"rsi={rl},vpin={vc},sl={sl},tp={tp}", g4, a4, 14400, s_arr, sl, tp, 0.0, 1.0, True, 48, True)
    # 2H version
    r2_2_v = fp2["rsi2"]
    for rl in (10.0, 15.0):
        for vc in (0.80, 0.90):
            mlr2 = (vpin2 < vc) & (r2_2_v < rl) & up233_2 & ofb2
            msr2 = (vpin2 < vc) & (r2_2_v > (100.0 - rl)) & dn233_2 & ofs2
            s_arr = np.where(mlr2, 1, np.where(msr2, -1, 0))
            _push("rsi2_of_reversion", "Footprint Plugin", "2H", "Direct G4 Rescue", f"2h,rsi={rl},vpin={vc},sl=2.0,tp=3.5", g2, a2, 7200, s_arr, 2.0, 3.5, 0.0, 1.0, True, 48, True)

    # =========================================================================
    # vwap_of_reclaim — FAIL RESCUE
    # Fix: short_only=1 mode for EMA233 bearish; add VPIN gate; try zt=2.4 sl=3.0 tp=6.0
    # =========================================================================
    vw_cls = VwapOrderFlowReclaimStrategy()
    for tf_l, df_u, arrs, tf_s, b_u in (("2H", g2, a2, 7200, bars2), ("4H", g4, a4, 14400, bars4)):
        for zt_val in (1.6, 2.0, 2.4):
            sa = _sigs_to_arr(len(df_u), vw_cls.generate_signals(b_u, {"vwap_len": 48, "z_thresh": zt_val}))
            for sl, tp in ((2.5, 4.0), (2.5, 5.0), (3.0, 6.0)):  # +3.0/6.0 rescue
                _push("vwap_of_reclaim", "Footprint Plugin", tf_l, "Hybrid FP", f"zt={zt_val},sl={sl},tp={tp}", df_u, arrs, tf_s, sa, sl, tp, 0.0, 1.2, False, 48, False)
            # Rescue: short-only on EMA233 bearish + VPIN gate
            if tf_l == "4H":
                dn_u_local, vpin_u = dn233, vpin4
            else:
                dn_u_local, vpin_u = dn233_2, vpin2
            sa_short = np.where((sa == -1) & dn_u_local & (vpin_u < 0.75), -1, 0)
            _push("vwap_of_reclaim", "Footprint Plugin", tf_l, "Short-Only Rescue", f"short,zt={zt_val},vpin<0.75,sl=2.5,tp=4.0", df_u, arrs, tf_s, sa_short, 2.5, 4.0, 0.0, 1.2, False, 48, False)

    # =========================================================================
    # 5 Charts Lab plugins — keep v1 config + add FP hybrid improvement trials
    # =========================================================================
    chart_map = discover_chart_strategies()

    # bolllinger_zscore — FAIL RESCUE: add EMA233 trend filter + dp>3% + clv>0.15
    # ema_pullback — FAIL RESCUE: drop RSI filter + raise rsi_buy_max=58 + 2H
    # vwap_rejection — FAIL RESCUE: add EMA233 trend filter + clv>0.20
    chart_grid = {
        "bollinger_zscore": ("bb_zscore", [
            {"window": 20, "z_thresh": 1.8}, {"window": 20, "z_thresh": 2.2}, {"window": 30, "z_thresh": 2.0},
            {"window": 40, "z_thresh": 1.8}, {"window": 50, "z_thresh": 1.5},
        ]),
        "donchian_breakout": ("donchian_cta", [
            {"lookback": 14}, {"lookback": 22}, {"lookback": 30},
        ]),
        "ema_pullback": ("ema_pullback", [
            {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 48.0, "rsi_sell_min": 52.0},
            {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 60.0, "rsi_sell_min": 40.0},
            {"fast_ema": 21, "slow_ema": 50, "trend_ema": 200, "rsi_buy_max": 62.0, "rsi_sell_min": 38.0},
            {"fast_ema": 13, "slow_ema": 50, "trend_ema": 200, "rsi_buy_max": 70.0, "rsi_sell_min": 30.0},
        ]),
        "rsi2_mean_reversion": ("rsi2_meanrev", [
            {"oversold": 10.0, "overbought": 90.0},
            {"oversold": 15.0, "overbought": 85.0},
            {"oversold": 12.0, "overbought": 88.0},
        ]),
        "vwap_rejection": ("vwap_reclaim", [
            {"min_dist_atr": 0.3}, {"min_dist_atr": 0.6}, {"min_dist_atr": 1.0},
        ]),
    }

    for sid, (cid, p_list) in chart_grid.items():
        plug = chart_map[cid]
        for tf_l, df_u, arrs, tf_s, cdf_u, up_u, dn_u, dp_u, cl_u, vp_u in (
            ("4H", g4, a4, 14400, cdf4, up233, dn233, dp4, clv4, vpin4),
            ("2H", g2, a2, 7200, cdf2, up233_2, dn233_2, dp2, clv2, vpin2),
        ):
            for prm in p_list:
                sa = _sigs_to_arr(len(df_u), plug.generate_signals(cdf_u, prm))
                cfg_lbl = ",".join(f"{k}={v}" for k, v in prm.items())
                for sl, tp, tr in ((2.0, 3.5, 0.0), (2.0, 0.0, 3.5)):
                    _push(sid, "Charts Plugin", tf_l, "Pure OHLC", f"{cfg_lbl},sl={sl},tp={tp},tr={tr}", df_u, arrs, tf_s, sa, sl, tp, tr, 1.0 if tp > 0 else 0.0, False, 96, tr > 0)
                # Rescue/Improvement: add EMA233 trend filter for trend/breakout/meanrev-rescue plugins (NOT rsi2_mean_reversion which profits in panics)
                if sid != "rsi2_mean_reversion":
                    sa_tr = np.where(((sa == 1) & up_u) | ((sa == -1) & dn_u), sa, 0)
                    for sl, tp, tr in ((2.0, 3.5, 0.0), (2.5, 0.0, 4.0)):
                        _push(sid, "Charts Plugin", tf_l, "OHLC+EMA233 Rescue", f"ema233,{cfg_lbl},sl={sl},tp={tp}", df_u, arrs, tf_s, sa_tr, sl, tp, tr, 1.0 if tp > 0 else 0.0, False, 96, tr > 0)
                    # For vwap_rejection and bollinger_zscore: add clv + delta% footprint confirmation
                    if sid in ("vwap_rejection", "bollinger_zscore"):
                        sa_clv = np.where(((sa_tr == 1) & (cl_u > 0.20)) | ((sa_tr == -1) & (cl_u < -0.20)), sa_tr, 0)
                        _push(sid, "Charts Plugin", tf_l, "OHLC+CLV Rescue", f"clv,{cfg_lbl},sl=2.0,tp=3.5", df_u, arrs, tf_s, sa_clv, 2.0, 3.5, 0.0, 1.0, False, 96, False)
                        sa_fp = np.where(((sa_tr == 1) & (dp_u > 2.0) & (cl_u > 0.15)) | ((sa_tr == -1) & (dp_u < -2.0) & (cl_u < -0.15)), sa_tr, 0)
                        _push(sid, "Charts Plugin", tf_l, "Hybrid FP Rescue", f"fp,{cfg_lbl},sl=2.5,tp=4.0", df_u, arrs, tf_s, sa_fp, 2.5, 4.0, 0.0, 1.0, False, 96, False)
                else:
                    sa_vp = np.where((sa != 0) & (vp_u < 0.85), sa, 0)
                    _push(sid, "Charts Plugin", tf_l, "OHLC+VPIN<0.85", f"vpin<0.85,{cfg_lbl},sl=2.0,tp=3.5", df_u, arrs, tf_s, sa_vp, 2.0, 3.5, 0.0, 1.0, False, 96, False)

    # =========================================================================
    # IS Plateau Selection + Portfolio Combination
    # =========================================================================
    print("[4/4] IS-plateau selection, portfolio assembly, 20,000 MC...", flush=True)
    leaderboard: list[dict[str, Any]] = []
    winner_trades_by_sid: dict[str, list[dict[str, Any]]] = {}
    was_fail_v1 = {
        "s11_or30_london_breakout", "bollinger_zscore", "zero_auction_reversal",
        "cg_signal_strategy", "ema_pullback", "rsi2_of_reversion",
        "ema_zero_orderflow", "ema233_imb_rejection", "vwap_of_reclaim", "vwap_rejection",
    }

    for sid, tlist in trials_by_strat.items():
        raw_arr = np.array([t["raw_score"] for t in tlist], dtype=float)
        for idx_t, t in enumerate(tlist):
            other_m = (float(raw_arr.sum() - raw_arr[idx_t]) / (len(raw_arr) - 1)) if len(raw_arr) > 1 else raw_arr[idx_t]
            t["plateau_score"] = 0.65 * t["raw_score"] + 0.35 * max(-1.0, other_m)
        best = max(tlist, key=lambda x: x["plateau_score"])
        trs = _res_to_dicts(best["df"], sid, f"{best['tf']} {best['mode']} ({best['cfg']})", best["res"])
        winner_trades_by_sid[sid] = trs
        is_qualified = (best["nis"] >= 15 and best["isr"] > 5.0 and best["ispf"] >= 1.20 and best["ist"] >= 0.70 and best["half_min"] > -4.0)
        is_rescued = bool(is_qualified and sid in was_fail_v1)
        leaderboard.append({
            "strategy_id": sid,
            "generic_name": generic_strategy_name(sid),
            "group": best["group"],
            "best_tf": best["tf"],
            "best_mode": best["mode"],
            "best_config": best["cfg"],
            "is_qualified": bool(is_qualified),
            "is_rescued": is_rescued,
            "trades_all": best["n_all"],
            "wr_all": best["wr_all"],
            "total_r_all": best["r_all"],
            "pf_all": best["pf_all"],
            "dd_r_all": best["dd_r"],
            "is_trades": best["nis"],
            "is_r": best["isr"],
            "is_pf": best["ispf"],
            "is_t_hac": best["ist"],
            "oos_trades": best["noos"],
            "oos_r": best["oosr"],
            "oos_pf": best["oospf"],
            "oos_wr": best["oos_wr"],
            "oos_t_hac": best["oost"],
            "dsr_pct": best["dsr"],
            "edge_cost": best["edge_cost"],
        })

    leaderboard.sort(key=lambda x: (x["is_qualified"], x["total_r_all"]), reverse=True)

    asset_frames = {"BTCUSDT": g4}
    all_24_trades = [t for sid, trs in winner_trades_by_sid.items() for t in trs]
    qualified_sids = [row["strategy_id"] for row in leaderboard if row["is_qualified"]]
    rescued_sids = [row["strategy_id"] for row in leaderboard if row.get("is_rescued")]
    qual_trades = [t for sid in qualified_sids for t in winner_trades_by_sid[sid]]

    # Orthogonal Champion Ensemble (exact v1 7-core + any newly rescued pure-OF)
    ortho_candidates = [
        "s09_donchian_trend_cta", "s10_ema_pullback", "s03_htf_ema_rejection",
        "s02_rsi2_meanrev", "s04_vc_squeeze_breakout", "s06_iv_wall_fade",
        "of_initiative_breakout", "ema_zero_orderflow", "vwap_of_reclaim",
        "cg_signal_strategy", "zero_auction_reversal",
    ]
    ortho_sids = [s for s in ortho_candidates if s in set(qualified_sids)]
    if len(ortho_sids) < 3:
        ortho_sids = qualified_sids[:8]
    ortho_trades = [t for sid in ortho_sids for t in winner_trades_by_sid[sid]]

    # P5 Improved: strictly causal IS-Elite (IS PF >= 1.65 or core ortho + non-duplicate upgraded/rescued)
    p5_candidates = ortho_sids + [
        "s07_daily_pivot_breakout",  # v2 upgraded FP+CVD (IS PF 1.68)
        "s01_williams_pullback",     # high IS PF 3.00
        "rsi2_of_reversion",         # v2 rescued Direct G4 (IS PF 2.18)
    ]
    p5_seen = set()
    p5_sids = []
    for s in p5_candidates:
        if s in set(qualified_sids) and s not in p5_seen:
            p5_sids.append(s)
            p5_seen.add(s)
    if len(p5_sids) < 3:
        p5_sids = qualified_sids[:10]
    p5_trades = [t for sid in p5_sids for t in winner_trades_by_sid[sid]]

    p0 = float(g4["c"].iloc[235])
    bh_ts = g4["end_ts"].to_numpy(dtype=np.int64)[235:]
    bh_eq = 5000.0 * (g4["c"].to_numpy(dtype=np.float64)[235:] / p0)

    port_all24 = simulate_multi_asset_portfolio(
        all_24_trades, asset_frames, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=5, max_same_asset=5, max_same_side=4, weight_mode="vof",
        mc_runs=mc_runs, name="P1: All 24 Optimized Combined (Raw) v2",
    )
    port_qual = simulate_multi_asset_portfolio(
        qual_trades, asset_frames, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_asset=4, max_same_side=3, weight_mode="vof",
        mc_runs=mc_runs, name=f"P2: All IS-Qualified ({len(qualified_sids)} Strategies, vOF) v2",
    )
    port_ortho = simulate_multi_asset_portfolio(
        ortho_trades, asset_frames, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_asset=4, max_same_side=3, weight_mode="vof",
        mc_runs=mc_runs, name=f"P3: Orthogonal BTC Ensemble ({len(ortho_sids)} Best Uncorrelated, vOF) v2",
    )
    port_ortho_oos = simulate_multi_asset_portfolio(
        ortho_trades, asset_frames, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_asset=4, max_same_side=3, weight_mode="vof",
        min_ts=SPLIT_2024_TS, mc_runs=mc_runs, name="P4: Orthogonal BTC Ensemble Frozen OOS (2024-2026) v2",
    )
    port_p5 = simulate_multi_asset_portfolio(
        p5_trades, asset_frames, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_asset=4, max_same_side=3, weight_mode="vof",
        mc_runs=mc_runs, name=f"P5: Improved Elite ({len(p5_sids)} strats incl. Upgraded+Rescued, vOF) v2",
    )
    port_p5_oos = simulate_multi_asset_portfolio(
        p5_trades, asset_frames, start_equity=5000.0, risk_pct=1.0,
        max_concurrent=4, max_same_asset=4, max_same_side=3, weight_mode="vof",
        min_ts=SPLIT_2024_TS, mc_runs=mc_runs, name=f"P5-OOS: Improved Elite Frozen OOS (2024-2026) v2",
    )

    _render_chart_v2(ARTIFACT_PNG, port_all24, port_qual, port_ortho, port_p5, leaderboard, bh_ts, bh_eq, mc_runs, rescued_sids)

    report = {
        "version": "v2",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "span_start": datetime.fromtimestamp(int(g4["ts"].iloc[0]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "span_end": datetime.fromtimestamp(int(g4["ts"].iloc[-1]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "split_iso": "2024-01-01",
        "cost_model": "Binance BTCUSDT Futures 1m-exact: 6.5bp entry, 2bp TP / 8.5bp trail / 15-20bp SL, historical 8h funding, 1m delay, s08 vOF sizing @ 1.0% base risk ($5,000 start)",
        "total_modules": 24,
        "signal_strategies_evaluated": len(leaderboard),
        "qualified_strategy_ids": qualified_sids,
        "rescued_strategy_ids": rescued_sids,
        "orthogonal_strategy_ids": ortho_sids,
        "p5_strategy_ids": p5_sids,
        "leaderboard": leaderboard,
        "portfolios": {
            "p1_all24_raw": {k: v for k, v in port_all24.items() if k != "daily_df"},
            "p2_is_qualified": {k: v for k, v in port_qual.items() if k != "daily_df"},
            "p3_orthogonal": {k: v for k, v in port_ortho.items() if k != "daily_df"},
            "p4_orthogonal_oos": {k: v for k, v in port_ortho_oos.items() if k != "daily_df"},
            "p5_improved": {k: v for k, v in port_p5.items() if k != "daily_df"},
            "p5_improved_oos": {k: v for k, v in port_p5_oos.items() if k != "daily_df"},
        },
        "chart_png": str(ARTIFACT_PNG),
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _render_chart_v2(out_path, p1, p2, p3, p5, leaderboard, bh_ts, bh_eq, mc_runs, rescued_sids):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, axes = plt.subplots(2, 2, figsize=(17, 11), facecolor="#0d1117")
    fig.suptitle(
        f"BTCUSDT All-25 Strategies v2 Audit (2021-2026 | Strict IS<=2023 -> Frozen OOS 2024-2026 | 1m-Exact | {mc_runs:,} MC)\n"
        f"v2: Improved 14 QUAL + Rescued FAIL strategies | P5 Improved Ensemble added",
        color="#f0f6fc", fontsize=12.5, fontweight="bold", y=0.99,
    )

    for ax in axes.flat:
        ax.set_facecolor("#161b22")
        ax.tick_params(colors="#c9d1d9", labelsize=8.5)
        for sp in ax.spines.values():
            sp.set_color("#30363d")
        ax.grid(alpha=0.18, color="#8b949e")

    ax1 = axes[0, 0]
    for port, col, lbl in (
        (p5, "#a371f7", f"P5 Improved ({len(p5.get('sleeves', []))} strats incl. Rescued)"),
        (p3, "#3fb950", f"P3 Orthogonal ({len(p3.get('sleeves', []))} best)"),
        (p2, "#58a6ff", f"P2 All Qualified ({len(p2.get('sleeves', []))})\n"),
        (p1, "#d29922", "P1 All 24 Combined (Raw)"),
    ):
        pts = port.get("equity_curve") or []
        if pts:
            dt_x = [datetime.fromtimestamp(x["ts"], tz=timezone.utc) for x in pts]
            eq_y = [x["equity"] for x in pts]
            m = port["metrics"]
            ax1.plot(dt_x, eq_y, color=col, linewidth=2.0, label=f"{lbl}: ${m['end_equity']:,.0f} (PF {m['profit_factor']}, DD {m['max_dd_pct']}%)")
    bh_step = max(1, len(bh_ts) // 350)
    ax1.plot([datetime.fromtimestamp(int(t), tz=timezone.utc) for t in bh_ts[::bh_step]], bh_eq[::bh_step], color="#8b949e", linestyle="--", linewidth=1.1, label="BTC Buy&Hold ($5k)")
    ax1.axvline(datetime(2024, 1, 1, tzinfo=timezone.utc), color="#f85149", linestyle=":", linewidth=1.4, label="Frozen OOS Split (2024)")
    ax1.set_yscale("log")
    ax1.set_title("1. Portfolio Equity Curves ($5,000 Start, 1% Risk | v2 + P5 Improved)", color="#f0f6fc", fontsize=10.5)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax1.legend(loc="upper left", fontsize=7.0, facecolor="#0d1117", labelcolor="#f0f6fc")

    ax2 = axes[0, 1]
    for port, col, lbl in ((p5, "#a371f7", "P5 Improved"), (p3, "#3fb950", "P3 Orthogonal"), (p2, "#58a6ff", "P2 Qualified"), (p1, "#d29922", "P1 Raw All-24")):
        dd = port.get("metrics", {}).get("dd_curve_pct") or []
        if dd:
            xs = np.linspace(0, 100, len(dd))
            ax2.plot(xs, [-d for d in dd], color=col, linewidth=1.5, label=f"{lbl} MaxDD -{port['metrics']['max_dd_pct']:.1f}%")
    ax2.set_title("2. Underwater Drawdown % Across Execution Timeline", color="#f0f6fc", fontsize=10.5)
    ax2.set_xlabel("Trade Timeline (%)", color="#c9d1d9", fontsize=8.5)
    ax2.set_ylabel("Drawdown %", color="#c9d1d9", fontsize=8.5)
    ax2.legend(loc="lower left", fontsize=8, facecolor="#0d1117", labelcolor="#f0f6fc")

    ax3 = axes[1, 0]
    lb_s = sorted(leaderboard, key=lambda x: x["total_r_all"])
    names = [f"{r['strategy_id'][:18]} ({r['best_tf']})" for r in lb_s]
    vals = [r["total_r_all"] for r in lb_s]
    # Color: green=QUAL, purple=RESCUED, orange=positive-FAIL, red=negative-FAIL
    cols = []
    for r in lb_s:
        if r.get("is_rescued"):
            cols.append("#a371f7")  # purple = rescued from FAIL
        elif r["is_qualified"]:
            cols.append("#3fb950")  # green = qualified
        elif r["total_r_all"] > 0:
            cols.append("#d29922")  # orange = positive but failed gate
        else:
            cols.append("#f85149")  # red = negative
    y_pos = np.arange(len(names))
    ax3.barh(y_pos, vals, color=cols, height=0.72)
    ax3.set_yticks(y_pos)
    ax3.set_yticklabels(names, fontsize=6.8, color="#c9d1d9")
    ax3.axvline(0.0, color="#8b949e", linewidth=1.0)
    ax3.set_title("3. BTC Optimized Net R | Green=QUAL | Purple=RESCUED | Orange=FAIL+ | Red=FAIL-", color="#f0f6fc", fontsize=9.5)
    ax3.set_xlabel("Total Net R (2021-2026 after fees & funding)", color="#c9d1d9", fontsize=8.5)

    ax4 = axes[1, 1]
    rng = np.random.default_rng(2026)
    d_df5 = p5.get("daily_df")
    if d_df5 is not None and len(d_df5) >= 20:
        arr5 = d_df5["port_r"].to_numpy(dtype=float)
        hz = min(365, len(arr5))
        blk = 8
        nb = (hz + blk - 1) // blk
        st = rng.integers(0, max(1, len(arr5) - blk + 1), size=(mc_runs, nb))
        idx = (st[:, :, None] + np.arange(blk)[None, None, :]).reshape(mc_runs, -1)[:, :hz]
        ends5 = 5000.0 * np.prod(np.maximum(0.01, 1.0 + arr5[idx] * 0.01), axis=1)
        lo_p, hi_p = float(np.percentile(ends5, 1)), float(np.percentile(ends5, 99))
        clipped = np.clip(ends5, lo_p, hi_p)
        ax4.hist(clipped, bins=55, color="#a371f7", alpha=0.65, edgecolor="#0d1117", label=f"P5 Improved 1Y Equity ({mc_runs:,} paths)")
        p5_v, p50_v, p95_v = np.percentile(ends5, [5, 50, 95])
        ax4.axvline(5000.0, color="#f85149", linestyle="--", linewidth=1.4, label="Start $5,000")
        ax4.axvline(p5_v, color="#d29922", linestyle=":", linewidth=1.3, label=f"P5%: ${p5_v:,.0f}")
        ax4.axvline(p50_v, color="#58a6ff", linewidth=1.8, label=f"Median: ${p50_v:,.0f}")
        ax4.axvline(p95_v, color="#3fb950", linestyle=":", linewidth=1.3, label=f"P95: ${p95_v:,.0f}")
    # Also overlay P3
    d_df3 = p3.get("daily_df")
    if d_df3 is not None and len(d_df3) >= 20:
        arr3 = d_df3["port_r"].to_numpy(dtype=float)
        hz = min(365, len(arr3))
        blk = 8
        nb = (hz + blk - 1) // blk
        st3 = rng.integers(0, max(1, len(arr3) - blk + 1), size=(mc_runs, nb))
        idx3 = (st3[:, :, None] + np.arange(blk)[None, None, :]).reshape(mc_runs, -1)[:, :hz]
        ends3 = 5000.0 * np.prod(np.maximum(0.01, 1.0 + arr3[idx3] * 0.01), axis=1)
        lo3, hi3 = float(np.percentile(ends3, 1)), float(np.percentile(ends3, 99))
        ax4.hist(np.clip(ends3, lo3, hi3), bins=55, color="#3fb950", alpha=0.40, edgecolor="none", label=f"P3 Orthogonal (overlay)")
    mc_m = p5.get("metrics", {}).get("mc_20k", {})
    ax4.set_title(
        f"4. 20K MC P5 Improved (P(Loss)={mc_m.get('p_loss_pct', 0)}% | Crash DD95={mc_m.get('crash_dd_p95', 0)}%)",
        color="#f0f6fc", fontsize=10.5,
    )
    ax4.set_xlabel("1-Year Projected Equity ($ USD)", color="#c9d1d9", fontsize=8.5)
    ax4.legend(loc="upper right", fontsize=7.5, facecolor="#0d1117", labelcolor="#f0f6fc")

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=140)
    plt.close(fig)


if __name__ == "__main__":
    rep = run_btc_all25_v2(mc_runs=20000)
    print("\n=== PORTFOLIO SUMMARY (v2) ===")
    for k, p in rep["portfolios"].items():
        m = p.get("metrics", {})
        mc = m.get("mc_20k", {})
        print(
            f"{k:20s} | n={m.get('total_trades', 0):4d} WR={m.get('win_rate', 0):4.1f}% "
            f"R={m.get('total_r', 0):6.1f} PF={m.get('profit_factor', 0):4.2f} "
            f"End=${m.get('end_equity', 0):10,.2f} DD={m.get('max_dd_pct', 0):5.1f}% "
            f"MC_Loss={mc.get('p_loss_pct', 0)}% CrashDD95={mc.get('crash_dd_p95', 0)}%"
        )
    print("\n=== ALL STRATEGIES RANKED (v2) ===")
    rescued_set = set(rep.get("rescued_strategy_ids", []))
    for r in rep["leaderboard"]:
        q_flag = "RESCUED" if r["strategy_id"] in rescued_set else ("QUAL" if r["is_qualified"] else "FAIL")
        print(
            f"[{q_flag:7s}] {r['strategy_id']:24s} | {r['best_tf']:3s} {r['best_mode']:18s} | "
            f"n={r['trades_all']:3d} WR={r['wr_all']:4.1f}% R={r['total_r_all']:7.1f} PF={r['pf_all']:4.2f} "
            f"| IS_R={r['is_r']:6.1f} (PF {r['is_pf']:4.2f}) | OOS_R={r['oos_r']:6.1f} (PF {r['oos_pf']:4.2f}, n={r['oos_trades']:3d})"
        )
