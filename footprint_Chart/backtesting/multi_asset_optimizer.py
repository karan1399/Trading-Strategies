"""Multi-Asset MT5 x Binance Order-Flow Optimization Engine (statsmodels + Correlated 20K MC).

Evaluates all downloaded MT5-intersected Crypto & Metals assets under real MT5 live spread,
overnight swaps, 1m execution latency, asymmetric SL slippage, and dynamic ATR trailing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller

from footprint_Chart.backtesting.portfolio_engine import (
    _build_optimal_8s_trades,
    _dsr_pct,
    _load_funding_arrays,
    _sim_sleeve_1m_exact,
    load_asset_tfs_cached,
)
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from footprint_Chart.data_pipeline.mt5_universe import CORE_12_ASSETS, load_mt5_contract_specs
from strategies.core.fp_features import build_fp_context
from strategies.core.registry import load_signal_fn
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd

SPLIT_2024_TS = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
METALS_START_TS = int(datetime(2025, 12, 11, tzinfo=timezone.utc).timestamp())
MULTI_REPORT_PATH = DATA_DIR / "multi_asset_optimal_report.json"

EXTRA_STRATEGY_SLEEVES: tuple[tuple[str, str, dict[str, Any], float, float], ...] = (
    ("s01_williams_pullback", "S01-FP Williams %R (4h)", {"fp_mode": True, "fp_sl_atr": 2.0}, 2.5, 0.0),
    ("s04_vc_squeeze_breakout", "S04-FP TTM Squeeze (4h)", {"fp_mode": True, "fp_sl_atr": 2.0}, 0.0, 1.0),
    ("s05_zscore_breakout", "S05-FP Vol Z-Breakout (4h)", {"fp_mode": True}, 2.5, 0.0),
    ("s07_daily_pivot_breakout", "S07-FP Daily Pivot (4h)", {"fp_mode": True}, 0.0, 1.0),
)


def compute_hac_stats(r_vals: np.ndarray, maxlags: int = 5) -> dict[str, float]:
    """Compute Newey-West HAC autocorrelation-robust mean, t-stat, and p-value via statsmodels."""
    arr = np.asarray(r_vals, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 8 or float(np.std(arr)) <= 1e-12:
        return {"mean_r": float(np.mean(arr)) if len(arr) else 0.0, "t_hac": 0.0, "p_hac": 1.0}
    res = sm.OLS(arr, np.ones((len(arr), 1), dtype=float)).fit(
        cov_type="HAC", cov_kwds={"maxlags": min(maxlags, max(1, len(arr) // 5))}
    )
    return {
        "mean_r": round(float(res.params[0]), 4),
        "t_hac": round(float(res.tvalues[0]), 3),
        "p_hac": round(float(res.pvalues[0]), 4),
    }


def _fast_s04_masks(g4: pd.DataFrame, fp4: dict[str, Any], params: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """O(N) causal vectorized equivalent of s04_vc_squeeze_breakout with fp_mode=True."""
    from strategies.core.indicators import ema_arr, rolling_std_arr, sma_arr, wilder_atr

    o, h, l, c = fp4["o"], fp4["h"], fp4["l"], fp4["c"]
    n = len(c)
    ml = np.zeros(n, dtype=bool)
    ms = np.zeros(n, dtype=bool)
    if n < 240:
        return ml, ms

    sma20 = sma_arr(c, 20)
    sd20 = rolling_std_arr(c, 20)
    atr_kc = wilder_atr(h, l, c, 14)
    ema200 = ema_arr(c, 200)
    ema50 = ema_arr(c, 50)

    bb_up = sma20 + 2.0 * sd20
    bb_dn = sma20 - 2.0 * sd20
    kc_up = sma20 + 1.5 * atr_kc
    kc_dn = sma20 - 1.5 * atr_kc
    sq = (bb_up < kc_up) & (bb_dn > kc_dn)
    sq3 = pd.Series(sq).shift(1).rolling(3).min().fillna(0.0).to_numpy() > 0.5

    atr = fp4["atr20"]
    e89 = fp4["ema89"]
    e89_p = pd.Series(e89).shift(3).to_numpy(dtype=float)
    e233 = fp4["ema233"]
    dp = fp4["d_pct"]
    clv = fp4["clv"]
    bi = fp4["buy_imb"]
    si = fp4["sell_imb"]
    vpm = fp4["vpm_rk"]
    vr4 = fp4["vr4"]

    min_vpm = float(params.get("fp_min_vpm", 0.40))
    min_vr4 = float(params.get("fp_min_vr4", 0.95))
    valid = np.isfinite(atr) & (atr > 0.0) & np.isfinite(e89) & np.isfinite(e89_p) & np.isfinite(e233) & sq3
    valid[:235] = False
    valid[-3:] = False

    ml = (
        valid & (c > bb_up) & (c >= o) & (c > ema200) & (ema50 >= ema200)
        & (e89 > e233) & (e89 > e89_p) & (c > e233) & (clv > 0.30) & (dp > 3.0)
        & (bi >= si) & (vpm > min_vpm) & (vr4 > min_vr4)
    )
    ms = (
        valid & (c < bb_dn) & (c <= o) & (c < ema200) & (ema50 <= ema200)
        & (e89 < e233) & (e89 < e89_p) & (c < e233) & (clv < -0.30) & (dp < -3.0)
        & (si >= bi) & (vpm > min_vpm) & (vr4 > min_vr4)
    )
    return ml, ms


def _fast_s07_masks(g4: pd.DataFrame, fp4: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """O(N) causal equivalent of s07_daily_pivot_breakout with fp_mode=True."""
    from strategies.core.indicators import ema_arr
    from strategies.s07_daily_pivot_breakout.strategy import _d1_bars_from_arrays

    p = params or {}
    h, l, c = fp4["h"], fp4["l"], fp4["c"]
    tm = (g4["ts"].to_numpy() // 60).astype(np.int64)
    n = len(c)
    ml = np.zeros(n, dtype=bool)
    ms = np.zeros(n, dtype=bool)
    if n < 240:
        return ml, ms

    e50 = ema_arr(c, 50)
    e200 = ema_arr(c, 200)
    atr = fp4["atr20"]
    e233 = fp4["ema233"]
    poc = fp4["poc"]
    clv = fp4["clv"]
    dp = fp4["d_pct"]
    bi = fp4["buy_imb"]
    si = fp4["sell_imb"]

    uniq_days, dh, dl, d_end = _d1_bars_from_arrays(h, l, c, tm)
    k = int(p.get("swing_k", 3))
    max_levels = int(p.get("max_levels", 3))
    # Precompute daily swing confirmation day index and max-broken close barrier in O(D)
    n_d = len(uniq_days)
    piv_h: list[tuple[int, float]] = []
    piv_l: list[tuple[int, float]] = []
    for j in range(k, n_d - k):
        conf_b = int(d_end[j + k])
        w_h = dh[j - k : j + k + 1]
        w_l = dl[j - k : j + k + 1]
        if dh[j] == float(np.max(w_h)):
            piv_h.append((conf_b, float(dh[j])))
        if dl[j] == float(np.min(w_l)):
            piv_l.append((conf_b, float(dl[j])))

    d_idx = 0
    for i in range(235, n - 3):
        a_i = float(atr[i])
        if not np.isfinite(a_i) or a_i <= 0.0:
            continue
        cur_day = int(tm[i] // 1440)
        while d_idx + 1 < n_d and uniq_days[d_idx] < cur_day:
            d_idx += 1
        # Exclude forming UTC day and last k unconfirmed days
        n_conf_days = d_idx - k
        if n_conf_days < k + 2:
            continue
        max_conf_bar = int(d_end[n_conf_days - 1 + k]) if (n_conf_days - 1 + k) < n_d else i
        c_prev, c_cur = float(c[i - 1]), float(c[i])
        up_ok = (e50[i] > e200[i] and c_cur > e200[i]) and (c_cur > c_prev)
        dn_ok = (e50[i] < e200[i] and c_cur < e200[i]) and (c_cur < c_prev)
        if up_ok:
            if np.isfinite(e233[i]) and c_cur > e233[i] and clv[i] > 0.25 and dp[i] > 0.0 and int(bi[i]) >= int(si[i]) and (not np.isfinite(poc[i]) or c_cur >= poc[i]):
                active_h = []
                for cb, lvl in piv_h:
                    if cb < i and cb <= max_conf_bar and lvl >= c_prev:
                        fl = cb + 1
                        if fl >= i or float(np.max(c[fl:i])) <= lvl:
                            active_h.append(lvl)
                near_h = sorted(active_h)[:max_levels]
                if any(c_prev <= lvl < c_cur for lvl in near_h):
                    ml[i] = True
        if dn_ok and not ml[i]:
            if np.isfinite(e233[i]) and c_cur < e233[i] and clv[i] < -0.25 and dp[i] < 0.0 and int(si[i]) >= int(bi[i]) and (not np.isfinite(poc[i]) or c_cur <= poc[i]):
                active_l = []
                for cb, lvl in piv_l:
                    if cb < i and cb <= max_conf_bar and lvl <= c_prev:
                        fl = cb + 1
                        if fl >= i or float(np.min(c[fl:i])) >= lvl:
                            active_l.append(lvl)
                near_l = sorted(active_l, reverse=True)[:max_levels]
                if any(c_prev >= lvl > c_cur for lvl in near_l):
                    ms[i] = True
    return ml, ms


def _run_sig_sleeve_4h(
    g4: pd.DataFrame,
    fp4: dict[str, Any],
    fn,
    params: dict[str, Any],
    name: str,
    trail_atr: float,
    be_r: float,
    m1_args: tuple[np.ndarray, ...],
    sim_kw: dict[str, Any],
    sid: str = "",
) -> list[dict[str, Any]]:
    o, h, l, c = fp4["o"], fp4["h"], fp4["l"], fp4["c"]
    tm = (g4["ts"].to_numpy() // 60).astype(np.int64)
    atr = fp4["atr20"]
    n = len(g4)
    if sid == "s04_vc_squeeze_breakout":
        ml, ms = _fast_s04_masks(g4, fp4, params)
        sl_atr, tp_r, max_hold = 2.0, 0.0, 96
    elif sid == "s07_daily_pivot_breakout":
        ml, ms = _fast_s07_masks(g4, fp4, params)
        sl_atr, tp_r, max_hold = 2.0, 0.0, 96
    else:
        ml = np.zeros(n, dtype=bool)
        ms = np.zeros(n, dtype=bool)
        extra = {"fp": fp4}
        sig_meta: dict[str, Any] = {}
        for i in range(235, n - 3):
            sig = fn(o, h, l, c, tm, atr, i, params, extra)
            if sig is not None:
                if sig["side"] == 1:
                    ml[i] = True
                elif sig["side"] == -1:
                    ms[i] = True
                if not sig_meta:
                    sig_meta = sig
        sl_atr = float(params.get("fp_sl_atr", sig_meta.get("sl_atr", 2.0)))
        tp_r = float(sig_meta.get("exit_r", 0.0)) if trail_atr <= 0 else 0.0
        max_hold = int(sig_meta.get("max_hold", 96))
    return _sim_sleeve_1m_exact(
        g4,
        pd.Series(ml, index=g4.index),
        pd.Series(ms, index=g4.index),
        14400,
        name,
        *m1_args,
        sl_atr=sl_atr,
        tp_r=tp_r,
        trail_atr=trail_atr,
        be_r=be_r,
        be_pad=0.2 if tp_r > 0 else 0.05,
        step_lock=False,
        max_hold=max_hold,
        dyn_atr=(trail_atr > 0),
        **sim_kw,
    )


def evaluate_symbol_all_sleeves(
    symbol: str,
    data_dir: Path = DATA_DIR,
    cost_mode: str = "mt5",
    specs_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build all 12 candidate sleeves (Optimal 8 + S01/S04/S05/S07-FP) for one asset under MT5/Binance costs."""
    sym = symbol.upper()
    specs = specs_map or load_mt5_contract_specs(data_dir)
    spec = specs.get(sym, {"binance": sym, "mt5": sym, "cat": "CRYPTO", "sp_bps": 12.0, "sw_l_bps": 0.0, "sw_s_bps": 0.0, "roll3d": 5})
    mt5_cost = {**spec, "symbol": sym} if cost_mode == "mt5" else None

    g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = load_asset_tfs_cached(sym, data_dir=data_dir, adaptive_tick=True)
    f_ts, f_rt = _load_funding_arrays(sym, data_dir=data_dir)
    m1_args = (m1_ts, m1_o, m1_h, m1_l, m1_c)

    opt8_trades = _build_optimal_8s_trades(
        g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c,
        cost_bps=12.0, funding_bps_8h=1.0, entry_delay_mins=1,
        realistic_costs=True, f_ts=f_ts, f_rt=f_rt, mt5_cost=mt5_cost,
    )
    for t in opt8_trades:
        t["symbol"] = sym

    fp4 = build_fp_context(g4)
    sim_kw = dict(
        cost_bps=12.0, fund_bps_8h=1.0, entry_delay_mins=1,
        realistic_costs=True, f_ts=f_ts, f_rt=f_rt, mt5_cost=mt5_cost,
    )
    extra_trades: list[dict[str, Any]] = []
    for sid, label, params, tr_atr, be_r in EXTRA_STRATEGY_SLEEVES:
        fn = load_signal_fn(sid)
        trs = _run_sig_sleeve_4h(g4, fp4, fn, params, label, tr_atr, be_r, m1_args, sim_kw, sid=sid)
        for t in trs:
            t["symbol"] = sym
        extra_trades.extend(trs)

    all_trades = opt8_trades + extra_trades
    sleeve_stats: list[dict[str, Any]] = []
    for s_name in sorted({t["sleeve"] for t in all_trades}):
        sub = [t for t in all_trades if t["sleeve"] == s_name]
        arr = np.array([t["r"] for t in sub], dtype=float)
        is_sub = np.array([t["r"] for t in sub if t["ts_in"] < SPLIT_2024_TS], dtype=float)
        oos_sub = np.array([t["r"] for t in sub if t["ts_in"] >= SPLIT_2024_TS], dtype=float)
        hac = compute_hac_stats(arr)
        hac_is = compute_hac_stats(is_sub)
        is_g = float(is_sub[is_sub > 0].sum()) if len(is_sub) else 0.0
        is_l = float(abs(is_sub[is_sub < 0].sum())) if len(is_sub) else 0.0
        is_pf = (is_g / is_l) if is_l > 0 else (9.99 if is_g > 0 else 0.0)
        sleeve_stats.append({
            "symbol": sym,
            "sleeve": s_name,
            "trades": len(arr),
            "wr": round(float((arr > 0).mean() * 100.0), 1) if len(arr) else 0.0,
            "sum_r": round(float(arr.sum()), 2),
            "is_trades": int(len(is_sub)),
            "is_r": round(float(is_sub.sum()), 2) if len(is_sub) else 0.0,
            "is_pf": round(is_pf, 2),
            "is_t_hac": hac_is["t_hac"],
            "oos_trades": int(len(oos_sub)),
            "oos_r": round(float(oos_sub.sum()), 2) if len(oos_sub) else 0.0,
            "t_hac": hac["t_hac"],
            "p_hac": hac["p_hac"],
        })

    return {
        "symbol": sym,
        "spec": spec,
        "g4": g4,
        "span_start": datetime.fromtimestamp(int(g4["ts"].iloc[0]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "span_end": datetime.fromtimestamp(int(g4["ts"].iloc[-1]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "opt8_trades": opt8_trades,
        "all_trades": all_trades,
        "sleeve_stats": sleeve_stats,
    }


def _correlated_block_mc_20k(
    daily_df: pd.DataFrame,
    start_eq: float,
    risk_pct: float,
    n_mc: int = 20000,
) -> dict[str, Any]:
    """Date-aligned 8-day block + crash-shock 20,000 Monte Carlo preserving cross-asset crash correlation."""
    arr = daily_df["port_r"].to_numpy(dtype=float)
    if len(arr) < 20:
        return {}
    rng = np.random.default_rng(2026)
    rp = risk_pct / 100.0
    horizon_days = min(365, len(arr))
    blk = 8
    n_blk = (horizon_days + blk - 1) // blk
    starts = rng.integers(0, max(1, len(arr) - blk + 1), size=(n_mc, n_blk))
    flat_idx = (starts[:, :, None] + np.arange(blk)[None, None, :]).reshape(n_mc, -1)[:, :horizon_days]
    samp = arr[flat_idx]
    eq_b = start_eq * np.cumprod(np.maximum(0.01, 1.0 + samp * rp), axis=1)
    eq_bf = np.hstack([np.full((n_mc, 1), start_eq), eq_b])
    pk_b = np.maximum.accumulate(eq_bf, axis=1)
    dds_blk = ((pk_b - eq_bf) / pk_b * 100.0).max(axis=1)
    ends_blk = eq_b[:, -1]

    # Synchronized portfolio crash shock (-4.5R across 3 consecutive market shock days)
    samp_cr = samp.copy()
    if horizon_days >= 10:
        cpos = rng.integers(0, horizon_days - 3, size=n_mc)
        rows = np.arange(n_mc)
        samp_cr[rows, cpos] += -1.5
        samp_cr[rows, cpos + 1] += -1.5
        samp_cr[rows, cpos + 2] += -1.5
    eq_cr = start_eq * np.cumprod(np.maximum(0.01, 1.0 + samp_cr * rp), axis=1)
    eq_crf = np.hstack([np.full((n_mc, 1), start_eq), eq_cr])
    pk_cr = np.maximum.accumulate(eq_crf, axis=1)
    dds_cr = ((pk_cr - eq_crf) / pk_cr * 100.0).max(axis=1)

    return {
        "runs": int(n_mc),
        "horizon_days": int(horizon_days),
        "blk_eq_p5": round(float(np.percentile(ends_blk, 5)), 0),
        "blk_eq_p50": round(float(np.percentile(ends_blk, 50)), 0),
        "blk_eq_p95": round(float(np.percentile(ends_blk, 95)), 0),
        "blk_dd_p95": round(float(np.percentile(dds_blk, 95)), 1),
        "p_loss_pct": round(float((ends_blk < start_eq).mean() * 100.0), 2),
        "crash_dd_p95": round(float(np.percentile(dds_cr, 95)), 1),
        "crash_p_loss_pct": round(float((eq_cr[:, -1] < start_eq).mean() * 100.0), 2),
    }


def simulate_multi_asset_portfolio(
    trades: list[dict[str, Any]],
    asset_frames: dict[str, pd.DataFrame],
    start_equity: float = 5000.0,
    risk_pct: float = 1.25,
    max_concurrent: int = 4,
    max_same_asset: int = 1,
    per_asset_max: dict[str, int] | None = None,
    max_same_side: int = 3,
    weight_mode: str = "corr_erc",
    asset_weights: dict[str, float] | None = None,
    min_ts: int | None = None,
    mc_runs: int = 20000,
    name: str = "Multi-Asset Portfolio",
) -> dict[str, Any]:
    """Chronologically simulate multi-asset portfolio with concurrency, corr/vOF sizing, statsmodels & 20K MC."""
    flt = [t for t in trades if (min_ts is None or t["ts_in"] >= min_ts)]
    sorted_t = sorted(flt, key=lambda x: x["ts_in"])
    active: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    pam = per_asset_max or {}
    for t in sorted_t:
        active = [x for x in active if x["ts_out"] > t["ts_in"]]
        sym_t = t.get("symbol")
        same_side = sum(1 for x in active if x["side"] == t["side"])
        same_asset = sum(1 for x in active if x.get("symbol") == sym_t)
        cap_asset = int(pam.get(sym_t, max_same_asset))
        if len(active) < max_concurrent and same_side < max_same_side and same_asset < cap_asset:
            executed.append(t)
            active.append(t)

    if not executed:
        return {"name": name, "metrics": {"total_trades": 0, "end_equity": start_equity}, "sleeves": [], "assets": []}

    # Pre-extract numpy references for s08 vOF sizing per symbol
    vof_refs: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    for sym, g4 in asset_frames.items():
        bi = g4["buy_imb"].to_numpy(dtype=int) if "buy_imb" in g4 else np.zeros(len(g4), dtype=int)
        si = g4["sell_imb"].to_numpy(dtype=int) if "sell_imb" in g4 else np.zeros(len(g4), dtype=int)
        vof_refs[sym] = (
            g4["c"].to_numpy(dtype=float),
            g4["end_ts"].to_numpy(),
            g4["d_pct"].to_numpy(dtype=float),
            bi - si,
            bi + si,
            g4["vpin_rank"].to_numpy(dtype=float),
        )

    eq = float(start_equity)
    pk = float(start_equity)
    max_dd_pct = 0.0
    max_dd_usd = 0.0
    rp = float(risk_pct) / 100.0
    aw = asset_weights or {}

    r_vals: list[float] = []
    dd_vals: list[float] = [0.0]
    curve: list[dict[str, Any]] = [{"ts": int(executed[0]["ts_in"]), "equity": round(eq, 2)}]
    by_asset: dict[str, dict[str, Any]] = {}
    by_sleeve: dict[str, dict[str, Any]] = {}
    by_year: dict[int, list[float]] = {}
    by_regime: dict[str, list[float]] = {}
    is_r: list[float] = []
    oos_r: list[float] = []
    daily_r_map: dict[int, dict[str, float]] = {}

    for t in executed:
        sym = t.get("symbol", "BTCUSDT")
        w_mult = float(aw.get(sym, 1.0))
        base_usd = eq * rp * w_mult
        if weight_mode == "vof" and sym in vof_refs:
            ca, ea, dpa, imba, sum_imba, vpa = vof_refs[sym]
            idx_r = max(0, int(np.searchsorted(ea, t["ts_in"], side="right")) - 1)
            s_sign = 1 if t["side"] == "BUY" else -1
            imb_val = int(imba[idx_r]) if sum_imba[idx_r] > 0 else s_sign
            scaled_usd = vol_of_conviction_risk_usd(
                ca[max(0, idx_r - 245) : idx_r + 1], "H4", s_sign,
                float(dpa[idx_r]), imb_val, float(vpa[idx_r]),
                base_usd, base_usd * 0.65, base_usd * 1.25, target_ann_vol=0.55, lookback=240,
            )
            eff_r = float(t["r"]) * (scaled_usd / (eq * rp) if eq > 0 else w_mult)
            pnl = scaled_usd * float(t["r"])
        else:
            eff_r = float(t["r"]) * w_mult
            pnl = base_usd * float(t["r"])

        eq += pnl
        pk = max(pk, eq)
        dd_usd = pk - eq
        dd_pct = (dd_usd / pk * 100.0) if pk > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_pct)
        max_dd_usd = max(max_dd_usd, dd_usd)
        dd_vals.append(dd_pct)
        r_vals.append(eff_r)
        curve.append({"ts": int(t["ts_out"]), "equity": round(eq, 2)})

        day_k = int(t["ts_out"] // 86400)
        dm = daily_r_map.setdefault(day_k, {})
        dm["port_r"] = dm.get("port_r", 0.0) + eff_r
        dm[sym] = dm.get(sym, 0.0) + eff_r

        yr = int(t["year"])
        by_year.setdefault(yr, []).append(eff_r)
        by_regime.setdefault(t["regime"], []).append(eff_r)
        if t["ts_in"] < SPLIT_2024_TS:
            is_r.append(eff_r)
        else:
            oos_r.append(eff_r)

        ast = by_asset.setdefault(sym, {"trades": 0, "wins": 0, "sum_r": 0.0, "pnl_usd": 0.0})
        ast["trades"] += 1
        ast["wins"] += 1 if t["r"] > 0 else 0
        ast["sum_r"] += eff_r
        ast["pnl_usd"] += pnl

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
    gains = float(arr[arr > 0].sum())
    losses = float(abs(arr[arr < 0].sum()))
    pf = (gains / losses) if losses > 0 else 9.99
    wr = float((arr > 0).mean() * 100.0)
    ulcer = float(math.sqrt(np.mean(np.square(dd_vals)))) if dd_vals else 0.0
    hac = compute_hac_stats(arr)

    oos_arr = np.array(oos_r, dtype=float)
    oos_g = float(oos_arr[oos_arr > 0].sum()) if len(oos_arr) else 0.0
    oos_l = float(abs(oos_arr[oos_arr < 0].sum())) if len(oos_arr) else 0.0
    oos_pf = (oos_g / oos_l) if oos_l > 0 else (9.99 if oos_g > 0 else 0.0)
    oos_wr = float((oos_arr > 0).mean() * 100.0) if len(oos_arr) else 0.0
    oeq, opk, o_mdd = 1.0, 1.0, 0.0
    for rv in oos_arr:
        oeq *= max(0.01, 1.0 + float(rv) * rp)
        opk = max(opk, oeq)
        o_mdd = max(o_mdd, (opk - oeq) / opk * 100.0)

    # Calendar-day aligned daily return DataFrame for correlated block MC + statsmodels ADF/corr
    d_min = min(daily_r_map.keys())
    d_max = max(daily_r_map.keys())
    all_days = list(range(d_min, d_max + 1))
    d_df = pd.DataFrame([daily_r_map.get(d, {}) for d in all_days], index=all_days).fillna(0.0)
    if "port_r" not in d_df:
        d_df["port_r"] = 0.0
    mc_20k = _correlated_block_mc_20k(d_df, float(start_equity), float(risk_pct), n_mc=mc_runs)

    try:
        adf_res = adfuller(d_df["port_r"].to_numpy(dtype=float), maxlag=10)
        adf_p = round(float(adf_res[1]), 5)
    except Exception:
        adf_p = 1.0

    # Annual breakdown
    annual = []
    for yr in sorted(by_year.keys()):
        ya = np.array(by_year[yr], dtype=float)
        yeq, ypk, ymdd = 1.0, 1.0, 0.0
        for rv in ya:
            yeq *= max(0.01, 1.0 + rv * rp)
            ypk = max(ypk, yeq)
            ymdd = max(ymdd, (ypk - yeq) / ypk * 100.0)
        annual.append({
            "year": yr,
            "trades": int(len(ya)),
            "wr": round(float((ya > 0).mean() * 100.0), 1),
            "sum_r": round(float(ya.sum()), 2),
            "ret_pct": round(float((yeq - 1.0) * 100.0), 1),
            "max_dd_pct": round(float(ymdd), 1),
            "oos": yr >= 2024,
        })

    return {
        "name": name,
        "span_start": datetime.fromtimestamp(int(executed[0]["ts_in"]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "span_end": datetime.fromtimestamp(int(executed[-1]["ts_out"]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "years_span": round(yrs, 2),
        "metrics": {
            "start_equity": round(start_equity, 2),
            "end_equity": round(eq, 2),
            "cagr_pct": round(cagr, 1),
            "total_trades": len(arr),
            "win_rate": round(wr, 1),
            "total_r": round(float(arr.sum()), 2),
            "profit_factor": round(pf, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "max_dd_usd": round(max_dd_usd, 2),
            "calmar_ratio": round(calmar, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "ulcer_index": round(ulcer, 2),
            "t_hac": hac["t_hac"],
            "p_hac": hac["p_hac"],
            "adf_daily_p": adf_p,
            "dsr_40_pct": _dsr_pct(arr, 40.0),
            "is_r": round(float(sum(is_r)), 2),
            "oos_r": round(float(sum(oos_r)), 2),
            "oos_trades": int(len(oos_arr)),
            "oos_wr": round(oos_wr, 1),
            "oos_pf": round(oos_pf, 2),
            "oos_dd_pct": round(o_mdd, 2),
            "oos_ret_pct": round(float((oeq - 1.0) * 100.0), 1),
            "mc_20k": mc_20k,
            "annual": annual,
            "dd_curve_pct": [round(float(x), 2) for x in dd_vals],
        },
        "assets": sorted(
            [
                {
                    "symbol": k,
                    "trades": v["trades"],
                    "win_rate": round(v["wins"] / v["trades"] * 100.0, 1) if v["trades"] else 0.0,
                    "sum_r": round(v["sum_r"], 2),
                    "pnl_usd": round(v["pnl_usd"], 2),
                }
                for k, v in by_asset.items()
            ],
            key=lambda x: x["pnl_usd"],
            reverse=True,
        ),
        "sleeves": sorted(
            [
                {
                    "name": k,
                    "trades": v["trades"],
                    "win_rate": round(v["wins"] / v["trades"] * 100.0, 1) if v["trades"] else 0.0,
                    "sum_r": round(v["sum_r"], 2),
                    "pnl_usd": round(v["pnl_usd"], 2),
                }
                for k, v in by_sleeve.items()
            ],
            key=lambda x: x["pnl_usd"],
            reverse=True,
        ),
        "equity_curve": curve[::max(1, len(curve) // 450)] + ([curve[-1]] if curve else []),
        "daily_df": d_df,
    }


def compute_cross_asset_regression(daily_df: pd.DataFrame) -> dict[str, Any]:
    """Run statsmodels OLS alpha/beta vs BTCUSDT and return pairwise correlation matrix."""
    asset_cols = [c for c in daily_df.columns if c != "port_r"]
    corr = daily_df[asset_cols].corr().round(3).to_dict() if len(asset_cols) >= 2 else {}
    btc_col = "BTCUSDT" if "BTCUSDT" in asset_cols else (asset_cols[0] if asset_cols else None)
    ols_vs_btc: dict[str, dict[str, Any]] = {}
    if btc_col and len(daily_df) > 30:
        x = sm.add_constant(daily_df[btc_col].to_numpy(dtype=float))
        for sym in asset_cols:
            if sym == btc_col:
                continue
            y = daily_df[sym].to_numpy(dtype=float)
            mask = (y != 0) | (daily_df[btc_col].to_numpy(dtype=float) != 0)
            if mask.sum() < 25:
                continue
            fit = sm.OLS(y[mask], x[mask]).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
            ols_vs_btc[sym] = {
                "ann_alpha_r": round(float(fit.params[0]) * 252.0, 2),
                "btc_beta": round(float(fit.params[1]), 3),
                "alpha_t_hac": round(float(fit.tvalues[0]), 2),
                "alpha_p_hac": round(float(fit.pvalues[0]), 4),
                "r2": round(float(fit.rsquared), 3),
            }
    return {"corr": corr, "ols_vs_btc": ols_vs_btc}


def _causal_wf_trades(expanded_all: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strictly causal expanding walk-forward selector for OOS 2024+: year Y only sees trades ending < Jan 1 Y."""
    out: list[dict[str, Any]] = []
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for t in expanded_all:
        by_pair.setdefault((str(t.get("symbol", "BTCUSDT")), str(t.get("sleeve", ""))), []).append(t)
    for yr in (2024, 2025, 2026):
        cut_ts = int(datetime(yr, 1, 1, tzinfo=timezone.utc).timestamp())
        next_ts = int(datetime(yr + 1, 1, 1, tzinfo=timezone.utc).timestamp())
        for (sym, slv), sub in by_pair.items():
            hist = np.array([t["r"] for t in sub if t["ts_out"] < cut_ts], dtype=float)
            if sym == "BTCUSDT" and "-FP " not in slv:
                # BTC Opt8 baseline is always eligible; alts/extra sleeves require prior positive WF proof
                pass
            elif len(hist) < 8:
                continue
            else:
                g = float(hist[hist > 0].sum())
                l = float(abs(hist[hist < 0].sum()))
                pf = (g / l) if l > 0 else (9.99 if g > 0 else 0.0)
                if float(hist.sum()) <= 2.0 or pf < 1.12:
                    continue
            out.extend([t for t in sub if cut_ts <= t["ts_in"] < next_ts])
    return out


def optimize_per_asset_strict_is_oos(
    per_symbol: dict[str, dict[str, Any]],
    data_dir: Path = DATA_DIR,
    cost_mode: str = "mt5",
    specs_map: dict[str, dict[str, Any]] | None = None,
    mc_runs: int = 5000,
) -> dict[str, Any]:
    """Optimize all 13 strategy families individually per asset strictly IS -> Frozen OOS with DSR + plateau defense."""
    from datetime import datetime, timezone
    from footprint_Chart.backtesting.fast_kernels import sim_1m_trades_nb
    from strategies.core.indicators import adx_arr, ema_arr, rolling_std_arr, sma_arr, wilder_atr

    specs = specs_map or load_mt5_contract_specs(data_dir)
    asset_frames = {sym: res["g4"] for sym, res in per_symbol.items()}

    def _arrays_to_dicts(sym: str, slv: str, df: pd.DataFrame, res_tup: tuple[np.ndarray, ...]) -> list[dict[str, Any]]:
        o_bi, o_ti, o_to, o_sd, o_rsn, o_ent, o_exp, o_r, o_fr, o_reg = res_tup
        rsn_map = ("TIME", "SL", "TRAIL", "TP")
        reg_map = ("TRANSITION", "BULL", "BEAR")
        n_b = max(1, len(df))
        out: list[dict[str, Any]] = []
        for k in range(len(o_bi)):
            bi_k = int(o_bi[k])
            ti_k = int(o_ti[k])
            ent = float(o_ent[k])
            exp = float(o_exp[k])
            px_r = 4 if ent < 100.0 else 2
            out.append({
                "symbol": sym,
                "sleeve": slv,
                "bar_i": bi_k,
                "ts_in": ti_k,
                "ts_out": int(o_to[k]),
                "year": int(datetime.fromtimestamp(ti_k, tz=timezone.utc).year),
                "regime": reg_map[int(o_reg[k])],
                "side": "BUY" if int(o_sd[k]) == 1 else "SELL",
                "entry": round(ent, px_r),
                "exit": round(exp, px_r),
                "reason": rsn_map[int(o_rsn[k])],
                "r": float(o_r[k]),
                "fee_r": float(o_fr[k]),
                "q": min(3, (bi_k * 4) // n_b),
            })
        return out

    def _eval_split(o_ti: np.ndarray, o_r: np.ndarray, o_fr: np.ndarray, split_ts: int, k_trials: int) -> dict[str, Any]:
        if len(o_ti) == 0:
            return {"nis": 0, "isr": 0.0, "ispf": 0.0, "ist": 0.0, "dsr": 0.0, "edge_cost": 0.0, "half_min": -99.0, "raw_score": -99.0, "noos": 0, "oosr": 0.0, "oospf": 0.0}
        is_m = o_ti < split_ts
        oos_m = ~is_m
        ris = o_r[is_m]
        fis = o_fr[is_m]
        roos = o_r[oos_m]
        nis = int(len(ris))
        noos = int(len(roos))
        is_g = float(ris[ris > 0].sum()) if nis else 0.0
        is_l = float(abs(ris[ris < 0].sum())) if nis else 0.0
        is_pf = (is_g / is_l) if is_l > 0 else (9.9 if is_g > 0 else 0.0)
        oos_g = float(roos[roos > 0].sum()) if noos else 0.0
        oos_l = float(abs(roos[roos < 0].sum())) if noos else 0.0
        oos_pf = (oos_g / oos_l) if oos_l > 0 else (9.9 if oos_g > 0 else 0.0)
        is_t = compute_hac_stats(ris)["t_hac"] if nis >= 8 else 0.0
        dsr = _dsr_pct(ris, max(2, k_trials)) if nis >= 10 else 0.0
        mean_r = float(ris.mean()) if nis else 0.0
        mean_f = float(fis.mean()) if nis else 0.0
        edge_cost = (mean_r + mean_f) / max(mean_f, 0.02) if nis else 0.0
        if nis >= 6:
            h1 = float(ris[: nis // 2].sum())
            h2 = float(ris[nis // 2 :].sum())
            half_min = min(h1, h2)
        else:
            half_min = -99.0
        # Stability-penalized IS score (zero OOS leakage)
        stab = 1.0 if half_min > 0.0 else (0.75 if half_min > -6.0 else 0.40)
        raw_score = float(is_t) * math.sqrt(max(0.25, min(4.0, is_pf))) * stab if is_t > 0 else -1.0
        return {
            "nis": nis,
            "isr": round(float(ris.sum()), 2) if nis else 0.0,
            "ispf": round(float(is_pf), 2),
            "ist": round(float(is_t), 2),
            "dsr": round(float(dsr), 1),
            "edge_cost": round(float(edge_cost), 2),
            "half_min": round(float(half_min), 2),
            "raw_score": raw_score,
            "noos": noos,
            "oosr": round(float(roos.sum()), 2) if noos else 0.0,
            "oospf": round(float(oos_pf), 2),
        }

    all_trades: list[dict[str, Any]] = []
    tight_trades: list[dict[str, Any]] = []
    alts_trades: list[dict[str, Any]] = []
    per_asset_recipes: list[dict[str, Any]] = []

    for sym, res in per_symbol.items():
        spec = res.get("spec") or specs.get(sym, {})
        is_metal = spec.get("cat") == "METAL"
        sp_bps = float(spec.get("sp_bps", 12.0))
        use_mt5 = cost_mode == "mt5"
        roll3d = int(spec.get("roll3d", 5))
        sw_l_bps = float(spec.get("sw_l_bps", 0.0))
        sw_s_bps = float(spec.get("sw_s_bps", 0.0))

        g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = load_asset_tfs_cached(sym, data_dir, adaptive_tick=True)
        f_ts, f_rt = _load_funding_arrays(sym, data_dir)
        m1_ts_c = np.ascontiguousarray(m1_ts, dtype=np.int64)
        m1_o_c = np.ascontiguousarray(m1_o, dtype=np.float64)
        m1_h_c = np.ascontiguousarray(m1_h, dtype=np.float64)
        m1_l_c = np.ascontiguousarray(m1_l, dtype=np.float64)
        m1_c_c = np.ascontiguousarray(m1_c, dtype=np.float64)
        fts_c = np.ascontiguousarray(f_ts, dtype=np.int64) if len(f_ts) else np.empty(0, dtype=np.int64)
        frt_c = np.ascontiguousarray(f_rt, dtype=np.float64) if len(f_rt) else np.empty(0, dtype=np.float64)

        fp4 = build_fp_context(g4)
        fp2 = build_fp_context(g2)

        def _prep_df_arrs(df_u: pd.DataFrame) -> tuple[np.ndarray, ...]:
            return (
                np.ascontiguousarray(df_u["ts"].to_numpy(), dtype=np.int64),
                np.ascontiguousarray(df_u["end_ts"].to_numpy(), dtype=np.int64),
                np.ascontiguousarray(df_u["atr"].to_numpy(), dtype=np.float64),
                np.ascontiguousarray(df_u["ema89"].to_numpy(), dtype=np.float64),
                np.ascontiguousarray(df_u["ema233"].to_numpy(), dtype=np.float64),
                np.ascontiguousarray(df_u["c"].to_numpy(), dtype=np.float64),
            )

        a4 = _prep_df_arrs(g4)
        a2 = _prep_df_arrs(g2)
        split_ts = SPLIT_2024_TS if (int(g4["ts"].iloc[0]) < SPLIT_2024_TS - 180 * 86400) else int(g4["ts"].iloc[len(g4) // 2])
        min_is_n = 10 if is_metal else 20

        # Vectorized base series for g4
        c4 = g4["c"].to_numpy(dtype=np.float64)
        h4 = g4["h"].to_numpy(dtype=np.float64)
        l4 = g4["l"].to_numpy(dtype=np.float64)
        e89_4, e233_4 = a4[3], a4[4]
        up233 = c4 > e233_4
        dn233 = c4 < e233_4
        e89_s3 = pd.Series(e89_4).shift(3).to_numpy(dtype=np.float64)
        stkL = (e89_4 > e233_4) & (e89_4 > e89_s3) & up233
        stkS = (e89_4 < e233_4) & (e89_4 < e89_s3) & dn233
        dp4 = g4["d_pct"].to_numpy(dtype=np.float64)
        clv4 = g4["clv"].to_numpy(dtype=np.float64)
        bi4 = g4["buy_imb"].to_numpy(dtype=np.int64)
        si4 = g4["sell_imb"].to_numpy(dtype=np.int64)
        has_b4 = (bi4 + si4) > 0
        e50_4 = ema_arr(c4, 50)
        e200_4 = ema_arr(c4, 200)
        adx4 = adx_arr(h4, l4, c4, 14)
        atr4_14 = wilder_atr(h4, l4, c4, 14)
        atr4_m20 = pd.Series(atr4_14).rolling(20).mean().to_numpy(dtype=np.float64)
        atr4_exp = atr4_14 >= atr4_m20

        # Group parameter trials by family so plateau score averages sibling configs strictly inside IS
        fam_trials: dict[str, list[tuple[Any, ...]]] = {}

        def _add(fam: str, cfg_s: str, is_2h: bool, ml: np.ndarray, ms: np.ndarray, sl: float, tp: float, tr: float, be: float, stp: bool, mh: int, dyn: bool):
            fam_trials.setdefault(fam, []).append((cfg_s, is_2h, np.asarray(ml, dtype=bool), np.asarray(ms, dtype=bool), sl, tp, tr, be, stp, mh, dyn))

        # 1) S09_Donch (9)
        for dn in (14, 22, 30):
            hi = pd.Series(h4).shift(1).rolling(dn).max().to_numpy()
            lo = pd.Series(l4).shift(1).rolling(dn).min().to_numpy()
            ml = (c4 > hi) & up233 & (clv4 > 0.25) & (dp4 > 0.0) & (~has_b4 | (bi4 >= si4))
            ms = (c4 < lo) & dn233 & (clv4 < -0.25) & (dp4 < 0.0) & (~has_b4 | (si4 >= bi4))
            for sl, tr, be in ((2.0, 3.5, 0.0), (2.5, 4.5, 0.0), (2.0, 3.5, 1.0)):
                _add("S09_Donch", f"dn={dn},sl={sl},tr={tr},be={be}", False, ml, ms, sl, 0.0, tr, be, False, 96, True)

        # 2) S10_EMA89 (18)
        zbot4 = g4["zero_bot"].to_numpy(dtype=bool) if "zero_bot" in g4 else np.zeros(len(g4), dtype=bool)
        ztop4 = g4["zero_top"].to_numpy(dtype=bool) if "zero_top" in g4 else np.zeros(len(g4), dtype=bool)
        vpm4 = fp4["vpm_rk"]
        for dpm in (2.0, 3.5, 5.0):
            for uz in (False, True):
                tl = stkL & (l4 <= e89_4 + 0.30 * a4[2]) & (c4 > e89_4) & (dp4 > dpm) & (clv4 > 0.20)
                ts_ = stkS & (h4 >= e89_4 - 0.30 * a4[2]) & (c4 < e89_4) & (dp4 < -dpm) & (clv4 < -0.20)
                if uz:
                    tl = tl | (stkL & zbot4 & (vpm4 > 0.40))
                    ts_ = ts_ | (stkS & ztop4 & (vpm4 > 0.40))
                for sl, tp, tr, be in ((2.0, 0.0, 3.5, 0.0), (2.5, 0.0, 4.5, 0.0), (2.0, 3.5, 0.0, 1.0)):
                    _add("S10_EMA89", f"dp={dpm},z={int(uz)},sl={sl},tp={tp},tr={tr},be={be}", False, tl, ts_, sl, tp, tr, be, False, 96, (tr > 0))

        # 3) Whale_p70 (12)
        asz4 = g4["avg_sz_rk"].to_numpy(dtype=np.float64)
        for szr in (0.65, 0.70, 0.80):
            for dpm in (6.0, 8.0):
                whL = up233 & (asz4 > szr) & (dp4 > dpm) & (clv4 > 0.35) & (vpm4 > 0.50)
                whS = dn233 & (asz4 > szr) & (dp4 < -dpm) & (clv4 < -0.35) & (vpm4 > 0.50)
                for sl, tr, be in ((2.0, 3.5, 0.0), (2.5, 4.5, 1.0)):
                    _add("Whale_p70", f"sz={szr},dp={dpm},sl={sl},tr={tr},be={be}", False, whL, whS, sl, 0.0, tr, be, False, 96, True)

        # 4) Sweep_OF (12)
        for sw in (10, 18, 28):
            lo = pd.Series(l4).shift(1).rolling(sw).min().to_numpy()
            hi = pd.Series(h4).shift(1).rolling(sw).max().to_numpy()
            for dpm in (2.0, 4.0):
                ml = up233 & (l4 < lo) & (c4 > lo) & (clv4 > 0.30) & (dp4 > dpm)
                ms = dn233 & (h4 > hi) & (c4 < hi) & (clv4 < -0.30) & (dp4 < -dpm)
                for sl, tp in ((2.0, 3.5), (2.5, 4.0)):
                    _add("Sweep_OF", f"sw={sw},dp={dpm},sl={sl},tp={tp}", False, ml, ms, sl, tp, 0.0, 0.0, False, 48, False)

        # 5) CVD_Div (8)
        cvdz4 = g4["cvd_z24"].to_numpy(dtype=np.float64)
        for sw in (10, 16):
            lo = pd.Series(l4).shift(1).rolling(sw).min().to_numpy()
            hi = pd.Series(h4).shift(1).rolling(sw).max().to_numpy()
            for cz in (0.5, 1.0):
                cvL = (l4 < lo) & (cvdz4 > cz) & (clv4 > 0.25) & (dp4 > 0.0)
                cvS = (h4 > hi) & (cvdz4 < -cz) & (clv4 < -0.25) & (dp4 < 0.0)
                for sl, tp in ((2.5, 3.5), (2.5, 4.5)):
                    _add("CVD_Div", f"sw={sw},cz={cz},sl={sl},tp={tp}", False, cvL, cvS, sl, tp, 0.0, 0.0, False, 48, False)

        # 6) Fund_VWAP (9)
        fbp4 = g4["fund_bp"].to_numpy(dtype=np.float64) if "fund_bp" in g4 else np.zeros(len(g4))
        vwz4 = g4["vwz48"].to_numpy(dtype=np.float64)
        for vwt in (1.25, 1.5, 2.0):
            fcL = (fbp4 <= -0.5) & (vwz4 < -vwt) & (dp4 > 0.0) & (clv4 > 0.2)
            fcS = (fbp4 >= 3.0) & (vwz4 > vwt) & (dp4 < 0.0) & (clv4 < -0.2)
            for sl, tp, be in ((2.0, 3.5, 1.0), (2.5, 4.0, 1.0), (2.5, 4.5, 0.0)):
                _add("Fund_VWAP", f"vwz={vwt},sl={sl},tp={tp},be={be}", False, fcL, fcS, sl, tp, 0.0, be, False, 60, False)

        # 7) S03_2H_Imb (8)
        c2, h2, l2 = a2[5], g2["h"].to_numpy(dtype=np.float64), g2["l"].to_numpy(dtype=np.float64)
        e233_2 = a2[4]
        e233_2s = pd.Series(e233_2).shift(2).to_numpy(dtype=np.float64)
        c2s = pd.Series(c2).shift(2).to_numpy(dtype=np.float64)
        bi2, si2, clv2 = g2["buy_imb"].to_numpy(dtype=np.int64), g2["sell_imb"].to_numpy(dtype=np.int64), g2["clv"].to_numpy(dtype=np.float64)
        t2L = (l2 <= e233_2 + 0.3 * a2[2]) & (c2 > e233_2) & (c2s > e233_2s)
        t2S = (h2 >= e233_2 - 0.3 * a2[2]) & (c2 < e233_2) & (c2s < e233_2s)
        for im in (1, 2):
            for sl, tp, tr, be in ((2.0, 3.5, 0.0, 1.0), (2.5, 3.5, 0.0, 1.0), (3.0, 4.0, 0.0, 1.0), (2.0, 0.0, 3.0, 0.0)):
                _add("S03_2H_Imb", f"imb={im},sl={sl},tp={tp},tr={tr}", True, (bi2 >= im) & (clv2 > 0.25) & t2L, (si2 >= im) & (clv2 < -0.25) & t2S, sl, tp, tr, be, False, 72, (tr > 0))

        # 8) S02_RSI2 (12)
        r2 = fp4["rsi2"]
        vpin4 = fp4["vpin_rank"]
        ofb = g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool) | ((dp4 > 5.0) & (clv4 > 0.25))
        ofs = g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool) | ((dp4 < -5.0) & (clv4 < -0.25))
        for rl in (10.0, 15.0, 20.0):
            for vc in (0.65, 0.80):
                mlr = (vpin4 < vc) & (r2 < rl) & up233 & ofb
                msr = (vpin4 < vc) & (r2 > (100.0 - rl)) & dn233 & ofs
                for sl, tp in ((2.0, 3.5), (2.5, 4.0)):
                    _add("S02_RSI2", f"rsi={rl},vpin={vc},sl={sl},tp={tp}", False, mlr, msr, sl, tp, 0.0, 1.0, True, 48, True)

        # 9) S01_Williams (12)
        wr14 = fp4["wr14"]
        wr14_p = pd.Series(wr14).shift(1).to_numpy(dtype=np.float64)
        for wt in (-80.0, -75.0):
            for dpm in (2.0, 5.0):
                wl = stkL & (wr14_p <= wt) & (wr14 > wt) & (dp4 > dpm) & (clv4 > 0.20) & (~has_b4 | (bi4 >= si4))
                ws = stkS & (wr14_p >= (-100.0 - wt)) & (wr14 < (-100.0 - wt)) & (dp4 < -dpm) & (clv4 < -0.20) & (~has_b4 | (si4 >= bi4))
                for sl, tr, be in ((2.0, 2.5, 1.0), (2.0, 3.5, 0.0), (2.5, 4.0, 0.0)):
                    _add("S01_Williams", f"wt={wt},dp={dpm},sl={sl},tr={tr},be={be}", False, wl, ws, sl, 0.0, tr, be, True, 96, True)

        # 10) S04_Squeeze (9)
        for vpm_m in (0.30, 0.40, 0.55):
            sqL, sqS = _fast_s04_masks(g4, fp4, {"fp_min_vpm": vpm_m, "fp_min_vr4": 0.90 if vpm_m < 0.40 else 0.95})
            for sl, tr, be in ((2.0, 2.5, 1.0), (2.0, 3.5, 0.0), (2.5, 4.0, 0.0)):
                _add("S04_Squeeze", f"vpm={vpm_m},sl={sl},tr={tr},be={be}", False, sqL, sqS, sl, 0.0, tr, be, True, 96, True)

        # 11) S05_ZBreak (8)
        for lb in (30, 50):
            mu = sma_arr(c4, lb)
            sd_z = np.where(rolling_std_arr(c4, lb) > 1e-12, rolling_std_arr(c4, lb), np.nan)
            zv = (c4 - mu) / sd_z
            zvp = pd.Series(zv).shift(1).to_numpy(dtype=np.float64)
            for ze in (1.0, 1.5):
                zl = (zvp < ze) & (zv >= ze) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 18.0) & atr4_exp & (dp4 > 0.0) & (clv4 > 0.15)
                zs = (zvp > -ze) & (zv <= -ze) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 18.0) & atr4_exp & (dp4 < 0.0) & (clv4 < -0.15)
                for sl, tp, tr, be in ((2.0, 3.5, 0.0, 1.0), (2.0, 0.0, 3.0, 0.0)):
                    _add("S05_ZBreak", f"lb={lb},z={ze},sl={sl},tp={tp},tr={tr}", False, zl, zs, sl, tp, tr, be, False, 96, (tr > 0))

        # 12) S07_Pivot (6)
        for sk in (2, 3):
            pL, pS = _fast_s07_masks(g4, fp4, {"swing_k": sk})
            for sl, tp, tr, be in ((2.0, 0.0, 3.0, 1.0), (2.0, 3.5, 0.0, 1.0), (2.5, 0.0, 4.0, 0.0)):
                _add("S07_Pivot", f"k={sk},sl={sl},tp={tp},tr={tr}", False, pL, pS, sl, tp, tr, be, (tr > 0), 96, (tr > 0))

        # 13) CG_Signals (9)
        exhL = g4["cg_exh_b"].to_numpy(dtype=bool) & (c4 > e89_4)
        exhS = g4["cg_exh_s"].to_numpy(dtype=bool) & (c4 < e89_4)
        absL = g4["cg_abs_b"].to_numpy(dtype=bool) & up233 & (clv4 > 0.15)
        absS = g4["cg_abs_s"].to_numpy(dtype=bool) & dn233 & (clv4 < -0.15)
        cgaL = (g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool)) & stkL & (dp4 > 0.0)
        cgaS = (g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool)) & stkS & (dp4 < 0.0)
        for tag_c, cml, cms in (("exh89", exhL, exhS), ("abs233", absL, absS), ("cgstk", cgaL, cgaS)):
            for sl, tr, be in ((2.0, 3.5, 0.0), (2.0, 2.5, 1.0), (2.5, 4.0, 0.0)):
                _add("CG_Signals", f"sig={tag_c},sl={sl},tr={tr},be={be}", False, cml, cms, sl, 0.0, tr, be, (be > 0), 96, True)

        best_by_fam: dict[str, dict[str, Any]] = {}
        for fam, trials in fam_trials.items():
            k_trials = len(trials)
            trial_results: list[dict[str, Any]] = []
            for cfg_s, is_2h, ml_u, ms_u, sl, tp, tr, be, stp, mh, dyn in trials:
                arrs = a2 if is_2h else a4
                tfs = 7200 if is_2h else 14400
                side_arr = np.where(ml_u, 1, np.where(ms_u, -1, 0)).astype(np.int8)
                res_nb = sim_1m_trades_nb(
                    arrs[0], arrs[1], arrs[2], arrs[3], arrs[4], arrs[5], side_arr,
                    m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c,
                    tfs, float(sl), float(tp), float(tr), float(be), (0.2 if tp > 0 else 0.05),
                    bool(stp), int(mh), 1, bool(dyn),
                    bool(use_mt5), sp_bps, True, 12.0, 1.0,
                    roll3d, sw_l_bps, sw_s_bps, fts_c, frt_c,
                )
                ev = _eval_split(res_nb[1], res_nb[7], res_nb[8], split_ts, k_trials)
                ev.update({"fam": fam, "cfg": cfg_s, "is_2h": is_2h, "res_nb": res_nb})
                trial_results.append(ev)

            # Parameter plateau regularization (strictly on IS scores!)
            raw_scores = np.array([t["raw_score"] for t in trial_results], dtype=float)
            mean_all = float(raw_scores.mean()) if len(raw_scores) else -1.0
            for idx_t, tr_info in enumerate(trial_results):
                other_mean = (float(raw_scores.sum() - raw_scores[idx_t]) / (len(raw_scores) - 1)) if len(raw_scores) > 1 else raw_scores[idx_t]
                tr_info["plateau_score"] = 0.65 * tr_info["raw_score"] + 0.35 * max(-1.0, other_mean)

            valid_cands = [
                t for t in trial_results
                if t["nis"] >= (10 if is_metal else 18)
                and t["isr"] >= (3.0 if is_metal else 8.5)
                and t["ispf"] >= 1.30
                and t["ist"] >= 0.70
                and t["half_min"] > -2.5
                and t["edge_cost"] >= 2.8
            ]
            if valid_cands:
                win = max(valid_cands, key=lambda x: x["plateau_score"])
                df_src = g2 if win["is_2h"] else g4
                win["trs"] = _arrays_to_dicts(sym, f"{fam} ({win['cfg']})", df_src, win["res_nb"])
                win.pop("res_nb", None)
                best_by_fam[fam] = win

        chosen = sorted(best_by_fam.values(), key=lambda x: x["plateau_score"], reverse=True)
        per_asset_recipes.append({
            "symbol": sym,
            "sp_bps": sp_bps,
            "is_tight_25bp": bool(sp_bps <= 25.0),
            "families": [
                {
                    "family": v["fam"], "config": v["cfg"], "is_trades": v["nis"],
                    "is_r": v["isr"], "is_pf": v["ispf"], "is_t_hac": v["ist"], "dsr": v["dsr"],
                    "edge_cost": v["edge_cost"], "oos_trades": v["noos"], "oos_r": v["oosr"], "oos_pf": v["oospf"],
                }
                for v in chosen
            ],
            "total_is_r": round(sum(v["isr"] for v in chosen), 2),
            "total_oos_r": round(sum(v["oosr"] for v in chosen), 2),
        })
        for v in chosen:
            all_trades.extend(v["trs"])
            if sp_bps <= 25.0:
                tight_trades.extend(v["trs"])
            if sym != "BTCUSDT":
                alts_trades.extend(v["trs"])

    oos_all = simulate_multi_asset_portfolio(
        all_trades, asset_frames, start_equity=5000.0, risk_pct=1.25, max_concurrent=6,
        max_same_asset=2, per_asset_max={"BTCUSDT": 3}, max_same_side=4, weight_mode="vof",
        min_ts=SPLIT_2024_TS, mc_runs=mc_runs, name=f"Per-Asset All-13 Robust IS->OOS Parallel ({cost_mode})",
    )
    oos_tight = simulate_multi_asset_portfolio(
        tight_trades, asset_frames, start_equity=5000.0, risk_pct=1.25, max_concurrent=5,
        max_same_asset=1, per_asset_max={"BTCUSDT": 3}, max_same_side=3, weight_mode="vof",
        min_ts=SPLIT_2024_TS, mc_runs=mc_runs, name=f"Per-Asset Tight<=25bp All-13 Robust OOS ({cost_mode})",
    )
    full_tight = simulate_multi_asset_portfolio(
        tight_trades, asset_frames, start_equity=5000.0, risk_pct=1.25, max_concurrent=5,
        max_same_asset=1, per_asset_max={"BTCUSDT": 3}, max_same_side=3, weight_mode="vof",
        min_ts=None, mc_runs=mc_runs, name=f"Per-Asset Tight<=25bp All-13 Robust Full 21-26 ({cost_mode})",
    )
    oos_alts = simulate_multi_asset_portfolio(
        alts_trades, asset_frames, start_equity=5000.0, risk_pct=1.25, max_concurrent=5,
        max_same_asset=2, max_same_side=3, weight_mode="vof",
        min_ts=SPLIT_2024_TS, mc_runs=mc_runs, name=f"Per-Asset All-13 Robust AltsOnly ({cost_mode})",
    )
    return {
        "cost_mode": cost_mode,
        "per_asset_recipes": per_asset_recipes,
        "all_trades": all_trades,
        "tight_trades": tight_trades,
        "parallel_all_oos": {k: v for k, v in oos_all.items() if k != "daily_df"},
        "parallel_tight_oos": {k: v for k, v in oos_tight.items() if k != "daily_df"},
        "parallel_tight_full": {k: v for k, v in full_tight.items() if k != "daily_df"},
        "parallel_alts_oos": {k: v for k, v in oos_alts.items() if k != "daily_df"},
    }


def run_full_multi_asset_optimization(
    symbols: list[str] | None = None,
    data_dir: Path = DATA_DIR,
    cost_mode: str = "mt5",
    mc_runs: int = 20000,
    save_report: bool = True,
) -> dict[str, Any]:
    """Execute end-to-end multi-asset screening, causal IS selection, statsmodels audit & 20K MC optimization."""
    specs = load_mt5_contract_specs(data_dir)
    avail = [a["binance"] for a in CORE_12_ASSETS if (data_dir / f"{a['binance']}_agg_footprint.parquet").is_file()]
    if symbols:
        wanted = {s.upper() for s in symbols}
        avail = [s for s in avail if s in wanted]

    import concurrent.futures
    if len(avail) > 1:
        per_symbol: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(avail))) as ex:
            futs = {ex.submit(evaluate_symbol_all_sleeves, sym, data_dir, cost_mode, specs): sym for sym in avail}
            for fut in concurrent.futures.as_completed(futs):
                sym_f = futs[fut]
                per_symbol[sym_f] = fut.result()
        per_symbol = {sym: per_symbol[sym] for sym in avail}
    else:
        per_symbol = {sym: evaluate_symbol_all_sleeves(sym, data_dir=data_dir, cost_mode=cost_mode, specs_map=specs) for sym in avail}
    asset_frames = {sym: res["g4"] for sym, res in per_symbol.items()}

    # 1) Build Asset-Level Scorecards & Strict Causal IS-Gated Sleeve Selection (zero OOS leakage)
    opt8_all: list[dict[str, Any]] = []
    expanded_all: list[dict[str, Any]] = []
    is_strict_trades: list[dict[str, Any]] = []
    btc_plus_is_opt8: list[dict[str, Any]] = []
    metals_trades: list[dict[str, Any]] = []
    asset_summary: list[dict[str, Any]] = []
    extra_sleeve_candidates: list[dict[str, Any]] = []

    for sym, res in per_symbol.items():
        cat = res["spec"].get("cat", "CRYPTO")
        sp_bps = float(res["spec"].get("sp_bps", 0.0))
        opt8_all.extend(res["opt8_trades"])
        expanded_all.extend(res["all_trades"])

        arr_o8 = np.array([t["r"] for t in res["opt8_trades"]], dtype=float)
        is_o8 = sum(t["r"] for t in res["opt8_trades"] if t["ts_in"] < SPLIT_2024_TS)
        oos_o8 = sum(t["r"] for t in res["opt8_trades"] if t["ts_in"] >= SPLIT_2024_TS)
        hac_o8 = compute_hac_stats(arr_o8)
        asset_summary.append({
            "symbol": sym,
            "mt5": res["spec"].get("mt5", sym),
            "cat": cat,
            "sp_bps": sp_bps,
            "span": f"{res['span_start']}..{res['span_end']}",
            "opt8_trades": len(arr_o8),
            "opt8_wr": round(float((arr_o8 > 0).mean() * 100.0), 1) if len(arr_o8) else 0.0,
            "opt8_sum_r": round(float(arr_o8.sum()), 2),
            "opt8_is_r": round(float(is_o8), 2),
            "opt8_oos_r": round(float(oos_o8), 2),
            "opt8_t_hac": hac_o8["t_hac"],
            "opt8_p_hac": hac_o8["p_hac"],
        })

        opt8_names = {t["sleeve"] for t in res["opt8_trades"]}
        if cat == "METAL":
            metals_trades.extend(res["opt8_trades"])
        else:
            for st in res["sleeve_stats"]:
                if sym == "BTCUSDT" and st["sleeve"] in opt8_names:
                    # Keep BTC's baseline 8-sleeve engine intact; only gate extra sleeves / non-BTC alts on IS
                    slv_tr = [t for t in res["opt8_trades"] if t["sleeve"] == st["sleeve"]]
                    is_strict_trades.extend(slv_tr)
                    btc_plus_is_opt8.extend(slv_tr)
                elif st["sleeve"] in opt8_names:
                    # Strict causal IS gate: IS(2021-2023) >= 6 trades, SumR > 2.5, PF >= 1.15, t_HAC >= 0.65
                    if st["is_trades"] >= 6 and st["is_r"] > 2.5 and st["is_pf"] >= 1.15 and st["is_t_hac"] >= 0.65:
                        slv_tr = [t for t in res["opt8_trades"] if t["sleeve"] == st["sleeve"]]
                        is_strict_trades.extend(slv_tr)
                        btc_plus_is_opt8.extend(slv_tr)
                else:
                    # Extra strategy sleeves (S01/S04/S05/S07-FP): require strong causal IS confirmation
                    if st["is_trades"] >= 8 and st["is_r"] > 3.5 and st["is_pf"] >= 1.25 and st["is_t_hac"] >= 0.90:
                        extra_sleeve_candidates.append(st)
                        is_strict_trades.extend([t for t in res["all_trades"] if t["sleeve"] == st["sleeve"]])

    # 2) Run Daily Alignment + Correlation Weights (ERC-lite: downweight high cross-crypto corr, cap [0.65, 1.35])
    pam_btc3 = {"BTCUSDT": 3, "XAUUSDT": 2, "XAGUSDT": 2}
    base_all_sim = simulate_multi_asset_portfolio(
        opt8_all, asset_frames, start_equity=5000.0, risk_pct=1.25, max_concurrent=5, max_same_asset=2,
        per_asset_max=pam_btc3, max_same_side=3, weight_mode="equal", mc_runs=min(2000, mc_runs),
        name="Unfiltered All Assets Opt8",
    )
    econ = compute_cross_asset_regression(base_all_sim["daily_df"])
    corr_map = econ.get("corr", {})
    corr_weights: dict[str, float] = {}
    if corr_map:
        syms_c = list(corr_map.keys())
        raw_inv = {}
        for s1 in syms_c:
            avg_c = sum(abs(corr_map[s1].get(s2, 0.0)) for s2 in syms_c) / max(1, len(syms_c))
            raw_inv[s1] = 1.0 / max(0.25, math.sqrt(avg_c))
        mean_w = sum(raw_inv.values()) / max(1, len(raw_inv))
        for s1, val in raw_inv.items():
            corr_weights[s1] = round(max(0.65, min(1.35, val / mean_w)), 3)

    # Low-beta diversifier subset: BTC + assets with daily corr vs BTC <= 0.55
    btc_corr_row = corr_map.get("BTCUSDT", {})
    low_corr_syms = {"BTCUSDT"} | {s for s, cval in btc_corr_row.items() if abs(float(cval)) <= 0.55}
    low_corr_trades = [t for t in is_strict_trades if t["symbol"] in low_corr_syms]
    tight_sp_trades = [t for t in is_strict_trades if float(specs.get(t["symbol"], {}).get("sp_bps", 99)) <= 25.0]
    wf_oos_trades = _causal_wf_trades(expanded_all)
    per_asset_opt = optimize_per_asset_strict_is_oos(per_symbol, data_dir=data_dir, cost_mode=cost_mode, specs_map=specs, mc_runs=min(5000, mc_runs))
    per_asset_opt_trades = per_asset_opt["all_trades"]
    per_asset_tight_trades = per_asset_opt["tight_trades"]

    # 3) Portfolio Combination Grid (Zero Lookahead, Full 20K Correlated Block MC)
    btc_only = simulate_multi_asset_portfolio(
        per_symbol["BTCUSDT"]["opt8_trades"], asset_frames,
        start_equity=5000.0, risk_pct=1.25, max_concurrent=3, max_same_asset=3, max_same_side=2,
        weight_mode="vof", mc_runs=mc_runs, name="1) BTCUSDT Only Opt8 (MT5 Friction Baseline)",
    ) if "BTCUSDT" in per_symbol else None

    combos_to_test = [
        ("2) Unfiltered All Assets Opt8 (mc=5, btc=3, Equal)", opt8_all, 5, 1, pam_btc3, 3, "equal", None, None),
        ("3) BTC(3) + IS-Strict Crypto Opt8 (mc=5, vOF)", btc_plus_is_opt8, 5, 1, pam_btc3, 3, "vof", None, None),
        ("4) BTC(3) + IS-Strict Crypto+ExtraFP (mc=5, Corr-ERC)", is_strict_trades, 5, 1, pam_btc3, 3, "corr_erc", corr_weights, None),
        ("5) BTC(3) + IS-Strict Crypto+ExtraFP (mc=6, sa=2, Corr-ERC)", is_strict_trades, 6, 2, pam_btc3, 4, "corr_erc", corr_weights, None),
        ("6) Tight-Spread <=25bp BTC(3)+Alts(2) (mc=5, Corr-ERC)", tight_sp_trades, 5, 2, pam_btc3, 3, "corr_erc", corr_weights, None),
        ("7) Tight-Spread <=25bp BTC(3)+Alts(1) (mc=4, vOF)", tight_sp_trades, 4, 1, pam_btc3, 3, "vof", None, None),
        ("8) Low-Corr Diversified <=0.55 BTC-Beta (mc=5, Corr-ERC)", low_corr_trades, 5, 1, pam_btc3, 3, "corr_erc", corr_weights, None),
        ("9) IS-Strict Crypto + Metals XAU/XAG Overlap 25-26 (mc=5, Corr-ERC)", is_strict_trades + metals_trades, 5, 1, pam_btc3, 3, "corr_erc", corr_weights, METALS_START_TS),
        ("10) Expanding Causal Walk-Forward 24-26 OOS (mc=5, Corr-ERC)", wf_oos_trades, 5, 1, pam_btc3, 3, "corr_erc", corr_weights, SPLIT_2024_TS),
        ("11) Per-Asset All-13 Robust IS->OOS 24-26 (mc=6, vOF)", per_asset_opt_trades, 6, 2, pam_btc3, 4, "vof", None, SPLIT_2024_TS),
        ("12) Tight<=25bp Per-Asset All-13 Robust Full (mc=5, vOF)", per_asset_tight_trades, 5, 1, pam_btc3, 3, "vof", None, None),
    ]

    evaluated: list[dict[str, Any]] = []
    if btc_only:
        evaluated.append({k: v for k, v in btc_only.items() if k != "daily_df"})
    for label, tr_list, mc_c, sa_c, pam_c, ss_c, w_mode, cw, min_t in combos_to_test:
        if not tr_list:
            continue
        res = simulate_multi_asset_portfolio(
            tr_list, asset_frames, start_equity=5000.0, risk_pct=1.25,
            max_concurrent=mc_c, max_same_asset=sa_c, per_asset_max=pam_c, max_same_side=ss_c,
            weight_mode=w_mode, asset_weights=cw, min_ts=min_t, mc_runs=mc_runs, name=label,
        )
        res_clean = {k: v for k, v in res.items() if k != "daily_df"}
        evaluated.append(res_clean)

    # Also test exact 11-month Crypto vs Crypto+Metals head-to-head on identical dates (2025-12-11+)
    overlap_crypto = simulate_multi_asset_portfolio(
        is_strict_trades, asset_frames, start_equity=5000.0, risk_pct=1.25,
        max_concurrent=5, max_same_asset=1, per_asset_max=pam_btc3, max_same_side=3, weight_mode="corr_erc",
        asset_weights=corr_weights, min_ts=METALS_START_TS, mc_runs=mc_runs,
        name="Overlap 25-26: Crypto Only",
    )

    # Rank multi-year causal candidates (years_span >= 3.0, excluding raw unfiltered #2, OOS SumR > 0) by Calmar * sqrt(PF)
    long_cands = [
        e for e in evaluated
        if e.get("years_span", 0) >= 3.0
        and e["metrics"]["total_trades"] >= 100
        and not e["name"].startswith("2)")
        and e["metrics"].get("oos_r", 0.0) > 0.0
    ]
    best_multi = max(
        long_cands,
        key=lambda x: (x["metrics"]["calmar_ratio"] * math.sqrt(max(0.5, x["metrics"]["profit_factor"]))),
        default=(evaluated[0] if evaluated else {}),
    )

    report = {
        "cost_mode": cost_mode,
        "generated_ts": int(datetime.now(timezone.utc).timestamp()),
        "asset_summary": sorted(asset_summary, key=lambda x: x["opt8_sum_r"], reverse=True),
        "extra_is_approved_sleeves": extra_sleeve_candidates,
        "per_asset_strict_opt": {k: v for k, v in per_asset_opt.items() if k != "all_trades"},
        "econometrics": econ,
        "corr_weights": corr_weights,
        "candidates": evaluated,
        "metals_overlap_head_to_head": {
            "crypto_only": {k: v for k, v in overlap_crypto.items() if k != "daily_df"},
            "crypto_plus_metals": next((c for c in evaluated if c["name"].startswith("9)")), {}),
        },
        "best_portfolio": best_multi,
    }
    if save_report:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MULTI_REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
