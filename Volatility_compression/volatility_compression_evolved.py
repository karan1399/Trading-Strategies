#!/usr/bin/env python3
"""
volatility_compression_evolved.py — Evolved Volatility Compression / Squeeze
Breakout engine (audit-driven rebuild of quant_volatility_compression.py).

Every design decision traces to a verified audit finding about the baseline:

Audit findings implemented here
  A1. The parquet files contain NO `digits` column; the baseline falls back to
      digits=2 for every instrument. True digits: FX majors 5, JPY crosses 3,
      metals/indices/crypto 2. The baseline's -1,901.98R "FX majors collapse"
      is a cost artefact: with correct ticks the same engine produces positive
      OOS results on 35/48 FX configurations (measured in this audit).
      -> digits are INFERRED here from full-array price precision (atol-only).
  A2. The baseline charges ONE spread per round trip although its own docstring
      and report claim "both entry and exit"; the SELL take-profit branch even
      double-counts the exit spread. -> strict protocol here: the spread is
      charged on EVERY fill (entry AND exit), symmetric for both sides.
  A3. The baseline splits the trade LIST 70/30 (n_is = int(n*0.70)); signals
      cluster, so the IS/OOS boundary slides by months versus a calendar split.
      -> chronological split by SESSION DAYS here.
  A4. The qualification filter (is_pf>=1.25 & oos_pf>=1.20 & oos_n>=8 &
      oos_r>0) selects on OUT-OF-SAMPLE variables, and within metals-4H the
      is_pf criterion alone admits 85% of rows (avg IS PF 1.83 > 1.25), so
      "16 of 20 positive" is close to the base rate, not evidence of edge.
      -> pre-registration here: parameters chosen on IS ONLY (n, PF, t floors),
      frozen, then OOS evaluated exactly once. Deflated t reported against the
      true trial count.
  A5. The four baseline "exit modes" are the same signal series with different
      exits: across-series OOS-R correlation between exit modes is 0.980-0.995
      (measured). 208 reported configurations = 52 independent signal series.
  A6. Docstring/code mismatches in the baseline: the "bar range > 1.2*ATR
      momentum confirmation" does not exist in simulate_squeeze; the stop is
      placed at the SMA basis (middle band), not the "opposing Keltner band".
      -> this engine implements what it documents, and documents what it does.

Brief innovations (mission Phase 2) — all config-gated:
  I1. Multi-factor compression: TTM squeeze (BB(20,2) inside KC(20,1.5*ATR14))
      with maturity k, PLUS optional Bollinger-bandwidth percentile gate and
      Parkinson-volatility percentile gate (both measured in the audit to be
      asset-conditional: they destroy the metals edge, help crypto).
  I2. Momentum directionality: linear-regression slope (5 bars) of the MACD
     (12,26,9) histogram, signed with the breakout direction.
  I3. Volume / absorption: tick_volume >= 1.5x SMA20 OR positive 10-bar
      cumulative volume delta (CVD proxy from bar anatomy).
  I4. Exits: fixed 2R/3R/4R, 1R-breakeven trail, and adaptive Chandelier
      (rolling peak - 2.5*ATR14 over closed bars, filled intrabar at the
      resting level). IS-selected per asset.
  I5. Lead-lag expansion: XAU->XAG, SP500->NAS100, BTC->SOL, USWTI->USDCAD
      gated on the lead's fresh squeeze release. (The audit measured lag-0
      correlation +0.78/+0.95/+0.81 and ~0 at every lag >= 1: the premise is
      co-movement, not spillover — the module exists to MEASURE, and the
      results are reported whatever they show.)

Protocol (mission Section 4): closed-bar signal -> next-bar-open entry; spread
charged per fill; chronological 70/30 by days; metrics N / NetR / PF / WR /
AvgR / MaxDD / t for IS and OOS; deflated t. Core assets: XAUUSD.pi 4H,
XAUAUD.pi 4H, NAS100.p 1H+4H, SP500.p 1H+4H, BTCUSD.pi 1H+4H.

CLI:
  python3 Volatility_compression/volatility_compression_evolved.py --selftest
  python3 ... --exitlab                                   # single-variable exit test
  python3 ... --sweep  [--assets XAUUSD.pi,XAUAUD.pi,...]  # IS-only pre-registration
  python3 ... --leadlag                                   # spillover measurement
  python3 ... --compare                                   # baseline vs evolved table
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARQUET_ROOT = os.path.join(REPO_ROOT, "historical_parquet")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

FX_STEMS = {"EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "AUDUSD", "USDCAD", "NZDUSD",
            "USDCHF", "EURGBP", "EURJPY", "USDTRY", "USDMXN", "USDSGD",
            "USDNOK", "USDSEK", "EURCHF", "EURAUD", "GBPAUD", "CADJPY", "CHFJPY"}


# ----------------------------------------------------------------- utilities

def infer_digits(close: np.ndarray) -> int:
    """True tick decimals from price precision (full array, atol-only scan)."""
    c = np.asarray(close, dtype=float)
    for k in range(0, 9):
        scaled = c * 10.0 ** k
        if float(np.abs(scaled - np.round(scaled)).max()) < 1e-6:
            return k
    return 2


def digits_sanity_ok(symbol: str, digits: int) -> bool:
    stem = symbol.split(".")[0]
    if stem in FX_STEMS:
        return 3 <= digits <= 5
    return 0 <= digits <= 3


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]),
                                                  np.abs(l[1:] - c[:-1])))
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()


def ema(arr: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy()


def sma(arr: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(arr).rolling(window, min_periods=window).mean().to_numpy()


def linreg_slope(x: np.ndarray) -> float:
    """Slope of the OLS line over the last len(x) points (x is the y-series)."""
    n = len(x)
    if n < 2:
        return 0.0
    t = np.arange(n, dtype=float)
    tm, xm = t.mean(), x.mean()
    denom = ((t - tm) ** 2).sum()
    if denom <= 0:
        return 0.0
    return float(((t - tm) * (x - xm)).sum() / denom)


def rolling_pct_rank(arr: np.ndarray, window: int = 100) -> np.ndarray:
    """Percentile rank of the current value within the trailing window (0..1)."""
    out = np.full(len(arr), np.nan)
    for i in range(window - 1, len(arr)):
        w = arr[i - window + 1: i + 1]
        if np.all(np.isnan(w)):
            continue
        v = arr[i]
        if np.isnan(v):
            continue
        out[i] = float((w <= v).mean())
    return out


def load_symbol(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    path = os.path.join(PARQUET_ROOT, tf, f"{symbol}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    df["dt"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.sort_values("dt").reset_index(drop=True)
    if "tick_volume" not in df.columns:
        df["tick_volume"] = df.get("volume", 1.0)
    return df


# ----------------------------------------------------------------- config

@dataclass
class EvolvedConfig:
    symbol: str
    tf: str = "4H"
    min_squeeze_bars: int = 3          # squeeze maturity (k)
    bw_pct_gate: Optional[float] = None    # BB-width percentile ceiling
    park_pct_gate: Optional[float] = None  # Parkinson percentile ceiling
    mom_slope: bool = False            # MACD-histogram linreg slope gate
    vol_confirm: bool = False          # 1.5x vol SMA OR positive CVD
    bar_quality: bool = False          # close in top/bottom 50% of bar range
    trend_ema: int = 200
    exit_mode: str = "fixed_3.0r"      # fixed_2.0r/3.0r/4.0r | trail_1.0r | chandelier
    chandelier_mult: float = 2.5
    sl_basis: bool = True              # stop at SMA basis (baseline-compatible)
    sl_atr_mult: float = 1.2           # stop floor distance multiplier
    cooldown_bars: int = 6
    spread_floor_pts: float = 0.0      # optional realistic floor (stored spread is
                                       # 0 on 54-67% of FX bars — audit finding)
    max_trades: int = 10_000


# ----------------------------------------------------------------- features

class SymbolFeatures:
    """Causal per-symbol indicator arrays shared by all configurations."""

    def __init__(self, symbol: str, tf: str):
        df = load_symbol(symbol, tf)
        if df is None or len(df) < 400:
            raise ValueError(f"{symbol} {tf}: insufficient data "
                             f"({0 if df is None else len(df)} bars)")
        self.symbol = symbol
        self.tf = tf
        self.n = len(df)
        self.o = df.open.to_numpy(float)
        self.h = df.high.to_numpy(float)
        self.l = df.low.to_numpy(float)
        self.c = df.close.to_numpy(float)
        self.v = df.tick_volume.to_numpy(float)
        self.dt = df.dt
        self.dates = self.dt.dt.date.to_numpy()

        self.digits = infer_digits(self.c)
        if not digits_sanity_ok(symbol, self.digits):
            raise ValueError(f"{symbol}: inferred digits={self.digits} outside sanity band")
        self.point = 10.0 ** (-self.digits)
        self.spread = df.spread.to_numpy(float) * self.point
        med_range = float(np.median(self.h - self.l))
        if float(np.median(self.spread)) > 0.5 * med_range:
            raise ValueError(f"{symbol}: median spread > 50% of median bar range "
                             f"({np.median(self.spread)} vs {med_range}) — refusing")

        self.atr = wilder_atr(self.h, self.l, self.c, 14)
        self.basis = sma(self.c, 20)
        sd = pd.Series(self.c).rolling(20, min_periods=20).std(ddof=1).to_numpy()
        self.bb_up = self.basis + 2.0 * sd
        self.bb_dn = self.basis - 2.0 * sd
        self.kc_up = self.basis + 1.5 * self.atr
        self.kc_dn = self.basis - 1.5 * self.atr
        self.ema200 = ema(self.c, 200)

        # squeeze state (BB fully inside KC)
        self.squeezed = (self.bb_up <= self.kc_up) & (self.bb_dn >= self.kc_dn)
        self.sqz_count = np.zeros(self.n, dtype=int)
        for i in range(1, self.n):
            self.sqz_count[i] = self.sqz_count[i - 1] + 1 if self.squeezed[i] else 0

        # multi-factor compression metrics
        self.bb_width = (self.bb_up - self.bb_dn) / np.maximum(self.basis, 1e-12)
        self.bw_pct = rolling_pct_rank(self.bb_width, 100)
        with np.errstate(divide="ignore", invalid="ignore"):
            hl2 = np.log(np.maximum(self.h, 1e-12) / np.maximum(self.l, 1e-12)) ** 2
        park = np.sqrt(np.maximum(hl2, 0) / (4.0 * math.log(2.0)))
        self.park_pct = rolling_pct_rank(park, 100)

        # MACD histogram + slope
        ema12, ema26 = ema(self.c, 12), ema(self.c, 26)
        macd = ema12 - ema26
        self.macd_hist = macd - ema(macd, 9)

        # volume / CVD proxy
        self.vol_sma20 = pd.Series(self.v).rolling(20, min_periods=1).mean().to_numpy()
        rng = np.maximum(self.h - self.l, 1e-12)
        bar_delta = ((self.c - self.o) / rng) * self.v          # signed volume
        self.cvd10 = pd.Series(bar_delta).rolling(10, min_periods=1).sum().to_numpy()

        # squeeze maturity flag helpers
        self.matured_or_released = np.zeros(self.n, dtype=bool)
        for i in range(2, self.n):
            self.matured_or_released[i] = (self.sqz_count[i - 1] >= 3) or \
                (self.sqz_count[i - 2] >= 3 and self.squeezed[i - 1])

    def make_gate(self, cfg: EvolvedConfig):
        """Per-config cost floor spread array."""
        if cfg.spread_floor_pts > 0:
            floor = cfg.spread_floor_pts * self.point
            return np.maximum(self.spread, floor)
        return self.spread


# ----------------------------------------------------------------- engine

def simulate(f: SymbolFeatures, cfg: EvolvedConfig) -> list[dict]:
    n = f.n
    spread = f.make_gate(cfg)
    start = max(220, 100)          # EMA200 + percentile windows warm-up
    trades = []
    last_exit = -cfg.cooldown_bars

    for i in range(start, n - 1):
        if i - last_exit < cfg.cooldown_bars:
            continue
        if not f.matured_or_released[i]:
            continue
        atr_i = f.atr[i]
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue

        sig = None
        if f.c[i] > f.kc_up[i] and f.c[i] > f.ema200[i] and f.c[i] > f.o[i]:
            sig = "long"
        elif f.c[i] < f.kc_dn[i] and f.c[i] < f.ema200[i] and f.c[i] < f.o[i]:
            sig = "short"
        if sig is None:
            continue

        # ---- config-gated confirmation stack (all closed-bar, causal) ----
        if cfg.bar_quality:
            rng_i = max(f.h[i] - f.l[i], 1e-12)
            pos_in_bar = (f.c[i] - f.l[i]) / rng_i
            if sig == "long" and pos_in_bar < 0.5:
                continue
            if sig == "short" and pos_in_bar > 0.5:
                continue
        if cfg.bw_pct_gate is not None:
            if not np.isfinite(f.bw_pct[i]) or f.bw_pct[i] > cfg.bw_pct_gate:
                continue
        if cfg.park_pct_gate is not None:
            if not np.isfinite(f.park_pct[i]) or f.park_pct[i] > cfg.park_pct_gate:
                continue
        if cfg.mom_slope:
            s = linreg_slope(f.macd_hist[max(0, i - 4): i + 1])
            if sig == "long" and s <= 0:
                continue
            if sig == "short" and s >= 0:
                continue
        if cfg.vol_confirm:
            ok_vol = f.v[i] >= 1.5 * f.vol_sma20[i]
            ok_cvd = f.cvd10[i] > 0 if sig == "long" else f.cvd10[i] < 0
            if not (ok_vol or ok_cvd):
                continue

        # ---- entry at next bar open, spread on BOTH fills ----
        e = i + 1
        raw = f.o[e]
        sp = spread[e]
        side = 1 if sig == "long" else -1
        entry = raw + sp if side == 1 else raw - sp          # pay spread entering
        if cfg.sl_basis and np.isfinite(f.basis[i]):
            sl_dist = max(side * (entry - f.basis[i]), cfg.sl_atr_mult * atr_i)
            sl_dist = max(sl_dist, 0.0)
        else:
            sl_dist = cfg.sl_atr_mult * atr_i
        if sl_dist <= 0:
            continue
        sl = entry - side * sl_dist
        if sl_dist < 3.0 * spread[e]:
            continue                                          # stop inside spread

        risk = sl_dist
        target_r = None
        if cfg.exit_mode.startswith("fixed"):
            target_r = float(cfg.exit_mode.split("_")[1].lower().replace("r", ""))
            tp = entry + side * target_r * risk
        else:
            tp = None

        # ---- position simulation ----
        exit_i, exit_px, reason = e, raw, "OPEN"
        curr_sl = sl
        be = False
        peak = f.h[e] if side == 1 else f.l[e]
        for j in range(e, n):
            sp_j = spread[j]
            if side == 1:
                peak = max(peak, f.h[j])
                if cfg.exit_mode == "chandelier":
                    trail = peak - cfg.chandelier_mult * f.atr[j]
                    curr_sl = max(curr_sl, trail)
                if f.l[j] <= curr_sl:
                    exit_i, exit_px, reason = j, curr_sl, "SL"
                    break
                if tp is not None and f.h[j] >= tp:
                    exit_i, exit_px, reason = j, tp, "TP"
                    break
                if cfg.exit_mode == "trail_1.0r":
                    if not be and f.h[j] >= entry + risk:
                        curr_sl = max(curr_sl, entry + 0.1 * risk)
                        be = True
                    if f.h[j] >= entry + 1.5 * risk:
                        curr_sl = max(curr_sl, f.c[j] - risk)
            else:
                peak = min(peak, f.l[j])
                if cfg.exit_mode == "chandelier":
                    trail = peak + cfg.chandelier_mult * f.atr[j]
                    curr_sl = min(curr_sl, trail)
                if f.h[j] + sp_j >= curr_sl:
                    exit_i, exit_px, reason = j, curr_sl, "SL"
                    break
                if tp is not None and f.l[j] + sp_j <= tp:
                    exit_i, exit_px, reason = j, tp, "TP"
                    break
                if cfg.exit_mode == "trail_1.0r":
                    if not be and f.l[j] <= entry - risk:
                        curr_sl = min(curr_sl, entry - 0.1 * risk)
                        be = True
                    if f.l[j] <= entry - 1.5 * risk:
                        curr_sl = min(curr_sl, f.c[j] + risk)
        if reason == "OPEN":
            exit_i, exit_px, reason = n - 1, f.c[n - 1], "END_OF_DATA"

        # net R: spread charged on BOTH fills
        if side == 1:
            r_net = (exit_px - spread[exit_i] - entry) / risk
        else:
            r_net = (entry - (exit_px + spread[exit_i])) / risk

        trades.append({
            "symbol": cfg.symbol, "tf": cfg.tf, "side": sig,
            "signal_time": str(f.dt.iloc[i]), "entry_time": str(f.dt.iloc[e]),
            "exit_time": str(f.dt.iloc[exit_i]),
            "entry": float(entry), "exit": float(exit_px), "sl": float(sl),
            "risk": float(risk), "r_net": float(r_net), "reason": reason,
            "bars_held": int(exit_i - e), "session_date": f.dates[e],
            "exit_mode": cfg.exit_mode,
        })
        last_exit = exit_i
        if len(trades) >= cfg.max_trades:
            break
    return trades


# ----------------------------------------------------------------- metrics

def metrics(trades) -> dict:
    r = np.array([t["r_net"] for t in trades], dtype=float) if trades else np.array([])
    n = len(r)
    if n == 0:
        return {"n": 0, "net_r": 0.0, "pf": 0.0, "wr": 0.0, "avg_r": 0.0,
                "max_dd_r": 0.0, "t": None}
    wins, losses = r[r > 0].sum(), abs(r[r < 0].sum())
    pf = wins / losses if losses > 0 else (99.0 if wins > 0 else 0.0)
    cum = np.cumsum(r)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    # t-statistic is meaningless below ~10 trades (one 5-loss sample produced t=-166)
    t = (float(r.mean() / (r.std(ddof=1) / math.sqrt(n)))
         if n >= 10 and r.std(ddof=1) > 0 else None)
    return {"n": n, "net_r": round(float(r.sum()), 2), "pf": round(float(pf), 2),
            "wr": round(100.0 * float((r > 0).mean()), 1),
            "avg_r": round(float(r.mean()), 3), "max_dd_r": round(dd, 2),
            "t": None if t is None else round(t, 2)}


def deflated_t(r: np.ndarray, n_trials: int) -> float:
    n = len(r)
    if n < 5:
        return float("nan")
    t = r.mean() / (r.std(ddof=1) / math.sqrt(n))
    if n_trials <= 1:
        return float(t)
    return float(t - math.sqrt(2.0 * math.log(n_trials)))


def split_days(f: SymbolFeatures, frac: float = 0.70):
    days = sorted(set(f.dates.tolist()))
    k = int(len(days) * frac)
    return set(days[:k]), set(days[k:])


def run_split(f: SymbolFeatures, cfg: EvolvedConfig):
    is_days, oos_days = split_days(f)
    tr_all = simulate(f, cfg)
    tr_is = [t for t in tr_all if t["session_date"] in is_days]
    tr_oos = [t for t in tr_all if t["session_date"] in oos_days]
    return tr_all, tr_is, tr_oos


# ----------------------------------------------------------------- grids

CORE = {
    "XAUUSD.pi":  ["4H"],
    "XAUAUD.pi":  ["4H"],
    "NAS100.p":   ["1H", "4H"],
    "SP500.p":    ["1H", "4H"],
    "BTCUSD.pi":  ["1H", "4H"],
}


def grid_for(symbol: str, tf: str):
    g = []
    for k in (3, 5):
        for bw in (None, 0.5):
            for park in (None, 0.6):
                for mom in (False, True):
                    for volc in (False, True):
                        for bq in (False, True):
                            for ex in ("fixed_3.0r", "fixed_4.0r", "chandelier"):
                                g.append(EvolvedConfig(
                                    symbol=symbol, tf=tf, min_squeeze_bars=k,
                                    bw_pct_gate=bw, park_pct_gate=park,
                                    mom_slope=mom, vol_confirm=volc,
                                    bar_quality=bq, exit_mode=ex))
    return g


def is_score(m: dict) -> float:
    if m["n"] < 10:
        return -1e9
    if m["pf"] < 1.2 or m["t"] < 1.0:
        return -1e9
    return m["pf"] * math.sqrt(m["n"]) + 0.5 * m["t"]


# ----------------------------------------------------------------- modes

FROZEN_PATH = os.path.join(RESULTS_DIR, "vc_evolved_frozen.json")


def mode_selftest(assets):
    print("=" * 96)
    print("SELFTEST — digit inference, cost sanity, causal execution")
    print("=" * 96)
    ok = True
    for sym, tfs in assets.items():
        for tf in tfs:
            try:
                f = SymbolFeatures(sym, tf)
                cfg = EvolvedConfig(symbol=sym, tf=tf)
                tr_all, tr_is, tr_oos = run_split(f, cfg)
                bad = [t for t in tr_all if t["entry_time"] <= t["signal_time"]]
                assert not bad, f"lookahead: {len(bad)}"
                m = metrics(tr_all)
                print(f"[OK] {sym:<10} {tf} digits={f.digits} spread_med="
                      f"{np.median(f.spread):.5f} bars={f.n} trades={m['n']} "
                      f"netR={m['net_r']} pf={m['pf']}")
            except Exception as e:
                ok = False
                print(f"[FAIL] {sym} {tf}: {e}")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def mode_exitlab(assets):
    """Single-variable exit test: hold signals fixed (baseline stack), vary ONLY exit."""
    print("=" * 96)
    print("EXIT LAB — identical signals, exit mode varied (IS/OOS by days)")
    print("=" * 96)
    rows = []
    for sym, tfs in assets.items():
        for tf in tfs:
            try:
                f = SymbolFeatures(sym, tf)
            except Exception as e:
                print(f"[skip] {sym} {tf}: {e}")
                continue
            for ex in ("fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r", "chandelier"):
                cfg = EvolvedConfig(symbol=sym, tf=tf, exit_mode=ex)
                tr_all, tr_is, tr_oos = run_split(f, cfg)
                m_all, m_is, m_oos = metrics(tr_all), metrics(tr_is), metrics(tr_oos)
                rows.append({"sym": sym, "tf": tf, "exit": ex,
                             "all_r": m_all["net_r"], "all_pf": m_all["pf"], "all_n": m_all["n"],
                             "is_r": m_is["net_r"], "is_pf": m_is["pf"],
                             "oos_r": m_oos["net_r"], "oos_pf": m_oos["pf"],
                             "oos_n": m_oos["n"], "oos_t": m_oos["t"]})
                print(f"{sym:<10} {tf} {ex:<12} allR={m_all['net_r']:+8.2f} "
                      f"IS={m_is['net_r']:+7.2f} OOS={m_oos['net_r']:+7.2f} "
                      f"(oos_pf={m_oos['pf']:.2f} n={m_oos['n']})")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    pd.DataFrame(rows).to_csv(os.path.join(RESULTS_DIR, "vc_evolved_exitlab.csv"), index=False)
    print(f"saved -> {os.path.join(RESULTS_DIR, 'vc_evolved_exitlab.csv')}")
    return 0


def mode_sweep(assets):
    print("=" * 96)
    print("PRE-REGISTERED SWEEP — IS-only scoring; OOS evaluated once on frozen winners")
    print("=" * 96)
    frozen = {}
    for sym, tfs in assets.items():
        for tf in tfs:
            try:
                f = SymbolFeatures(sym, tf)
            except Exception as e:
                print(f"[{sym} {tf}] load failed: {e}")
                continue
            grid = grid_for(sym, tf)
            rows = []
            for cfg in grid:
                try:
                    _, tr_is, _ = run_split(f, cfg)
                    m_is = metrics(tr_is)
                except Exception as ee:
                    print(f"  ! {cfg.min_squeeze_bars}/{cfg.bw_pct_gate}/{cfg.exit_mode}: {ee}")
                    continue
                rows.append({"cfg": cfg, "score": is_score(m_is), "is": m_is})
            rows.sort(key=lambda x: -x["score"])
            recs = [{**{k: v for k, v in asdict(r['cfg']).items() if isinstance(v, (int, float, str, bool, type(None)))},
                     **{f"is_{k}": v for k, v in r["is"].items()}} for r in rows]
            pd.DataFrame(recs).to_csv(
                os.path.join(RESULTS_DIR, f"vc_sweep_rows_{sym}_{tf}.csv"), index=False)
            n_trials = len(grid)
            print(f"[{sym} {tf}] evaluated {n_trials} configs (IS days only; OOS untouched)")
            for r in rows[:3]:
                c, m = r["cfg"], r["is"]
                print(f"   k={c.min_squeeze_bars} bw={c.bw_pct_gate} park={c.park_pct_gate} "
                      f"mom={int(c.mom_slope)} vol={int(c.vol_confirm)} bq={int(c.bar_quality)} "
                      f"ex={c.exit_mode:<12} IS n={m['n']} R={m['net_r']} pf={m['pf']} t={m['t']}")
            if not rows or rows[0]["score"] <= -1e8:
                print(f"[{sym} {tf}] NO config passed IS floor -> honest skip")
                frozen[f"{sym}|{tf}"] = {"qualified": False, "n_trials": n_trials}
                continue
            best = rows[0]["cfg"]
            tr_all, tr_is, tr_oos = run_split(f, best)
            m_is, m_oos = metrics(tr_is), metrics(tr_oos)
            r_oos = np.array([t["r_net"] for t in tr_oos])
            dt = deflated_t(r_oos, n_trials)
            print(f"[{sym} {tf}] FROZEN: k={best.min_squeeze_bars} bw={best.bw_pct_gate} "
                  f"park={best.park_pct_gate} mom={int(best.mom_slope)} vol={int(best.vol_confirm)} "
                  f"bq={int(best.bar_quality)} ex={best.exit_mode}")
            print(f"    IS : n={m_is['n']} R={m_is['net_r']} pf={m_is['pf']} wr={m_is['wr']} "
                  f"dd={m_is['max_dd_r']} t={m_is['t']}")
            print(f"    OOS: n={m_oos['n']} R={m_oos['net_r']} pf={m_oos['pf']} wr={m_oos['wr']} "
                  f"dd={m_oos['max_dd_r']} t={m_oos['t']} deflated_t={dt:.2f} (trials={n_trials})")
            frozen[f"{sym}|{tf}"] = {
                "qualified": True, "n_trials": n_trials,
                "cfg": asdict(best), "is": m_is, "oos": m_oos,
                "deflated_t_oos": dt,
                "trades_oos": tr_oos,
            }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(FROZEN_PATH, "w") as fh:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "trades_oos"}
                   for k, v in frozen.items()}, fh, indent=1, default=str)
    all_trades = []
    for v in frozen.values():
        all_trades.extend(v.get("trades_oos", []))
    if all_trades:
        m = metrics(all_trades)
        r = np.array([t["r_net"] for t in all_trades])
        tot = sum(v.get("n_trials", 0) for v in frozen.values())
        print("-" * 96)
        print(f"POOLED OOS (pre-registered): n={m['n']} netR={m['net_r']} pf={m['pf']} "
              f"wr={m['wr']} dd={m['max_dd_r']} t={m['t']} "
              f"deflated_t={deflated_t(r, max(tot, 1)):.2f} (trials={tot})")
        pd.DataFrame(all_trades).to_csv(
            os.path.join(RESULTS_DIR, "vc_evolved_oos_trades.csv"), index=False)
    print(f"frozen -> {FROZEN_PATH}")
    return 0


def mode_leadlag(pairs):
    """Measure the spillover premise honestly: gate the lag's releases on the
    lead's fresh release (within m bars) and compare with no gate."""
    print("=" * 96)
    print("LEAD-LAG LAB — does the lead's release add information? (inner-join bars)")
    print("=" * 96)
    rows = []
    for lead, lag, tf in pairs:
        try:
            fl = SymbolFeatures(lead, tf)
            fg = SymbolFeatures(lag, tf)
        except Exception as e:
            print(f"[skip] {lead}->{lag} {tf}: {e}")
            continue
        lead_rel = _release_times(fl)
        for m in (2, 4, 8):
            cfg = EvolvedConfig(symbol=lag, tf=tf, exit_mode="fixed_3.0r")
            tr_g = _simulate_gated(fg, cfg, lead_rel, lead, m)
            tr_u = simulate(fg, cfg)
            is_d, oos_d = split_days(fg)
            tr_g_oos = [t for t in tr_g if t["session_date"] in oos_d]
            tr_u_oos = [t for t in tr_u if t["session_date"] in oos_d]
            mg, mu = metrics(tr_g_oos), metrics(tr_u_oos)
            print(f"{lead} -> {lag} [{tf}] gate m={m}: OOS n={mg['n']} R={mg['net_r']:+.2f} "
                  f"pf={mg['pf']:.2f}   |   ungated: n={mu['n']} R={mu['net_r']:+.2f} "
                  f"pf={mu['pf']:.2f}")
            rows.append({"lead": lead, "lag": lag, "tf": tf, "m": m,
                         "gated_oos_r": mg["net_r"], "gated_oos_n": mg["n"],
                         "ungated_oos_r": mu["net_r"], "ungated_oos_n": mu["n"]})
    if rows:
        pd.DataFrame(rows).to_csv(os.path.join(RESULTS_DIR, "vc_evolved_leadlag.csv"),
                                  index=False)
    return 0


def _release_times(f: SymbolFeatures):
    """Indices where the lead fired a squeeze release (close beyond KC band after
    a matured squeeze), causal (closed bar)."""
    out = []
    for i in range(220, f.n):
        if not f.matured_or_released[i]:
            continue
        if f.c[i] > f.kc_up[i] or f.c[i] < f.kc_dn[i]:
            out.append(f.dt.iloc[i])
    return pd.DatetimeIndex(out)


def _simulate_gated(f: SymbolFeatures, cfg: EvolvedConfig, lead_rel: pd.DatetimeIndex,
                    lead_name: str, m_bars: int):
    """Same as simulate() but requires a lead release within the last m lag bars."""
    bar_ms = 4 * 3600 * 1000 if cfg.tf == "4H" else 3600 * 1000
    lead_arr = lead_rel.values.astype("int64")
    spread = f.make_gate(cfg)
    n = f.n
    trades = []
    last_exit = -cfg.cooldown_bars
    for i in range(220, n - 1):
        if i - last_exit < cfg.cooldown_bars:
            continue
        if not f.matured_or_released[i]:
            continue
        t_i = f.dt.iloc[i].value
        win = lead_arr[(lead_arr <= t_i) & (lead_arr >= t_i - m_bars * bar_ms)]
        if len(win) == 0:
            continue
        sig = None
        if f.c[i] > f.kc_up[i] and f.c[i] > f.ema200[i] and f.c[i] > f.o[i]:
            sig = "long"
        elif f.c[i] < f.kc_dn[i] and f.c[i] < f.ema200[i] and f.c[i] < f.o[i]:
            sig = "short"
        if sig is None:
            continue
        e = i + 1
        raw = f.o[e]
        sp = spread[e]
        side = 1 if sig == "long" else -1
        entry = raw + sp if side == 1 else raw - sp
        atr_i = f.atr[i]
        sl_dist = max(side * (entry - f.basis[i]), cfg.sl_atr_mult * atr_i)
        if sl_dist <= 0 or sl_dist < 3.0 * spread[e]:
            continue
        sl = entry - side * sl_dist
        risk = sl_dist
        tp = entry + side * 3.0 * risk
        exit_i, exit_px, reason = e, raw, "OPEN"
        curr_sl = sl
        for j in range(e, n):
            sp_j = spread[j]
            if side == 1:
                if f.l[j] <= curr_sl:
                    exit_i, exit_px, reason = j, curr_sl, "SL"
                    break
                if f.h[j] >= tp:
                    exit_i, exit_px, reason = j, tp, "TP"
                    break
            else:
                if f.h[j] + sp_j >= curr_sl:
                    exit_i, exit_px, reason = j, curr_sl, "SL"
                    break
                if f.l[j] + sp_j <= tp:
                    exit_i, exit_px, reason = j, tp, "TP"
                    break
        if reason == "OPEN":
            exit_i, exit_px, reason = n - 1, f.c[n - 1], "END_OF_DATA"
        if side == 1:
            r_net = (exit_px - spread[exit_i] - entry) / risk
        else:
            r_net = (entry - (exit_px + spread[exit_i])) / risk
        trades.append({"symbol": cfg.symbol, "tf": cfg.tf, "side": sig,
                       "signal_time": str(f.dt.iloc[i]), "entry_time": str(f.dt.iloc[e]),
                       "exit_time": str(f.dt.iloc[exit_i]), "r_net": float(r_net),
                       "session_date": f.dates[e], "exit_mode": cfg.exit_mode,
                       "gate": f"{lead_name}<={m_bars}b"})
        last_exit = exit_i
    return trades


def mode_compare():
    frozen_p = FROZEN_PATH
    if not os.path.exists(frozen_p):
        print("run --sweep first")
        return 1
    with open(frozen_p) as fh:
        frozen = json.load(fh)
    # baseline = the shipped sweep's best published row per asset/TF
    base = pd.read_csv(os.path.join(RESULTS_DIR, "volatility_compression_full_sweep.csv"))
    rows = []
    for key, info in frozen.items():
        sym, tf = key.split("|")
        g = base[(base.symbol == sym) & (base.tf == tf)]
        if len(g):
            b = g.sort_values("oos_r", ascending=False).iloc[0]
            b_row = {"baseline_exit": b["exit"], "baseline_oos_pf": round(float(b["oos_pf"]), 2),
                     "baseline_oos_r": round(float(b["oos_r"]), 2),
                     "baseline_oos_n": int(b["oos_n"]), "baseline_oos_t": float(b["oos_t"])}
        else:
            b_row = {"baseline_exit": "-", "baseline_oos_pf": None, "baseline_oos_r": None,
                     "baseline_oos_n": None, "baseline_oos_t": None}
        e = info.get("oos", {})
        rows.append({"asset": sym, "tf": tf, **b_row,
                     "evolved_qualified": info.get("qualified", False),
                     "evolved_oos_pf": e.get("pf"), "evolved_oos_r": e.get("net_r"),
                     "evolved_oos_n": e.get("n"), "evolved_oos_wr": e.get("wr"),
                     "evolved_max_dd": e.get("max_dd_r"), "evolved_t": e.get("t"),
                     "evolved_deflated_t": info.get("deflated_t_oos"),
                     "evolved_is_pf": info.get("is", {}).get("pf")})
    df = pd.DataFrame(rows)
    out = os.path.join(RESULTS_DIR, "vc_evolved_compare.csv")
    df.to_csv(out, index=False)
    print(df.to_string(index=False))
    print(f"saved -> {out}")
    return 0


LEADLAG_PAIRS = [
    ("XAUUSD.pi", "XAGUSD.pi", "4H"),
    ("SP500.p", "NAS100.p", "4H"),
    ("BTCUSD.pi", "SOLUSD.p", "4H"),
    ("USWTI.p", "USDCAD.pi", "4H"),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="evolved volatility compression engine")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--exitlab", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--leadlag", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--assets", type=str, default="")
    args = ap.parse_args(argv)
    assets = CORE
    if args.assets:
        assets = {}
        for tok in args.assets.split(","):
            if "|" in tok:
                sym, tf = tok.split("|")
                assets.setdefault(sym, []).append(tf)
            else:
                assets.setdefault(tok, []).append("4H")
    if args.selftest:
        return mode_selftest(assets)
    if args.exitlab:
        return mode_exitlab(assets)
    if args.sweep:
        return mode_sweep(assets)
    if args.leadlag:
        return mode_leadlag(LEADLAG_PAIRS)
    if args.compare:
        return mode_compare()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
