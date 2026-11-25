#!/usr/bin/env python3
"""AEC-Trend research dataset builder.

Combines the dumped orderflow + footprint parquet into one 5m research table with:
  - price/ATR/body-EMA structure (open/close rebuilt from 1m bars)
  - trend regime flags (strict/loose, long/short)
  - pullback-touch masks (two bands)
  - AUCTION-COMPLETE event flags (absorption, delta-flip, HVN-at-extreme,
    imbalance-at-extreme, volume climax) — long and short mirrors
  - higher-TF (1h) regime, metrics context (OI change, taker L/S ratio)

Output: binance_orderflow/data/research/research_5m.parquet (+ CSV summary)
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

DATA = "/home/z/my-project/binance_orderflow/data"
OUT = f"{DATA}/research"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
           "LINKUSDT", "AVAXUSDT", "LTCUSDT", "TRXUSDT", "DOTUSDT", "BCHUSDT",
           "XLMUSDT", "UNIUSDT", "ATOMUSDT", "FILUSDT", "NEARUSDT", "XMRUSDT",
           "ALGOUSDT", "VETUSDT", "1000PEPEUSDT", "1000SHIBUSDT"]


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def load_symbol(sym: str, tf: str = "5m") -> pd.DataFrame | None:
    m1 = sorted(glob.glob(f"{DATA}/derived/{sym}/{sym}-orderflow-1m-*.parquet"))
    if tf == "15m":
        m15 = sorted(glob.glob(f"{DATA}/derived/{sym}/{sym}-orderflow-15m-*.parquet"))
        m5src = sorted(glob.glob(f"{DATA}/derived/{sym}/{sym}-orderflow-5m-*.parquet"))
        if not m15 and m5src:
            o5 = pd.concat([pd.read_parquet(p) for p in m5src], ignore_index=True)
            o5["b"] = o5["ts"] // 900000 * 900000
            g5 = o5.groupby("b")
            o5 = pd.DataFrame({
                "ts": g5["b"].first(), "n_trades": g5["n_trades"].sum(),
                "vol_base": g5["vol_base"].sum(), "vol_quote": g5["vol_quote"].sum(),
                "taker_buy_vol": g5["taker_buy_vol"].sum(), "taker_sell_vol": g5["taker_sell_vol"].sum(),
                "taker_buy_quote": g5["taker_buy_quote"].sum(), "taker_sell_quote": g5["taker_sell_quote"].sum(),
                "delta_vol": g5["delta_vol"].sum(), "delta_quote": g5["delta_quote"].sum(),
                "cvd_vol": g5["cvd_vol"].last(), "cvd_quote": g5["cvd_quote"].last(),
                "p_high": g5["p_high"].max(), "p_low": g5["p_low"].min()}).reset_index(drop=True)
            o5["delta_pct"] = o5["delta_quote"] / o5["vol_quote"].where(o5["vol_quote"] > 0)
            o5["aggressor_imbalance"] = o5["delta_vol"] / o5["vol_base"].where(o5["vol_base"] > 0)
            o5["avg_trade_size"] = o5["vol_base"] / o5["n_trades"].where(o5["n_trades"] > 0)
            o5.to_parquet(f"{DATA}/derived/{sym}/{sym}-orderflow-15m-aggregated.parquet", index=False)
            m15 = [f"{DATA}/derived/{sym}/{sym}-orderflow-15m-aggregated.parquet"]
        m5 = m15
    else:
        m5 = sorted(glob.glob(f"{DATA}/derived/{sym}/{sym}-orderflow-{tf}-*.parquet"))
    f5 = sorted(glob.glob(f"{DATA}/derived/{sym}/{sym}-footprint-{tf}-*.parquet"))
    f1h = sorted(glob.glob(f"{DATA}/derived/{sym}/{sym}-footprint-1h-*.parquet"))
    if not (m1 and m5 and f5):
        print(f"{sym}: missing pieces (m1={len(m1)} m5={len(m5)} f5={len(f5)}) — skipped")
        return None
    a = pd.concat([pd.read_parquet(p) for p in m1], ignore_index=True).sort_values("ts")
    o5 = pd.concat([pd.read_parquet(p) for p in m5], ignore_index=True).sort_values("ts")
    fp = pd.concat([pd.read_parquet(p) for p in f5], ignore_index=True).sort_values("ts")
    tf_ms = {"5m": 300000, "15m": 900000}[tf]
    a["b5"] = a["ts"] // tf_ms * tf_ms
    g = a.groupby("b5")
    ohlc = pd.DataFrame({
        "o": g["p_first"].first(), "h": g["p_high"].max(), "l": g["p_low"].min(),
        "c": g["p_last"].last(), "n1m": g["n_trades"].sum()}).reset_index().rename(columns={"b5": "ts"})
    df = o5.merge(ohlc, on="ts", how="inner").merge(fp, on="ts", how="inner", suffixes=("", "_fp"))
    df = df.sort_values("ts").reset_index(drop=True)
    # dedupe overlapping columns (vol_base etc. identical from both sources)
    for c in ("vol_base", "vol_quote", "n_trades", "delta_vol", "delta_quote",
              "taker_buy_vol", "taker_sell_vol"):
        if f"{c}_fp" in df.columns:
            df = df.drop(columns=[f"{c}_fp"])
    # ---- structure
    df["body_mid"] = (df["o"] + df["c"]) / 2
    df["ema20b"] = ema(df["body_mid"], 20)
    df["ema50b"] = ema(df["body_mid"], 50)
    tr = np.maximum(df["h"] - df["l"], np.maximum((df["h"] - df["c"].shift()).abs(),
                                                  (df["l"] - df["c"].shift()).abs()))
    df["atr"] = tr.rolling(14).mean()
    df["rng"] = (df["h"] - df["l"]).replace(0, np.nan)
    df["close_pos"] = ((df["c"] - df["l"]) / df["rng"]).clip(0, 1)
    df["vol_ma20"] = df["vol_base"].rolling(20).mean()
    df["is_low12"] = df["l"] <= df["l"].rolling(12).min() + 1e-12
    df["is_high12"] = df["h"] >= df["h"].rolling(12).max() - 1e-12
    # ---- regime flags
    df["trend_up_strict"] = (df["c"] > df["ema50b"]) & (df["ema20b"] > df["ema50b"])
    df["trend_up_loose"] = df["c"] > df["ema20b"]
    df["trend_dn_strict"] = (df["c"] < df["ema50b"]) & (df["ema20b"] < df["ema50b"])
    df["trend_dn_loose"] = df["c"] < df["ema20b"]
    # ---- pullback touch masks (two bands) over lookback L=3
    for band, tag in ((0.001, "b1"), (0.0025, "b2")):
        touch_l = (df["l"] <= df["ema20b"] * (1 + band)).rolling(3).max().fillna(0).astype(bool)
        touch_s = (df["h"] >= df["ema20b"] * (1 - band)).rolling(3).max().fillna(0).astype(bool)
        df[f"pb_low_{tag}"] = touch_l
        df[f"pb_high_{tag}"] = touch_s
    # ---- auction-complete flags (long side: at pullback lows)
    dq = df["delta_quote"]
    dp = df["delta_pct"]
    K = 12
    lower_bins = [f"fp{k}_b_vol" for k in range(4)]
    lower_s = [f"fp{k}_s_vol" for k in range(4)]
    upper_b = [f"fp{k}_b_vol" for k in range(8, K)]
    upper_s = [f"fp{k}_s_vol" for k in range(8, K)]
    imb_low = (df[lower_bins].sum(axis=1) / (df[lower_s].sum(axis=1) + 1e-9))
    imb_high = (df[upper_s].sum(axis=1) / (df[upper_b].sum(axis=1) + 1e-9))
    vol_climax = df["vol_base"] > 2.0 * df["vol_ma20"]
    strong_close = df["close_pos"] > 0.6
    weak_close = df["close_pos"] < 0.4
    poc_frac = df["fp_poc_bin"] / (K - 1)
    df["ac_abs_low"] = df["is_low12"] & (dp > 0.02)
    df["ac_dflip_low"] = (dq.shift(1) < 0) & (dq > 0) & (df["l"] <= df["l"].shift(1))
    df["ac_hvn_low"] = (poc_frac <= 0.30) & (df["c"] > df["o"]) & strong_close & (df["vol_base"] > 1.2 * df["vol_ma20"])
    df["ac_imb_low"] = (imb_low >= 1.8) & df["is_low12"]
    df["ac_climax_low"] = vol_climax & (df["c"] > df["o"]) & df["is_low12"]
    df["ac_any_low"] = df["ac_abs_low"] | df["ac_dflip_low"] | df["ac_hvn_low"] | df["ac_imb_low"] | df["ac_climax_low"]
    df["ac_abs_high"] = df["is_high12"] & (dp < -0.02)
    df["ac_dflip_high"] = (dq.shift(1) > 0) & (dq < 0) & (df["h"] >= df["h"].shift(1))
    df["ac_hvn_high"] = (poc_frac >= 0.70) & (df["c"] < df["o"]) & weak_close & (df["vol_base"] > 1.2 * df["vol_ma20"])
    df["ac_imb_high"] = (imb_high >= 1.8) & df["is_high12"]
    df["ac_climax_high"] = vol_climax & (df["c"] < df["o"]) & df["is_high12"]
    df["ac_any_high"] = df["ac_abs_high"] | df["ac_dflip_high"] | df["ac_hvn_high"] | df["ac_imb_high"] | df["ac_climax_high"]
    df["imb_low"] = imb_low.clip(0, 10)
    df["imb_high"] = imb_high.clip(0, 10)
    df["poc_frac"] = poc_frac
    df["run_min_q"] = df["delta_run_min_quote"]
    df["run_max_q"] = df["delta_run_max_quote"]
    # ---- v2 quality features: pullback depth + reclaim + strong-AC composite
    ext_up = ((df["h"] - df["ema20b"]) / df["atr"]).rolling(12).max()   # extension above EMA20
    ext_dn = ((df["ema20b"] - df["l"]) / df["atr"]).rolling(12).max()
    df["depth_up_ok"] = (ext_up >= 1.5).fillna(False)   # was trending AWAY from EMA20 before the dip
    df["depth_dn_ok"] = (ext_dn >= 1.5).fillna(False)
    df["reclaim_up"] = df["c"] > df["ema20b"]            # closed back above EMA20 (acceptance)
    df["reclaim_dn"] = df["c"] < df["ema20b"]
    df["ac_strong_low"] = df["ac_abs_low"] | df["ac_imb_low"] | df["ac_climax_low"]
    df["ac_strong_high"] = df["ac_abs_high"] | df["ac_imb_high"] | df["ac_climax_high"]
    df["atr_pct"] = df["atr"] / df["c"]
    df["touch_low5"] = (df["l"] <= df["ema20b"] * (1 + 0.0025)).rolling(5).max().fillna(0).astype(bool)
    df["touch_high5"] = (df["h"] >= df["ema20b"] * (1 - 0.0025)).rolling(5).max().fillna(0).astype(bool)
    # ---- higher-TF regime (1h body EMA from 1m)
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
    # ---- metrics context (OI, taker L/S)
    mm = sorted(glob.glob(f"{DATA}/metrics/{sym}/{sym}-metrics-*.parquet"))
    if mm:
        m = pd.concat([pd.read_parquet(p) for p in mm], ignore_index=True).sort_values("ts")
        m["oi_pct"] = m["sum_open_interest"].pct_change(6) * 100  # ~30min on 5-min rows
        m["oi_pct"] = m["oi_pct"].clip(-20, 20)
        m5m = m.set_index("ts")[["oi_pct", "sum_taker_long_short_vol_ratio"]] \
            .reindex(df["ts"], method="ffill")
        df["oi_chg_30m"] = m5m["oi_pct"].values
        df["taker_ls"] = m5m["sum_taker_long_short_vol_ratio"].fillna(1.0).values
    else:
        df["oi_chg_30m"] = 0.0
        df["taker_ls"] = 1.0
    df["symbol"] = sym
    keep = ["ts", "symbol", "o", "h", "l", "c", "body_mid", "ema20b", "ema50b", "atr", "rng",
            "close_pos", "vol_base", "vol_quote", "vol_ma20", "n_trades", "delta_pct", "delta_quote",
            "cvd_vol", "vwap", "trend_up_strict", "trend_up_loose", "trend_dn_strict", "trend_dn_loose",
            "pb_low_b1", "pb_low_b2", "pb_high_b1", "pb_high_b2",
            "ac_abs_low", "ac_dflip_low", "ac_hvn_low", "ac_imb_low", "ac_climax_low", "ac_any_low",
            "ac_abs_high", "ac_dflip_high", "ac_hvn_high", "ac_imb_high", "ac_climax_high", "ac_any_high",
            "imb_low", "imb_high", "poc_frac", "run_min_q", "run_max_q", "is_low12", "is_high12",
            "htf_up", "htf_dn", "oi_chg_30m", "taker_ls", "avg_trade_size",
            "depth_up_ok", "depth_dn_ok", "reclaim_up", "reclaim_dn",
            "ac_strong_low", "ac_strong_high", "atr_pct", "touch_low5", "touch_high5"]
    keep = [c for c in keep if c in df.columns]
    return df[keep]


def main():
    tf = sys.argv[1] if len(sys.argv) > 1 else "5m"
    os.makedirs(OUT, exist_ok=True)
    frames = []
    for sym in SYMBOLS:
        d = load_symbol(sym, tf)
        if d is not None and len(d) > 5000:
            frames.append(d)
            print(f"{sym}: {len(d):,} bars  {pd.Timestamp(d['ts'].iloc[0], unit='ms', tz='UTC')} → "
                  f"{pd.Timestamp(d['ts'].iloc[-1], unit='ms', tz='UTC')}")
    if not frames:
        print("NO DATA"); sys.exit(1)
    all_df = pd.concat(frames, ignore_index=True)
    all_df.to_parquet(f"{OUT}/research_{tf}.parquet", index=False)
    ac_cols = [c for c in all_df.columns if c.startswith("ac_")]
    print(f"\ncombined: {len(all_df):,} bars, {all_df['symbol'].nunique()} symbols -> {OUT}/research_{tf}.parquet")
    print("\nAuction-complete flag frequencies (per symbol avg %):")
    for c in ac_cols:
        print(f"  {c:16s} {all_df[c].mean()*100:5.2f}%")


if __name__ == "__main__":
    main()
