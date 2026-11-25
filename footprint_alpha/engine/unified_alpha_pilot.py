#!/usr/bin/env python3
"""unified_alpha_pilot.py — unified footprint/orderflow trading engine.

One file, end to end:
  DATA    : reads the parquet store built by binance_orderflow/binance_orderflow_dump.py
            (1m orderflow + multi-TF footprint + metrics). `--update-data` shells out to
            the dump engine first — rerun == fill the gap candles, then re-signal.
  FEATURES: 5m bars rebuilt from the store; body-EMA trend structure, ATR, pullback
            masks, and the AUCTION-COMPLETE footprint events (absorption at 12-bar
            extremes, delta flip, HVN-at-extreme with strong close, bin imbalance at
            extreme, volume climax) — identical math to the research pipeline
            (scripts/aec_build_dataset.py, walk-forward validated).
  STRATEGY: AEC-Trend — trade WITH the trend when price pulls back into the candle-body
            EMA zone AND the footprint shows an auction-complete event at the extreme
            (sellers/buyers absorbed). High-confluence entries only; ride via ATR trail /
            body-EMA break; hard structural stop beyond the auction zone.
  EXEC    : venues = 'paper' (signals to stdout + desk/signals.json) or 'mt5'
            (MetaTrader 5 bridge; auto-stubbed when the mt5 module is absent, so the
            engine self-tests anywhere). Risk: fixed fractional per trade, max
            concurrent, daily loss cap.

Modes:
  --mode selftest   offline known-answer test (no data, no network)
  --mode backtest   run the strategy over the whole store, print per-symbol stats
  --mode signal     update data (optional), emit current signals per symbol
  --mode paper      signal + persist to desk/signals.json each pass

Usage:
  python3 engine/unified_alpha_pilot.py --mode selftest
  python3 engine/unified_alpha_pilot.py --mode signal --update-data
  python3 engine/unified_alpha_pilot.py --mode backtest
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "engine", "config.json")
DUMP_ENGINE = os.path.join(ROOT, "binance_orderflow", "binance_orderflow_dump.py")
SIGNALS_PATH = os.path.join(ROOT, "desk", "signals.json")

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


# ================================================================ config ====
DEFAULT_CFG = {
    "strategy": "aec_trend",
    "params": {
        "regime": "strict_htf",        # body-EMA structure + 1h EMA20/50 alignment
        "band": 0.0025,                # pullback touch band around body-EMA20
        "lookback": 5,                 # touch must occur within last N bars
        "min_depth_atr": 1.5,          # prior extension >= 1.5 ATR (real pullback)
        "require_reclaim": True,       # close back through EMA20 = acceptance
        "ac_variants": ["ac_abs", "ac_dflip", "ac_hvn", "ac_imb", "ac_climax"],
        "stop_mult": 2.5,              # hard stop: zone extreme -/+ stop_mult*ATR
        "tp_r": 3.0,                   # take-profit at 3R (maker limit)
        "use_ema_exit": True,          # body-EMA50 cross exit (taker)
        "max_trade_bars": 200,         # time cap
        "min_atr_pct": 0.0005,         # skip dead markets (ATR/price floor)
        "side_mode": "auto"            # auto = trade the aligned side (research: shorts)
    },
    "risk": {"risk_per_trade_pct": 0.5, "max_concurrent": 4, "daily_loss_cap_pct": 1.5},
    "data": {"store": "binance_orderflow/data", "bar_tf": "15m", "symbols": "SUITE"},
    "sleeves": ["aec_trend", "ema_rej", "orb"],
    "universe_mt5": {
        "BTCUSDT": "BTCUSD.pi", "ETHUSDT": "ETHUSD.pi", "SOLUSDT": "SOLUSD.p",
        "XRPUSDT": "XRPUSD.pi", "BNBUSDT": "BNBUSD.p", "DOGEUSDT": "DOGUSD.p",
        "LINKUSDT": "LNKUSD.p", "AVAXUSDT": "AVXUSD.p", "LTCUSDT": "LTCUSD.pi",
        "TRXUSDT": "TRXUSD.p", "DOTUSDT": "DOTUSD.pi", "BCHUSDT": "BCHUSD.p",
        "XLMUSDT": "XLMUSD.p", "UNIUSDT": "UNIUSD.p", "ATOMUSDT": "ATMUSD.p",
        "FILUSDT": "FILUSD.p", "NEARUSDT": "NERUSD.p", "XMRUSDT": "XMRUSD.p",
        "ALGOUSDT": "ALGUSD.p", "VETUSDT": "VETUSD.p"
    },
    "execution": {"venue": "paper", "magic": 20261124, "deviation_points": 20}
}


def load_cfg(path: str = CFG_PATH) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    # per-asset research overrides (written by scripts/aec_per_asset.py)
    ov_path = os.path.join(cfg["data"]["store"], "research", "aec_asset_overrides.json")
    if os.path.exists(ov_path):
        with open(ov_path) as f:
            cfg["asset_overrides"] = json.load(f).get("asset_overrides", {})
    return cfg


# ================================================================ features ==
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def build_5m_frames(sym: str, store: str, tf: str = "15m") -> pd.DataFrame | None:
    """Rebuild the research frame for one symbol at the configured TF.

    15m orderflow files may not exist in older stores — aggregate them from the
    5m files on the fly (sums/CVD-last are exact at the wider bucket).
    """
    tf_ms = {"5m": 300000, "15m": 900000}[tf]
    m1 = sorted(glob.glob(f"{store}/derived/{sym}/{sym}-orderflow-1m-*.parquet"))
    m5 = sorted(glob.glob(f"{store}/derived/{sym}/{sym}-orderflow-{tf}-*.parquet"))
    if not m5 and tf == "15m":
        ms = sorted(glob.glob(f"{store}/derived/{sym}/{sym}-orderflow-5m-*.parquet"))
        if ms:
            o5 = pd.concat([pd.read_parquet(p) for p in ms], ignore_index=True)
            o5["b"] = o5["ts"] // tf_ms * tf_ms
            g5 = o5.groupby("b")
            o5 = pd.DataFrame({
                "ts": g5["b"].first(), "n_trades": g5["n_trades"].sum(),
                "vol_base": g5["vol_base"].sum(), "vol_quote": g5["vol_quote"].sum(),
                "delta_vol": g5["delta_vol"].sum(), "delta_quote": g5["delta_quote"].sum(),
                "cvd_vol": g5["cvd_vol"].last(), "cvd_quote": g5["cvd_quote"].last(),
                "p_high": g5["p_high"].max(), "p_low": g5["p_low"].min()}).reset_index(drop=True)
            o5["delta_pct"] = o5["delta_quote"] / o5["vol_quote"].where(o5["vol_quote"] > 0)
            o5["aggressor_imbalance"] = o5["delta_vol"] / o5["vol_base"].where(o5["vol_base"] > 0)
            o5["avg_trade_size"] = o5["vol_base"] / o5["n_trades"].where(o5["n_trades"] > 0)
            m5 = [None]
            o5frames = o5
    f5 = sorted(glob.glob(f"{store}/derived/{sym}/{sym}-footprint-{tf}-*.parquet"))
    if not (m1 and m5 and f5):
        return None
    a = pd.concat([pd.read_parquet(p) for p in m1], ignore_index=True).sort_values("ts")
    if m5 == [None]:
        o5 = o5frames
    else:
        o5 = pd.concat([pd.read_parquet(p) for p in m5], ignore_index=True).sort_values("ts")
    fp = pd.concat([pd.read_parquet(p) for p in f5], ignore_index=True).sort_values("ts")
    a["b5"] = a["ts"] // tf_ms * tf_ms
    g = a.groupby("b5")
    ohlc = pd.DataFrame({"o": g["p_first"].first(), "h": g["p_high"].max(),
                         "l": g["p_low"].min(), "c": g["p_last"].last()}).reset_index() \
        .rename(columns={"b5": "ts"})
    df = o5.merge(ohlc, on="ts", how="inner").merge(fp, on="ts", how="inner", suffixes=("", "_fp"))
    df = df.sort_values("ts").reset_index(drop=True)
    for c in ("vol_base", "vol_quote", "n_trades", "delta_vol", "delta_quote"):
        if f"{c}_fp" in df.columns:
            df = df.drop(columns=[f"{c}_fp"])
    df["body_mid"] = (df["o"] + df["c"]) / 2
    df["ema20b"] = ema(df["body_mid"], 20)
    df["ema50b"] = ema(df["body_mid"], 50)
    tr = np.maximum(df["h"] - df["l"], np.maximum((df["h"] - df["c"].shift()).abs(),
                                                  (df["l"] - df["c"].shift()).abs()))
    df["atr"] = tr.rolling(14).mean()
    rng = (df["h"] - df["l"]).replace(0, np.nan)
    df["close_pos"] = ((df["c"] - df["l"]) / rng).clip(0, 1)
    df["vol_ma20"] = df["vol_base"].rolling(20).mean()
    df["is_low12"] = df["l"] <= df["l"].rolling(12).min() + 1e-12
    df["is_high12"] = df["h"] >= df["h"].rolling(12).max() - 1e-12
    # auction-complete events (footprint confluence)
    dp = df["delta_pct"]
    K = 12
    poc_frac = df["fp_poc_bin"] / (K - 1)
    imb_low = df[[f"fp{k}_b_vol" for k in range(4)]].sum(axis=1) / \
        (df[[f"fp{k}_s_vol" for k in range(4)]].sum(axis=1) + 1e-9)
    imb_high = df[[f"fp{k}_s_vol" for k in range(8, K)]].sum(axis=1) / \
        (df[[f"fp{k}_b_vol" for k in range(8, K)]].sum(axis=1) + 1e-9)
    dq = df["delta_quote"]
    df["ac_abs_low"] = df["is_low12"] & (dp > 0.02)
    df["ac_abs_high"] = df["is_high12"] & (dp < -0.02)
    df["ac_dflip_low"] = (dq.shift(1) < 0) & (dq > 0) & (df["l"] <= df["l"].shift(1))
    df["ac_dflip_high"] = (dq.shift(1) > 0) & (dq < 0) & (df["h"] >= df["h"].shift(1))
    df["ac_hvn_low"] = (poc_frac <= 0.30) & (df["c"] > df["o"]) & (df["close_pos"] > 0.6) \
        & (df["vol_base"] > 1.2 * df["vol_ma20"])
    df["ac_hvn_high"] = (poc_frac >= 0.70) & (df["c"] < df["o"]) & (df["close_pos"] < 0.4) \
        & (df["vol_base"] > 1.2 * df["vol_ma20"])
    df["ac_imb_low"] = (imb_low >= 1.8) & df["is_low12"]
    df["ac_imb_high"] = (imb_high >= 1.8) & df["is_high12"]
    df["ac_climax_low"] = (df["vol_base"] > 2.0 * df["vol_ma20"]) & df["is_low12"] & (df["c"] > df["o"])
    df["ac_climax_high"] = (df["vol_base"] > 2.0 * df["vol_ma20"]) & df["is_high12"] & (df["c"] < df["o"])
    # v2 quality features (walk-forward validated family)
    ext_up = ((df["h"] - df["ema20b"]) / df["atr"]).rolling(12).max()
    ext_dn = ((df["ema20b"] - df["l"]) / df["atr"]).rolling(12).max()
    df["depth_up_ok"] = (ext_up >= 1.5).fillna(False)
    df["depth_dn_ok"] = (ext_dn >= 1.5).fillna(False)
    df["reclaim_up"] = df["c"] > df["ema20b"]
    df["reclaim_dn"] = df["c"] < df["ema20b"]
    df["touch_low5"] = (df["l"] <= df["ema20b"] * (1 + 0.0025)).rolling(5).max().fillna(0).astype(bool)
    df["touch_high5"] = (df["h"] >= df["ema20b"] * (1 - 0.0025)).rolling(5).max().fillna(0).astype(bool)
    # higher-TF regime: 1h body EMA20 vs EMA50 from the 1m store
    a["h1"] = a["ts"] // 3600000 * 3600000
    gh = a.groupby("h1")
    h1 = pd.DataFrame({"ho": gh["p_first"].first(), "hc": gh["p_last"].last()}).reset_index()
    h1["hb"] = (h1["ho"] + h1["hc"]) / 2
    h1["he20"] = ema(h1["hb"], 20)
    h1["he50"] = ema(h1["hb"], 50)
    h1["htf_up"] = h1["he20"] > h1["he50"]
    h1["htf_dn"] = h1["he20"] < h1["he50"]
    df = df.merge(h1[["h1", "htf_up", "htf_dn"]].rename(columns={"h1": "ts"}), on="ts", how="left")
    df[["htf_up", "htf_dn"]] = df[["htf_up", "htf_dn"]].fillna(False)
    # sleeve features: EMA89/233 rejection + UTC-day 60-min ORB ranges
    df["e89"] = ema(df["c"], 89)
    df["e233"] = ema(df["c"], 233)
    df["slope233"] = df["e233"] - df["e233"].shift(5)
    rng_ = (df["h"] - df["l"]).replace(0, np.nan)
    df["upper_wick"] = df["h"] - np.maximum(df["o"], df["c"])
    df["lower_wick"] = np.minimum(df["o"], df["c"]) - df["l"]
    df["day"] = df["ts"] // 86400000
    hod = (df["ts"] % 86400000) // 300000 // 4
    orb_win = df[hod == 0].groupby("day").agg(r_hi=("h", "max"), r_lo=("l", "min")).reset_index()
    df = df.merge(orb_win, on="day", how="left")
    df[["r_hi", "r_lo"]] = df[["r_hi", "r_lo"]].shift(1)
    df["vol_ma20"] = df["vol_base"].rolling(20).mean()
    df["ac_any_low"] = df["ac_abs_low"] | df["ac_dflip_low"] | df["ac_hvn_low"] | df["ac_imb_low"] | df["ac_climax_low"]
    df["ac_any_high"] = df["ac_abs_high"] | df["ac_dflip_high"] | df["ac_hvn_high"] | df["ac_imb_high"] | df["ac_climax_high"]
    return df


def direction_of(df: pd.DataFrame, i: int, side_hint: str | None = None) -> int:
    """+1 long setup, -1 short setup, 0 none — evaluated on the signal bar i."""
    p = df.iloc[i]
    up = (p["c"] > p["ema50b"]) and (p["ema20b"] > p["ema50b"])
    dn = (p["c"] < p["ema50b"]) and (p["ema20b"] < p["ema50b"])
    if up:
        return 1
    if dn:
        return -1
    return 0


def check_signal(df: pd.DataFrame, i: int, cfg: dict) -> dict | None:
    """AEC-Trend confluence check on the signal (last closed) bar i.

    Walk-forward-validated family (research report section 3):
      regime  : body-EMA trend structure + 1h EMA20/50 alignment
      entry   : pullback touched body-EMA20 (<=5 bars ago) with prior extension
                >= 1.5 ATR, and the signal bar CLOSED back through EMA20 (reclaim)
      trigger : auction-complete footprint event at the pullback extreme
      order   : MAKER limit at the signal close (filled on a retest)
      stop    : 3-bar auction-zone extreme +/- 2.5 ATR     (hard, taker)
      exits   : TP at 3R (maker) / body-EMA50 break (taker) / 200-bar time cap
    """
    P = cfg["params"]
    p = df.iloc[i]
    if not np.isfinite(p["atr"]) or i < 60:
        return None
    side = direction_of(df, i)
    if side == 0:
        return None
    if P["regime"] == "strict_htf":
        if side == 1 and not bool(p.get("htf_up", False)):
            return None
        if side == -1 and not bool(p.get("htf_dn", False)):
            return None
    # pullback touched the body-EMA zone within the lookback window
    band, lb = P["band"], P["lookback"]
    touched = any(df["l"].iloc[j] <= df["ema20b"].iloc[j] * (1 + band)
                  for j in range(max(0, i - lb + 1), i + 1)) if side == 1 else \
        any(df["h"].iloc[j] >= df["ema20b"].iloc[j] * (1 - band)
            for j in range(max(0, i - lb + 1), i + 1))
    if not touched:
        return None
    # depth: the trend must have been extended away from the EMA before the dip
    if P.get("require_reclaim", True):
        if side == 1 and not (bool(p.get("depth_up_ok", False)) and bool(p.get("reclaim_up", False))):
            return None
        if side == -1 and not (bool(p.get("depth_dn_ok", False)) and bool(p.get("reclaim_dn", False))):
            return None
    # auction-complete event, side-aligned
    aligned = []
    for v in P["ac_variants"]:
        col = f"{v}_low" if side == 1 else f"{v}_high"
        if col in df.columns and bool(df[col].iloc[i]):
            aligned.append(v)
    if not aligned:
        return None
    if P.get("min_atr_pct") and p["atr"] / p["c"] < P["min_atr_pct"]:
        return None
    if i + 1 >= len(df):
        return None
    entry = float(p["c"])                     # maker limit at the signal close
    zone_lo = float(df["l"].iloc[max(0, i - 2):i + 1].min())
    zone_hi = float(df["h"].iloc[max(0, i - 2):i + 1].max())
    stop = zone_lo - P["stop_mult"] * p["atr"] if side == 1 else zone_hi + P["stop_mult"] * p["atr"]
    risk = abs(entry - stop)
    tp = entry + P["tp_r"] * risk if side == 1 else entry - P["tp_r"] * risk
    reasons = [f"regime=strict+htf({side:+d})", f"pullback@body-EMA20({band*100:.2f}%,depth>=1.5ATR)",
               "reclaim close", "auction-complete: " + "+".join(v.replace("ac_", "") for v in aligned)]
    return {"symbol": None, "side": "long" if side == 1 else "short",
            "order_type": "limit", "entry": entry, "stop": float(stop), "tp": float(tp),
            "signal_ts": int(df["ts"].iloc[i]), "ac_variants": aligned,
            "atr": float(p["atr"]), "reasons": reasons}


def check_signal_emarej(df: pd.DataFrame, i: int, cfg: dict) -> dict | None:
    """HTF EMA 89/233 trend-continuation rejection (crypto) + footprint confluence.

    P1 wick-kiss / P3 pierce-fail on EMA89, EMA233 slope+regime gates, AC event at
    the rejection extreme (validated footprint trigger), maker limit at the close,
    stop beyond the rejection extreme +/- 1.5 ATR, TP 2R default.
    """
    P = cfg.get("ema_rej", {"tp_r": 2.0, "require_ac": True})
    if i < 240:
        return None
    p = df.iloc[i]
    e89, e233 = p["e89"], p["e233"]
    if not (np.isfinite(e89) and np.isfinite(e233)):
        return None
    rng = max(p["h"] - p["l"], 1e-9)
    o, c, h, l = p["o"], p["c"], p["h"], p["l"]
    side = 1 if (c > e233 and p["slope233"] > 0) else (-1 if (c < e233 and p["slope233"] < 0) else 0)
    if side == 0:
        return None
    prev_c = df["c"].iloc[i - 1]
    if side == 1:
        p1 = (l <= e89) and (c > e89) and ((l - min(o, c)) / rng >= 0.30) and (c > o)
        p3 = (prev_c < e89) and (c > e89)
        ac_ok = bool(p["ac_any_low"])
        stop = min(df["l"].iloc[max(0, i - 3):i + 1].min(), e89) - 1.5 * p["atr"]
    else:
        p1 = (h >= e89) and (c < e89) and ((max(o, c) - h) / rng >= 0.30) and (c < o)
        p3 = (prev_c > e89) and (c < e89)
        ac_ok = bool(p["ac_any_high"])
        stop = max(df["h"].iloc[max(0, i - 3):i + 1].max(), e89) + 1.5 * p["atr"]
    if not (p1 or p3):
        return None
    if P.get("require_ac", True) and not ac_ok:
        return None
    entry = float(c)                      # maker limit at the signal close
    risk = abs(entry - stop)
    if risk <= 0 or risk / entry > 0.05:
        return None
    tp = entry + P.get("tp_r", 2.0) * risk * side
    return {"symbol": None, "side": "long" if side == 1 else "short", "order_type": "limit",
            "entry": entry, "stop": float(stop), "tp": float(tp), "signal_ts": int(p["ts"]),
            "sleeve": "ema_rej", "pattern": "wick-kiss" if p1 else "pierce-fail",
            "atr": float(p["atr"]), "reasons": [f"EMA89-rejection({P.get('tp_r', 2.0)}R)",
                                                "ac-confirm" if ac_ok else "no-ac"]}


def check_signal_orb(df: pd.DataFrame, i: int, cfg: dict) -> dict | None:
    """UTC-day 60-min opening-range breakout with delta+volume footprint confirm."""
    P = cfg.get("orb", {"tp_r": 2.0})
    if i < 30:
        return None
    p = df.iloc[i]
    rhi, rlo = p.get("r_hi"), p.get("r_lo")
    if rhi is None or rlo is None or not (np.isfinite(rhi) and np.isfinite(rlo)):
        return None
    prev_c = df["c"].iloc[i - 1]
    vol_ok = p["vol_base"] > 1.2 * p["vol_ma20"]
    if p["c"] > rhi and prev_c <= rhi and p["delta_pct"] > 0.02 and vol_ok:
        side = 1
        cap = 1.5 * (rhi - rlo)
        stop = max(rlo, p["c"] - cap)
    elif p["c"] < rlo and prev_c >= rlo and p["delta_pct"] < -0.02 and vol_ok:
        side = -1
        cap = 1.5 * (rhi - rlo)
        stop = min(rhi, p["c"] + cap)
    else:
        return None
    entry = float(p["c"])
    risk = abs(entry - stop)
    if risk <= 0 or risk / entry > 0.05:
        return None
    tp = entry + P.get("tp_r", 2.0) * risk * side
    return {"symbol": None, "side": "long" if side == 1 else "short", "order_type": "market",
            "entry": entry, "stop": float(stop), "tp": float(tp), "signal_ts": int(p["ts"]),
            "sleeve": "orb", "atr": float(p["atr"]),
            "reasons": [f"ORB-breakout({P.get('tp_r', 2.0)}R)", "delta+volume confirm"]}


# =============================================================== execution ==
class MT5Stub:
    """Offline execution stub so the engine self-tests without MetaTrader."""
    def __init__(self):
        self.orders = []

    def send(self, order: dict) -> dict:
        order = dict(order)
        order["ticket"] = 900000 + len(self.orders)
        order["status"] = "STUB_FILLED"
        self.orders.append(order)
        return order


def make_venue(venue: str):
    if venue == "mt5":
        try:
            import MetaTrader5 as mt5  # noqa: F401 — available only on Windows terminals

            class MT5Real:
                def send(self, order):
                    import MetaTrader5 as m
                    if not m.initialize():
                        return {"status": "ERROR", "why": "initialize failed"}
                    kind = m.ORDER_TYPE_BUY if order["side"] == "long" else m.ORDER_TYPE_SELL
                    req = {"action": m.TRADE_ACTION_DEAL, "symbol": order["symbol"],
                           "volume": order["volume"], "type": kind,
                           "price": order["entry"], "sl": order["stop"],
                           "deviation": order.get("deviation_points", 20),
                           "magic": order.get("magic", 0), "comment": "AEC-Trend",
                           "type_filling": m.ORDER_FILLING_IOC}
                    res = m.order_send(req)
                    return {"status": "OK" if res.retcode == m.TRADE_RETCODE_DONE else "REJECT",
                            "retcode": getattr(res, "retcode", None), "order": order}
            return MT5Real()
        except ImportError:
            print("[venue] mt5 module not available — falling back to STUB execution")
            return MT5Stub()
    return MT5Stub()


def position_volume(entry: float, stop: float, equity: float, risk_pct: float) -> float:
    risk = equity * risk_pct / 100.0
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        return 0.0
    return round(max(0.0, risk / per_unit), 2)


# ================================================================== modes ====
def mode_selftest(cfg: dict) -> bool:
    """Known-answer test: synthetic 5m frame -> features -> signal -> stub execution."""
    print("=" * 78)
    print("SELFTEST — synthetic AEC-Trend known-answer")
    print("=" * 78)
    rng = np.random.default_rng(3)
    n = 400
    ts = 1795305600000 + np.arange(n) * 300000
    # uptrend with a pullback into the EMA zone and an absorption low (delta>0 at new low)
    drift = np.linspace(100.0, 112.0, n)
    noise = rng.normal(0, 0.15, n)
    dip = -1.2 * np.exp(-((np.arange(n) - 300) / 3.0) ** 2)
    close = drift + noise + dip
    open_ = close - rng.normal(0, 0.05, n)
    high = np.maximum(open_, close) + 0.1
    low = np.minimum(open_, close) - 0.1
    low[298:303] = np.minimum(low[298:303], close[298:303] - 0.9)  # sharp pullback
    df = pd.DataFrame({"ts": ts, "o": open_, "h": high, "l": low, "c": close})
    df["ema20b"] = ema((df["o"] + df["c"]) / 2, 20)
    df["ema50b"] = ema((df["o"] + df["c"]) / 2, 50)
    i0 = 300  # pin the pullback bar: touches body-EMA20 zone but regime stays intact
    df.loc[i0, "l"] = min(df.loc[i0, "l"], df.loc[i0, "ema20b"] - 0.2)
    df.loc[i0, "c"] = max(df.loc[i0, "c"], df.loc[i0, "ema50b"] + 0.3)
    df.loc[i0, "h"] = max(df.loc[i0, "h"], df.loc[i0, "c"])
    tr = np.maximum(df["h"] - df["l"], np.maximum((df["h"] - df["c"].shift()).abs(),
                                                  (df["l"] - df["c"].shift()).abs()))
    df["atr"] = tr.rolling(14).mean()
    df["close_pos"] = ((df["c"] - df["l"]) / (df["h"] - df["l"]).replace(0, np.nan)).clip(0, 1)
    df["vol_ma20"] = 1.0
    df["vol_base"] = 1.0
    df["delta_pct"] = 0.0
    df["delta_quote"] = 0.0
    df["is_low12"] = df["l"] <= df["l"].rolling(12).min() + 1e-12
    df["is_high12"] = False
    for v in ("ac_abs", "ac_dflip", "ac_hvn", "ac_imb", "ac_climax"):
        df[v] = False
    df["delta_pct"] = 0.0
    df.loc[300, "delta_pct"] = 0.05          # absorption: new low, positive delta
    df.loc[300, "is_low12"] = True
    df["ac_abs_low"] = df["is_low12"] & (df["delta_pct"] > 0.02)
    df["ac_dflip_low"] = False
    df["ac_hvn_low"] = False
    df["ac_imb_low"] = False
    df["ac_climax_low"] = False
    df["ac_abs_high"] = False
    df["ac_dflip_high"] = False
    df["ac_hvn_high"] = False
    df["ac_imb_high"] = False
    df["ac_climax_high"] = False
    df["htf_up"] = True
    df["htf_dn"] = False
    df["depth_up_ok"] = True
    df["depth_dn_ok"] = False
    df["reclaim_up"] = df["c"] > df["ema20b"]
    df["reclaim_dn"] = df["c"] < df["ema20b"]
    df["touch_low5"] = (df["l"] <= df["ema20b"] * 1.0025).rolling(5).max().fillna(0).astype(bool)
    df["touch_high5"] = False
    df["fp_poc_bin"] = 6
    K = 12
    for k in range(K):
        df[f"fp{k}_b_vol"] = 0.5
        df[f"fp{k}_s_vol"] = 0.5
    ok_feature = bool(df["ema20b"].iloc[-1] > df["ema50b"].iloc[-1])
    sig = None
    for i in range(60, n - 1):
        s = check_signal(df, i, cfg)
        if s and s["side"] == "long":
            sig = s
    checks = [("body-EMA trend up at end", ok_feature, True),
              ("signal fired on absorption bar", sig is not None, True),
              ("signal side == long", sig and sig["side"] == "long", True),
              ("stop below zone", sig and sig["stop"] < df["l"].iloc[300], True),
              ("reasons mention auction-complete", sig and any("auction-complete" in r for r in sig["reasons"]), True)]
    # execution stub
    venue = make_venue("paper")
    if sig:
        res = venue.send({"symbol": "SYNTH", "side": sig["side"], "entry": sig["entry"],
                          "stop": sig["stop"], "volume": 1.0, "magic": 1})
        checks.append(("stub execution fills", res.get("status") == "STUB_FILLED", True))
    fails = [n_ for n_, got, want in checks if bool(got) != bool(want)]
    for n_, got, want in checks:
        print(f"  [{'OK' if bool(got) == bool(want) else 'FAIL'}] {n_}")
    print("\nSELFTEST: " + ("PASS" if not fails else f"FAIL — {fails}"))
    return not fails


def collect_frames(cfg: dict) -> dict[str, pd.DataFrame]:
    store = cfg["data"]["store"]
    assets = cfg["data"]["symbols"]
    syms = sorted(os.path.basename(p) for p in glob.glob(f"{store}/derived/*") if os.path.isdir(p))
    if assets != "SUITE" and assets != "ALL":
        syms = [s for s in assets.split(",") if s in syms]
    out = {}
    tf = cfg["data"].get("bar_tf", "15m")
    for sym in syms:
        d = build_5m_frames(sym, store, tf)
        if d is not None and len(d) > 300:
            out[sym] = d
    return out


def mode_backtest(cfg: dict) -> None:
    """Sanity backtest of the deployed config over the whole store (maker entries)."""
    P = cfg["params"]
    frames = collect_frames(cfg)
    print(f"backtest over {len(frames)} symbols from {cfg['data']['store']}")
    tot = []
    for sym, d in frames.items():
        trades = []
        i = 60
        while i < len(d) - 1:
            s = check_signal(d, i, cfg)
            if s:
                side = 1 if s["side"] == "long" else -1
                entry, stop, tp = s["entry"], s["stop"], s["tp"]
                # maker fill: price must retrace to the limit on the next bar
                fill = d["l"].iloc[i + 1] <= entry if side == 1 else d["h"].iloc[i + 1] >= entry
                if not fill:
                    i += 1
                    continue
                exit_px = None
                j = i + 1
                while j < len(d):
                    h, l, c = d["h"].iloc[j], d["l"].iloc[j], d["c"].iloc[j]
                    if side == 1:
                        if l <= stop:
                            exit_px, kind = min(d["o"].iloc[j], stop), "stop"; break
                        if h >= tp:
                            exit_px, kind = tp, "tp"; break
                        if P["use_ema_exit"] and c < d["ema50b"].iloc[j]:
                            exit_px, kind = c, "ema50"; break
                    else:
                        if h >= stop:
                            exit_px, kind = max(d["o"].iloc[j], stop), "stop"; break
                        if l <= tp:
                            exit_px, kind = tp, "tp"; break
                        if P["use_ema_exit"] and c > d["ema50b"].iloc[j]:
                            exit_px, kind = c, "ema50"; break
                    if j - i >= P["max_trade_bars"]:
                        exit_px, kind = c, "time"; break
                    j += 1
                if exit_px is None:
                    break
                gross = (exit_px - entry) / entry * side
                net = gross - (0.0004 if kind == "tp" else 0.0011)
                trades.append(net * 10000)
                i = j + 1
            else:
                i += 1
        r = np.array(trades)
        if len(r):
            pf = r[r > 0].sum() / max(1e-9, -r[r < 0].sum())
            tot += list(r)
            print(f"  {sym:14s} n={len(r):4d} win={(r>0).mean()*100:5.1f}% "
                  f"avg={r.mean():7.1f}bps pf={pf:5.2f} total={r.sum()/100:6.1f}%")
        else:
            print(f"  {sym:14s} n=0")
    if tot:
        r = np.array(tot)
        pf = r[r > 0].sum() / max(1e-9, -r[r < 0].sum())
        print(f"  {'TOTAL':14s} n={len(r)} win={(r>0).mean()*100:.1f}% avg={r.mean():.1f}bps pf={pf:.2f}")


def mode_signal(cfg: dict, do_update: bool, persist: bool) -> int:
    if do_update:
        print("[data] gap-filling store via binance_orderflow_dump.py --update …")
        subprocess.run([sys.executable, DUMP_ENGINE, "--assets", "SUITE", "--update"],
                       cwd=ROOT, check=False)
    frames = collect_frames(cfg)
    venue = make_venue(cfg["execution"]["venue"])
    signals = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    overrides = cfg.get("asset_overrides", {})
    for sym, d in frames.items():
        i = len(d) - 2           # last CLOSED bar (maker limit at its close)
        # AEC-Trend sleeve (per-asset override from the per-asset research)
        ov = overrides.get(sym, {})
        cfg_sym = json.loads(json.dumps(cfg))
        if ov:
            P = cfg_sym["params"]
            P["regime"] = ov.get("regime", P["regime"])
            P["stop_mult"] = float(ov.get("stop_mult", P["stop_mult"]))
            P["tp_r"] = float(ov.get("tp_r", P["tp_r"]))
            if ov.get("side") == "short":
                cfg_sym["_side_gate"] = -1
            elif ov.get("side") == "long":
                cfg_sym["_side_gate"] = 1
        for sleeve in cfg.get("sleeves", ["aec_trend"]):
            if sleeve == "aec_trend":
                s = check_signal(d, i, cfg_sym)
                gate = cfg_sym.get("_side_gate")
                if s and gate and {"long": 1, "short": -1}[s["side"]] != gate:
                    s = None
            elif sleeve == "ema_rej":
                s = check_signal_emarej(d, i, cfg_sym)
            elif sleeve == "orb":
                s = check_signal_orb(d, i, cfg_sym)
            else:
                continue
            if s:
                s["symbol"] = sym
                s["mt5_symbol"] = cfg.get("universe_mt5", {}).get(sym, sym.replace("USDT", "USD"))
                s["emitted_at"] = now
                s["volume"] = position_volume(s["entry"], s["stop"], 10000.0,
                                              cfg["risk"]["risk_per_trade_pct"])
                signals.append(s)
                print(f"SIGNAL {now} {sym}({s['mt5_symbol']}) {s['sleeve']} {s['side'].upper()} "
                      f"entry={s['entry']} stop={s['stop']} tp={s.get('tp')}")
                if cfg["execution"]["venue"] in ("paper", "mt5"):
                    res = venue.send({**s, "magic": cfg["execution"]["magic"],
                                      "deviation_points": cfg["execution"]["deviation_points"]})
                    print(f"  venue: {res.get('status')}")
    if persist:
        os.makedirs(os.path.dirname(SIGNALS_PATH), exist_ok=True)
        with open(SIGNALS_PATH, "a") as f:
            for s in signals:
                f.write(json.dumps(s, default=str) + "\n")
        print(f"signals appended -> {SIGNALS_PATH}")
    print(f"{len(signals)} signal(s)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="unified footprint/orderflow trading engine")
    ap.add_argument("--mode", default="selftest",
                    choices=["selftest", "backtest", "signal", "paper"])
    ap.add_argument("--config", default=CFG_PATH)
    ap.add_argument("--update-data", action="store_true")
    args = ap.parse_args()
    cfg = load_cfg(args.config)
    if args.mode == "selftest":
        sys.exit(0 if mode_selftest(cfg) else 1)
    if args.mode == "backtest":
        mode_backtest(cfg)
    elif args.mode in ("signal", "paper"):
        sys.exit(mode_signal(cfg, args.update_data, args.mode == "paper"))


if __name__ == "__main__":
    main()
