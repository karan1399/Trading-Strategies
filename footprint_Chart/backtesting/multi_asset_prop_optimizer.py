"""26-Asset Individual Prop-Firm Optimizer (20 Crypto + 6 Commodities, IS->OOS + 20K MC).

Attacks each asset independently for a dedicated $100,000 Prop-Firm allocation:
  1. Loads 2H/4H footprint + 1m OHLC + 8h funding from cache.
  2. Strict causal IS -> Frozen OOS split:
       - Crypto (2021-2026): IS <= 2023-12-31 -> OOS 2024-2026
       - Commodities (late 2025/2026 inception): chronological 50% IS -> 50% OOS
         + multi-year MT5 H4 OHLC cross-check (~4,800 bars).
  3. Evaluates 24 strategies (10 live sleeves + 9 Footprint plugins + 5 Charts plugins
     incl. v2 EMA233/CVD/Direct-G4 upgrades) via 1m-exact `sim_1m_trades_nb`.
  4. Prunes IS clones (|corr_IS| > 0.78), builds each asset's bespoke specialist ensemble,
     and calibrates `PROP_10_STATIC` (<=10% DD / 5% daily) and `PROP_8_TRAILING` (<=8% DD)
     via 20,000 block-bootstrap + prop-breach Monte Carlo simulations.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import gc
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from Charts.core.strategy_base import discover_strategies as discover_chart_strategies
from footprint_Chart.backtesting.btc_all25_optimizer_v2 import (
    _df_to_bars,
    _fast_ohlc_fallbacks,
    _prep_arrs,
    _res_to_dicts,
    _to_chart_df,
)
from footprint_Chart.backtesting.btc_quant_pareto_optimizer import (
    ALPHA_BUCKETS,
    _simulate_quant_portfolio,
)
from footprint_Chart.backtesting.fast_kernels import sim_1m_trades_nb
from footprint_Chart.backtesting.multi_asset_optimizer import (
    SPLIT_2024_TS,
    _fast_s04_masks,
    _fast_s07_masks,
    compute_hac_stats,
)
from footprint_Chart.backtesting.oi_vwap_sweep_research import attach_causal_oi_vwap_features, ensure_full_metrics
from footprint_Chart.backtesting.portfolio_engine import _load_funding_arrays, load_asset_tfs_cached
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from footprint_Chart.data_pipeline.dump_26_prop_universe import SPECS_26_PATH, UNIVERSE_26
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import adx_arr, ema_arr, rolling_std_arr, sma_arr, wilder_atr
from strategies.delta_divergence import DeltaDivergenceStrategy
from strategies.ema233_imb_rejection import Ema233ImbalanceRejectionStrategy
from strategies.ema_zero_orderflow import EmaZeroOrderFlowStrategy
from strategies.imbalance_stack import ImbalanceStackStrategy
from strategies.of_initiative_breakout import OrderFlowInitiativeBreakoutStrategy
from strategies.vwap_of_reclaim import VwapOrderFlowReclaimStrategy
from strategies.zero_auction_reversal import ZeroAuctionReversalStrategy

REPORT_JSON = DATA_DIR / "prop26_multi_asset_report.json"
ARTIFACT_PNG = Path(
    r"C:\Users\SIGMA\.gemini\antigravity\brain\4ed94c81-b1da-4868-8a7c-aec85386790d\prop26_multi_asset_dashboard.png"
)


def _sigs_to_arr(n_b: int, sigs: list[Any]) -> np.ndarray:
    arr = np.zeros(n_b, dtype=np.int8)
    for s in sigs:
        idx = int(getattr(s, "index"))
        if 0 <= idx < n_b and arr[idx] == 0:
            arr[idx] = int(getattr(s, "side"))
    return arr


def _score_is_oos(
    o_ti: np.ndarray,
    o_r: np.ndarray,
    split_ts: int,
    min_is: int,
    o_fr: np.ndarray | None = None,
) -> dict[str, Any]:
    if len(o_ti) == 0:
        return {
            "n_all": 0, "r_all": 0.0, "pf_all": 0.0, "wr_all": 0.0,
            "nis": 0, "isr": 0.0, "ispf": 0.0, "ist": 0.0, "avg_is_r": 0.0, "mean_fee_r": 0.0,
            "hmin": -99.0, "raw_score": -99.0,
            "noos": 0, "oosr": 0.0, "oospf": 0.0, "oos_wr": 0.0,
        }
    is_m = o_ti < split_ts
    oos_m = ~is_m
    ris, roos = o_r[is_m], o_r[oos_m]
    nis, noos = int(len(ris)), int(len(roos))
    ig, il = float(ris[ris > 0].sum()), float(abs(ris[ris < 0].sum()))
    is_pf = (ig / il) if il > 0 else (9.9 if ig > 0 else 0.0)
    og, ol = float(roos[roos > 0].sum()), float(abs(roos[roos < 0].sum()))
    oos_pf = (og / ol) if ol > 0 else (9.9 if og > 0 else 0.0)
    ag, al = float(o_r[o_r > 0].sum()), float(abs(o_r[o_r < 0].sum()))
    all_pf = (ag / al) if al > 0 else (9.9 if ag > 0 else 0.0)
    is_t = compute_hac_stats(ris)["t_hac"] if nis >= 6 else (float(ris.mean() / (ris.std() + 1e-9) * math.sqrt(nis)) if nis >= 3 else 0.0)
    avg_is_r = float(ris.mean()) if nis > 0 else 0.0
    mean_fee_r = float(o_fr[is_m].mean()) if (o_fr is not None and nis > 0) else 0.0
    hmin = float(min(ris[: nis // 2].sum(), ris[nis // 2 :].sum())) if nis >= 6 else (float(ris.sum()) if nis > 0 else -99.0)
    stab = 1.0 if hmin > 0.0 else (0.75 if hmin > -5.0 else 0.40)
    n_pen = 1.0 if nis >= min_is else (0.35 if nis >= max(4, min_is // 2) else 0.02)
    # Anti-scalp-noise & anti-fee-drag adjustment: reward per-trade R expectancy and penalize high friction
    eff_t = min(float(is_t), 4.0) + 0.25 * max(0.0, float(is_t) - 4.0) if is_t > 0 else float(is_t)
    exp_mult = min(1.8, max(0.30, (max(0.0, avg_is_r) / 0.25) ** 0.5)) if avg_is_r > 0 else 0.10
    fee_pen = max(0.25, 1.0 - max(0.0, mean_fee_r - 0.14) * 2.5) if o_fr is not None else 1.0
    raw = (
        eff_t * math.sqrt(max(0.25, min(4.0, is_pf))) * exp_mult * fee_pen * stab * n_pen
        if (is_t > 0 and avg_is_r > 0 and nis >= 4)
        else (-10.0 + 0.01 * float(ris.sum()) if nis < 4 else -1.0 + 0.01 * float(ris.sum()))
    )
    return {
        "n_all": int(len(o_r)), "r_all": round(float(o_r.sum()), 2), "pf_all": round(all_pf, 2),
        "wr_all": round(float((o_r > 0).mean() * 100.0), 1),
        "nis": nis, "isr": round(float(ris.sum()), 2) if nis else 0.0, "ispf": round(is_pf, 2), "ist": round(is_t, 2),
        "avg_is_r": round(avg_is_r, 4), "mean_fee_r": round(mean_fee_r, 4),
        "hmin": round(hmin, 2), "raw_score": float(raw),
        "noos": noos, "oosr": round(float(roos.sum()), 2) if noos else 0.0, "oospf": round(oos_pf, 2),
        "oos_wr": round(float((roos > 0).mean() * 100.0), 1) if noos else 0.0,
    }


def _prop_mc_eval(daily_r: np.ndarray, risk_pct: float, nmc: int = 20000, seed: int = 2026) -> dict[str, float]:
    """20,000 correlated block-bootstrap paths evaluating Prop Firm pass/breach probabilities over min(365, N_days)."""
    if len(daily_r) < 15:
        return {"hz": len(daily_r), "med_1y_pct": 0.0, "pass10_pct": 0.0, "breach8_pct": 0.0, "breach10_pct": 0.0, "dd_p95": 0.0}
    rng = np.random.default_rng(seed)
    hz = min(365, len(daily_r))
    blk = min(8, max(3, len(daily_r) // 8))
    nb = (hz + blk - 1) // blk
    st = rng.integers(0, max(1, len(daily_r) - blk + 1), size=(nmc, nb))
    idx = (st[:, :, None] + np.arange(blk)[None, None, :]).reshape(nmc, -1)[:, :hz]
    paths = np.cumprod(np.maximum(0.01, 1.0 + daily_r[idx] * (risk_pct / 100.0)), axis=1)
    p_full = np.hstack([np.ones((nmc, 1)), paths])
    pks = np.maximum.accumulate(p_full, axis=1)
    dds = ((pks - p_full) / pks * 100.0).max(axis=1)
    return {
        "hz": int(hz),
        "med_1y_pct": round(float((np.median(paths[:, -1]) - 1.0) * 100.0), 1),
        "pass10_pct": round(float(((paths.max(axis=1) >= 1.10) & (dds < 10.0)).mean() * 100.0), 1),
        "breach8_pct": round(float((dds >= 8.0).mean() * 100.0), 1),
        "breach10_pct": round(float((dds >= 10.0).mean() * 100.0), 1),
        "dd_p95": round(float(np.percentile(dds, 95)), 1),
    }


def _mt5_h4_commodity_crosscheck(sym: str, sp_bps: float) -> dict[str, Any]:
    """Run causal IS->OOS multi-year MT5 H4 OHLC trend+pullback sanity check for commodities."""
    p = DATA_DIR / f"{sym}_mt5_h4.parquet"
    if not p.is_file():
        return {"bars": 0}
    df = pq.read_table(p).to_pandas().sort_values("ts").reset_index(drop=True)
    if len(df) < 400:
        return {"bars": len(df)}
    df["end_ts"] = df["ts"] + 14400
    df["tr"] = np.maximum(df["h"] - df["l"], np.maximum((df["h"] - df["c"].shift(1)).abs(), (df["l"] - df["c"].shift(1)).abs()))
    df["atr"] = df["tr"].rolling(20).mean()
    df["ema89"] = ema_arr(df["c"].to_numpy(dtype=float), 89)
    df["ema233"] = ema_arr(df["c"].to_numpy(dtype=float), 233)
    ohlc = _fast_ohlc_fallbacks(df)
    c = df["c"].to_numpy(dtype=float)
    h = df["h"].to_numpy(dtype=float)
    l = df["l"].to_numpy(dtype=float)
    o = df["o"].to_numpy(dtype=float)
    e233 = df["ema233"].to_numpy(dtype=float)
    ts = df["ts"].to_numpy(dtype=np.int64)
    split_ts = int(ts[len(ts) // 2])
    # Simple causal 4H OHLC signal blend (Donchian + EMA pullback + Squeeze) evaluated bar-by-bar at H4 resolution
    sig = np.where(ohlc["s09"] != 0, ohlc["s09"], np.where(ohlc["s10"] != 0, ohlc["s10"], ohlc["s04"]))
    # Simulate cleanly bar-by-bar on H4 with 2.0 ATR SL / 3.5 ATR TP
    atr = df["atr"].to_numpy(dtype=float)
    r_list, ti_list = [], []
    i = 240
    cost_r = (sp_bps + 2.0) / 10000.0
    while i < len(c) - 2:
        sd = int(sig[i])
        if sd == 0 or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        ent = o[i + 1]
        sl_d = 2.0 * atr[i]
        tp_d = 3.5 * atr[i]
        sl_p = ent - sd * sl_d
        tp_p = ent + sd * tp_d
        j = i + 1
        res_r = 0.0
        while j < min(len(c), i + 72):
            if (sd == 1 and l[j] <= sl_p) or (sd == -1 and h[j] >= sl_p):
                res_r = -1.0 - (cost_r * ent / sl_d)
                break
            if (sd == 1 and h[j] >= tp_p) or (sd == -1 and l[j] <= tp_p):
                res_r = 1.75 - (cost_r * ent / sl_d)
                break
            j += 1
        else:
            res_r = (sd * (c[j - 1] - ent) / sl_d) - (cost_r * ent / sl_d)
        r_list.append(res_r)
        ti_list.append(int(ts[i + 1]))
        i = j + 1
    ra = np.array(r_list, dtype=float)
    ta = np.array(ti_list, dtype=np.int64)
    sc = _score_is_oos(ta, ra, split_ts, min_is=10)
    span_y = round(float((ts[-1] - ts[0]) / (365.25 * 86400.0)), 1)
    return {"bars": len(df), "years": span_y, "n": sc["n_all"], "pf": sc["pf_all"], "r": sc["r_all"], "oos_r": sc["oosr"], "oos_pf": sc["oospf"]}


def optimize_single_asset(row: dict[str, Any], spec: dict[str, Any], chart_map: dict[str, Any]) -> dict[str, Any]:
    sym = row["binance"]
    cat = row["cat"]
    print(f"\n>>> Optimizing {sym} ({cat})...", flush=True)
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
    # Crypto-Prop / Binance Futures cost model (`sp_bps`: 2.65 BTC/ETH, 3.5 Majors/Commodities, 4.5 Alts)
    fut_sp = 2.65 if sym in ("BTCUSDT", "ETHUSDT") else (3.5 if (cat == "COMMODITY" or sym in ("SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT")) else 4.5)
    mt5_sp = float(spec.get("sp_bps", fut_sp))

    def _run(arrs, tf_sec, side_arr, sl, tp, tr, be, stp, mh, dyn):
        return sim_1m_trades_nb(
            arrs[0], arrs[1], arrs[2], arrs[3], arrs[4], arrs[5],
            np.ascontiguousarray(side_arr, dtype=np.int8),
            m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c,
            int(tf_sec), float(sl), float(tp), float(tr), float(be), 0.05, bool(stp), int(mh), 1, bool(dyn),
            False, float(fut_sp), True, 0.0, 0.0, 5, 0.0, 0.0, fts_c, frt_c,
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
    vpin2, vpm2 = fp2["vpin_rank"], fp2["vpm_rk"]

    trials: dict[str, list[dict[str, Any]]] = {}

    def _push(sid, tf_l, mode_l, cfg_s, df_u, arrs, tf_s, sa, sl, tp, tr, be, stp, mh, dyn):
        res = _run(arrs, tf_s, sa, sl, tp, tr, be, stp, mh, dyn)
        sc = _score_is_oos(res[1], res[7], split_ts, min_is)
        trials.setdefault(sid, []).append({"sid": sid, "tf": tf_l, "mode": mode_l, "cfg": cfg_s, "df": df_u, "res": res, **sc})

    ohlc4 = _fast_ohlc_fallbacks(g4)

    # ---- Live Sleeves s01..s10 ----
    wr4 = fp4["wr14"]
    wr4_p = pd.Series(wr4).shift(1).to_numpy(dtype=float)
    for wt in (-80.0, -75.0):
        wl = stkL & (wr4_p <= wt) & (wr4 > wt) & (dp4 > 3.0) & (clv4 > 0.20) & (~has_b4 | (bi4 >= si4))
        ws = stkS & (wr4_p >= (-100.0 - wt)) & (wr4 < (-100.0 - wt)) & (dp4 < -3.0) & (clv4 < -0.20) & (~has_b4 | (si4 >= bi4))
        _push("s01_williams_pullback", "4H", "Footprint", f"wt={wt},sl=2.0,tr=3.5", g4, a4, 14400, np.where(wl, 1, np.where(ws, -1, 0)), 2.0, 0.0, 3.5, 0.0, True, 96, True)
    _push("s01_williams_pullback", "4H", "OHLC", "ohlc", g4, a4, 14400, ohlc4["s01"], 2.0, 3.5, 0.0, 1.0, False, 96, False)

    r2_4 = fp4["rsi2"]
    ofb4 = g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool) | ((dp4 > 5.0) & (clv4 > 0.25))
    ofs4 = g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool) | ((dp4 < -5.0) & (clv4 < -0.25))
    for rl, vc in ((15.0, 0.65), (20.0, 0.65), (15.0, 0.80)):
        sa2 = np.where((vpin4 < vc) & (r2_4 < rl) & up233 & ofb4, 1, np.where((vpin4 < vc) & (r2_4 > (100.0 - rl)) & dn233 & ofs4, -1, 0))
        _push("s02_rsi2_meanrev", "4H", "Footprint", f"rsi={rl},vp={vc}", g4, a4, 14400, sa2, 2.5, 4.0, 0.0, 1.0, True, 48, True)
        _push("rsi2_of_reversion", "4H", "Direct G4", f"rsi={rl},vp={vc}", g4, a4, 14400, sa2, 2.5, 4.0, 0.0, 1.0, True, 48, True)
    _push("s02_rsi2_meanrev", "4H", "OHLC", "ohlc", g4, a4, 14400, ohlc4["s02"], 1.5, 2.5, 0.0, 1.0, False, 48, False)

    e233_2s, c2s = pd.Series(e233_2).shift(2).to_numpy(dtype=float), pd.Series(c2).shift(2).to_numpy(dtype=float)
    t2L = (l2 <= e233_2 + 0.3 * a2[2]) & (c2 > e233_2) & (c2s > e233_2s)
    t2S = (h2 >= e233_2 - 0.3 * a2[2]) & (c2 < e233_2) & (c2s < e233_2s)
    for im, dg in ((1, 0.0), (1, 3.0), (2, 0.0)):
        sa3 = np.where((bi2 >= im) & (clv2 > 0.25) & t2L & (dp2 > dg), 1, np.where((si2 >= im) & (clv2 < -0.25) & t2S & (dp2 < -dg), -1, 0))
        _push("s03_htf_ema_rejection", "2H", "Footprint", f"imb={im},dp>={dg}", g2, a2, 7200, sa3, 2.5, 3.5, 0.0, 1.0, False, 72, False)
    _push("s03_htf_ema_rejection", "4H", "OHLC", "ohlc", g4, a4, 14400, ohlc4["s03"], 1.5, 2.5, 0.0, 1.0, False, 72, False)

    for vpm_m in (0.30, 0.45):
        sqL, sqS = _fast_s04_masks(g4, fp4, {"fp_min_vpm": vpm_m, "fp_min_vr4": 0.90})
        _push("s04_vc_squeeze_breakout", "4H", "Footprint", f"vpm={vpm_m},sl=2.0,tr=3.5", g4, a4, 14400, np.where(sqL, 1, np.where(sqS, -1, 0)), 2.0, 0.0, 3.5, 0.0, True, 96, True)
    _push("s04_vc_squeeze_breakout", "4H", "OHLC", "ohlc,tp=4.0", g4, a4, 14400, ohlc4["s04"], 1.5, 4.0, 0.0, 1.0, False, 96, False)

    mu50 = sma_arr(c4, 50)
    sd50 = np.where(rolling_std_arr(c4, 50) > 1e-12, rolling_std_arr(c4, 50), np.nan)
    zv50 = (c4 - mu50) / sd50
    zvp50 = pd.Series(zv50).shift(1).to_numpy(dtype=float)
    atr_exp = atr4_14 >= pd.Series(atr4_14).rolling(20).mean().to_numpy(dtype=float)
    zl5 = (zvp50 < 1.5) & (zv50 >= 1.5) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 18.0) & atr_exp
    zs5 = (zvp50 > -1.5) & (zv50 <= -1.5) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 18.0) & atr_exp
    _push("s05_zscore_breakout", "4H", "OHLC", "ohlc,lb=50,z=1.5", g4, a4, 14400, np.where(zl5, 1, np.where(zs5, -1, 0)), 2.0, 3.5, 0.0, 1.0, False, 96, False)
    _push("s05_zscore_breakout", "4H", "Footprint", "fp,lb=50,z=1.5", g4, a4, 14400, np.where(zl5 & (dp4 > 0) & (clv4 > 0.15), 1, np.where(zs5 & (dp4 < 0) & (clv4 < -0.15), -1, 0)), 2.0, 0.0, 3.0, 0.0, False, 96, True)

    for zt_v, vc in ((1.75, 0.75), (2.25, 0.60)):
        fl6 = (vpin4 < vc) & (vwz4 < -zt_v) & ((dp4 > 0.0) | (clv4 > 0.20) | zbot4)
        fs6 = (vpin4 < vc) & (vwz4 > zt_v) & ((dp4 < 0.0) | (clv4 < -0.20) | ztop4)
        _push("s06_iv_wall_fade", "4H", "Footprint", f"vwz={zt_v},vp={vc}", g4, a4, 14400, np.where(fl6, 1, np.where(fs6, -1, 0)), 2.5, 4.5, 0.0, 1.2, False, 48, False)

    for sk in (2, 3):
        pL, pS = _fast_s07_masks(g4, fp4, {"swing_k": sk})
        sf7 = np.where(pL, 1, np.where(pS, -1, 0))
        _push("s07_daily_pivot_breakout", "4H", "Footprint", f"fp,k={sk},sl=2.0,tr=3.0", g4, a4, 14400, sf7, 2.0, 0.0, 3.0, 1.0, True, 96, True)
        _push("s07_daily_pivot_breakout", "4H", "FP+CVD", f"cvd,k={sk},sl=2.0,tr=3.5", g4, a4, 14400, np.where((sf7 == 1) & (cvdz4 > 0.5), 1, np.where((sf7 == -1) & (cvdz4 < -0.5), -1, 0)), 2.0, 0.0, 3.5, 1.0, True, 96, True)
    _push("s07_daily_pivot_breakout", "4H", "OHLC", "ohlc,k=2", g4, a4, 14400, ohlc4["s07"], 2.0, 2.5, 0.0, 1.0, False, 96, False)

    for dn in (16, 22):
        hi_d, lo_d = pd.Series(h4).shift(1).rolling(dn).max().to_numpy(), pd.Series(l4).shift(1).rolling(dn).min().to_numpy()
        ml9 = (c4 > hi_d) & up233 & (clv4 > 0.25) & (dp4 > 0.0) & (~has_b4 | (bi4 >= si4))
        ms9 = (c4 < lo_d) & dn233 & (clv4 < -0.25) & (dp4 < 0.0) & (~has_b4 | (si4 >= bi4))
        _push("s09_donchian_trend_cta", "4H", "Footprint", f"dn={dn},sl=2.0,tr=3.5", g4, a4, 14400, np.where(ml9, 1, np.where(ms9, -1, 0)), 2.0, 0.0, 3.5, 0.0, False, 96, True)
    _push("s09_donchian_trend_cta", "4H", "OHLC", "ohlc,dn=20", g4, a4, 14400, ohlc4["s09"], 1.5, 3.0, 0.0, 1.0, False, 96, False)

    for dpm in (2.0, 5.0):
        tl10 = stkL & (l4 <= e89_4 + 0.30 * a4[2]) & (c4 > e89_4) & (dp4 > dpm) & (clv4 > 0.20)
        ts10 = stkS & (h4 >= e89_4 - 0.30 * a4[2]) & (c4 < e89_4) & (dp4 < -dpm) & (clv4 < -0.20)
        _push("s10_ema_pullback", "4H", "Footprint", f"dp={dpm},sl=2.0,tr=3.5", g4, a4, 14400, np.where(tl10, 1, np.where(ts10, -1, 0)), 2.0, 0.0, 3.5, 0.0, False, 96, True)
    _push("s10_ema_pullback", "4H", "OHLC", "ohlc", g4, a4, 14400, ohlc4["s10"], 1.5, 3.5, 0.0, 1.0, False, 96, False)

    # ---- Footprint Plugins ----
    cg_b = (g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool)) & stkL & (dp4 > 0.0)
    cg_s = (g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool)) & stkS & (dp4 < 0.0)
    _push("cg_signal_strategy", "4H", "Hybrid FP", "cgstk", g4, a4, 14400, np.where(cg_b, 1, np.where(cg_s, -1, 0)), 2.0, 0.0, 3.5, 0.0, False, 96, True)

    dd_cls, imb_cls = DeltaDivergenceStrategy(), ImbalanceStackStrategy()
    zr_cls, e233_cls = ZeroAuctionReversalStrategy(), Ema233ImbalanceRejectionStrategy()
    ez_cls, of_cls, vw_cls = EmaZeroOrderFlowStrategy(), OrderFlowInitiativeBreakoutStrategy(), VwapOrderFlowReclaimStrategy()

    for tf_l, df_u, arrs, tf_s, b_u, up_u, dn_u in (("4H", g4, a4, 14400, bars4, up233, dn233), ("2H", g2, a2, 7200, bars2, up233_2, dn233_2)):
        sa_dd = _sigs_to_arr(len(df_u), dd_cls.generate_signals(b_u, {"min_abs_delta_pct": 10.0}))
        _push("delta_divergence", tf_l, "FP+EMA233", "dp>=10,ema233", df_u, arrs, tf_s, np.where(((sa_dd == 1) & up_u) | ((sa_dd == -1) & dn_u), sa_dd, 0), 2.5, 3.5, 0.0, 1.0, False, 48, False)

        sa_imb = _sigs_to_arr(len(df_u), imb_cls.generate_signals(b_u, {"min_imbalances": 3}))
        _push("imbalance_stack", tf_l, "Pure FP", "imb>=3", df_u, arrs, tf_s, sa_imb, 2.0, 3.0, 0.0, 1.0, False, 48, False)
        _push("imbalance_stack", tf_l, "FP+EMA233", "imb>=3,ema233", df_u, arrs, tf_s, np.where(((sa_imb == 1) & up_u) | ((sa_imb == -1) & dn_u), sa_imb, 0), 2.0, 0.0, 3.5, 0.0, False, 72, True)

        sa_zr = _sigs_to_arr(len(df_u), zr_cls.generate_signals(b_u, {"require_delta_confirm": 1, "min_extreme_val": 1.0}))
        _push("zero_auction_reversal", tf_l, "FP+EMA233", "reqd=1,ema233", df_u, arrs, tf_s, np.where(((sa_zr == 1) & up_u) | ((sa_zr == -1) & dn_u), sa_zr, 0), 2.0, 0.0, 3.5, 0.0, False, 72, True)

        sa_e2 = _sigs_to_arr(len(df_u), e233_cls.generate_signals(b_u, {"min_imb": 2, "min_clv": 0.25}))
        _push("ema233_imb_rejection", tf_l, "Hybrid FP", "imb=2,clv=0.25", df_u, arrs, tf_s, sa_e2, 2.0, 3.5, 0.0, 1.0, False, 72, False)

        sa_ez = _sigs_to_arr(len(df_u), ez_cls.generate_signals(b_u, {"min_delta_pct": 3.0, "enable_ema_touch": 1, "enable_zero_trend": 1}))
        _push("ema_zero_orderflow", tf_l, "Hybrid FP", "dp=3.0", df_u, arrs, tf_s, sa_ez, 2.5, 0.0, 4.5, 0.0, False, 96, True)

        for lb in (16, 22):
            sa_of = _sigs_to_arr(len(df_u), of_cls.generate_signals(b_u, {"lookback": lb, "min_delta_pct": 2.0, "min_clv": 0.25, "require_imb_dom": 1}))
            _push("of_initiative_breakout", tf_l, "Hybrid FP", f"lb={lb},dp=2,sl=2.0,tr=3.5", df_u, arrs, tf_s, sa_of, 2.0, 0.0, 3.5, 0.0, False, 96, True)

        sa_vw = _sigs_to_arr(len(df_u), vw_cls.generate_signals(b_u, {"vwap_len": 48, "z_thresh": 2.0}))
        _push("vwap_of_reclaim", tf_l, "Hybrid FP", "zt=2.0", df_u, arrs, tf_s, sa_vw, 2.5, 4.5, 0.0, 1.2, False, 48, False)

    # ---- Strategies #25-#27: Independent OI Z-Score + 2D/4D VWAP + VolPump Strategies (Individually Optimizable & Combinable) ----
    try:
        oi_start_d = date(2025, 11, 1) if cat == "COMMODITY" else date(2021, 12, 1)
        m_df = ensure_full_metrics(sym, start_d=oi_start_d)
        if not m_df.empty and len(m_df) >= 200:
            g4_oi = attach_causal_oi_vwap_features(g4, m_df, bar_hours=4)
            g2_oi = attach_causal_oi_vwap_features(g2, m_df, bar_hours=2)
            oi_t0 = int(m_df["ts"].iloc[0]) + (24 * 7200 if cat == "COMMODITY" else 48 * 7200)
            valid4 = g4_oi["ts"].to_numpy(dtype=np.int64) >= oi_t0
            valid2 = g2_oi["ts"].to_numpy(dtype=np.int64) >= oi_t0

            zdoi4 = g4_oi["z_doi"].to_numpy(dtype=float)
            zint4 = g4_oi["z_intrabar_drop"].to_numpy(dtype=float)
            zvol4 = g4_oi["z_vol"].to_numpy(dtype=float)
            vwl4 = g4_oi["vwz2d_low"].to_numpy(dtype=float) <= -2.0
            vwh4 = g4_oi["vwz2d_high"].to_numpy(dtype=float) >= 2.0

            # Strategy A (`oi_4h_sqz_mom`): 4H Short-Cover / Squeeze Momentum > EMA233 (+ both-side option per asset)
            for zt_oi, zt_v in ((1.5, 1.5), (1.25, 1.25)):
                p4 = (zdoi4 <= -zt_oi) | (zint4 <= -(zt_oi + 0.3))
                sa_c1_long = np.where(valid4 & vwh4 & p4 & (zvol4 >= zt_v) & up233, 1, 0)
                sa_c1_both = np.where(valid4 & vwh4 & p4 & (zvol4 >= zt_v) & up233 & (clv4 > 0.1), 1, np.where(valid4 & vwl4 & p4 & (zvol4 >= zt_v) & dn233 & (clv4 < -0.1), -1, 0))
                _push("oi_4h_sqz_mom", "4H", "OI+VolPump", f"4H_C1_Long,oi={zt_oi},v={zt_v}", g4, a4, 14400, sa_c1_long, 2.5, 0.0, 3.5, 1.0, False, 72, True)
                _push("oi_4h_sqz_mom", "4H", "OI+VolPump", f"4H_C1_Both,oi={zt_oi},v={zt_v}", g4, a4, 14400, sa_c1_both, 2.5, 0.0, 3.5, 1.0, False, 72, True)

            # Causal 4H HTF EMA233 aligned to 2H
            htf_r = pd.DataFrame({"end_ts": g4["end_ts"].to_numpy(dtype=np.int64), "up233": up233, "dn233": dn233}).sort_values("end_ts")
            m_htf = pd.merge_asof(g2[["end_ts"]].sort_values("end_ts"), htf_r, on="end_ts", direction="backward")
            u2h = m_htf["up233"].fillna(False).to_numpy(dtype=bool)
            d2h = m_htf["dn233"].fillna(False).to_numpy(dtype=bool)
            zdoi2 = g2_oi["z_doi"].to_numpy(dtype=float)
            zint2 = g2_oi["z_intrabar_drop"].to_numpy(dtype=float)
            zvol2 = g2_oi["z_vol"].to_numpy(dtype=float)
            vwl2 = g2_oi["vwz2d_low"].to_numpy(dtype=float) <= -2.0
            vwh2 = g2_oi["vwz2d_high"].to_numpy(dtype=float) >= 2.0

            # Strategy B (`oi_2h_liq_break`): B1_2H_LiqMomentum_Break (Both-Side HTF233 Liquidation Cascade Breakout)
            for zt_oi, zt_v, sl_m, tr_m in ((1.5, 1.5, 2.5, 3.5), (1.25, 1.25, 2.5, 3.5), (1.5, 1.5, 2.0, 3.0)):
                purge2 = (zdoi2 <= -zt_oi) | (zint2 <= -(zt_oi + 0.3))
                sa_b1 = np.where(
                    valid2 & purge2 & (zvol2 >= zt_v) & vwl2 & d2h & (clv2 < -0.2), -1,
                    np.where(valid2 & purge2 & (zvol2 >= zt_v) & vwh2 & u2h & (clv2 > 0.2), 1, 0),
                )
                _push("oi_2h_liq_break", "2H", "OI+VolPump", f"2H_B1,oi={zt_oi},v={zt_v},sl={sl_m}", g2, a2, 7200, sa_b1, sl_m, 0.0, tr_m, 1.0, False, 72, True)

            # Strategy C (`oi_2h_reclaim_fade`): Next-Bar Reclaim SLFix (4D VWAP Reclaim B3 + ShortSqueeze Fade A2)
            purge2_def = (zdoi2 <= -1.5) | (zint2 <= -1.8)
            v48_Lp = pd.Series((g2_oi["vwz48"].to_numpy(dtype=float) <= -2.0) & (zdoi2 <= -1.5)).shift(1).fillna(False).to_numpy(dtype=bool) & (c2 > g2["o"].to_numpy(dtype=float))
            v48_Sp = pd.Series((g2_oi["vwz48"].to_numpy(dtype=float) >= 2.0) & (zdoi2 <= -1.5)).shift(1).fillna(False).to_numpy(dtype=bool) & (c2 < g2["o"].to_numpy(dtype=float))
            sa_b3 = np.where(valid2 & v48_Lp & u2h, 1, np.where(valid2 & v48_Sp & d2h, -1, 0))
            _push("oi_2h_reclaim_fade", "2H", "OI+Reclaim", "2H_B3_4DVWAP_Reclaim", g2, a2, 7200, sa_b3, 3.0, 0.0, 4.0, 1.5, False, 96, True)

            swS_p = pd.Series(vwh2 & purge2_def & (zvol2 >= 1.5)).shift(1).fillna(False).to_numpy(dtype=bool)
            sa_a2 = np.where(valid2 & swS_p & (c2 < g2["o"].to_numpy(dtype=float)) & d2h, -1, 0)
            _push("oi_2h_reclaim_fade", "2H", "OI+Reclaim", "2H_A2_ShortSqzFade_DN233", g2, a2, 7200, sa_a2, 3.0, 0.0, 4.0, 1.5, False, 96, True)
    except Exception as oi_exc:
        print(f"    [oi_warn] {sym} OI sweep skipped: {oi_exc}", flush=True)

    # ---- 5 Charts Plugins (with Fib-Prime 13/89/233 & EMA233 rescues) ----
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
                _push(sid, tf_l, "Pure OHLC", f"ohlc,{prm}", df_u, arrs, tf_s, sa, 2.0, 0.0, 3.5, 0.0, False, 96, True)
                if sid != "rsi2_mean_reversion":
                    sa_tr = np.where(((sa == 1) & up_u) | ((sa == -1) & dn_u), sa, 0)
                    _push(sid, tf_l, "OHLC+EMA233", f"ema233,{prm}", df_u, arrs, tf_s, sa_tr, 2.5, 0.0, 4.0, 0.0, False, 96, True)

    # ---- IS-Plateau Selection (zero OOS leakage) ----
    winners: dict[str, list[dict[str, Any]]] = {}
    strat_rows: list[dict[str, Any]] = []
    for sid, tlist in trials.items():
        raw_arr = np.array([t["raw_score"] for t in tlist], dtype=float)
        for idx_t, t in enumerate(tlist):
            om = (float(raw_arr.sum() - raw_arr[idx_t]) / (len(raw_arr) - 1)) if len(raw_arr) > 1 else raw_arr[idx_t]
            t["plateau"] = 0.65 * t["raw_score"] + 0.35 * max(-1.0, om)
        best = max(tlist, key=lambda x: x["plateau"])
        trs = _res_to_dicts(best["df"], sid, f"{best['tf']} {best['mode']}", best["res"])
        for t in trs:
            t["symbol"] = sym
        winners[sid] = trs
        is_q = (best["nis"] >= min_is and best["isr"] > (2.0 if is_short else 5.0) and best["ispf"] >= 1.20 and best["ist"] >= 0.60 and best["hmin"] > -4.0)
        strat_rows.append({"sid": sid, "tf": best["tf"], "mode": best["mode"], "is_q": is_q, "n": best["n_all"], "r": best["r_all"], "pf": best["pf_all"], "isr": best["isr"], "ispf": best["ispf"], "ist": best["ist"], "oosr": best["oosr"], "oospf": best["oospf"], "noos": best["noos"]})

    # ---- Build Asset-Specific Orthogonal Prop Ensemble (IS ONLY) ----
    qual_rows = [r for r in strat_rows if r["is_q"]]
    qual_rows.sort(key=lambda x: x["ist"] * x["ispf"], reverse=True)
    if len(qual_rows) < 3:
        # Clean fallback strictly sorted by IS performance
        qual_rows = sorted([r for r in strat_rows if r["isr"] > 0 and r["n"] >= 5], key=lambda x: x["ist"] * x["ispf"], reverse=True)[:6]

    # IS daily correlation clone pruning (|corr| <= 0.78)
    is_dm: dict[str, dict[int, float]] = {}
    all_d = set()
    for r in qual_rows:
        dm: dict[int, float] = {}
        for t in winners[r["sid"]]:
            if t["ts_in"] < split_ts:
                dk = int(t["ts_out"] // 86400)
                dm[dk] = dm.get(dk, 0.0) + float(t["r"])
                all_d.add(dk)
        is_dm[r["sid"]] = dm
    if len(all_d) >= 10 and len(qual_rows) >= 2:
        mat = pd.DataFrame({r["sid"]: [is_dm[r["sid"]].get(d, 0.0) for d in sorted(all_d)] for r in qual_rows}).fillna(0.0)
        cmat = mat.corr()
        picked: list[str] = []
        for r in qual_rows:
            s = r["sid"]
            if any(abs(float(cmat.loc[s, p])) > 0.78 for p in picked):
                continue
            picked.append(s)
            if len(picked) >= 10:
                break
    else:
        picked = [r["sid"] for r in qual_rows[:8]]

    # ---- Standalone $100,000 Prop Firm Calibration ----
    bi, si = g4["buy_imb"].to_numpy(dtype=int), g4["sell_imb"].to_numpy(dtype=int)
    vof_refs = (g4["c"].to_numpy(dtype=float), g4["end_ts"].to_numpy(), g4["d_pct"].to_numpy(dtype=float), bi - si, bi + si, g4["vpin_rank"].to_numpy(dtype=float))

    prop_grid = []
    if picked:
        for arch_l, mc, ms, bkt in (("Raw_c4_s3", 4, 3, None), ("Bkt(1,1,2)_c3_s2", 3, 2, {"BREAKOUT": 1, "PULLBACK": 1, "MEANREV": 2})):
            for rp in (0.20, 0.25, 0.30, 0.35, 0.45, 0.60):
                p_full = _simulate_quant_portfolio(picked, winners, vof_refs, 100000.0, rp, mc, ms, bkt, 0, None, True, None, None, 0, f"{sym} {arch_l} {rp}%")
                p_oos  = _simulate_quant_portfolio(picked, winners, vof_refs, 100000.0, rp, mc, ms, bkt, 0, None, True, split_ts, None, 0, f"{sym} OOS")
                fm, om = p_full["metrics"], p_oos["metrics"]
                if fm["total_trades"] < 10:
                    continue
                d_arr = p_full["daily_df"]["port_r"].to_numpy(dtype=float)
                worst_day = float(np.min(d_arr) * rp)
                mc_st = _prop_mc_eval(d_arr, rp, nmc=20000)
                prop_grid.append({
                    "arch": arch_l, "risk_pct": rp,
                    "trades": fm["total_trades"], "wr": fm["win_rate"], "net_r": fm["total_r"],
                    "pf": fm["profit_factor"], "cagr": fm["cagr_pct"], "max_dd": fm["max_dd_pct"],
                    "calmar": fm["calmar_ratio"], "sharpe": fm["sharpe_ratio"],
                    "oos_trades": om["total_trades"], "oos_r": om["total_r"], "oos_pf": om["profit_factor"], "oos_dd": om["max_dd_pct"],
                    "worst_day_pct": round(worst_day, 2), **mc_st,
                })

    def _pick_prop(cands: list[dict[str, Any]], max_dd_lim: float, breach_key: str, max_br: float) -> dict[str, Any]:
        if not cands:
            return {}
        ok = [x for x in cands if x["max_dd"] <= max_dd_lim and x["oos_dd"] <= max_dd_lim and x[breach_key] <= max_br and x["worst_day_pct"] > -3.5]
        if not ok:
            ok = [x for x in cands if x["max_dd"] <= max_dd_lim + 1.0] or sorted(cands, key=lambda x: x["max_dd"])[:3]
        return max(ok, key=lambda x: (x["pass10_pct"], x["cagr"], x["pf"]))

    prop10 = _pick_prop(prop_grid, 9.0, "breach10_pct", 5.0)
    prop8 = _pick_prop(prop_grid, 7.2, "breach8_pct", 8.0)

    mt5_chk = _mt5_h4_commodity_crosscheck(sym, mt5_sp) if cat == "COMMODITY" else {}

    # Honest Research Classification
    if not prop10 or not picked:
        verdict = "REJECT"
    elif is_short:
        verdict = "EXPLORATORY_COMMODITY" if (prop10.get("oos_pf", 0) >= 1.15 and prop10.get("pf", 0) >= 1.30) else "EXPLORATORY_WEAK"
    else:
        if prop10.get("oos_pf", 0) >= 1.25 and prop10.get("pf", 0) >= 1.35 and prop10.get("max_dd", 99) <= 9.5 and prop10.get("breach10_pct", 99) <= 6.0:
            verdict = "TIER1_PROP_READY"
        elif prop10.get("oos_pf", 0) >= 1.08 and prop10.get("pf", 0) >= 1.20:
            verdict = "TIER2_CONDITIONAL"
        else:
            verdict = "REJECT"

    cfd_flag = "TIGHT_CFD_OK" if (spec.get("mt5_available") and mt5_sp <= 15.0) else ("HIGH_CFD_SPREAD" if spec.get("mt5_available") else "CRYPTO_PROP_ONLY")

    top_strats = sorted(strat_rows, key=lambda x: x["r"], reverse=True)[:5]
    print(f"    -> {sym:10s} [{verdict:21s}] span={span_days:.0f}d | picked={len(picked)} | Prop10: rp={prop10.get('risk_pct')}% PF={prop10.get('pf')} OOS_PF={prop10.get('oos_pf')} MaxDD={prop10.get('max_dd')}% CAGR={prop10.get('cagr')}% Pass10={prop10.get('pass10_pct')}% | CFD={mt5_sp}bp ({cfd_flag})", flush=True)

    del g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c, bars4, bars2, cdf4, cdf2
    gc.collect()

    return {
        "symbol": sym,
        "category": cat,
        "mt5_symbol": row.get("mt5"),
        "span_days": round(span_days, 1),
        "span_start": datetime.fromtimestamp(ts_start, tz=timezone.utc).strftime("%Y-%m-%d"),
        "span_end": datetime.fromtimestamp(ts_end, tz=timezone.utc).strftime("%Y-%m-%d"),
        "split_iso": datetime.fromtimestamp(split_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "is_short_history": is_short,
        "fut_sp_bps": fut_sp,
        "mt5_sp_bps": mt5_sp,
        "cfd_viability": cfd_flag,
        "qualified_count": len([r for r in strat_rows if r["is_q"]]),
        "selected_ensemble": picked,
        "prop_10_static": prop10,
        "prop_8_trailing": prop8,
        "top_strategies": top_strats,
        "mt5_h4_crosscheck": mt5_chk,
        "verdict": verdict,
    }


def _render_dashboard(results: list[dict[str, Any]]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    valid = [r for r in results if r.get("prop_10_static")]
    valid_s = sorted(valid, key=lambda x: (x["verdict"].startswith("TIER1"), x["prop_10_static"].get("pass10_pct", 0), x["prop_10_static"].get("pf", 0)), reverse=True)

    fig, axes = plt.subplots(2, 2, figsize=(18, 11), facecolor="#0d1117")
    fig.suptitle(
        "26-Asset Individual Prop-Firm Allocation Audit (20 Crypto + 6 Commodities | Strict IS->OOS | 20,000 MC per Asset)",
        color="#f0f6fc", fontsize=13, fontweight="bold", y=0.985,
    )
    for ax in axes.flat:
        ax.set_facecolor("#161b22")
        ax.tick_params(colors="#c9d1d9", labelsize=8)
        for sp in ax.spines.values():
            sp.set_color("#30363d")
        ax.grid(alpha=0.18, color="#8b949e")

    color_map = {"TIER1_PROP_READY": "#3fb950", "TIER2_CONDITIONAL": "#58a6ff", "EXPLORATORY_COMMODITY": "#a371f7", "EXPLORATORY_WEAK": "#d29922", "REJECT": "#f85149"}

    # 1) 1Y +10% Prop Pass Probability vs MaxDD%
    ax1 = axes[0, 0]
    for r in valid_s:
        p10 = r["prop_10_static"]
        col = color_map.get(r["verdict"], "#8b949e")
        mk = "^" if r["category"] == "COMMODITY" else "o"
        ax1.scatter([p10["max_dd"]], [p10["pass10_pct"]], color=col, marker=mk, s=95, edgecolors="#ffffff", linewidth=0.7)
        ax1.annotate(r["symbol"].replace("USDT", ""), (p10["max_dd"], p10["pass10_pct"]), fontsize=6.8, color="#f0f6fc", xytext=(4, 2), textcoords="offset points")
    ax1.axvline(9.0, color="#f85149", linestyle="--", linewidth=1.2, label="9% Target MaxDD Ceiling (<10% Prop Limit)")
    ax1.axhline(60.0, color="#3fb950", linestyle=":", linewidth=1.2, label="60% 1Y Challenge Pass Threshold")
    ax1.set_title("1. Prop Challenge Survival Map (MaxDD% vs 20K MC +10% Pass%)", color="#f0f6fc", fontsize=10.5)
    ax1.set_xlabel("Historical Full/OOS Max Drawdown (%)", color="#c9d1d9")
    ax1.set_ylabel("20K MC 1-Year +10% Pass Rate (%)", color="#c9d1d9")
    ax1.legend(loc="lower left", fontsize=7.5, facecolor="#0d1117", labelcolor="#f0f6fc")

    # 2) Horizontal Bar: Profit Factor (Full vs Frozen OOS)
    ax2 = axes[0, 1]
    rev = list(reversed(valid_s))
    y = np.arange(len(rev))
    lbls = [f"{r['symbol'].replace('USDT','')} ({'CMD' if r['category']=='COMMODITY' else 'CRY'})" for r in rev]
    cols = [color_map.get(r["verdict"], "#8b949e") for r in rev]
    pfs = [r["prop_10_static"]["pf"] for r in rev]
    opfs = [r["prop_10_static"]["oos_pf"] for r in rev]
    ax2.barh(y - 0.18, pfs, height=0.35, color=cols, alpha=0.85, label="Full PF")
    ax2.barh(y + 0.18, opfs, height=0.35, color="#f0f6fc", alpha=0.55, label="Frozen OOS PF")
    ax2.axvline(1.0, color="#f85149", linewidth=1.0)
    ax2.axvline(1.30, color="#3fb950", linestyle=":", linewidth=1.0)
    ax2.set_yticks(y)
    ax2.set_yticklabels(lbls, fontsize=6.8, color="#c9d1d9")
    ax2.set_title("2. Profit Factor Integrity (Colored=Full | White=Frozen OOS)", color="#f0f6fc", fontsize=10.5)
    ax2.legend(loc="lower right", fontsize=7.5, facecolor="#0d1117", labelcolor="#f0f6fc")

    # 3) Prop-Safe CAGR % per Standalone Asset Account
    ax3 = axes[1, 0]
    cagrs = [r["prop_10_static"]["cagr"] for r in rev]
    ax3.barh(y, cagrs, color=cols, height=0.68)
    ax3.set_yticks(y)
    ax3.set_yticklabels(lbls, fontsize=6.8, color="#c9d1d9")
    ax3.axvline(0.0, color="#8b949e", linewidth=1.0)
    ax3.set_title("3. Prop-Safe Annual CAGR % (Under MaxDD <= 9% Constraint)", color="#f0f6fc", fontsize=10.5)
    ax3.set_xlabel("Annualized Return %", color="#c9d1d9")

    # 4) Breach Risk vs CFD Friction
    ax4 = axes[1, 1]
    br10 = [r["prop_10_static"]["breach10_pct"] for r in rev]
    ax4.barh(y, br10, color=["#3fb950" if b <= 5.0 else ("#d29922" if b <= 10.0 else "#f85149") for b in br10], height=0.68)
    ax4.axvline(5.0, color="#f85149", linestyle="--", linewidth=1.2, label="5% Max Acceptable 1Y 10%-Breach Risk")
    ax4.set_yticks(y)
    ax4.set_yticklabels([f"{r['symbol'].replace('USDT','')} (CFD {r['mt5_sp_bps']:.0f}bp)" for r in rev], fontsize=6.8, color="#c9d1d9")
    ax4.set_title("4. 20K MC 1-Year 10%-DD Breach Risk % & MT5 Spread", color="#f0f6fc", fontsize=10.5)
    ax4.legend(loc="lower right", fontsize=7.5, facecolor="#0d1117", labelcolor="#f0f6fc")

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    ARTIFACT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(ARTIFACT_PNG), dpi=140)
    plt.close(fig)


ASSETS_DIR = DATA_DIR / "prop26_assets"


def run_all_26_prop(symbols_filter: set[str] | None = None, merge_only: bool = False) -> dict[str, Any]:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    specs = json.loads(SPECS_26_PATH.read_text(encoding="utf-8")) if SPECS_26_PATH.is_file() else {}
    chart_map = {} if merge_only else discover_chart_strategies()
    results: list[dict[str, Any]] = []
    for row in UNIVERSE_26:
        sym = row["binance"]
        ckpt_path = ASSETS_DIR / f"{sym}.json"
        if merge_only:
            if ckpt_path.is_file():
                results.append(json.loads(ckpt_path.read_text(encoding="utf-8")))
            else:
                results.append({"symbol": sym, "category": row["cat"], "verdict": "MISSING", "error": "Checkpoint not found"})
            continue

        if symbols_filter is not None and sym not in symbols_filter:
            if ckpt_path.is_file():
                results.append(json.loads(ckpt_path.read_text(encoding="utf-8")))
            continue

        try:
            res = optimize_single_asset(row, specs.get(sym, {}), chart_map)
            ckpt_path.write_text(json.dumps(res, indent=2), encoding="utf-8")
            results.append(res)
        except Exception as exc:
            print(f"[SKIP] {sym} failed: {exc}", flush=True)
            err_res = {"symbol": sym, "category": row["cat"], "verdict": "ERROR", "error": str(exc)}
            ckpt_path.write_text(json.dumps(err_res, indent=2), encoding="utf-8")
            results.append(err_res)

    if symbols_filter is None or merge_only:
        _render_dashboard(results)
        out = {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "total_assets": len(results),
            "tier1_prop_ready": [r["symbol"] for r in results if r.get("verdict") == "TIER1_PROP_READY"],
            "tier2_conditional": [r["symbol"] for r in results if r.get("verdict") == "TIER2_CONDITIONAL"],
            "exploratory_commodities": [r["symbol"] for r in results if str(r.get("verdict", "")).startswith("EXPLORATORY")],
            "rejected": [r["symbol"] for r in results if r.get("verdict") == "REJECT"],
            "assets": results,
            "chart_png": str(ARTIFACT_PNG),
        }
        REPORT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out

    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "total_assets": len(results),
        "tier1_prop_ready": [r["symbol"] for r in results if r.get("verdict") == "TIER1_PROP_READY"],
        "tier2_conditional": [r["symbol"] for r in results if r.get("verdict") == "TIER2_CONDITIONAL"],
        "exploratory_commodities": [r["symbol"] for r in results if str(r.get("verdict", "")).startswith("EXPLORATORY")],
        "rejected": [r["symbol"] for r in results if r.get("verdict") == "REJECT"],
        "assets": results,
        "chart_png": str(ARTIFACT_PNG),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default="", help="Comma-separated Binance symbols subset")
    ap.add_argument("--merge-only", action="store_true", help="Merge existing prop26_assets/*.json checkpoints")
    args = ap.parse_args()
    sym_set = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} if args.symbols else None
    rep = run_all_26_prop(symbols_filter=sym_set, merge_only=args.merge_only)
    print("\n=== FINAL PROP ALLOCATION SUMMARY ===")
    print("TIER-1 PROP READY :", rep["tier1_prop_ready"])
    print("TIER-2 CONDITIONAL:", rep["tier2_conditional"])
    print("COMMODITIES (EXP) :", rep["exploratory_commodities"])
    print("REJECTED          :", rep["rejected"])
