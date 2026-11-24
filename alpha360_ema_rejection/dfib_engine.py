#!/usr/bin/env python3
"""Delta-Fibonacci (multi-dim VWAP sd-band + MTF EMA candle-rejection) backtest
engine with REAL broker spread costs, faithful to the repo's strategy semantics.

Fidelity ports (from Delta-Fibonacci-Institutional-Business/engine/):
  - candle_rejection.detect_rejection_wick / check_vwap_band_signal /
    check_ema_rejection_signal / evaluate_level_rejection (5 pattern variants)
  - vwap_bands.calculate_session_vwap_bands (session-anchored, volume-weighted)
  - multidim_grid_backtest semantics: signal on close i -> entry open i+1,
    structural SL (level +/- 0.2 ATR, min 1.2 ATR risk distance, fallback 1.5 ATR),
    fixed-RR or trailing exits (BE lock at +1R, trail from +1.5R),
    6-bar cooldown, adverse-first intrabar sequencing.

Corrections vs their engine:
  - broker spread (parquet `spread` points x 10^-digits) charged on BOTH sides
    at the entry-bar and exit-bar spreads (their engine: $0.05 commission, no spread)
  - IS/OOS 70/30 chronological split computed per run; selection must use IS only.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
PARQUET_ROOT = os.path.join(REPO, "historical_parquet")

import sys
sys.path.insert(0, os.path.join(REPO, "Delta-Fibonacci-Institutional-Business"))
from engine.candle_rejection import (  # noqa: E402
    check_vwap_band_signal, check_ema_rejection_signal, evaluate_level_rejection)


# ---------------------------------------------------------------- data ------
def list_symbols(tf: str) -> list[str]:
    d = os.path.join(PARQUET_ROOT, tf)
    return sorted(f[:-8] for f in os.listdir(d) if f.endswith(".parquet"))


def infer_digits(close: pd.Series) -> int:
    """MT5 digits = dominant decimal count (mode, cap 8). Max-decimals is unsafe."""
    s = close.astype(str).str.extract(r"\.(\d+)")[0].fillna("")
    s = s.str.rstrip("0")
    dd = s.str.len()
    if not len(dd):
        return 2
    return int(min(int(dd.mode().iloc[0]), 8))


def load_symbol(tf: str, symbol: str) -> pd.DataFrame:
    df = pd.read_parquet(os.path.join(PARQUET_ROOT, tf, symbol + ".parquet"))
    df = df[["datetime", "open", "high", "low", "close", "tick_volume", "spread"]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    df["digits"] = infer_digits(df["close"])
    return df


def is_crypto(symbol: str) -> bool:
    return any(k in symbol.upper() for k in ["BTC", "ETH", "XLM", "XRP", "ADA", "DOGE", "SOL", "LTC"])


# ------------------------------------------------------------ indicators ----
def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    atr = np.zeros(n)
    if n <= period:
        return atr
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def ema(c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    out = np.zeros(n)
    if n < period:
        return out
    out[period - 1] = c[:period].mean()
    a = 2.0 / (period + 1.0)
    for i in range(period, n):
        out[i] = a * c[i] + (1 - a) * out[i - 1]
    return out


def session_vwap_bands(df: pd.DataFrame, symbol: str, anchor: str = "daily") -> pd.DataFrame:
    """Session-anchored VWAP with +/-1,2 SD bands.

    anchor: 'daily' -> broker day (+2h for non-crypto, UTC day for crypto),
            'weekly' -> ISO week of the broker-shifted clock (for 4H bars).
    """
    out = df.reset_index().rename(columns={"datetime": "time"})
    vol = out["tick_volume"].astype(float).clip(lower=1.0)
    shift = pd.Timedelta(hours=0) if is_crypto(symbol) else pd.Timedelta(hours=2)
    st = out["time"] + shift
    if anchor == "weekly":
        session_id = st.dt.isocalendar().year.astype(str) + "-W" + st.dt.isocalendar().week.astype(str)
    else:
        session_id = st.dt.date
    tp = (out["high"].astype(float) + out["low"].astype(float) + out["close"].astype(float)) / 3.0
    pv, pv2 = tp * vol, vol * tp ** 2
    cum_vol = vol.groupby(session_id).cumsum()
    vwap = pv.groupby(session_id).cumsum() / cum_vol
    var = (pv2.groupby(session_id).cumsum() / cum_vol) - vwap ** 2
    std = np.sqrt(np.maximum(0.0, var))
    out["vwap"], out["vwap_std"] = vwap, std
    out["vwap_p1"], out["vwap_p2"] = vwap + std, vwap + 2 * std
    out["vwap_m1"], out["vwap_m2"] = vwap - std, vwap - 2 * std
    return out


# ------------------------------------------------------------- signals ------
def precompute_signals(df: pd.DataFrame, symbol: str, tf: str, family: str,
                       ema_period: int = 50, multivariant: bool = True, start_bar: int = 20):
    """Signal sides/setups/risk-distances per bar, ported from their engine."""
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    atr = wilder_atr(h, l, c, 14)
    n = len(c)
    sig_sides = np.zeros(n, np.int8)
    sig_setups = [""] * n
    sig_rds = np.zeros(n)

    if family == "VWAP":
        vb = session_vwap_bands(df, symbol, anchor="weekly" if tf == "4H" else "daily")
        vwap = vb["vwap"].to_numpy(float); p1 = vb["vwap_p1"].to_numpy(float)
        p2 = vb["vwap_p2"].to_numpy(float); m1 = vb["vwap_m1"].to_numpy(float)
        m2 = vb["vwap_m2"].to_numpy(float)
    else:
        ema_vals = ema(c, ema_period)
        start_bar = max(start_bar, ema_period + 10)

    for i in range(start_bar, n - 1):
        res = {"side": 0}
        if family == "VWAP":
            res = check_vwap_band_signal(
                o[i], h[i], l[i], c[i], c[i - 1], vwap[i], p1[i], p2[i], m1[i], m2[i], atr[i])
            if res["side"] == 0 and multivariant:
                tol = 0.20 * atr[i]
                if l[i] <= m2[i] + tol:
                    mv = evaluate_level_rejection(o, h, l, c, atr, i, m2[i], 1)
                    if mv["detected"]:
                        res = {"side": 1, "setup": f"VWAP_M2_{mv['pattern']}", "risk_dist": mv.get("risk_dist")}
                elif h[i] >= p2[i] - tol:
                    mv = evaluate_level_rejection(o, h, l, c, atr, i, p2[i], -1)
                    if mv["detected"]:
                        res = {"side": -1, "setup": f"VWAP_P2_{mv['pattern']}", "risk_dist": mv.get("risk_dist")}
                elif c[i - 1] <= vwap[i] + tol and h[i] >= vwap[i] - tol:
                    mv = evaluate_level_rejection(o, h, l, c, atr, i, vwap[i], -1)
                    if mv["detected"]:
                        res = {"side": -1, "setup": f"VWAP_0_{mv['pattern']}", "risk_dist": mv.get("risk_dist")}
                elif c[i - 1] >= vwap[i] - tol and l[i] <= vwap[i] + tol:
                    mv = evaluate_level_rejection(o, h, l, c, atr, i, vwap[i], 1)
                    if mv["detected"]:
                        res = {"side": 1, "setup": f"VWAP_0_{mv['pattern']}", "risk_dist": mv.get("risk_dist")}
        else:
            res = check_ema_rejection_signal(
                o[i], h[i], l[i], c[i], ema_vals[i], ema_vals[i - 1], atr[i], ema_period)
            if res["side"] == 0 and multivariant:
                tol = 0.20 * atr[i]
                if ema_vals[i] >= ema_vals[i - 1] * 0.9995 and l[i] <= ema_vals[i] + tol:
                    mv = evaluate_level_rejection(o, h, l, c, atr, i, ema_vals[i], 1)
                    if mv["detected"]:
                        res = {"side": 1, "setup": f"EMA{ema_period}_{mv['pattern']}", "risk_dist": mv.get("risk_dist")}
                elif ema_vals[i] <= ema_vals[i - 1] * 1.0005 and h[i] >= ema_vals[i] - tol:
                    mv = evaluate_level_rejection(o, h, l, c, atr, i, ema_vals[i], -1)
                    if mv["detected"]:
                        res = {"side": -1, "setup": f"EMA{ema_period}_{mv['pattern']}", "risk_dist": mv.get("risk_dist")}
        if res["side"] != 0:
            sig_sides[i] = res["side"]
            sig_setups[i] = res["setup"]
            sig_rds[i] = res.get("risk_dist", 0.0) or 0.0
    return sig_sides, sig_setups, sig_rds


# ------------------------------------------------------------ simulator -----
@dataclass
class SimCfg:
    exit_mode: str = "fixed_2.0r"      # fixed_Xr or trail_Xr
    cooldown_bars: int = 6
    spread_on: bool = True             # charge real broker spread both sides
    commission_usd: float = 0.0        # per trade (their engine used 0.05)
    risk_usd: float = 5.0
    cost_mult: float = 1.0             # spread stress multiplier
    max_cost_frac: float = math.inf     # skip entries where 2*spread/rd exceeds this (Stage-2 refinement; inf = off)
    rd_floor_atr: float = 1.2           # minimum stop distance in ATR multiples (their floor = 1.2)


def parse_exit(exit_mode: str):
    if "trail" in exit_mode:
        step = 0.5 if "0.5" in exit_mode else (1.0 if "1.0" in exit_mode else 1.5)
        return True, step, 6.0 if step <= 1.0 else 8.0
    return False, 0.0, float(exit_mode.split("_")[1].rstrip("r"))


def simulate(df: pd.DataFrame, sig_sides, sig_setups, sig_rds, symbol: str, tf: str,
             cfg: SimCfg = SimCfg()):
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    times = df.index
    spr = df["spread"].to_numpy(float) * (10.0 ** -int(df["digits"].iloc[0])) * cfg.cost_mult
    atr_full = wilder_atr(h, l, c, 14)
    n = len(c)
    is_trail, trail_step, target_rr = parse_exit(cfg.exit_mode)
    trades = []
    in_pos = side = entry_i = 0
    pos_entry = pos_sl = pos_tp = pos_rd = pos_mfe = 0.0
    cooldown_until = 0
    for i in range(20, n - 1):
        if in_pos:
            bullish = c[i] >= o[i]
            seq = [("L", l[i]), ("H", h[i])] if bullish else [("H", h[i]), ("L", l[i])]
            exited, exit_p, reason = False, 0.0, ""
            for ttype, px in seq:
                if side == 1:
                    if ttype == "L" and px <= pos_sl:
                        exited, exit_p, reason = True, pos_sl, ("SL_HIT" if pos_sl < pos_entry else "TRAIL_HIT")
                        break
                    if ttype == "H":
                        pos_mfe = max(pos_mfe, (px - pos_entry) / pos_rd)
                        if pos_tp > 0 and px >= pos_tp:
                            exited, exit_p, reason = True, pos_tp, "TP_HIT"
                            break
                else:
                    if ttype == "H" and px >= pos_sl:
                        exited, exit_p, reason = True, pos_sl, ("SL_HIT" if pos_sl > pos_entry else "TRAIL_HIT")
                        break
                    if ttype == "L":
                        pos_mfe = max(pos_mfe, (pos_entry - px) / pos_rd)
                        if pos_tp > 0 and px <= pos_tp:
                            exited, exit_p, reason = True, pos_tp, "TP_HIT"
                            break
            if not exited and is_trail:
                if pos_mfe >= 1.0:
                    be = pos_entry + (0.05 * pos_rd if side == 1 else -0.05 * pos_rd)
                    if (side == 1 and be > pos_sl) or (side == -1 and be < pos_sl):
                        pos_sl = be
                if pos_mfe >= 1.5:
                    tr = pos_entry + ((pos_mfe - trail_step) * pos_rd if side == 1 else -(pos_mfe - trail_step) * pos_rd)
                    if (side == 1 and tr > pos_sl) or (side == -1 and tr < pos_sl):
                        pos_sl = tr
            if exited:
                r_raw = (exit_p - pos_entry) / pos_rd if side == 1 else (pos_entry - exit_p) / pos_rd
                cost = (spr[entry_i] + spr[i]) if cfg.spread_on else 0.0
                r_net = r_raw - cost / pos_rd - cfg.commission_usd / cfg.risk_usd
                trades.append({
                    "symbol": symbol, "tf": tf, "side": "BUY" if side == 1 else "SELL",
                    "setup": sig_setups[entry_i - 1],
                    "entry_time": times[entry_i], "exit_time": times[i],
                    "entry": pos_entry, "exit": exit_p, "risk_dist": pos_rd,
                    "r_raw": r_raw, "r_net": r_net, "reason": reason,
                    "bars_held": i - entry_i,
                })
                in_pos = 0
                cooldown_until = i + cfg.cooldown_bars
                continue
        if in_pos or i < cooldown_until:
            continue
        if sig_sides[i] != 0:
            nxt = i + 1
            pos_entry, entry_i, side = o[nxt], nxt, int(sig_sides[i])
            rd = sig_rds[i] if sig_rds[i] > 0 else 1.5 * atr_full[i]
            pos_rd = max(1e-5, rd, cfg.rd_floor_atr * atr_full[i])
            if cfg.spread_on and (2.0 * spr[nxt]) / pos_rd > cfg.max_cost_frac:
                continue  # spread would eat more than max_cost_frac of the stop distance
            pos_sl = pos_entry - pos_rd if side == 1 else pos_entry + pos_rd
            pos_tp = pos_entry + target_rr * pos_rd if side == 1 else pos_entry - target_rr * pos_rd
            pos_mfe, in_pos = 0.0, 1
    return pd.DataFrame(trades)


# ------------------------------------------------------------- metrics ------
def metrics(trades: pd.DataFrame) -> dict:
    if not len(trades):
        return {"n": 0, "total_r": 0.0, "pf": 0.0, "win_rate": 0.0, "avg_r": 0.0,
                "max_dd_r": 0.0, "t_stat": 0.0, "pnl_usd": 0.0}
    r = trades["r_net"].to_numpy(float)
    wins, losses = r[r > 0.05], r[r < -0.05]
    cum = np.cumsum(r)
    dd = np.maximum.accumulate(cum) - cum if len(cum) else np.array([0.0])
    return {
        "n": int(len(r)), "total_r": float(r.sum()),
        "pf": float(wins.sum() / abs(losses.sum())) if losses.sum() < 0 else 99.0,
        "win_rate": float((r > 0).mean()), "avg_r": float(r.mean()),
        "max_dd_r": float(dd.max()), "t_stat": float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 2 else 0.0,
        "pnl_usd": float(r.sum() * 5.0),
    }


def split_is_oos(trades: pd.DataFrame, q: float = 0.70):
    if not len(trades):
        return trades, trades
    cut = trades["entry_time"].quantile(q)
    return trades[trades["entry_time"] <= cut], trades[trades["entry_time"] > cut]
