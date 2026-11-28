"""Delta Exchange India All-Futures Individual Pure Price-Action Trend Optimizer.

Evaluates 10 Pine-Script-ready Pure Price-Action Trend-Following strategies (PA01..PA10)
across 4H (with 1H intrabar execution) and 1H individually for every Delta Exchange India
future contract dumped in Delta_Exchange_Data/, enforcing:
  - Strict causal timing (signal on closed bar t -> fill at open of t+1)
  - Realistic round-trip costs (2x5bps taker fees + live spread + liquidity-tier slippage)
  - Fixed UTC fit and evaluation boundaries with settled labels and IS clone pruning
  - Multi-strategy ensemble combination + 20,000-path block-bootstrap Monte Carlo
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from numba import njit
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from footprint_Chart.backtesting.mt5_shared_account_portfolio_optimizer import simulate_shared_account

DATA_DIR = ROOT / "Delta_Exchange_Data"
CATALOG_PATH = DATA_DIR / "delta_products_catalog.json"
REPORT_JSON = DATA_DIR / "delta_pa_optimization_report.json"
DELTA_FIT_TS = int(pd.Timestamp("2026-06-01", tz="UTC").timestamp())
DELTA_OUTER_TS = int(pd.Timestamp("2026-12-01", tz="UTC").timestamp())
ARTIFACT_PNG = Path(
    r"C:\Users\SIGMA\.gemini\antigravity\brain\4ed94c81-b1da-4868-8a7c-aec85386790d\delta_pa_dashboard.png"
)


# ---------------------------------------------------------------------------
# Fast Causal Indicators (identical semantics to TradingView Pine Script)
# ---------------------------------------------------------------------------
def _ema(x: np.ndarray, span: int) -> np.ndarray:
    out = np.empty(len(x), dtype=np.float64)
    if len(x) == 0:
        return out
    alpha = 2.0 / (span + 1.0)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rma(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(x), np.nan, dtype=np.float64)
    if len(x) < period:
        return out
    out[period - 1] = float(np.nanmean(x[:period]))
    alpha = 1.0 / period
    for i in range(period, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def _atr_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    pc = np.empty_like(c)
    pc[0] = c[0]
    pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return _rma(tr, period)


def _adx_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    up = np.zeros_like(h)
    dn = np.zeros_like(l)
    up[1:] = h[1:] - h[:-1]
    dn[1:] = l[:-1] - l[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = _atr_wilder(h, l, c, period)
    pdi = 100.0 * _rma(pdm, period) / np.where(atr > 1e-12, atr, np.nan)
    ndi = 100.0 * _rma(ndm, period) / np.where(atr > 1e-12, atr, np.nan)
    dx = 100.0 * np.abs(pdi - ndi) / np.where((pdi + ndi) > 1e-12, (pdi + ndi), np.nan)
    adx = _rma(np.nan_to_num(dx, nan=0.0), period)
    return adx, pdi, ndi


def _rsi_wilder(c: np.ndarray, period: int = 2) -> np.ndarray:
    diff = np.zeros_like(c)
    diff[1:] = c[1:] - c[:-1]
    gain = _rma(np.maximum(diff, 0.0), period)
    loss = _rma(np.maximum(-diff, 0.0), period)
    rs = gain / np.where(loss > 1e-12, loss, 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))


@njit(cache=True)
def _supertrend_dir(h: np.ndarray, l: np.ndarray, c: np.ndarray, atr: np.ndarray, mult: float) -> np.ndarray:
    n = len(c)
    st_dir = np.ones(n, dtype=np.int8)
    ub = np.zeros(n, dtype=np.float64)
    lb = np.zeros(n, dtype=np.float64)
    for i in range(n):
        a = atr[i] if np.isfinite(atr[i]) and atr[i] > 0 else (h[i] - l[i])
        hl2 = 0.5 * (h[i] + l[i])
        b_ub = hl2 + mult * a
        b_lb = hl2 - mult * a
        if i == 0:
            ub[i] = b_ub
            lb[i] = b_lb
            continue
        ub[i] = b_ub if (b_ub < ub[i - 1] or c[i - 1] > ub[i - 1]) else ub[i - 1]
        lb[i] = b_lb if (b_lb > lb[i - 1] or c[i - 1] < lb[i - 1]) else lb[i - 1]
        prev_d = st_dir[i - 1]
        if prev_d == 1:
            st_dir[i] = -1 if c[i] < lb[i] else 1
        else:
            st_dir[i] = 1 if c[i] > ub[i] else -1
    return st_dir


@njit(cache=True)
def _sim_delta_nb(
    sig_ts: np.ndarray,
    sig_end_ts: np.ndarray,
    sig_atr: np.ndarray,
    sig_c: np.ndarray,
    side_arr: np.ndarray,
    exec_ts: np.ndarray,
    exec_o: np.ndarray,
    exec_h: np.ndarray,
    exec_l: np.ndarray,
    exec_c: np.ndarray,
    sl_mult: float,
    tp_r: float,
    trail_mult: float,
    be_r: float,
    max_hold_bars: int,
    tf_sec: int,
    rt_cost_bps: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Conservative intrabar simulation on execution grid (1H for 4H/1H signals; SL-first tie-break)."""
    n_s = len(sig_c)
    n_e = len(exec_c)
    out_ti = np.empty(n_s, dtype=np.int64)
    out_to = np.empty(n_s, dtype=np.int64)
    out_r = np.empty(n_s, dtype=np.float64)
    cnt = 0

    e_idx = 0
    cooldown_ts = 0
    mh_sec = int(max_hold_bars) * int(tf_sec)
    cost_frac = rt_cost_bps * 1e-4

    for i in range(235, n_s):
        sd = side_arr[i]
        if sd == 0:
            continue
        if sig_ts[i] < cooldown_ts:
            continue
        atr_i = sig_atr[i]
        if not np.isfinite(atr_i) or atr_i <= 0.0:
            continue

        # Entry STRICTLY at open of first execution bar at or after signal bar closes (`sig_end_ts[i]`)
        target_t = sig_end_ts[i]
        while e_idx < n_e and exec_ts[e_idx] < target_t:
            e_idx += 1
        if e_idx >= n_e:
            break
        # A missing expected entry hour is a data gap, not a delayed signal fill.
        if exec_ts[e_idx] != target_t:
            continue

        ent = exec_o[e_idx]
        if ent <= 0.0:
            continue
        rd = sl_mult * atr_i
        if rd <= 1e-12:
            continue

        sl = ent - sd * rd
        tp = (ent + sd * (tp_r * rd)) if tp_r > 0.0 else 0.0
        best_px = ent
        t_in = exec_ts[e_idx]
        t_out = exec_ts[e_idx]
        raw_pnl = 0.0
        closed = False

        j = e_idx
        while j < n_e:
            bh = exec_h[j]
            bl = exec_l[j]
            bc = exec_c[j]
            bo = exec_o[j]
            t_out = exec_ts[j]

            # Update extreme for BE & Chandelier trail cleanly
            if sd == 1:
                if bl <= sl:
                    raw_pnl = (min(sl, bo) - ent) / rd
                    closed = True
                    break
                if tp > 0.0 and bh >= tp:
                    raw_pnl = (tp - ent) / rd
                    closed = True
                    break
                if bh > best_px:
                    best_px = bh
                if be_r > 0.0 and (best_px - ent) >= be_r * rd and sl < ent + 0.05 * rd:
                    sl = max(sl, ent + 0.05 * rd)
                if (best_px - ent) >= 2.0 * rd:
                    sl = max(sl, ent + 1.0 * rd)
                if (best_px - ent) >= 3.0 * rd:
                    sl = max(sl, ent + 2.0 * rd)
                if (best_px - ent) >= 4.0 * rd:
                    sl = max(sl, ent + 3.0 * rd)
                if trail_mult > 0.0 and (best_px - ent) >= 1.0 * rd:
                    cand = best_px - trail_mult * atr_i
                    if cand > sl:
                        sl = cand
            else:
                if bh >= sl:
                    raw_pnl = (ent - max(sl, bo)) / rd
                    closed = True
                    break
                if tp > 0.0 and bl <= tp:
                    raw_pnl = (ent - tp) / rd
                    closed = True
                    break
                if bl < best_px:
                    best_px = bl
                if be_r > 0.0 and (ent - best_px) >= be_r * rd and sl > ent - 0.05 * rd:
                    sl = min(sl, ent - 0.05 * rd)
                if (ent - best_px) >= 2.0 * rd:
                    sl = min(sl, ent - 1.0 * rd)
                if (ent - best_px) >= 3.0 * rd:
                    sl = min(sl, ent - 2.0 * rd)
                if (ent - best_px) >= 4.0 * rd:
                    sl = min(sl, ent - 3.0 * rd)
                if trail_mult > 0.0 and (ent - best_px) >= 1.0 * rd:
                    cand = best_px + trail_mult * atr_i
                    if cand < sl:
                        sl = cand

            if (exec_ts[j] - t_in) >= mh_sec:
                raw_pnl = sd * (bc - ent) / rd
                closed = True
                break
            j += 1

        if not closed:
            break

        cost_r = (cost_frac * ent) / rd
        out_ti[cnt] = t_in
        out_to[cnt] = t_out
        out_r[cnt] = raw_pnl - cost_r
        cnt += 1
        cooldown_ts = t_out + 3600

    return out_ti[:cnt], out_to[:cnt], out_r[:cnt]


def _build_pa_signals(df: pd.DataFrame) -> dict[str, list[tuple[str, np.ndarray]]]:
    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    v = df["volume"].to_numpy(dtype=np.float64)
    n = len(c)

    e13 = _ema(c, 13)
    e20 = _ema(c, 20)
    e89 = _ema(c, 89)
    e233 = _ema(c, 233)
    atr14 = _atr_wilder(h, l, c, 14)
    adx14, pdi, ndi = _adx_wilder(h, l, c, 14)
    rsi2 = _rsi_wilder(c, 2)

    s_c = pd.Series(c)
    s_h = pd.Series(h)
    s_l = pd.Series(l)
    s_e89 = pd.Series(e89)
    s_e233 = pd.Series(e233)
    s_e13 = pd.Series(e13)

    up233 = c > e233
    dn233 = c < e233
    stkL = (e89 > e233) & (e89 > s_e89.shift(3).to_numpy()) & up233
    stkS = (e89 < e233) & (e89 < s_e89.shift(3).to_numpy()) & dn233
    rng = np.maximum(h - l, 1e-12)
    c_prev = s_c.shift(1).to_numpy()
    h_prev = s_h.shift(1).to_numpy()
    l_prev = s_l.shift(1).to_numpy()

    out: dict[str, list[tuple[str, np.ndarray]]] = {}

    # PA01: Fib-Prime EMA 89/233 Pullback Rejection
    p1L = stkL & (l <= e89 + 0.35 * atr14) & (c > e89) & (c > o)
    p1S = stkS & (h >= e89 - 0.35 * atr14) & (c < e89) & (c < o)
    out["PA01_FibEMA_89_233_Pullback"] = [("tol0.35", np.where(p1L, 1, np.where(p1S, -1, 0)).astype(np.int8))]

    # PA02: Fib-Prime EMA 13/89/233 Reclaim
    p2L = stkL & (c_prev <= s_e13.shift(1).to_numpy()) & (c > e13) & (c > o) & (adx14 >= 18.0)
    p2S = stkS & (c_prev >= s_e13.shift(1).to_numpy()) & (c < e13) & (c < o) & (adx14 >= 18.0)
    out["PA02_EMA13_89_233_Reclaim"] = [("adx18", np.where(p2L, 1, np.where(p2S, -1, 0)).astype(np.int8))]

    # PA03: EMA 233 Rejection Cluster
    e233_s2 = s_e233.shift(2).to_numpy()
    e233_s5 = s_e233.shift(5).to_numpy()
    c_s2 = s_c.shift(2).to_numpy()
    p3L = (l <= e233 + 0.30 * atr14) & up233 & (c_s2 > e233_s2) & (e233 > e233_s5) & (c > o) & ((c - l) / rng >= 0.55)
    p3S = (h >= e233 - 0.30 * atr14) & dn233 & (c_s2 < e233_s2) & (e233 < e233_s5) & (c < o) & ((h - c) / rng >= 0.55)
    out["PA03_EMA233_Rejection"] = [("pin55", np.where(p3L, 1, np.where(p3S, -1, 0)).astype(np.int8))]

    # PA04: Donchian Channel Breakout + EMA233 + ADX18
    for lb in (20, 30):
        hi_n = s_h.shift(1).rolling(lb).max().to_numpy()
        lo_n = s_l.shift(1).rolling(lb).min().to_numpy()
        p4L = (c > hi_n) & up233 & (adx14 >= 18.0)
        p4S = (c < lo_n) & dn233 & (adx14 >= 18.0)
        out.setdefault("PA04_Donchian_Trend", []).append((f"lb={lb}", np.where(p4L, 1, np.where(p4S, -1, 0)).astype(np.int8)))

    # PA05: Supertrend Flip + Fib-Prime EMA233
    for mult in (2.5, 3.0):
        st = _supertrend_dir(h, l, c, atr14, mult)
        stp = np.roll(st, 1)
        stp[0] = st[0]
        p5L = (stp == -1) & (st == 1) & up233
        p5S = (stp == 1) & (st == -1) & dn233
        out.setdefault("PA05_Supertrend_EMA233", []).append((f"m={mult}", np.where(p5L, 1, np.where(p5S, -1, 0)).astype(np.int8)))

    # PA06: Bollinger/Keltner Squeeze Breakout + EMA233
    sma20 = s_c.rolling(20).mean().to_numpy()
    std20 = s_c.rolling(20).std().to_numpy()
    bb_u, bb_l = sma20 + 2.0 * std20, sma20 - 2.0 * std20
    kc_u, kc_l = e20 + 1.5 * atr14, e20 - 1.5 * atr14
    sq = (bb_u <= kc_u) & (bb_l >= kc_l)
    sq_rec = pd.Series(sq).shift(1).rolling(3).max().fillna(0).to_numpy() > 0
    p6L = sq_rec & (c > bb_u) & up233 & (e89 >= e233)
    p6S = sq_rec & (c < bb_l) & dn233 & (e89 <= e233)
    out["PA06_BB_KC_Squeeze_Trend"] = [("ttm3", np.where(p6L, 1, np.where(p6S, -1, 0)).astype(np.int8))]

    # PA07: Causal Pivot BOS + Directional Engulfing (non-repainting 3-bar pivot delay)
    piv_h = s_h.shift(3).rolling(5).max().to_numpy()
    piv_l = s_l.shift(3).rolling(5).min().to_numpy()
    body = np.abs(c - o) / rng
    p7L = (c > piv_h) & (c_prev <= piv_h) & (c > o) & (body >= 0.45) & up233
    p7S = (c < piv_l) & (c_prev >= piv_l) & (c < o) & (body >= 0.45) & dn233
    out["PA07_Causal_BOS_Engulf"] = [("k3_b45", np.where(p7L, 1, np.where(p7S, -1, 0)).astype(np.int8))]

    # PA08: Connors RSI(2) Dip Reclaim inside Stacked Trend
    r2_p = pd.Series(rsi2).shift(1).to_numpy()
    p8L = stkL & (r2_p < 15.0) & (c > o) & (c > h_prev)
    p8S = stkS & (r2_p > 85.0) & (c < o) & (c < l_prev)
    out["PA08_RSI2_Trend_Dip"] = [("r15_rec", np.where(p8L, 1, np.where(p8S, -1, 0)).astype(np.int8))]

    # PA09: MACD Zero-Cross + ADX/DI Trend Surge
    macd = _ema(c, 12) - _ema(c, 26)
    sig_m = _ema(macd, 9)
    hist = macd - sig_m
    hist_p = pd.Series(hist).shift(1).to_numpy()
    p9L = (hist_p <= 0.0) & (hist > 0.0) & stkL & (adx14 >= 20.0) & (pdi > ndi)
    p9S = (hist_p >= 0.0) & (hist < 0.0) & stkS & (adx14 >= 20.0) & (ndi > pdi)
    out["PA09_MACD_ADX_Surge"] = [("adx20", np.where(p9L, 1, np.where(p9S, -1, 0)).astype(np.int8))]

    # PA10: Keltner Channel Runner
    kcu = e20 + 1.75 * atr14
    kcl = e20 - 1.75 * atr14
    kcu_p = pd.Series(kcu).shift(1).to_numpy()
    kcl_p = pd.Series(kcl).shift(1).to_numpy()
    p10L = (c > kcu) & (c_prev <= kcu_p) & stkL & (adx14 >= 20.0)
    p10S = (c < kcl) & (c_prev >= kcl_p) & stkS & (adx14 >= 20.0)
    out["PA10_Keltner_Runner"] = [("k1.75", np.where(p10L, 1, np.where(p10S, -1, 0)).astype(np.int8))]

    return out


def _score_arr(ti: np.ndarray, to: np.ndarray, r: np.ndarray, split_ts: int,
               evaluation_end_ts: int = DELTA_OUTER_TS) -> dict[str, Any]:
    if len(r) == 0:
        return {"n": 0, "r": 0.0, "pf": 0.0, "wr": 0.0, "nis": 0, "isr": 0.0, "ispf": 0.0, "ist": 0.0, "noos": 0, "oosr": 0.0, "oospf": 0.0, "score": -99.0}
    available = to + 3600
    ism = (ti < split_ts) & (available < split_ts)
    oosm = (ti >= split_ts) & (ti < evaluation_end_ts) & (available < evaluation_end_ts)
    ris, roos = r[ism], r[oosm]
    r = r[ism | oosm]
    nis, noos = int(len(ris)), int(len(roos))
    ig, il = float(ris[ris > 0].sum()), float(abs(ris[ris <= 0].sum()))
    is_pf = (ig / il) if il > 0 else (9.9 if ig > 0 else 0.0)
    og, ol = float(roos[roos > 0].sum()), float(abs(roos[roos <= 0].sum()))
    oos_pf = (og / ol) if ol > 0 else (9.9 if og > 0 else 0.0)
    ag, al = float(r[r > 0].sum()), float(abs(r[r <= 0].sum()))
    pf_all = (ag / al) if al > 0 else (9.9 if ag > 0 else 0.0)
    ist = float(ris.mean() / (ris.std() + 1e-9) * math.sqrt(nis)) if nis >= 3 else 0.0
    hmin = float(min(ris[: nis // 2].sum(), ris[nis // 2 :].sum())) if nis >= 6 else (float(ris.sum()) if nis else -99.0)
    stab = 1.0 if hmin > 0 else (0.65 if hmin > -3.0 else 0.25)
    sc = ist * math.sqrt(max(0.2, min(4.0, is_pf))) * stab if (nis >= 6 and ist > 0) else (-10.0 + 0.05 * float(ris.sum()))
    return {
        "n": int(len(r)), "r": round(float(r.sum()), 2), "pf": round(pf_all, 2),
        "wr": round(float((r > 0).mean() * 100.0), 1) if len(r) else 0.0,
        "nis": nis, "isr": round(float(ris.sum()), 2), "ispf": round(is_pf, 2), "ist": round(ist, 2), "hmin": round(hmin, 2),
        "noos": noos, "oosr": round(float(roos.sum()), 2), "oospf": round(oos_pf, 2), "score": float(sc),
    }


def _sim_combo(
    trades_list: list[tuple[str, int, int, float]],
    risk_pct: float = 0.50,
    max_conc: int = 3,
    min_ts: int | None = None,
    nmc: int = 20000,
    run_mc: bool = False,
    max_ts: int | None = None,
) -> dict[str, Any]:
    """Use the same cash, capacity and label clock as the shared offline replay."""
    rows = [dict(sleeve=sid, symbol="DELTA_PRODUCT", mt5_symbol="DELTA_PRODUCT", category="DELTA",
                 bucket="PA", side="", ts_in=ti, ts_out=to, exit_available_ts=to + 3600, r=r)
            for sid, ti, to, r in trades_list]
    model = simulate_shared_account("Delta", rows, risk_pct=risk_pct, max_global_conc=max_conc,
                                    max_per_asset=1, use_vof=False, dd_throttle_1=0,
                                    min_ts=min_ts, max_ts=max_ts, mc_runs=nmc if run_mc else 0)["metrics"]
    monte_carlo = model.get("mc_20k", {})
    return {
        "n": model["trades"], "cagr": model["cagr_pct"], "max_dd": model["max_dd_pct"],
        "calmar": model["calmar"], "pf": model["pf"], "wr": model.get("wr", 0.0),
        "net_r": model.get("net_r", 0.0), "sharpe": model.get("sharpe", 0.0),
        "mc_med_1y": monte_carlo.get("med_1y_pct"), "mc_ploss": monte_carlo.get("p_loss_pct"),
        "mc_dd95": monte_carlo.get("dd95"),
    }


def optimize_delta_symbol(
    prod: dict[str, Any], *, run_mc: bool = True, cost_stress_bps: float = 0.0,
) -> dict[str, Any] | None:
    sym = prod["symbol"]
    p4 = DATA_DIR / "ohlc_4h" / f"{sym}.parquet"
    p1 = DATA_DIR / "ohlc_1h" / f"{sym}.parquet"
    if not p4.is_file() or not p1.is_file():
        return None
    df4 = pq.read_table(p4).to_pandas().sort_values("timestamp").reset_index(drop=True)
    df1 = pq.read_table(p1).to_pandas().sort_values("timestamp").reset_index(drop=True)
    df4 = df4[df4["timestamp"] + 14400 < DELTA_OUTER_TS].reset_index(drop=True)
    df1 = df1[df1["timestamp"] + 3600 < DELTA_OUTER_TS].reset_index(drop=True)
    if len(df4) < 300 or len(df1) < 1000:
        return {"symbol": sym, "verdict": "INSUFFICIENT_HISTORY", "bars_4h": len(df4), "bars_1h": len(df1), "turnover_usd": prod["turnover_usd"]}

    span_d = (int(df4["timestamp"].iloc[-1]) - int(df4["timestamp"].iloc[0])) / 86400.0
    if span_d < 90.0:
        return {"symbol": sym, "verdict": "INSUFFICIENT_HISTORY", "bars_4h": len(df4), "bars_1h": len(df1), "turnover_usd": prod["turnover_usd"], "span_days": round(span_d, 1)}

    # Liquidity-aware realistic round-trip fee + spread + slippage
    turn = float(prod.get("turnover_usd") or 0.0)
    sp = max(4.0, min(40.0, float(prod.get("live_sp_bps") or 8.0)))
    slip = 2.0 if turn >= 5_000_000 else (6.0 if turn >= 500_000 else 15.0)
    rt_cost = float(prod.get("fixed_research_cost_bps", float(prod.get("taker_fee_bps", 5.0)) * 2.0 + sp + slip))
    rt_cost += cost_stress_bps

    e1_ts = np.ascontiguousarray(df1["timestamp"].to_numpy(dtype=np.int64))
    e1_o = np.ascontiguousarray(df1["open"].to_numpy(dtype=np.float64))
    e1_h = np.ascontiguousarray(df1["high"].to_numpy(dtype=np.float64))
    e1_l = np.ascontiguousarray(df1["low"].to_numpy(dtype=np.float64))
    e1_c = np.ascontiguousarray(df1["close"].to_numpy(dtype=np.float64))

    split_ts = DELTA_FIT_TS
    min_is = 8 if span_d >= 270 else 5

    strat_best: list[dict[str, Any]] = []
    strat_trades: dict[str, list[tuple[str, int, int, float]]] = {}

    for tf_l, df_u, tf_sec, mh in (("4H", df4, 14400, 96), ("1H", df1, 3600, 144)):
        s_ts = np.ascontiguousarray(df_u["timestamp"].to_numpy(dtype=np.int64))
        s_end = s_ts + tf_sec
        s_atr = np.ascontiguousarray(_atr_wilder(df_u["high"].to_numpy(dtype=float), df_u["low"].to_numpy(dtype=float), df_u["close"].to_numpy(dtype=float), 14))
        s_c = np.ascontiguousarray(df_u["close"].to_numpy(dtype=np.float64))
        sigs = _build_pa_signals(df_u)

        for sid, variants in sigs.items():
            for cfg_s, sa in variants:
                for sl_m, tp_m, tr_m, be_m in (
                    (2.0, 2.5, 2.5, 1.25),
                    (2.0, 4.0, 3.0, 1.25),
                    (2.0, 6.0, 3.0, 1.25),
                    (2.5, 3.5, 3.0, 1.25),
                    (2.5, 6.0, 3.5, 1.25),
                    (3.0, 6.0, 4.0, 1.5),
                ):
                    o_ti, o_to, o_r = _sim_delta_nb(s_ts, s_end, s_atr, s_c, sa, e1_ts, e1_o, e1_h, e1_l, e1_c, sl_m, tp_m, tr_m, be_m, mh, tf_sec, rt_cost)
                    sc = _score_arr(o_ti, o_to, o_r, split_ts)
                    cand = {
                        "sid": sid, "tf": tf_l, "cfg": f"{cfg_s},sl={sl_m},tp={tp_m},tr={tr_m},be={be_m}",
                        "ti": o_ti, "to": o_to, "ra": o_r, **sc,
                    }
                    prev = next((x for x in strat_best if x["sid"] == sid), None)
                    if prev is None or cand["score"] > prev["score"]:
                        if prev is not None:
                            strat_best.remove(prev)
                        strat_best.append(cand)
                        strat_trades[sid] = [(sid, int(o_ti[k]), int(o_to[k]), float(o_r[k])) for k in range(len(o_r))]

    qual = [x for x in strat_best if x["nis"] >= min_is and x["isr"] > 2.0 and x["ispf"] >= 1.20 and x["ist"] >= 0.65]
    qual.sort(key=lambda x: x["score"], reverse=True)

    # IS Clone Pruning (|corr_IS| <= 0.72)
    picked: list[dict[str, Any]] = []
    if qual:
        is_dm: dict[str, dict[int, float]] = {}
        days_s = set()
        for q in qual:
            dm: dict[int, float] = {}
            for _, tin, tout, rv in strat_trades[q["sid"]]:
                if tin < split_ts and tout + 3600 < split_ts:
                    dk = int(tout // 86400)
                    dm[dk] = dm.get(dk, 0.0) + rv
                    days_s.add(dk)
            is_dm[q["sid"]] = dm
        if len(days_s) >= 8 and len(qual) >= 2:
            mat = pd.DataFrame({q["sid"]: [is_dm[q["sid"]].get(d, 0.0) for d in sorted(days_s)] for q in qual}).fillna(0.0)
            cmat = mat.corr()
            for q in qual:
                if any(abs(float(cmat.loc[q["sid"], p["sid"]])) > 0.72 for p in picked):
                    continue
                picked.append(q)
                if len(picked) >= 5:
                    break
        else:
            picked = qual[:4]

    if not picked:
        return {"symbol": sym, "verdict": "REJECT", "bars_4h": len(df4), "bars_1h": len(df1), "span_days": round(span_d, 1), "turnover_usd": turn}

    comb_trades = [t for p in picked for t in strat_trades[p["sid"]]]
    # Pick best causal risk_pct strictly inside MaxDD <= 12%
    best_is, best_rp = {}, 0.50
    for rp in (0.35, 0.50, 0.75, 1.00):
        cf = _sim_combo(comb_trades, risk_pct=rp, max_conc=1, max_ts=split_ts)
        if cf["n"] >= min_is and cf["max_dd"] <= 12.0 and (
            not best_is or cf["calmar"] > best_is["calmar"]
        ):
            best_is = cf
            best_rp = rp
    if not best_is:
        return {"symbol": sym, "verdict": "REJECT", "turnover_usd": turn}
    best_full = _sim_combo(comb_trades, risk_pct=best_rp, max_conc=1, max_ts=DELTA_OUTER_TS,
                            nmc=20000, run_mc=run_mc)
    best_oos = _sim_combo(comb_trades, risk_pct=best_rp, max_conc=1, min_ts=split_ts, max_ts=DELTA_OUTER_TS)

    if best_full["pf"] >= 1.45 and best_oos["pf"] >= 1.25 and best_full["max_dd"] <= 14.0 and best_full["cagr"] >= 20.0 and turn >= 100_000:
        verdict = "CHAMPION_TREND"
    elif best_full["pf"] >= 1.25 and best_oos["pf"] >= 1.08:
        verdict = "QUALIFIED_TREND"
    else:
        verdict = "WEAK_OR_OVERFIT"

    recipe = [{"sid": p["sid"], "tf": p["tf"], "cfg": p["cfg"], "pf": p["pf"], "oos_pf": p["oospf"]} for p in picked]
    return {
        "symbol": sym,
        "verdict": verdict,
        "span_days": round(span_d, 1),
        "span_start": str(df4["datetime"].iloc[0])[:10],
        "span_end": str(df4["datetime"].iloc[-1])[:10],
        "turnover_usd": round(turn, 0),
        "rt_cost_bps": round(rt_cost, 1),
        "risk_pct": best_rp,
        "fit_asof_ts": DELTA_FIT_TS,
        "outer_holdout_start_ts": DELTA_OUTER_TS,
        "research_status": "SHADOW_TRIAL",
        "train": best_is,
        "recipe": recipe,
        "full": best_full,
        "oos": best_oos,
    }


def run_delta_pa_all(min_turnover: float = 0.0, limit: int = 0) -> dict[str, Any]:
    cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if min_turnover > 0:
        cat = [c for c in cat if float(c.get("turnover_usd") or 0) >= min_turnover]
    if limit > 0:
        cat = cat[:limit]

    print(f"[delta-opt] Running Individual PA Trend Optimization on {len(cat)} Delta Exchange futures...", flush=True)
    results: list[dict[str, Any]] = []
    for idx, prod in enumerate(cat, 1):
        res = optimize_delta_symbol(prod)
        if res:
            results.append(res)
            if res.get("verdict") in ("CHAMPION_TREND", "QUALIFIED_TREND") or idx % 25 == 0:
                f = res.get("full", {})
                o = res.get("oos", {})
                if f:
                    print(f"  [{idx:3d}/{len(cat)}] {res['symbol']:12s} [{res['verdict']:16s}] turn=${res['turnover_usd']:>10,.0f} | strats={len(res['recipe'])} | CAGR={f.get('cagr')}% DD={f.get('max_dd')}% Cal={f.get('calmar')} PF={f.get('pf')} OOS_PF={o.get('pf')} MC1Y={f.get('mc_med_1y')}%", flush=True)

    champs = sorted([r for r in results if r.get("verdict") == "CHAMPION_TREND"], key=lambda x: (x["full"]["calmar"], x["full"]["pf"]), reverse=True)
    quals = sorted([r for r in results if r.get("verdict") == "QUALIFIED_TREND"], key=lambda x: x["full"]["calmar"], reverse=True)

    # Render 4-panel dashboard for Top 20 Champions
    top20 = (champs + quals)[:20]
    if top20:
        plt.style.use("dark_background")
        fig, axes = plt.subplots(2, 2, figsize=(16, 11), facecolor="#0d1117")
        fig.suptitle(f"Delta Exchange India Pure PA Trend Optimization ({len(champs)} Champions | {len(quals)} Qualified / {len(results)} Tested)", fontsize=13.5, fontweight="bold", color="#f0f6fc")

        rev = list(reversed(top20))
        y = np.arange(len(rev))
        lbls = [f"{r['symbol']} (${r['turnover_usd']/1e6:.1f}M)" if r["turnover_usd"] >= 1e6 else f"{r['symbol']} (${r['turnover_usd']/1e3:.0f}K)" for r in rev]
        cols = ["#3fb950" if r["verdict"] == "CHAMPION_TREND" else "#58a6ff" for r in rev]

        axes[0, 0].barh(y - 0.18, [r["full"]["pf"] for r in rev], height=0.35, color=cols, label="Full PF")
        axes[0, 0].barh(y + 0.18, [r["oos"]["pf"] for r in rev], height=0.35, color="#f0f6fc", alpha=0.6, label="Frozen OOS PF")
        axes[0, 0].axvline(1.25, color="#d29922", linestyle="--", linewidth=1.0)
        axes[0, 0].set_yticks(y); axes[0, 0].set_yticklabels(lbls, fontsize=7.5)
        axes[0, 0].set_title("1. Top 20 Delta Futures: Full vs Frozen OOS Profit Factor", fontsize=10)
        axes[0, 0].legend(loc="lower right", fontsize=8)

        axes[0, 1].barh(y, [r["full"]["calmar"] for r in rev], color=cols, height=0.65)
        axes[0, 1].set_yticks(y); axes[0, 1].set_yticklabels([r["symbol"] for r in rev], fontsize=7.5)
        axes[0, 1].set_title("2. Calmar Ratio (Compounded CAGR / MaxDD)", fontsize=10)

        axes[1, 0].barh(y, [r["full"]["cagr"] for r in rev], color=cols, height=0.65)
        axes[1, 0].set_yticks(y); axes[1, 0].set_yticklabels([r["symbol"] for r in rev], fontsize=7.5)
        axes[1, 0].set_title("3. Net Annualized CAGR % (After 15-30bps Delta Cost)", fontsize=10)

        axes[1, 1].barh(y, [r["full"]["mc_med_1y"] for r in rev], color="#a371f7", height=0.65, label="20K MC Median 1Y%")
        axes[1, 1].set_yticks(y); axes[1, 1].set_yticklabels([f"{r['symbol']} (DD95={r['full']['mc_dd95']}%)" for r in rev], fontsize=7.2)
        axes[1, 1].set_title("4. 20,000-Path Monte Carlo Median 1Y Return % & Crash DD95", fontsize=10)

        plt.tight_layout(rect=[0, 0, 1, 0.96])
        ARTIFACT_PNG.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(ARTIFACT_PNG), dpi=140)
        plt.close(fig)

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "total_catalog": len(cat),
        "total_tested": len(results),
        "champions": len(champs),
        "qualified": len(quals),
        "top20_symbols": [r["symbol"] for r in top20],
        "assets": results,
        "chart_png": str(ARTIFACT_PNG),
    }
    REPORT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[delta-opt] DONE: {len(champs)} CHAMPIONS | {len(quals)} QUALIFIED | Saved -> {REPORT_JSON.name}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-turnover", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    run_delta_pa_all(min_turnover=args.min_turnover, limit=args.limit)
