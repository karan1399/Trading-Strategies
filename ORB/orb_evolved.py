#!/usr/bin/env python3
"""
orb_evolved.py — Evolved Opening Range Breakout engine (audit-driven rebuild).

Built after an independent red-team audit of ORB/orb_engine.py + orb_mtf_engine.py.
Every design choice below traces to a verified audit finding:

Audit findings implemented here
  A1. The parquet files contain NO `digits` column (schema: timestamp, datetime,
      open, high, low, close, tick_volume, spread, real_volume). The baseline
      falls back to digits=2 — correct for indices/metals/BTC (2 decimals), but
      1000x wrong for EURUSD/GBPUSD (true digits 5) and 10x for JPY pairs (3).
      -> digits are INFERRED here from full-array price precision with sanity bands.
  A2. Strict protocol charges the spread on BOTH entry and exit fills
      (baseline charged ~1x per round trip).  Cost floor: a trade whose stop
      distance < 3x spread is rejected (a stop inside the spread is not a stop).
  A3. 70/30 split by SESSION DAYS (baseline split the trade LIST 70/30).
  A4. Pre-registration: the parameter sweep scores IN-SAMPLE ONLY; OOS is
      evaluated exactly once on the frozen winner (baseline qualified setups
      using oos_pf/oos_n/oos_r directly — selection on the test set).
  A5. Deflated t / DSR reported against the true number of trials.
  A6. 4H trend merge is causal: 15M bar at T sees the last 4H bar with
      open_time <= T - 4h (verified: forward-4h aggregation reproduces 326/326
      of SP500 4H bars -> timestamps are bar opens).  No .bfill() leak.
  A7. Multiple Opening Range variants (the alpha-basket question):
      OR30 / OR60 / OR120 (fixed windows after the session anchor),
      PD_HL (prior-day high/low), ON (overnight/pre-anchor range),
      ASIAN (00:00-08:00 UTC range).  Anchor 14:00 or 14:30 UTC for US assets
      (data liquidity step lands on 14:00 = broker session open 09:00 EST;
      NYSE cash open is 14:30 UTC in winter).

Brief innovations (mission §3) — all config-gated:
  I1. Session VWAP ± 1σ/2σ bands: Rule A (trend gate vs VWAP + slope),
      Rule B (scale-out at the +2σ band).
  I2. Initial Balance (first 60m) open-type classifier: Open-Drive /
      Open-Test-Drive / Open-Rejection-Reverse; ORR days fade the false
      breakout back toward the IB midpoint.
  I3. Volatility compression: prev-day range / mean(prev 10 daily ranges)
      (daily-normalised — the naive prev-day/ATR-H1 ratio is a unit error),
      plus NR4 (narrowest range of last 4 days) boost.
  I4. Breakout confirmation: volume >= 1.5x 20-bar SMA OR body >= 65% of range.
  I5. Chandelier exit: HH(22) - 2.5 * ATR14, evaluated on closed bars, filled
      intrabar at the resting-stop level (no lookahead).

Protocol (mission §4): closed-bar trigger -> next-bar-open entry; spread charged
per fill; chronological 70/30 by days; metrics N / NetR / PF / WR / AvgR / MaxDD
/ t-stat for IS and OOS; >= 4 asset classes.

CLI:
  python3 ORB/orb_evolved.py --selftest
  python3 ORB/orb_evolved.py --sweep  --assets NAS100.p,SP500.p,GER40.p,BTCUSD.pi,XAUUSD.pi
  python3 ORB/orb_evolved.py --basket
  python3 ORB/orb_evolved.py --compare
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARQUET_ROOT = os.path.join(REPO_ROOT, "historical_parquet")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# ----------------------------------------------------------------------------- utils

def infer_digits(close: np.ndarray) -> int:
    """True tick decimals from price precision (full array, atol-only).

    The baseline's allclose(rtol=1e-5) equivalent would misread BTC (~8e4) as
    digits=0 because the relative tolerance swallows the fractional part.
    """
    c = np.asarray(close, dtype=float)
    for k in range(0, 9):
        scaled = c * 10.0 ** k
        if float(np.abs(scaled - np.round(scaled)).max()) < 1e-6:
            return k
    return 2


# FX pairs quote to 3-5 decimals; CFD indices/metals/crypto to 0-3.
FX_STEMS = {"EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "AUDUSD", "USDCAD", "NZDUSD",
            "USDCHF", "EURGBP", "EURJPY", "USDTRY", "USDMXN", "USDSGD",
            "USDNOK", "USDSEK", "EURCHF", "EURAUD", "GBPAUD"}


def digits_sanity_ok(symbol: str, digits: int) -> bool:
    stem = symbol.split(".")[0]
    if stem in FX_STEMS:
        return 3 <= digits <= 5
    return 0 <= digits <= 3


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr = pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()
    return atr


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


# ----------------------------------------------------------------------------- config

@dataclass
class EvolvedConfig:
    symbol: str
    variant: str = "OR60"          # OR30 | OR60 | OR120 | PD_HL | ON | ASIAN
    anchor: str = "14:00"          # session anchor UTC "HH:MM"
    session_hours: float = 8.0     # trading window after anchor
    signal_window_hours: float = 6.0   # no new entries after anchor + this
    htf_trend: bool = False        # 4H EMA200 gate
    vwap_gate: bool = False        # Rule A
    vwap_scaleout: bool = False    # Rule B (+2 sigma band books half)
    volume_confirm: bool = False   # 1.5x vol SMA OR 65% body
    vcr_filter: bool = False       # prev-day range / mean(10) <= vcr_max
    vcr_max: float = 1.10
    nr4_boost: bool = False        # NR4 day waives the vcr gate
    ib_fade: bool = False          # Open-Rejection-Reverse fade to IB mid
    sl_mode: str = "midpoint"      # midpoint | opposite | atr
    atr_sl_mult: float = 1.0
    exit_mode: str = "eod"         # eod | chandelier | fixed_2R | fixed_3R
    be_lock: bool = False          # move stop to entry +0.1R after +1R
    orvr_max: Optional[float] = None   # skip day if OR range / ATR14 exceeds this
    buffer_atr: float = 0.05
    spread_charge: str = "both"    # "both" = charge per fill (protocol); "one" = baseline
    stop_floor_spread: float = 3.0
    max_trades_per_day: int = 1

    @property
    def anchor_minutes(self) -> int:
        hh, mm = self.anchor.split(":")
        return int(hh) * 60 + int(mm)


@dataclass
class TradeRec:
    symbol: str
    variant: str
    side: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    sl: float
    risk: float
    r_net: float
    reason: str
    session_date: str
    leg: str = "full"              # full | vwap_half_1 | vwap_half_2


# ----------------------------------------------------------------------------- features

class SymbolFeatures:
    """Pre-computed, causal per-symbol feature arrays shared by all configs."""

    def __init__(self, symbol: str, tf: str = "15M"):
        df = load_symbol(symbol, tf)
        if df is None or len(df) < 400:
            raise ValueError(f"{symbol} {tf}: insufficient data ({0 if df is None else len(df)} bars)")
        self.symbol = symbol
        self.tf = tf
        self.df = df
        self.n = len(df)
        self.o = df.open.to_numpy(float)
        self.h = df.high.to_numpy(float)
        self.l = df.low.to_numpy(float)
        self.c = df.close.to_numpy(float)
        self.v = df.tick_volume.to_numpy(float).astype(float)
        self.dt = df.dt
        self.dates = self.dt.dt.date.to_numpy()
        self.hm = self.dt.dt.hour.to_numpy() * 60 + self.dt.dt.minute.to_numpy()
        self.atr = wilder_atr(self.h, self.l, self.c, 14)
        self.vol_sma20 = pd.Series(self.v).rolling(20, min_periods=1).mean().to_numpy()
        # digits & costs (audit A1)
        self.digits = infer_digits(self.c)
        if not digits_sanity_ok(symbol, self.digits):
            raise ValueError(f"{symbol}: inferred digits={self.digits} outside sanity band")
        self.point = 10.0 ** (-self.digits)
        self.spread = df.spread.to_numpy(float) * self.point
        med_range = float(np.median(self.h - self.l))
        med_spread = float(np.median(self.spread))
        if med_spread > 0.5 * med_range:
            raise ValueError(f"{symbol}: median spread {med_spread} > 50% of median bar "
                             f"range {med_range} — cost model implausible, refusing to trade")
        # 4H trend gate (causal, audit A6)
        d4 = load_symbol(symbol, "4H")
        if d4 is not None and len(d4) > 220:
            c4 = d4.close.to_numpy(float)
            ema200 = pd.Series(c4).ewm(span=200, adjust=False).mean().to_numpy()
            self.t4 = d4.dt.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
            self.ema200_4h = ema200
            self.trend_up = c4 > ema200
        else:
            self.t4 = None
        # per-day aggregates for VCR / NR4 / PD_HL / ON / ASIAN
        self.day_rows = {}
        dfc = df.assign(d=df.dt.dt.date)
        for d, g in dfc.groupby("d"):
            gi = g.index.to_numpy()
            self.day_rows[d] = gi
        self.day_list = sorted(self.day_rows.keys())
        self.day_range = {d: float(self.h[gi].max() - self.l[gi].min())
                          for d, gi in self.day_rows.items()}

    def day_of(self, i: int):
        return self.dates[i]

    def bars_between(self, gi: np.ndarray, t0: int, t1: int) -> np.ndarray:
        return gi[(self.hm[gi] >= t0) & (self.hm[gi] < t1)]

    def prev_days(self, d, k: int):
        """Up to k trading days strictly before d (chronological)."""
        pos = self.day_list.index(d)
        return self.day_list[max(0, pos - k):pos]

    def htf_trend_up_at(self, ts) -> Optional[bool]:
        """Last 4H bar CLOSED at or before ts (open_time <= ts - 4h). Causal."""
        if self.t4 is None:
            return None
        ts_utc = pd.Timestamp(ts).tz_convert("UTC").tz_localize(None)
        cut = ts_utc.to_datetime64() - np.timedelta64(4, "h")
        j = int(np.searchsorted(self.t4, cut, side="right")) - 1
        if j < 210:            # EMA200 warm-up guard
            return None
        return bool(self.trend_up[j])

    def session_vwap(self, gi: np.ndarray, anchor: int, end: int):
        """Developing session VWAP + sigma bands from the anchor onward.

        Returns GLOBAL arrays (len = n) so callers can index by absolute bar id."""
        vwap = np.full(self.n, np.nan)
        sig = np.full(self.n, np.nan)
        tp = (self.h[gi] + self.l[gi] + self.c[gi]) / 3.0
        vv = np.maximum(self.v[gi], 1.0)
        cum_pv = 0.0
        cum_v = 0.0
        cum_pvv = 0.0
        for k in range(len(gi)):
            t = self.hm[gi[k]]
            if t < anchor:
                continue
            if t >= end:
                break
            cum_pv += vv[k] * tp[k]
            cum_v += vv[k]
            cum_pvv += vv[k] * tp[k] * tp[k]
            if cum_v > 0:
                mu = cum_pv / cum_v
                var = max(cum_pvv / cum_v - mu * mu, 0.0)
                vwap[gi[k]] = mu
                sig[gi[k]] = math.sqrt(var)
        return vwap, sig


# ----------------------------------------------------------------------------- engine

def level_bounds(f: SymbolFeatures, d, cfg: EvolvedConfig):
    """Opening-range bounds for the requested variant on day d. Returns
    (levels_ready_at_minute, or_h, or_l, extra dict) or None."""
    gi = f.day_rows.get(d)
    if gi is None:
        return None
    anchor = cfg.anchor_minutes
    if f.tf == "1H":
        anchor = (anchor // 60) * 60        # snap to the hourly grid
    bar_min = 15 if f.tf == "15M" else 60
    if cfg.variant in ("OR30", "OR60", "OR120"):
        minutes = {"OR30": 30, "OR60": 60, "OR120": 120}[cfg.variant]
        nbars = max(1, math.ceil(minutes / bar_min))
        orb = f.bars_between(gi, anchor, anchor + nbars * bar_min)
        if len(orb) < nbars:
            return None
        return (anchor + nbars * bar_min, float(f.h[orb].max()),
                float(f.l[orb].min()), {"anchor_eff": anchor})
    if cfg.variant == "PD_HL":
        prevs = f.prev_days(d, 1)
        if not prevs:
            return None
        pj = f.day_rows[prevs[-1]]
        return anchor, float(f.h[pj].max()), float(f.l[pj].min()), {"anchor_eff": anchor}
    if cfg.variant == "ON":
        orb = f.bars_between(gi, 0, anchor)
        if len(orb) < 8:
            return None
        return anchor, float(f.h[orb].max()), float(f.l[orb].min()), {"anchor_eff": anchor}
    if cfg.variant == "ASIAN":
        orb = f.bars_between(gi, 0, 8 * 60)
        if len(orb) < 8 or anchor < 8 * 60 + 15:
            return None
        return anchor, float(f.h[orb].max()), float(f.l[orb].min()), {"anchor_eff": anchor}
    raise ValueError(f"unknown variant {cfg.variant}")


def classify_open(f: SymbolFeatures, gi: np.ndarray, anchor: int, atr0: float):
    """Initial Balance open-type classification from the first 60 minutes.

    Returns (ib_h, ib_l, ib_mid, open_type, orr_side) with open_type in
    {drive_long, drive_short, test_drive, orr_short, orr_long, unknown}.
    """
    ib = f.bars_between(gi, anchor, anchor + 60)
    r30 = f.bars_between(gi, anchor, anchor + 30)
    if len(ib) < 3 or len(r30) < 1:
        return None
    ib_h, ib_l = float(f.h[ib].max()), float(f.l[ib].min())
    mid30 = 0.5 * (float(f.h[r30].max()) + float(f.l[r30].min()))
    c_last = float(f.c[ib[-1]])
    rng_ib = ib_h - ib_l
    otype, orr_side = "test_drive", None
    if rng_ib <= 0:
        return None
    hi30, lo30 = float(f.h[r30].max()), float(f.l[r30].min())
    # rejection-reverse: 30-60m price broke the 30m extreme then closed back through mid
    late = f.bars_between(gi, anchor + 30, anchor + 60)
    if len(late) >= 1:
        broke_up = float(f.h[late].max()) > hi30
        broke_dn = float(f.l[late].min()) < lo30
        if broke_up and c_last < mid30:
            otype, orr_side = "orr_short", "short"
        elif broke_dn and c_last > mid30:
            otype, orr_side = "orr_long", "long"
    if otype == "test_drive":
        if c_last >= ib_l + 0.75 * rng_ib and rng_ib >= 0.9 * atr0:
            otype = "drive_long"
        elif c_last <= ib_l + 0.25 * rng_ib and rng_ib >= 0.9 * atr0:
            otype = "drive_short"
    return ib_h, ib_l, 0.5 * (ib_h + ib_l), otype, orr_side


def simulate_day(f: SymbolFeatures, d, cfg: EvolvedConfig, day_gate_ok: bool):
    """Simulate one session. Returns list[TradeRec]."""
    gi = f.day_rows.get(d)
    if gi is None:
        return []
    lb = level_bounds(f, d, cfg)
    if lb is None:
        return []
    ready_at, or_h, or_l, extra = lb
    if or_h <= or_l:
        return []
    anchor = extra.get("anchor_eff", cfg.anchor_minutes)
    end = anchor + int(cfg.session_hours * 60)
    sig_end = anchor + int(cfg.signal_window_hours * 60)
    atr0 = float(f.atr[gi[0]])
    if cfg.orvr_max is not None and (or_h - or_l) / atr0 > cfg.orvr_max:
        return []                    # over-expanded open (baseline's always-on filter)

    # day-level gates
    if cfg.vcr_filter and not day_gate_ok:
        return []

    buf = cfg.buffer_atr * atr0
    spread_cost = f.spread                      # GLOBAL array — index by global bar i
    charge = 1.0 if cfg.spread_charge == "both" else 0.5   # per-fill vs baseline-style

    # IB classification (for fade logic)
    ibp = classify_open(f, gi, anchor, atr0) if cfg.ib_fade else None

    # session VWAP (Rule A / Rule B)
    if cfg.vwap_gate or cfg.vwap_scaleout:
        vw, sg = f.session_vwap(gi, anchor, end)
    else:
        vw = sg = None

    trades: list[TradeRec] = []
    taken = 0
    pos = None    # open position dict
    half_booked = False

    for k, i in enumerate(gi):
        t = f.hm[i]
        # manage any open position on EVERY bar (also after session end)
        if pos is not None:
            pos = manage_position(f, i, pos, cfg, sg, gi, trades, spread_cost, charge, d, end)
            if pos is None:
                continue
        if t < ready_at or t >= end:
            continue
        if taken >= cfg.max_trades_per_day or t >= sig_end:
            continue
        # --- signal: IB-fade on Open-Rejection-Reverse days, else OR breakout ---
        fade_sig = False
        if cfg.ib_fade and ibp is not None and ibp[4] is not None:
            # ORR day: standard OR breakouts are disabled; fade the failed move
            # back toward the IB midpoint (trigger = bearish/bullish close beyond
            # the first-30m extreme in the reversal direction).
            lo30, hi30, orr_side = ibp[0], ibp[1], ibp[4]
            if orr_side == "short" and f.c[i] < lo30 - buf and f.c[i] < f.o[i]:
                sig, fade_sig = "short", True
            elif orr_side == "long" and f.c[i] > hi30 + buf and f.c[i] > f.o[i]:
                sig, fade_sig = "long", True
            else:
                continue
        else:
            sig = None
            if f.c[i] > or_h + buf and f.c[i] > f.o[i]:
                sig = "long"
            elif f.c[i] < or_l - buf and f.c[i] < f.o[i]:
                sig = "short"
            if sig is None:
                continue
            # gates (day-structure fade trades skip trend/volume/VWAP gates)
            if cfg.htf_trend:
                up = f.htf_trend_up_at(f.dt.iloc[i])
                if up is None or (sig == "long" and not up) or (sig == "short" and up):
                    continue
            if cfg.volume_confirm:
                ok_vol = f.v[i] >= 1.5 * f.vol_sma20[i]
                rng_i = max(f.h[i] - f.l[i], 1e-12)
                ok_body = abs(f.c[i] - f.o[i]) / rng_i >= 0.65
                if not (ok_vol or ok_body):
                    continue
            if cfg.vwap_gate and vw is not None:
                if not np.isfinite(vw[i]):
                    continue
                p2 = vw[i - 2] if i >= 2 and np.isfinite(vw[i - 2]) else np.nan
                p1 = vw[i - 1] if i >= 1 and np.isfinite(vw[i - 1]) else np.nan
                ref = p2 if np.isfinite(p2) else p1
                slope = vw[i] - ref if np.isfinite(ref) else np.nan
                if sig == "long" and not (f.c[i] > vw[i] and slope > 0):
                    continue
                if sig == "short" and not (f.c[i] < vw[i] and slope < 0):
                    continue
        # entry on next bar open (strict protocol)
        if k + 1 >= len(gi):
            continue
        e_i = gi[k + 1]
        raw = f.o[e_i]
        sp = f.spread[e_i] * charge
        side = 1 if sig == "long" else -1
        if side == 1:
            entry = raw + sp
        else:
            entry = raw - sp
        # stop selection
        tp_override = None
        if fade_sig:
            lo30, hi30, ib_mid = ibp[0], ibp[1], ibp[2]
            sl = hi30 + 0.25 * atr0 if sig == "short" else lo30 - 0.25 * atr0
            tp_override = ib_mid
        elif cfg.sl_mode == "midpoint":
            sl = 0.5 * (or_h + or_l)
        elif cfg.sl_mode == "opposite":
            sl = or_l if side == 1 else or_h
        else:
            sl = entry - side * cfg.atr_sl_mult * atr0
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        if risk < cfg.stop_floor_spread * f.spread[e_i]:
            continue                  # stop inside the spread -> not a stop
        pos = {
            "side": side, "entry_i": e_i, "entry": entry, "sl": sl,
            "risk": risk, "half_booked": False, "r_partial": 0.0,
            "tp_override": tp_override,
        }
        taken += 1

    # force-close any surviving position at session end
    if pos is not None:
        close_at_end(f, pos, gi, end, trades, spread_cost, charge, d, cfg)
    return trades


def _r(pos, px, spread_cost_i, charge):
    """Net R of exiting at price px (bid-side quote) given spreads charged per fill."""
    if pos["side"] == 1:
        # entry paid spread (ask), exit receives bid -> subtract exit spread
        return (px - spread_cost_i * charge - pos["entry"]) / pos["risk"]
    return (pos["entry"] - (px + spread_cost_i * charge)) / pos["risk"]


def manage_position(f, i, pos, cfg, sg, gi, trades, spread_cost, charge, d, end):
    """Update stops / book scale-outs / detect exits on bar i (closed-bar levels,
    intrabar stop fills). Returns None when the position fully closed."""
    side = pos["side"]
    h, l, c = f.h[i], f.l[i], f.c[i]
    atr_i = f.atr[i]

    # chandelier trail level from CLOSED bars (highest high of last 22 closed bars)
    if cfg.exit_mode == "chandelier":
        j0 = max(0, i - 22)
        hh = float(f.h[j0:i + 1].max()) if side == 1 else None
        ll = float(f.l[j0:i + 1].min()) if side == -1 else None
        trail = (hh - 2.5 * atr_i) if side == 1 else (ll + 2.5 * atr_i)
        if side == 1:
            pos["sl"] = max(pos["sl"], trail)
        else:
            pos["sl"] = min(pos["sl"], trail)

    # breakeven lock at +1R
    if cfg.be_lock:
        if side == 1 and h >= pos["entry"] + pos["risk"]:
            pos["sl"] = max(pos["sl"], pos["entry"] + 0.1 * pos["risk"])
        if side == -1 and l <= pos["entry"] - pos["risk"]:
            pos["sl"] = min(pos["sl"], pos["entry"] - 0.1 * pos["risk"])

    # Rule B: VWAP +2sigma scale-out (books half at the band touch)
    if cfg.vwap_scaleout and sg is not None and not pos["half_booked"] and np.isfinite(sg[i]):
        band = sg[i] * 2.0
        if side == 1 and h >= band:
            pos["r_partial"] = _r(pos, band, spread_cost[i], charge)
            pos["half_booked"] = True
        elif side == -1 and l <= -band:
            pos["r_partial"] = _r(pos, -band, spread_cost[i], charge)
            pos["half_booked"] = True

    # stop fill (intrabar, at the resting level)
    if (side == 1 and l <= pos["sl"]) or (side == -1 and h >= pos["sl"]):
        finalize(f, pos, pos["sl"], i, "SL", trades, spread_cost, charge, d)
        return None

    # fixed-R target (or the IB-midpoint target on fade trades)
    if "tp_override" in pos and pos["tp_override"] is not None:
        tgt = pos["tp_override"]
        if (side == 1 and h >= tgt) or (side == -1 and l <= tgt):
            finalize(f, pos, tgt, i, "TP_IB", trades, spread_cost, charge, d)
            return None
    elif cfg.exit_mode.startswith("fixed"):
        mult = float(cfg.exit_mode.split("_")[1].replace("R", ""))
        tgt = pos["entry"] + side * mult * pos["risk"]
        if (side == 1 and h >= tgt) or (side == -1 and l <= tgt):
            finalize(f, pos, tgt, i, "TP", trades, spread_cost, charge, d)
            return None

    # session end close
    if f.hm[i] + 15 >= end:
        finalize(f, pos, c, i, "EOD", trades, spread_cost, charge, d)
        return None
    return pos


def close_at_end(f, pos, gi, end, trades, spread_cost, charge, d, cfg):
    i = gi[-1]
    finalize(f, pos, f.c[i], i, "EOD", trades, spread_cost, charge, d)


def finalize(f, pos, px, i, reason, trades, spread_cost, charge, d):
    r_exit = _r(pos, px, spread_cost[i], charge)
    if pos["half_booked"]:
        r_net = 0.5 * pos["r_partial"] + 0.5 * r_exit
    else:
        r_net = r_exit
    trades.append(TradeRec(
        symbol=f.symbol, variant="", side="long" if pos["side"] == 1 else "short",
        signal_time=str(f.dt.iloc[max(0, pos["entry_i"] - 1)]),
        entry_time=str(f.dt.iloc[pos["entry_i"]]),
        exit_time=str(f.dt.iloc[i]),
        entry=float(pos["entry"]), exit=float(px), sl=float(pos["sl"]),
        risk=float(pos["risk"]), r_net=float(r_net), reason=reason,
        session_date=str(d),
    ))


# ----------------------------------------------------------------------------- metrics

def metrics(rs) -> dict:
    r = np.array([t.r_net for t in rs], dtype=float)
    n = len(r)
    if n == 0:
        return {"n": 0, "net_r": 0.0, "pf": 0.0, "wr": 0.0, "avg_r": 0.0,
                "max_dd_r": 0.0, "t": 0.0}
    wins = r[r > 0].sum()
    losses = abs(r[r < 0].sum())
    pf = wins / losses if losses > 0 else (99.0 if wins > 0 else 0.0)
    cum = np.cumsum(r)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    t = float(r.mean() / (r.std(ddof=1) / math.sqrt(n))) if n > 1 and r.std(ddof=1) > 0 else 0.0
    return {"n": n, "net_r": round(float(r.sum()), 2), "pf": round(float(pf), 2),
            "wr": round(100.0 * float((r > 0).mean()), 1),
            "avg_r": round(float(r.mean()), 3), "max_dd_r": round(dd, 2),
            "t": round(t, 2)}


def deflated_t(r: np.ndarray, n_trials: int) -> float:
    """Expected-max haircut on the t-statistic (Bailey & López de Prado style):
    E[max z over N trials] ~ sqrt(2 ln N); returns observed t minus the haircut."""
    n = len(r)
    if n < 5:
        return float("nan")
    t = r.mean() / (r.std(ddof=1) / math.sqrt(n))
    if n_trials <= 1:
        return float(t)
    return float(t - math.sqrt(2.0 * math.log(n_trials)))


def split_days(f: SymbolFeatures, cfg: EvolvedConfig, is_frac: float = 0.70):
    """Chronological 70/30 split by SESSION DAYS with >= 8 tradable bars."""
    days = [d for d in f.day_list if len(f.day_rows[d]) >= 8]
    k = int(len(days) * is_frac)
    return days[:k], days[k:]


def vcr_day_gate(f: SymbolFeatures, d, cfg: EvolvedConfig) -> bool:
    prevs = f.prev_days(d, 11)
    if len(prevs) < 11:
        return False
    pd_range = f.day_range[prevs[-1]]
    mean10 = float(np.mean([f.day_range[x] for x in prevs[:-1]]))
    if mean10 <= 0:
        return False
    ratio = pd_range / mean10
    nr4 = pd_range == min(f.day_range[x] for x in prevs[-4:])
    if cfg.nr4_boost and nr4:
        return True
    return ratio <= cfg.vcr_max


def run_config(f: SymbolFeatures, cfg: EvolvedConfig, days):
    out = []
    for d in days:
        ok = True
        if cfg.vcr_filter:
            ok = vcr_day_gate(f, d, cfg)
        out.extend(simulate_day(f, d, cfg, ok))
    for t in out:
        t.variant = cfg.variant
    return out


# ----------------------------------------------------------------------------- sweeps

ASSETS = {
    "NAS100.p":  {"anchors": ["14:00", "14:30"], "class": "index_us"},
    "SP500.p":   {"anchors": ["14:00", "14:30"], "class": "index_us"},
    "GER40.p":   {"anchors": ["08:00"], "class": "index_eu"},
    "BTCUSD.pi": {"anchors": ["14:00"], "class": "crypto"},
    "XAUUSD.pi": {"anchors": ["14:00", "14:30"], "class": "metal"},
}

VARIANTS = ["OR30", "OR60", "OR120", "PD_HL", "ON", "ASIAN"]


def grid_for(asset: str):
    g = []
    for variant in VARIANTS:
        anchors = ASSETS[asset]["anchors"] if variant != "ASIAN" else ["14:00"] \
            if asset != "GER40.p" else ["08:00"]
        # ASIAN levels exist from 08:00; US assets break them at the NY anchor
        for anchor in anchors:
            for htf in (False, True):
                for vwap in (False, True):
                    for volc in (False, True):
                        for orvr in (None, 1.2):
                            for sl in ("midpoint", "opposite"):
                                for ex in ("eod", "chandelier"):
                                    g.append(EvolvedConfig(
                                        symbol=asset, variant=variant, anchor=anchor,
                                        htf_trend=htf, vwap_gate=vwap, vwap_scaleout=vwap,
                                        volume_confirm=volc, orvr_max=orvr,
                                        be_lock=(ex == "chandelier"),
                                        sl_mode=sl, exit_mode=ex))
    return g


def is_score(m: dict) -> float:
    """In-sample-only pre-registration score (audit A4)."""
    if m["n"] < 12:
        return -1e9
    if m["pf"] < 1.2 or m["t"] < 1.0:
        return -1e9
    return m["pf"] * math.sqrt(m["n"]) + 0.5 * m["t"]


def sweep(asset: str, f: SymbolFeatures, verbose: bool = True):
    is_days, oos_days = split_days(f, EvolvedConfig(symbol=asset))
    rows = []
    n_cfg = 0
    for cfg in grid_for(asset):
        n_cfg += 1
        try:
            tr_is = run_config(f, cfg, is_days)
        except Exception as e:                      # robust error handling
            if verbose:
                print(f"  ! {cfg.variant}/{cfg.anchor}/{cfg.sl_mode}/{cfg.exit_mode}: {e}")
            continue
        m_is = metrics(tr_is)
        s = is_score(m_is)
        rows.append({"cfg": cfg, "is": m_is, "score": s})
    rows.sort(key=lambda x: -x["score"])
    recs = []
    for row in rows:
        c, m = row["cfg"], row["is"]
        recs.append({"variant": c.variant, "anchor": c.anchor, "htf": int(c.htf_trend),
                     "vwap": int(c.vwap_gate), "vol": int(c.volume_confirm),
                     "orvr": "" if c.orvr_max is None else c.orvr_max,
                     "sl": c.sl_mode, "ex": c.exit_mode, **{f"is_{k}": v for k, v in m.items()}})
    pd.DataFrame(recs).to_csv(os.path.join(RESULTS_DIR, f"evolved_sweep_rows_{asset}.csv"),
                              index=False)
    if verbose:
        print(f"[{asset}] evaluated {n_cfg} configs on {len(is_days)} IS days "
              f"(OOS days: {len(oos_days)} — untouched during search)")
        for row in rows[:5]:
            c, m = row["cfg"], row["is"]
            print(f"   {c.variant:>6}@{c.anchor} htf={int(c.htf_trend)} vwap={int(c.vwap_gate)} "
                  f"vol={int(c.volume_confirm)} sl={c.sl_mode:<8} ex={c.exit_mode:<10} "
                  f"IS: n={m['n']} R={m['net_r']} pf={m['pf']} wr={m['wr']} t={m['t']}")
    best = rows[0] if rows and rows[0]["score"] > -1e8 else None
    return best, rows, (is_days, oos_days, n_cfg)


def evaluate_frozen(asset: str, cfg: EvolvedConfig, f: SymbolFeatures,
                    is_days, oos_days, n_trials: int):
    tr_is = run_config(f, cfg, is_days)
    tr_oos = run_config(f, cfg, oos_days)
    m_is, m_oos = metrics(tr_is), metrics(tr_oos)
    r_oos = np.array([t.r_net for t in tr_oos], dtype=float)
    dt_stat = deflated_t(r_oos, n_trials)
    return {"asset": asset, "cfg": cfg, "is": m_is, "oos": m_oos,
            "trades_is": tr_is, "trades_oos": tr_oos, "deflated_t_oos": dt_stat}


# ----------------------------------------------------------------------------- main modes

FROZEN_PATH = os.path.join(RESULTS_DIR, "evolved_frozen.json")


def mode_selftest(assets):
    print("=" * 96)
    print("SELFTEST — engine sanity on every asset (default OR60 config, all days)")
    print("=" * 96)
    ok = True
    for asset in assets:
        try:
            f = SymbolFeatures(asset, "15M")
            cfg = EvolvedConfig(symbol=asset, variant="OR60", anchor=ASSETS[asset]["anchors"][0])
            days = [d for d in f.day_list if len(f.day_rows[d]) >= 8]
            tr = run_config(f, cfg, days)
            m = metrics(tr)
            # causal checks
            bad = [t for t in tr if t.entry_time <= t.signal_time]
            assert not bad, f"lookahead: {len(bad)} trades with entry <= signal"
            assert all(t.risk > 0 for t in tr), "non-positive risk"
            print(f"[OK] {asset:<10} digits={f.digits} spread_med={np.median(f.spread):.5f} "
                  f"days={len(days)} trades={m['n']} netR={m['net_r']} pf={m['pf']}")
        except Exception as e:
            ok = False
            print(f"[FAIL] {asset}: {e}")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def mode_sweep(assets, out_json=FROZEN_PATH):
    print("=" * 96)
    print("PRE-REGISTERED SWEEP — IS-only scoring; OOS evaluated once on frozen winners")
    print("=" * 96)
    frozen = {}
    all_oos = []
    for asset in assets:
        f = SymbolFeatures(asset, "15M")
        best, rows, (is_days, oos_days, n_cfg) = sweep(asset, f)
        if best is None:
            print(f"[{asset}] NO config passed IS pre-registration floor "
                  f"(n>=12, PF>=1.2, t>=1.0) -> no trade, honest skip")
            frozen[asset] = {"qualified": False, "n_trials": n_cfg}
            continue
        cfg = best["cfg"]
        res = evaluate_frozen(asset, cfg, f, is_days, oos_days, n_cfg)
        m_is, m_oos = res["is"], res["oos"]
        print(f"[{asset}] FROZEN: {cfg.variant}@{cfg.anchor} htf={int(cfg.htf_trend)} "
              f"vwap={int(cfg.vwap_gate)} vol={int(cfg.volume_confirm)} "
              f"sl={cfg.sl_mode} ex={cfg.exit_mode}")
        print(f"    IS : n={m_is['n']} R={m_is['net_r']} pf={m_is['pf']} wr={m_is['wr']} "
              f"dd={m_is['max_dd_r']} t={m_is['t']}")
        print(f"    OOS: n={m_oos['n']} R={m_oos['net_r']} pf={m_oos['pf']} wr={m_oos['wr']} "
              f"dd={m_oos['max_dd_r']} t={m_oos['t']}  deflated_t={res['deflated_t_oos']:.2f} "
              f"(trials={n_cfg})")
        frozen[asset] = {
            "qualified": True, "n_trials": n_cfg,
            "cfg": asdict(cfg), "is": m_is, "oos": m_oos,
            "deflated_t_oos": res["deflated_t_oos"],
        }
        all_oos.extend(res["trades_oos"])
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(out_json, "w") as fh:
        json.dump(frozen, fh, indent=1, default=str)
    if all_oos:
        r = np.array([t.r_net for t in all_oos])
        m = metrics(all_oos)
        total_trials = sum(v.get("n_trials", 0) for v in frozen.values())
        dt = deflated_t(r, max(total_trials, 1))
        print("-" * 96)
        print(f"POOLED OOS (pre-registered): n={m['n']} netR={m['net_r']} pf={m['pf']} "
              f"wr={m['wr']} dd={m['max_dd_r']} t={m['t']} deflated_t={dt:.2f} "
              f"(trials={total_trials})")
        pd.DataFrame([asdict(t) for t in all_oos]).to_csv(
            os.path.join(RESULTS_DIR, "evolved_oos_trades.csv"), index=False)
    print(f"frozen configs saved -> {out_json}")
    return 0


def _load_frozen():
    if not os.path.exists(FROZEN_PATH):
        print("no frozen configs — run --sweep first")
        return None
    with open(FROZEN_PATH) as fh:
        return json.load(fh)


def mode_validate1h():
    """Robustness: re-run each FROZEN config on 1H bars (4x coarser, ~10 months
    of history vs ~2.5 months on 15M). No new search -> no extra trials."""
    frozen = _load_frozen()
    if frozen is None:
        return 1
    print("=" * 96)
    print("1H LONG-HISTORY VALIDATION — frozen rules on coarser bars (no re-search)")
    print("=" * 96)
    rows = []
    for asset, info in frozen.items():
        if not info.get("qualified"):
            print(f"[{asset}] not qualified on 15M — skipped")
            continue
        cfg = EvolvedConfig(**{k: v for k, v in info["cfg"].items()})
        try:
            f1 = SymbolFeatures(asset, "1H")
        except Exception as e:
            print(f"[{asset}] 1H load failed: {e}")
            continue
        is_d, oos_d = split_days(f1, cfg)
        m_is = metrics(run_config(f1, cfg, is_d))
        m_oos = metrics(run_config(f1, cfg, oos_d))
        print(f"[{asset}] {cfg.variant}@{cfg.anchor} on 1H: "
              f"window {f1.day_list[0]}..{f1.day_list[-1]} "
              f"(IS {len(is_d)}d / OOS {len(oos_d)}d)")
        print(f"    IS : n={m_is['n']} R={m_is['net_r']} pf={m_is['pf']} wr={m_is['wr']} "
              f"dd={m_is['max_dd_r']} t={m_is['t']}")
        print(f"    OOS: n={m_oos['n']} R={m_oos['net_r']} pf={m_oos['pf']} wr={m_oos['wr']} "
              f"dd={m_oos['max_dd_r']} t={m_oos['t']}")
        rows.append({"asset": asset, "variant": cfg.variant, "anchor": cfg.anchor,
                     "is_n": m_is["n"], "is_r": m_is["net_r"], "is_pf": m_is["pf"],
                     "is_t": m_is["t"], "oos_n": m_oos["n"], "oos_r": m_oos["net_r"],
                     "oos_pf": m_oos["pf"], "oos_t": m_oos["t"],
                     "oos_dd": m_oos["max_dd_r"]})
    if rows:
        df = pd.DataFrame(rows)
        out = os.path.join(RESULTS_DIR, "evolved_1h_validation.csv")
        df.to_csv(out, index=False)
        print(f"saved -> {out}")
    return 0


def mode_basket():
    frozen = _load_frozen()
    if frozen is None:
        return 1
    print("=" * 96)
    print("ALPHA BASKET — frozen best-variant config per asset, pooled OOS stats")
    print("=" * 96)
    all_trades = []
    for asset, info in frozen.items():
        if not info.get("qualified"):
            print(f"[{asset}] not qualified — excluded from basket")
            continue
        cfg = EvolvedConfig(**{k: v for k, v in info["cfg"].items()})
        f = SymbolFeatures(asset, "15M")
        _, oos_days = split_days(f, cfg)
        tr = run_config(f, cfg, oos_days)
        for t in tr:
            t.variant = cfg.variant
        m = metrics(tr)
        print(f"[{asset}] {cfg.variant}@{cfg.anchor}: OOS n={m['n']} R={m['net_r']} "
              f"pf={m['pf']} t={m['t']}")
        all_trades.extend(tr)
    if not all_trades:
        print("basket empty")
        return 1
    df = pd.DataFrame([asdict(t) for t in all_trades]).sort_values("entry_time")
    m = metrics(all_trades)
    r = df.r_net.to_numpy()
    total_trials = sum(v.get("n_trials", 0) for v in frozen.values())
    dt = deflated_t(r, max(total_trials, 1))
    # cross-asset correlation of daily R (overlap days)
    pv = df.pivot_table(index="session_date", columns="symbol", values="r_net",
                        aggfunc="sum").fillna(0.0)
    corr = pv.corr().round(2)
    print("-" * 96)
    print(f"BASKET OOS: n={m['n']} netR={m['net_r']} pf={m['pf']} wr={m['wr']}% "
          f"avgR={m['avg_r']} maxDD={m['max_dd_r']}R t={m['t']} deflated_t={dt:.2f}")
    print("daily-R correlation matrix:")
    print(corr.to_string())
    out_csv = os.path.join(RESULTS_DIR, "evolved_basket_trades.csv")
    df.to_csv(out_csv, index=False)
    cum = df.sort_values("entry_time").r_net.cumsum()
    eq = pd.DataFrame({"entry_time": df.sort_values("entry_time").entry_time,
                       "cum_r": cum.to_numpy()})
    eq.to_csv(os.path.join(RESULTS_DIR, "evolved_basket_equity.csv"), index=False)
    print(f"basket trades -> {out_csv}")
    return 0


def mode_compare():
    """Comparative table: baseline (shipped CSVs, as published) vs evolved (frozen)."""
    frozen = _load_frozen()
    if frozen is None:
        return 1
    base_rows = {
        "SP500.p":  ("15m OR + midpoint + EOD", 7.45, 48.23, 10),
        "NAS100.p": ("1H OR + 15M + 4H trend + EOD", 3.03, 16.46, 13),
        "GER40.p":  ("15m OR + opposite + 3R", 4.73, 15.09, 11),
        "BTCUSD.pi": ("1H OR + 15M + 4H trend + 3R", 3.43, 12.73, 12),
        "XAUUSD.pi": ("Arch1 4H strong trend", 3.44, 20.26, None),
    }
    rows = []
    for asset, info in frozen.items():
        if not info.get("qualified"):
            rows.append({"asset": asset, "baseline_setup": base_rows.get(asset, ("-",))[0],
                         "baseline_oos_pf": base_rows.get(asset, ("-", None))[1],
                         "baseline_oos_r": base_rows.get(asset, ("-", None, None))[2],
                         "baseline_oos_n": base_rows.get(asset, ("-", None, None, None))[3],
                         "evolved_setup": "NO IS-QUALIFIED CONFIG (honest skip)",
                         "evolved_oos_pf": None, "evolved_oos_r": None,
                         "evolved_oos_n": None, "evolved_oos_wr": None,
                         "evolved_max_dd": None, "evolved_t": None,
                         "evolved_deflated_t": None, "evolved_is_pf": None})
            continue
        b = base_rows.get(asset, ("-", None, None, None))
        e = info.get("oos", {})
        rows.append({
            "asset": asset,
            "baseline_setup": b[0],
            "baseline_oos_pf": b[1], "baseline_oos_r": b[2], "baseline_oos_n": b[3],
            "evolved_setup": f"{info['cfg']['variant']}@{info['cfg']['anchor']} "
                             f"htf={int(info['cfg']['htf_trend'])} vwap={int(info['cfg']['vwap_gate'])} "
                             f"vol={int(info['cfg']['volume_confirm'])} sl={info['cfg']['sl_mode']} "
                             f"ex={info['cfg']['exit_mode']}",
            "evolved_oos_pf": e.get("pf"), "evolved_oos_r": e.get("net_r"),
            "evolved_oos_n": e.get("n"), "evolved_oos_wr": e.get("wr"),
            "evolved_max_dd": e.get("max_dd_r"), "evolved_t": e.get("t"),
            "evolved_deflated_t": info.get("deflated_t_oos"),
            "evolved_is_pf": info.get("is", {}).get("pf"),
        })
    df = pd.DataFrame(rows)
    out = os.path.join(RESULTS_DIR, "evolved_compare.csv")
    df.to_csv(out, index=False)
    print(df.to_string(index=False))
    print(f"saved -> {out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="evolved ORB engine")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--basket", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--validate1h", action="store_true")
    ap.add_argument("--assets", type=str,
                    default="NAS100.p,SP500.p,GER40.p,BTCUSD.pi,XAUUSD.pi")
    args = ap.parse_args(argv)
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    if args.selftest:
        return mode_selftest(assets)
    if args.sweep:
        return mode_sweep(assets)
    if args.basket:
        return mode_basket()
    if args.compare:
        return mode_compare()
    if args.validate1h:
        return mode_validate1h()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
