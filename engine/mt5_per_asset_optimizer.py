"""MT5 151-Symbol Multi-Asset x Multi-Sleeve Causal Per-Asset Optimizer (Numba + Walk-Forward).

Optimizes every live-feasible MT5 symbol individually across active strategy sleeves
(`s01`, `s02`, `s03`, `s04`, `s05`, `s07`, `s09`, `s10`) on `historical_parquet/` with
strict 3-way chronological splits (Train 55% -> Val 23% -> Locked Holdout OOS 22%),
exact live MT5 spreads / min-lot $6 cap feasibility, parameter plateau regularization,
and DSR multi-testing correction.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from exchange_feed.backtesting.fast_kernels import sim_ohlc_trades_nb
from exchange_feed.backtesting.multi_asset_optimizer import compute_hac_stats
from exchange_feed.backtesting.portfolio_engine import _dsr_pct
from strategies.core.indicators import (
    adx_arr,
    ema_arr,
    er_arr,
    rolling_std_arr,
    rsi_wilder_arr,
    sma_arr,
    wilder_atr,
    williams_r_arr,
)

HIST_DIR = ROOT / "historical_data" / "mt5_parquet"
CONFIG_PATH = ROOT / "engine" / "config.json"
REPORT_PATH = ROOT / "exchange_feed" / "data" / "mt5_per_asset_opt_report.json"

TF_DIR = {"M1": "1M", "M5": "5M", "M15": "15M", "H1": "1H", "H4": "4H"}


def snapshot_all_mt5_specs(symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Query live MT5 symbol_info & tick spread for all visible broker symbols (fallback offline specs)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        import MetaTrader5 as mt5
    except Exception:
        return out
    if not mt5.initialize():
        return out
    try:
        items = [mt5.symbol_info(s) for s in symbols] if symbols else list(mt5.symbols_get() or [])
        for s in items:
            if s is None or not getattr(s, "visible", True):
                continue
            name = str(s.name)
            tk = mt5.symbol_info_tick(name)
            bid = float(tk.bid) if (tk and tk.bid > 0) else float(getattr(s, "bid", 0.0) or 0.0)
            ask = float(tk.ask) if (tk and tk.ask > 0) else float(getattr(s, "ask", 0.0) or bid)
            sp_px = max(0.0, ask - bid) if (bid > 0 and ask >= bid) else float(s.spread) * float(s.point)
            sp_bps = (sp_px / bid * 10000.0) if bid > 0 else 25.0
            tsize = float(s.trade_tick_size) or float(s.point)
            tval = float(s.trade_tick_value)
            val_per_pt = (tval / tsize) if tsize > 0 and tval > 0 else float(s.trade_contract_size)
            if s.swap_mode == 1 and bid > 0:
                sw_l = float(s.swap_long) * float(s.point) / bid * 10000.0
                sw_s = float(s.swap_short) * float(s.point) / bid * 10000.0
            elif s.swap_mode == 3:
                sw_l = float(s.swap_long) / 365.0 * 100.0
                sw_s = float(s.swap_short) / 365.0 * 100.0
            else:
                sw_l, sw_s = -1.0, -0.5
            out[name] = {
                "symbol": name,
                "bid": bid,
                "sp_px": sp_px,
                "sp_bps": round(sp_bps, 2),
                "vol_min": float(s.volume_min),
                "vol_step": float(s.volume_step),
                "val_per_pt": float(val_per_pt),
                "sw_l_bps": round(sw_l, 3),
                "sw_s_bps": round(sw_s, 3),
                "roll3d": int(s.swap_rollover3days),
            }
    finally:
        mt5.shutdown()
    return out


def _load_hist_df(symbol: str, tf: str, hist_dir: Path = HIST_DIR) -> pd.DataFrame | None:
    p = hist_dir / TF_DIR.get(tf, tf) / f"{symbol}.parquet"
    if not p.is_file():
        return None
    df = pd.read_parquet(p).sort_values("datetime").reset_index(drop=True)
    if len(df) < 400:
        return None
    return df


def _extract_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    o = np.ascontiguousarray(df["open"].to_numpy(dtype=np.float64))
    h = np.ascontiguousarray(df["high"].to_numpy(dtype=np.float64))
    l = np.ascontiguousarray(df["low"].to_numpy(dtype=np.float64))
    c = np.ascontiguousarray(df["close"].to_numpy(dtype=np.float64))
    t = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
    ts_sec = np.ascontiguousarray(t.astype("datetime64[s]").astype(np.int64).to_numpy())
    tm = ts_sec // 60
    atr = np.ascontiguousarray(wilder_atr(h, l, c, 14), dtype=np.float64)
    return {"o": o, "h": h, "l": l, "c": c, "ts": ts_sec, "tm": tm, "atr": atr}


def _d1_pivots_signals(h: np.ndarray, l: np.ndarray, c: np.ndarray, tm: np.ndarray, e50: np.ndarray, e200: np.ndarray, sk: int, trend_f: bool = True) -> tuple[np.ndarray, np.ndarray]:
    from strategies.s07_daily_pivot_breakout.strategy import _d1_bars_from_arrays

    n = len(c)
    ml = np.zeros(n, dtype=bool)
    ms = np.zeros(n, dtype=bool)
    uniq_days, dh, dl, d_end = _d1_bars_from_arrays(h, l, c, tm)
    n_d = len(uniq_days)
    if n_d < 2 * sk + 3:
        return ml, ms
    piv_h: list[tuple[int, float]] = []
    piv_l: list[tuple[int, float]] = []
    for j in range(sk, n_d - sk):
        conf_b = int(d_end[j + sk])
        if dh[j] == float(np.max(dh[j - sk : j + sk + 1])):
            piv_h.append((conf_b, float(dh[j])))
        if dl[j] == float(np.min(dl[j - sk : j + sk + 1])):
            piv_l.append((conf_b, float(dl[j])))

    d_idx = 0
    for i in range(205, n - 3):
        cur_day = int(tm[i] // 1440)
        while d_idx + 1 < n_d and uniq_days[d_idx] < cur_day:
            d_idx += 1
        n_conf_days = d_idx - sk
        if n_conf_days < sk + 2:
            continue
        max_conf_bar = int(d_end[n_conf_days - 1 + sk]) if (n_conf_days - 1 + sk) < n_d else i
        cp, cc = float(c[i - 1]), float(c[i])
        up_ok = (e50[i] > e200[i] and cc > e200[i]) if trend_f else (cc > e200[i])
        dn_ok = (e50[i] < e200[i] and cc < e200[i]) if trend_f else (cc < e200[i])
        if up_ok and cc > cp:
            ah = [lvl for cb, lvl in piv_h if cb < i and cb <= max_conf_bar and lvl >= cp and (cb + 1 >= i or float(np.max(c[cb + 1 : i])) <= lvl)]
            for lvl in sorted(ah)[:3]:
                if cp <= lvl < cc:
                    ml[i] = True
                    break
        elif dn_ok and cc < cp:
            al = [lvl for cb, lvl in piv_l if cb < i and cb <= max_conf_bar and lvl <= cp and (cb + 1 >= i or float(np.min(c[cb + 1 : i])) >= lvl)]
            for lvl in sorted(al, reverse=True)[:3]:
                if cp >= lvl > cc:
                    ms[i] = True
                    break
    return ml, ms


def _cluster_s03_signals(h: np.ndarray, l: np.ndarray, c: np.ndarray, atr: np.ndarray, ep: int, slope_gate: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(c)
    ml = np.zeros(n, dtype=bool)
    ms = np.zeros(n, dtype=bool)
    rd_a = np.zeros(n, dtype=np.float64)
    ef = ema_arr(c, ep)
    start_i = max(ep + 10, 5)
    for i in range(start_i, n - 3):
        ai = float(atr[i])
        ei = float(ef[i])
        if h[i] <= l[i] or not np.isfinite(ai) or ai <= 0.0 or ei <= 0.0:
            continue
        rising = ei > float(ef[i - 5])
        falling = ei < float(ef[i - 5])
        if slope_gate and not (rising or falling):
            continue
        tol = max(1e-5, 0.20 * ai)
        ch, cl, cc = h[i - 3 : i], l[i - 3 : i], c[i - 3 : i]
        if rising:
            if int((cl >= ei - tol).sum()) >= 2 and float(np.min(cc)) >= ei - 0.15 * ai and c[i] > float(np.max(ch)):
                ml[i] = True
                rd_a[i] = max(float(c[i]) - (float(np.min(cl)) - 0.20 * ai), 1.2 * ai)
        elif falling:
            if int((ch <= ei + tol).sum()) >= 2 and float(np.max(cc)) <= ei + 0.15 * ai and c[i] < float(np.min(cl)):
                ms[i] = True
                rd_a[i] = max((float(np.max(ch)) + 0.20 * ai) - float(c[i]), 1.2 * ai)
    return ml, ms, rd_a


def _eval_split_3way(o_ti: np.ndarray, o_r: np.ndarray, o_fr: np.ndarray, t_train_end: int, t_val_end: int, k_trials: int) -> dict[str, Any]:
    if len(o_ti) == 0:
        return {"n_tr": 0, "r_tr": 0.0, "pf_tr": 0.0, "t_tr": 0.0, "dsr": 0.0, "raw_score": -1.0,
                "n_val": 0, "r_val": 0.0, "pf_val": 0.0, "n_oos": 0, "r_oos": 0.0, "pf_oos": 0.0, "edge_cost": 0.0}
    m_tr = o_ti < t_train_end
    m_val = (o_ti >= t_train_end) & (o_ti < t_val_end)
    m_oos = o_ti >= t_val_end

    def _stats(arr: np.ndarray):
        if len(arr) == 0:
            return 0, 0.0, 0.0
        g = float(arr[arr > 0].sum())
        l = float(abs(arr[arr < 0].sum()))
        pf = (g / l) if l > 0 else (9.9 if g > 0 else 0.0)
        return int(len(arr)), round(float(arr.sum()), 2), round(float(pf), 2)

    ntr, rtr, pftr = _stats(o_r[m_tr])
    nval, rval, pfval = _stats(o_r[m_val])
    noos, roos, pfoos = _stats(o_r[m_oos])

    r_tr_a = o_r[m_tr]
    f_tr_a = o_fr[m_tr]
    t_tr = compute_hac_stats(r_tr_a)["t_hac"] if ntr >= 8 else 0.0
    dsr = _dsr_pct(r_tr_a, max(2, k_trials)) if ntr >= 10 else 0.0
    ec = ((float(r_tr_a.mean()) + float(f_tr_a.mean())) / max(float(f_tr_a.mean()), 0.02)) if ntr else 0.0
    h1 = float(r_tr_a[: ntr // 2].sum()) if ntr >= 6 else 0.0
    h2 = float(r_tr_a[ntr // 2 :].sum()) if ntr >= 6 else 0.0
    stab = 1.0 if min(h1, h2) > 0.0 else (0.75 if min(h1, h2) > -5.0 else 0.45)
    raw = float(t_tr) * math.sqrt(max(0.25, min(4.0, pftr))) * stab if t_tr > 0 else -1.0
    return {
        "n_tr": ntr, "r_tr": rtr, "pf_tr": pftr, "t_tr": round(float(t_tr), 2), "dsr": round(float(dsr), 1),
        "edge_cost": round(float(ec), 2), "half_min": round(float(min(h1, h2)), 2), "raw_score": raw,
        "n_val": nval, "r_val": rval, "pf_val": pfval,
        "n_oos": noos, "r_oos": roos, "pf_oos": pfoos,
    }


def _evaluate_one_mt5_symbol(
    sym: str,
    spec: dict[str, Any],
    hist_dir: Path = HIST_DIR,
    risk_cap_usd: float = 6.0,
) -> dict[str, Any] | None:
    """Evaluate & per-asset optimize all 8 MT5 sleeves for one symbol with strict Train->Val->Holdout OOS."""
    df4 = _load_hist_df(sym, "H4", hist_dir)
    if df4 is None:
        return None
    a4 = _extract_arrays(df4)
    atr_med4 = float(np.nanmedian(a4["atr"][200:]))
    val_per_pt = float(spec.get("val_per_pt", 0.0))
    vol_min = float(spec.get("vol_min", 0.01))
    sp_px = float(spec.get("sp_px", 0.0))
    sp_bps = float(spec.get("sp_bps", 12.0))
    sw_l = float(spec.get("sw_l_bps", -1.0))
    sw_s = float(spec.get("sw_s_bps", -0.5))
    roll3d = int(spec.get("roll3d", 5))

    # Strict live MT5 feasibility check: <=$6 min-lot risk, <=25bp spread, and 2x spread <= 25% of risk
    if sp_bps > 25.0:
        return None
    if atr_med4 <= 0 or (val_per_pt > 0 and vol_min * (1.5 * atr_med4) * val_per_pt > risk_cap_usd * 1.15):
        return None
    if sp_px > 0 and (2.0 * sp_px) / max(1.5 * atr_med4, 1e-12) > 0.25:
        return None

    df1 = _load_hist_df(sym, "H1", hist_dir)
    a1 = _extract_arrays(df1) if df1 is not None else None

    c4, h4, l4, o4, atr4, tm4 = a4["c"], a4["h"], a4["l"], a4["o"], a4["atr"], a4["tm"]
    ts4 = a4["ts"]
    n4 = len(c4)
    t4_tr_end = int(ts4[int(n4 * 0.55)])
    t4_val_end = int(ts4[int(n4 * 0.78)])

    e50_4 = ema_arr(c4, 50)
    e200_4 = ema_arr(c4, 200)
    e200_4s = pd.Series(e200_4).shift(5).to_numpy(dtype=np.float64)
    adx4 = adx_arr(h4, l4, c4, 14)
    er4 = er_arr(c4, 14)
    atr4_m20 = pd.Series(atr4).rolling(20).mean().to_numpy(dtype=np.float64)
    atr4_exp = atr4 >= atr4_m20

    winners: dict[str, dict[str, Any]] = {}
    all_oos_trades: list[dict[str, Any]] = []

    def _run_family(sleeve_key: str, tf_name: str, trials: list[dict[str, Any]], arrs: dict[str, np.ndarray], t_tr: int, t_val: int, min_tr_n: int = 12):
        ts_u, o_u, h_u, l_u, c_u, atr_u = arrs["ts"], arrs["o"], arrs["h"], arrs["l"], arrs["c"], arrs["atr"]
        k_trials = len(trials)
        res_list: list[dict[str, Any]] = []
        for tr in trials:
            sd_arr = np.where(tr["ml"], 1, np.where(tr["ms"], -1, 0)).astype(np.int8)
            rd_arr = np.ascontiguousarray(tr.get("rd_arr", np.zeros(len(c_u), dtype=np.float64)), dtype=np.float64)
            o_bi, o_ti, o_to, o_sd, o_r, o_fr = sim_ohlc_trades_nb(
                ts_u, o_u, h_u, l_u, c_u, atr_u, sd_arr, rd_arr,
                float(tr.get("sl_atr", 1.5)), float(tr.get("tp_r", 3.0)), float(tr.get("trail_atr", 0.0)),
                float(tr.get("be_r", 1.0)), 0.05, int(tr.get("max_hold", 96)),
                sp_bps, sw_l, sw_s, roll3d, int(tr.get("cooldown", 6)),
            )
            ev = _eval_split_3way(o_ti, o_r, o_fr, t_tr, t_val, k_trials)
            ev["params"] = tr["params"]
            ev["tf"] = tf_name
            ev["be_lock_enabled"] = bool(tr.get("be_r", 1.0) > 0.0)
            ev["o_ti"] = o_ti
            ev["o_to"] = o_to
            ev["o_sd"] = o_sd
            ev["o_r"] = o_r
            res_list.append(ev)

        raws = np.array([x["raw_score"] for x in res_list], dtype=float)
        for idx, x in enumerate(res_list):
            other = (float(raws.sum() - raws[idx]) / (len(raws) - 1)) if len(raws) > 1 else raws[idx]
            x["plateau_score"] = 0.65 * x["raw_score"] + 0.35 * max(-1.0, other)

        passing = [
            x for x in res_list
            if x["n_tr"] >= min_tr_n and x["r_tr"] >= 8.0 and x["pf_tr"] >= 1.35 and x["t_tr"] >= 1.10
            and x["dsr"] >= 25.0 and x["half_min"] > -2.0 and x["edge_cost"] >= 2.8
            and x["n_val"] >= 5 and x["r_val"] >= 1.0 and x["pf_val"] >= 1.25
        ]
        if passing:
            best = max(passing, key=lambda x: x["plateau_score"])
            m_oos = best["o_ti"] >= t_val
            for ti_v, to_v, sd_v, r_v in zip(best["o_ti"][m_oos], best["o_to"][m_oos], best["o_sd"][m_oos], best["o_r"][m_oos]):
                all_oos_trades.append({
                    "symbol": sym, "sleeve": sleeve_key, "tf": tf_name,
                    "ts_in": int(ti_v), "ts_out": int(to_v),
                    "side": "BUY" if int(sd_v) == 1 else "SELL", "r": float(r_v),
                })
            clean = {k: v for k, v in best.items() if k not in ("o_ti", "o_to", "o_sd", "o_r")}
            winners[sleeve_key] = clean

    is_fx = (len(sym) >= 6 and sym[:6].isalpha() and not any(k in sym for k in ("XAU", "XAG", "GAU", "XPT", "BTC", "ETH", "SOL", "BCH", "ZEC", "DSH", "UNI", "AVX", "BAR", "GRT", "VET", "THT", "COM", "CHZ", "WTI", "BRENT", "HK50", "JP225", "SP500", "NAS", "AU200", "STOXX", "UK100", "US2000")))
    if is_fx:
        # Audited Round-2B proved FX belongs in M15 session specialists (ivwall / orb_nas), not 4H trend breakout
        return None

    # 1) ig_s9_donchian_cta (H4)
    t_s9 = []
    for dn in (14, 20, 28):
        hh = pd.Series(h4).shift(1).rolling(dn).max().to_numpy()
        ll = pd.Series(l4).shift(1).rolling(dn).min().to_numpy()
        for ax in (15.0, 18.0, 22.0):
            for er_m in (0.15, 0.20, 0.25):
                ml = (c4 > hh) & (e50_4 > e200_4) & (c4 > e200_4) & (adx4 >= ax) & (er4 >= er_m)
                ms = (c4 < ll) & (e50_4 < e200_4) & (c4 < e200_4) & (adx4 >= ax) & (er4 >= er_m)
                for sl_a, tp_r in ((1.5, 3.0), (2.0, 3.5)):
                    t_s9.append({
                        "ml": ml, "ms": ms, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0,
                        "params": {"donchian_n": dn, "adx_min": ax, "er_min": er_m, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0},
                    })
    _run_family("ig_s9_donchian_cta", "H4", t_s9, a4, t4_tr_end, t4_val_end)

    # 2) ig_s10_ema_pullback (H4 & H1 if available)
    for tf_k, arr_u in ([("H4", a4)] + ([("H1", a1)] if a1 is not None else [])):
        cu, hu, lu, ou, atru, tsu = arr_u["c"], arr_u["h"], arr_u["l"], arr_u["o"], arr_u["atr"], arr_u["ts"]
        e200_u = ema_arr(cu, 200)
        adx_u = adx_arr(hu, lu, cu, 14)
        rng_u = np.maximum(hu - lu, 1e-12)
        bfrac_u = np.abs(cu - ou) / rng_u
        tu_tr = int(tsu[int(len(tsu) * 0.55)])
        tu_val = int(tsu[int(len(tsu) * 0.78)])
        t_s10 = []
        for ep in (21, 50):
            ef = ema_arr(cu, ep)
            efs = pd.Series(ef).shift(5).to_numpy()
            tol = 0.25 * atru
            rd_l = np.maximum(cu - (np.minimum(lu, ef) - 0.2 * atru), 1.2 * atru)
            rd_s = np.maximum((np.maximum(hu, ef) + 0.2 * atru) - cu, 1.2 * atru)
            for ax in (18.0, 22.0):
                for bm in (0.25, 0.35):
                    ml = (adx_u >= ax) & (bfrac_u >= bm) & (ef > e200_u) & (ef > efs) & (lu <= ef + tol) & (cu > ef) & (cu > ou)
                    ms = (adx_u >= ax) & (bfrac_u >= bm) & (ef < e200_u) & (ef < efs) & (hu >= ef - tol) & (cu < ef) & (cu < ou)
                    rd_a = np.where(ml, rd_l, np.where(ms, rd_s, 0.0))
                    for tp_r, be_r in ((3.0, 1.0), (3.5, 1.0), (2.5, 0.0)):
                        t_s10.append({
                            "ml": ml, "ms": ms, "rd_arr": rd_a, "tp_r": tp_r, "be_r": be_r,
                            "params": {"tf": tf_k, "ema_period": ep, "adx_min": ax, "body_min": bm, "tp_r": tp_r, "be_r": be_r},
                        })
        _run_family(f"ig_s10_ema_pullback_{tf_k}", tf_k, t_s10, arr_u, tu_tr, tu_val, min_tr_n=12)

    # 3) ig_s1_williams_pullback (H4)
    t_s1 = []
    for wp in (10, 14):
        wr = williams_r_arr(h4, l4, c4, wp)
        wrp = pd.Series(wr).shift(1).to_numpy()
        for ax in (16.0, 20.0):
            ml = (adx4 >= ax) & (wrp <= -80.0) & (wr > -80.0) & (e50_4 > e200_4) & (c4 > e200_4) & (c4 > o4)
            ms = (adx4 >= ax) & (wrp >= -20.0) & (wr < -20.0) & (e50_4 < e200_4) & (c4 < e200_4) & (c4 < o4)
            for sl_a, tp_r in ((1.5, 3.0), (1.5, 3.5), (2.0, 3.0)):
                t_s1.append({
                    "ml": ml, "ms": ms, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0,
                    "params": {"wr_period": wp, "adx_min": ax, "rev_close": True, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0},
                })
    _run_family("ig_s1_williams_pullback", "H4", t_s1, a4, t4_tr_end, t4_val_end)

    # 4) ig_s2_rsi2_meanrev (H1)
    if a1 is not None:
        c1, h1, l1, tsu = a1["c"], a1["h"], a1["l"], a1["ts"]
        r2_1 = rsi_wilder_arr(c1, 2)
        cum2 = 0.5 * (r2_1 + pd.Series(r2_1).shift(1).to_numpy())
        e200_1 = ema_arr(c1, 200)
        e200_1s = pd.Series(e200_1).shift(5).to_numpy()
        adx1 = adx_arr(h1, l1, c1, 14)
        c1p = pd.Series(c1).shift(1).to_numpy()
        tu_tr = int(tsu[int(len(tsu) * 0.55)])
        tu_val = int(tsu[int(len(tsu) * 0.78)])
        t_s2 = []
        for rl in (10.0, 15.0):
            for ax_m in (30.0, 35.0, 40.0):
                ml = (adx1 <= ax_m) & (cum2 < rl + 5.0) & (c1 > e200_1) & (e200_1 > e200_1s) & (c1 > c1p)
                ms = (adx1 <= ax_m) & (cum2 > 100.0 - (rl + 5.0)) & (c1 < e200_1) & (e200_1 < e200_1s) & (c1 < c1p)
                for sl_a, tp_r, mh in ((1.5, 2.5, 30), (2.0, 2.5, 30), (1.5, 3.0, 36)):
                    t_s2.append({
                        "ml": ml, "ms": ms, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0, "max_hold": mh,
                        "params": {"connors_rev_adx35": True, "rsi_lo": rl, "adx_max": ax_m, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0, "max_hold_bars": mh},
                    })
        _run_family("ig_s2_rsi2_meanrev", "H1", t_s2, a1, tu_tr, tu_val, min_tr_n=14)

    # 5) ig_s5_zscore_breakout (H4)
    t_s5 = []
    for lb in (30, 50):
        mu = sma_arr(c4, lb)
        sdz = rolling_std_arr(c4, lb)
        zv = np.where(sdz > 1e-12, (c4 - mu) / sdz, np.nan)
        zvp = pd.Series(zv).shift(1).to_numpy()
        for ze in (1.0, 1.5):
            for ax in (18.0, 22.0):
                ml = (adx4 >= ax) & atr4_exp & (zvp < ze) & (zv >= ze) & (e50_4 > e200_4) & (c4 > e200_4)
                ms = (adx4 >= ax) & atr4_exp & (zvp > -ze) & (zv <= -ze) & (e50_4 < e200_4) & (c4 < e200_4)
                for sl_a, tp_r in ((1.5, 3.0), (2.0, 3.5)):
                    t_s5.append({
                        "ml": ml, "ms": ms, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0,
                        "params": {"lookback": lb, "z_entry": ze, "cross_only": True, "adx_min": ax, "atr_exp": True, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0},
                    })
    _run_family("ig_s5_zscore_breakout", "H4", t_s5, a4, t4_tr_end, t4_val_end)

    # 6) ig_s7_daily_pivot (H4)
    t_s7 = []
    for sk in (2, 3):
        ml_p, ms_p = _d1_pivots_signals(h4, l4, c4, tm4, e50_4, e200_4, sk, trend_f=True)
        for ax in (0.0, 18.0):
            ml = ml_p & (adx4 >= ax) if ax > 0 else ml_p
            ms = ms_p & (adx4 >= ax) if ax > 0 else ms_p
            for sl_a, tp_r in ((1.5, 2.5), (1.5, 3.0), (2.0, 3.0)):
                t_s7.append({
                    "ml": ml, "ms": ms, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0,
                    "params": {"swing_k": sk, "adx_min": ax, "sl_atr": sl_a, "tp_r": tp_r, "be_r": 1.0, "trend_ema50_200": True},
                })
    _run_family("ig_s7_daily_pivot", "H4", t_s7, a4, t4_tr_end, t4_val_end)

    # 7) htf_gold / s03 (H4)
    t_s3 = []
    for ep in (144, 233):
        for sg in (True, False):
            ml_c, ms_c, rd_c = _cluster_s03_signals(h4, l4, c4, atr4, ep, sg)
            for er in (2.5, 3.0):
                t_s3.append({
                    "ml": ml_c, "ms": ms_c, "rd_arr": rd_c, "tp_r": er, "be_r": 1.0,
                    "params": {"ema_period": ep, "pattern": "P4_cluster", "slope_gate": sg, "regime_gate": False, "exit_r": er},
                })
    _run_family("s03_htf_ema_rejection", "H4", t_s3, a4, t4_tr_end, t4_val_end)

    # 8) vc_gold / s04 (H4)
    sma20 = sma_arr(c4, 20)
    sd20 = rolling_std_arr(c4, 20)
    bb_up, bb_dn = sma20 + 2.0 * sd20, sma20 - 2.0 * sd20
    kc_up, kc_dn = sma20 + 1.5 * atr4, sma20 - 1.5 * atr4
    sq_b = (bb_up < kc_up) & (bb_dn > kc_dn)
    t_s4 = []
    for msq in (2, 3, 5):
        sq_m = pd.Series(sq_b).shift(1).rolling(msq).min().fillna(0.0).to_numpy() > 0.5
        for ax in (0.0, 18.0):
            ml = sq_m & (adx4 >= ax) & (c4 > bb_up) & (c4 >= o4) & (c4 > e200_4) & (e50_4 >= e200_4)
            ms = sq_m & (adx4 >= ax) & (c4 < bb_dn) & (c4 <= o4) & (c4 < e200_4) & (e50_4 <= e200_4)
            for sf in (1.2, 1.5):
                rd_a = np.maximum(np.abs(c4 - sma20), sf * atr4)
                for er in (2.5, 3.5, 4.0):
                    t_s4.append({
                        "ml": ml, "ms": ms, "rd_arr": rd_a, "tp_r": er, "be_r": 1.0 if er >= 3.0 else 0.0,
                        "params": {"min_squeeze_bars": msq, "adx_min": ax, "sl_atr_floor": sf, "exit_r": er, "sl_basis": True},
                    })
    _run_family("s04_vc_squeeze_breakout", "H4", t_s4, a4, t4_tr_end, t4_val_end)

    if not winners:
        return None

    # Merge ig_s10 H4/H1 picking better plateau score
    if "ig_s10_ema_pullback_H1" in winners and "ig_s10_ema_pullback_H4" in winners:
        win10 = max(winners["ig_s10_ema_pullback_H1"], winners["ig_s10_ema_pullback_H4"], key=lambda x: x["plateau_score"])
        winners["ig_s10_ema_pullback"] = win10
        all_oos_trades = [t for t in all_oos_trades if not (t["sleeve"].startswith("ig_s10_") and t["tf"] != win10["tf"])]
        winners.pop("ig_s10_ema_pullback_H1", None)
        winners.pop("ig_s10_ema_pullback_H4", None)
    elif "ig_s10_ema_pullback_H1" in winners:
        winners["ig_s10_ema_pullback"] = winners.pop("ig_s10_ema_pullback_H1")
    elif "ig_s10_ema_pullback_H4" in winners:
        winners["ig_s10_ema_pullback"] = winners.pop("ig_s10_ema_pullback_H4")
    for t in all_oos_trades:
        if t["sleeve"].startswith("ig_s10_"):
            t["sleeve"] = "ig_s10_ema_pullback"

    return {
        "symbol": sym,
        "sp_bps": sp_bps,
        "winners": winners,
        "oos_trades": all_oos_trades,
    }


def optimize_mt5_universe_per_asset(
    apply_to_config: bool = False,
    max_workers: int = 6,
    risk_cap_usd: float = 6.0,
    save_report: bool = True,
) -> dict[str, Any]:
    """Optimize every feasible MT5 broker symbol individually per sleeve and verify Holdout OOS uplift."""
    manifest = json.loads((HIST_DIR / "manifest.json").read_text(encoding="utf-8"))
    all_syms = sorted(manifest.get("symbols", {}).keys())
    specs = snapshot_all_mt5_specs(all_syms)

    # Only scan symbols with live broker spec (or fallback if offline test)
    targets = [(sym, specs.get(sym, {"sp_bps": 10.0, "vol_min": 0.01, "val_per_pt": 1.0, "sp_px": 0.0, "roll3d": 5, "sw_l_bps": -1.0, "sw_s_bps": -0.5})) for sym in all_syms]
    if specs:
        targets = [(sym, sp) for sym, sp in targets if sym in specs]

    per_asset: dict[str, Any] = {}
    all_oos_trades: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(targets)))) as ex:
        futs = {ex.submit(_evaluate_one_mt5_symbol, sym, sp, HIST_DIR, risk_cap_usd): sym for sym, sp in targets}
        for fut in concurrent.futures.as_completed(futs):
            sym_f = futs[fut]
            res = fut.result()
            if res is not None:
                per_asset[sym_f] = res
                all_oos_trades.extend(res["oos_trades"])

    # Portfolio Holdout OOS check (max_concurrent=4, max_per_sleeve=1)
    by_sleeve: dict[str, dict[str, Any]] = {}
    for sym, res in sorted(per_asset.items()):
        for slv, info in res["winners"].items():
            sb = by_sleeve.setdefault(slv, {"symbols": [], "symbol_params": {}, "oos_r": 0.0, "oos_n": 0, "tr_r": 0.0})
            sb["symbols"].append(sym)
            sb["symbol_params"][sym] = info["params"]
            sb["oos_r"] = round(sb["oos_r"] + info["r_oos"], 2)
            sb["oos_n"] += int(info["n_oos"])
            sb["tr_r"] = round(sb["tr_r"] + info["r_tr"], 2)

    oos_sorted = sorted(all_oos_trades, key=lambda x: x["ts_in"])
    active: list[dict[str, Any]] = []
    exec_oos: list[dict[str, Any]] = []
    for t in oos_sorted:
        active = [x for x in active if x["ts_out"] > t["ts_in"]]
        if len(active) < 4 and sum(1 for x in active if x["sleeve"] == t["sleeve"]) < 1 and sum(1 for x in active if x["symbol"] == t["symbol"]) < 1:
            exec_oos.append(t)
            active.append(t)

    arr_r = np.array([t["r"] for t in exec_oos], dtype=float)
    pos_r = float(arr_r[arr_r > 0].sum()) if len(arr_r) else 0.0
    neg_r = float(abs(arr_r[arr_r < 0].sum())) if len(arr_r) else 0.0
    pf_oos = round(pos_r / neg_r, 2) if neg_r > 0 else (9.9 if pos_r > 0 else 0.0)
    hac_oos = compute_hac_stats(arr_r) if len(arr_r) else {"t_hac": 0.0, "p_hac": 1.0}

    report = {
        "generated_ts": int(datetime.now(timezone.utc).timestamp()),
        "scanned_symbols": len(targets),
        "qualified_symbols": len(per_asset),
        "holdout_portfolio_oos": {
            "trades": len(arr_r),
            "wr_pct": round(float((arr_r > 0).mean() * 100.0), 1) if len(arr_r) else 0.0,
            "sum_r": round(float(arr_r.sum()), 2) if len(arr_r) else 0.0,
            "profit_factor": pf_oos,
            "t_hac": hac_oos["t_hac"],
            "p_hac": hac_oos["p_hac"],
        },
        "by_sleeve": by_sleeve,
        "per_asset": {sym: {slv: {k: v for k, v in info.items() if k != "raw_score"} for slv, info in res["winners"].items()} for sym, res in sorted(per_asset.items())},
    }
    if save_report:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if apply_to_config and CONFIG_PATH.is_file() and pf_oos >= 1.25 and float(arr_r.sum()) > 10.0:
        from engine.unified_alpha_pilot import _resolve_config_file

        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        # Preserve audited gold_cluster (htf_gold/vc_gold) and FX session specialists (ivwall/orb_nas);
        # promote Holdout-OOS-verified per-symbol params & tight-spread symbols into ig_s* sleeves.
        for slv in ("ig_s1_williams_pullback", "ig_s2_rsi2_meanrev", "ig_s7_daily_pivot", "ig_s9_donchian_cta", "ig_s10_ema_pullback", "ig_s5_zscore_breakout"):
            sb = by_sleeve.get(slv)
            if not sb or slv not in cfg.get("sleeves", {}):
                continue
            scfg = cfg["sleeves"][slv]
            existing_sp = dict(scfg.get("symbol_params") or {})
            base_syms = list(scfg.get("symbols") or [])
            if not base_syms and scfg.get("config_file"):
                cf_p = _resolve_config_file(scfg["config_file"])
                if cf_p:
                    ext = json.loads(Path(cf_p).read_text(encoding="utf-8"))
                    base_syms = list(ext.get("symbols") or [])
                    if not existing_sp and isinstance(ext.get("symbol_params"), dict):
                        existing_sp.update(ext["symbol_params"])

            if slv == "ig_s5_zscore_breakout":
                # Promote Gold-cluster verified pairs only into S5 so 42007 shares the Gold risk unit cleanly
                gold_only = {k: v for k, v in sb["symbol_params"].items() if k.startswith("GAU") or k.startswith("XAU")}
                if not gold_only or sb["oos_r"] < 5.0:
                    continue
                for sym_k, p_dict in gold_only.items():
                    existing_sp[sym_k] = p_dict
                scfg["symbol_params"] = existing_sp
                scfg["symbols"] = sorted(gold_only.keys())
                scfg["enabled"] = True
                scfg["mode"] = "live-eligible"
                gcm = set(int(x) for x in cfg.get("governor", {}).get("gold_cluster_magics", [42003, 42004, 42010]))
                gcm.add(int(scfg.get("magic", 42007)))
                cfg["governor"]["gold_cluster_magics"] = sorted(gcm)
                continue

            for sym_k, p_dict in sb["symbol_params"].items():
                existing_sp[sym_k] = p_dict
            scfg["symbol_params"] = existing_sp
            if slv == "ig_s2_rsi2_meanrev":
                # Keep audited equity index basket alongside any verified tight-spread additions
                scfg["symbols"] = sorted(set(base_syms) | set(sb["symbols"]))
            elif isinstance(scfg.get("symbols"), list):
                scfg["symbols"] = sorted(set(scfg["symbols"]) | set(sb["symbols"]))
            else:
                scfg["symbols"] = sorted(set(base_syms) | set(sb["symbols"]))
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Promote verified Holdout-OOS symbol_params into engine/config.json")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    rep = optimize_mt5_universe_per_asset(apply_to_config=args.apply, max_workers=args.workers)
    print(json.dumps({k: v for k, v in rep.items() if k != "per_asset"}, indent=2))
