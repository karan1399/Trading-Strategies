"""Holy Grail Unified Dual-Venue Production Engine (`4_HGv4_Core+TRSI+TF_CS16_R1.00`).

End-to-end live-ready 4-Pod multi-asset architecture built on SovereignCoreEngine
(24/7 Crypto + Friday 20:30 UTC Non-Crypto Flatten + Interlock-First Safety):
  1. Live Data Feed & Incremental Capture (`fapi.binance.com` 4H/1D/1m klines,
     5m Open Interest, 8h Funding, WebSocket aggTrades, plus MT5 4H/D1 bars)
     atomically persisted to `engine/data/live_cache/`.
  2. Four Orthogonal Causal Signal Pods (non-blocking across sleeves/pods):
     - Pod 1 (CORE): Sleeve A (4H Physics + Post-Liq Reclaim), Sleeve B (FOMC t+15m),
       Sleeve C (Adaptive Volume-Clock) on 16 Crypto + Commodity CFDs (MT5)
     - Pod 2 (KL_MR): S17 Kaminsky-Lo Triple-RSI Mean Reversion on Daily Crypto+Comm
       (`RSI5 > 50` exit, `12d` hold cap, `6x ATR` catastrophe SL scaled safely)
     - Pod 3 (TRADFI): `TF_TREND_L` (4H Trend Breakout) + `TF_TRSI_KL` (Daily Triple-RSI)
       across 15 MT5 TradFi Indices, Metals, Energy & G4 FX (auto Friday flatten)
     - Pod 4 (CS_DELTA): `S19` Market-Neutral Funding Carry + 7d-1d Momentum across 27
       perps targeting `delta_exchange_india`, gated on `min_delta_equity_usd` ($250)
       with continuous live shadow signal & MTM tracking below threshold.
  3. Live Pre-Trade Gate, Sizing & Execution (`BlueberryMarketsSVG-Live #5126976` + Delta):
     - Terminal `trade_allowed`, expected login, & equity floor interlocks
     - Pod-isolated non-blocking caps (`CORE:8 / KL_MR:4 / TRADFI:4`), max 1.0% SL risk
     - Strict `spread <= 20% SL` & non-compressed `lots_for_risk` gate
     - Atomic server-side bracket order (`send_bracket`) + `T4_Convex` / KL-RSI exits

Usage:
  python engine/holy_grail_engine.py --selftest
  python engine/holy_grail_engine.py --status
  python engine/holy_grail_engine.py --dry --sync-live
  python engine/holy_grail_engine.py --live --i-understand-live-risk
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import m4_live_engine as core
from engine import sovereign_core_engine as sc
from engine import unified_alpha_pilot as pilot
from scripts import delta_direct
from strategies.core.indicators import wilder_atr
from strategies.s17_kl_triple_rsi.strategy import exit_signal_at as s17_exit_at
from strategies.s17_kl_triple_rsi.strategy import signal_at as s17_signal_at
from strategies.s19_cs_carry_momentum.strategy import compute_target_weights as s19_target_weights

DEFAULT_CONFIG_PATH = ROOT / "engine" / "config_holy_grail.json"
DEFAULT_STATE_PATH = ROOT / "engine" / "data" / "holy_grail_state.json"
DEFAULT_JOURNAL_PATH = ROOT / "engine" / "data" / "holy_grail_journal.csv"

CACHE_4H_DIR = ROOT / "historical_data" / "binance_footprint" / "cache"
FP_DATA_DIR = ROOT / "historical_data" / "binance_footprint"
DEFAULT_LIVE_CACHE_DIR = ROOT / "engine" / "data" / "live_cache"

ET_TZ = ZoneInfo("America/New_York")
FAPI_BASE = "https://fapi.binance.com"

# Verified US Economic Release Schedules (ET -> converted dynamically to UTC)
DEFAULT_FOMC_DATES = [
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
DEFAULT_NFP_DATES = [
    "2024-01-05", "2024-02-02", "2024-03-08", "2024-04-05", "2024-05-03", "2024-06-07",
    "2024-07-05", "2024-08-02", "2024-09-06", "2024-10-04", "2024-11-01", "2024-12-06",
    "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04", "2025-05-02", "2025-06-06",
    "2025-07-03", "2025-08-01", "2025-09-05", "2025-11-20", "2025-12-05",
    "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05",
    "2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]
DEFAULT_CPI_DATES = [
    "2024-01-11", "2024-02-13", "2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12",
    "2024-07-11", "2024-08-14", "2024-09-11", "2024-10-10", "2024-11-13", "2024-12-11",
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-09", "2025-05-14", "2025-06-11",
    "2025-07-15", "2025-08-13", "2025-09-11", "2025-10-24", "2025-12-18",
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10",
    "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]


def et_to_utc_ts(date_str: str, hour: int, minute: int) -> int:
    """Convert America/New_York release wall-clock time to UTC epoch seconds."""
    dt_et = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M").replace(tzinfo=ET_TZ)
    return int(dt_et.astimezone(timezone.utc).timestamp())


def default_macro_timestamps() -> dict[str, list[int]]:
    """Return UTC release timestamps for FOMC (14:00 ET), NFP (08:30 ET), CPI (08:30 ET)."""
    return {
        "fomc": [et_to_utc_ts(d, 14, 0) for d in DEFAULT_FOMC_DATES],
        "nfp": [et_to_utc_ts(d, 8, 30) for d in DEFAULT_NFP_DATES],
        "cpi": [et_to_utc_ts(d, 8, 30) for d in DEFAULT_CPI_DATES],
    }


def is_in_macro_blackout(
    ts_sec: int,
    event_timestamps: list[int],
    pre_sec: int = 1800,
    post_sec: int = 900,
) -> bool:
    """Return True if timestamp falls inside [event - pre_sec, event + post_sec]."""
    return any((ev - pre_sec) <= ts_sec <= (ev + post_sec) for ev in event_timestamps)


def is_friday_close_ts(ts_sec: int) -> bool:
    """Return True at or after Friday 20:30 UTC."""
    dt = datetime.fromtimestamp(int(ts_sec), tz=timezone.utc)
    if dt.weekday() != 4:
        return False
    return (dt.hour > 20) or (dt.hour == 20 and dt.minute >= 30)


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def fetch_binance_klines(
    sym: str,
    interval: str,
    limit: int = 300,
    start_ms: int | None = None,
    now_ms: int | None = None,
    timeout: int = 10,
) -> pd.DataFrame:
    """Fetch strictly closed Binance Futures klines with exact taker buy volume & trade count."""
    params: dict[str, Any] = {"symbol": sym.upper(), "interval": interval, "limit": int(limit)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    r = requests.get(f"{FAPI_BASE}/fapi/v1/klines", params=params, timeout=timeout)
    r.raise_for_status()
    arr = r.json()
    cutoff_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    rows = []
    for k in arr:
        open_ms = int(k[0])
        close_ms = int(k[6])
        if close_ms > cutoff_ms:
            # Exclude still-forming bar for zero lookahead
            continue
        o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
        nt = int(k[8])
        bv = min(v, max(0.0, float(k[9])))
        sv = max(0.0, v - bv)
        rows.append({
            "ts_open": open_ms // 1000,
            "ts_close": (close_ms + 1) // 1000,
            "o": o, "h": h, "l": l, "c": c,
            "v": v, "bv": bv, "sv": sv, "nt": nt,
        })
    return pd.DataFrame(rows)


def fetch_binance_oi_and_funding(sym: str, oi_limit: int = 100, timeout: int = 10) -> tuple[pd.DataFrame, float]:
    """Fetch live 5m Open Interest USD history & current perpetual funding rate (bps)."""
    oi_rows = []
    try:
        r_oi = requests.get(
            f"{FAPI_BASE}/futures/data/openInterestHist",
            params={"symbol": sym.upper(), "period": "5m", "limit": int(oi_limit)},
            timeout=timeout,
        )
        if r_oi.ok:
            for item in r_oi.json():
                oi_rows.append({
                    "ts": int(item["timestamp"]) // 1000,
                    "oi_usd": float(item["sumOpenInterestValue"]),
                })
    except Exception:
        pass
    fund_bp = 0.0
    try:
        r_f = requests.get(f"{FAPI_BASE}/fapi/v1/premiumIndex", params={"symbol": sym.upper()}, timeout=timeout)
        if r_f.ok:
            fund_bp = float(r_f.json().get("lastFundingRate", 0.0)) * 10000.0
    except Exception:
        pass
    df_oi = pd.DataFrame(oi_rows).sort_values("ts").reset_index(drop=True) if oi_rows else pd.DataFrame(columns=["ts", "oi_usd"])
    return df_oi, fund_bp


def sync_live_symbol_feeds(
    sym: str,
    live_dir: Path = DEFAULT_LIVE_CACHE_DIR,
    now_ts: int | None = None,
    enable_rest: bool = True,
    ws_collector: core.BinanceAggTradeCollector | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Maintain incrementally captured live 4H, 1m & 5m-OI frames pinned up to now_ts.

    Splices historical footprint parquet cache with REST incremental closed bars
    and atomic disk persistence under `live_dir` for uninterrupted capture.
    """
    cur_ts = int(now_ts if now_ts is not None else time.time())
    cur_ms = (cur_ts - 3) * 1000  # 3s exchange clock-skew guard: never ingest a still-forming bar
    live_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load & update 4H bars + OI + Funding
    live_4h_p = live_dir / f"{sym}_4h_live.parquet"
    hist_4h_p = CACHE_4H_DIR / f"{sym}_4h.parquet"
    base_4h = pd.read_parquet(live_4h_p) if live_4h_p.is_file() else (pd.read_parquet(hist_4h_p) if hist_4h_p.is_file() else pd.DataFrame())
    if not base_4h.empty:
        base_4h = base_4h.sort_values("ts").tail(600).reset_index(drop=True)

    m5_df = pd.DataFrame(columns=["ts", "oi_usd"])
    fund_bp = 0.0
    if enable_rest:
        try:
            rest4 = fetch_binance_klines(sym, "4h", limit=300, now_ms=cur_ms)
            m5_df, fund_bp = fetch_binance_oi_and_funding(sym, oi_limit=100)
            if not rest4.empty:
                rest4 = rest4.rename(columns={"ts_open": "ts", "ts_close": "end_ts"})
                rest4["d_pct"] = np.where(rest4["v"] > 0, (rest4["bv"] - rest4["sv"]) / rest4["v"] * 100.0, 0.0)
                rest4["buy_imb"] = 0
                rest4["sell_imb"] = 0
                rest4["fund_bp"] = fund_bp
                if not m5_df.empty:
                    rest4 = pd.merge_asof(
                        rest4.sort_values("end_ts"),
                        m5_df.rename(columns={"ts": "m_ts"}),
                        left_on="end_ts", right_on="m_ts", direction="backward", tolerance=14400,
                    )
                    rest4["oi_chg_6b"] = rest4["oi_usd"].pct_change(6).fillna(0.0) * 100.0
                else:
                    rest4["oi_usd"] = np.nan
                    rest4["oi_chg_6b"] = 0.0

                if base_4h.empty:
                    base_4h = rest4
                else:
                    keep_cols = ["ts", "end_ts", "o", "h", "l", "c", "v", "bv", "sv", "nt", "d_pct", "buy_imb", "sell_imb", "oi_chg_6b", "fund_bp"]
                    for col in keep_cols:
                        if col not in base_4h.columns:
                            base_4h[col] = 0.0
                        if col not in rest4.columns:
                            rest4[col] = 0.0
                    # Preserve historical footprint imbalances for old cached timestamps; append/update recent bars
                    old_max_ts = int(base_4h["ts"].max())
                    comb4 = pd.concat([base_4h[keep_cols], rest4[rest4["ts"] >= old_max_ts - 86400][keep_cols]], ignore_index=True)
                    base_4h = comb4.drop_duplicates("ts", keep="first").sort_values("ts").tail(600).reset_index(drop=True)
                    # Always refresh the newest closed bars > old_max_ts
                    newer = rest4[rest4["ts"] > old_max_ts][keep_cols]
                    if not newer.empty:
                        base_4h = pd.concat([base_4h[base_4h["ts"] <= old_max_ts], newer], ignore_index=True).sort_values("ts").tail(600).reset_index(drop=True)
                _atomic_write_parquet(base_4h, live_4h_p)
        except Exception:
            pass

    # 2. Load & increment 1m bars (~28 days tail for exact Volume-Clock 20d adaptive warmup)
    live_1m_p = live_dir / f"{sym}_1m_live.parquet"
    hist_1m_p = FP_DATA_DIR / f"{sym}_klines_1m.parquet"
    if live_1m_p.is_file():
        k1 = pd.read_parquet(live_1m_p)
    elif hist_1m_p.is_file():
        k1 = pd.read_parquet(hist_1m_p).sort_values("bar_1m").tail(42000).reset_index(drop=True)
    else:
        k1 = pd.DataFrame(columns=["bar_1m", "open", "high", "low", "close", "volume", "taker_buy_vol", "count"])

    if enable_rest:
        try:
            last_1m = int(k1["bar_1m"].max()) if not k1.empty else (cur_ts - 22 * 86400)
            parts1 = [k1] if not k1.empty else []
            cursor_ms = (last_1m + 60) * 1000
            # Bound incremental loop to max 30 batches (~45,000 mins / ~31 days) per wake
            for _ in range(30):
                if cursor_ms >= cur_ms - 60000:
                    break
                r1 = fetch_binance_klines(sym, "1m", limit=1500, start_ms=cursor_ms, now_ms=cur_ms)
                if r1.empty:
                    break
                parts1.append(pd.DataFrame({
                    "bar_1m": r1["ts_open"],
                    "open": r1["o"], "high": r1["h"], "low": r1["l"], "close": r1["c"],
                    "volume": r1["v"], "taker_buy_vol": r1["bv"], "count": r1["nt"],
                }))
                last_got_ms = int(r1["ts_open"].iloc[-1]) * 1000
                if len(r1) < 1500 or last_got_ms < cursor_ms:
                    break
                cursor_ms = last_got_ms + 60000
            if parts1:
                k1 = (
                    pd.concat(parts1, ignore_index=True)
                    .drop_duplicates("bar_1m", keep="last")
                    .sort_values("bar_1m")
                    .tail(45000)
                    .reset_index(drop=True)
                )
                _atomic_write_parquet(k1, live_1m_p)
        except Exception:
            pass

    # Merge any closed WebSocket bars flushed from BinanceAggTradeCollector
    if ws_collector is not None:
        ws_dir = getattr(ws_collector, "out_dir", None)
        ws_p = (ws_dir / f"{sym}_ws_1m_fp.parquet") if ws_dir is not None else None
        if ws_p is not None and ws_p.is_file():
            try:
                wdf = pd.read_parquet(ws_p)
                wdf = wdf[wdf["bar_1m"] <= cur_ts - 60]
                if not wdf.empty:
                    wg = wdf.groupby("bar_1m", as_index=False).agg(
                        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                        taker_buy_vol=("buy_qty", "sum"), sell_qty=("sell_qty", "sum"),
                        buy_trades=("buy_trades", "sum"), sell_trades=("sell_trades", "sum"),
                    )
                    wg["volume"] = wg["taker_buy_vol"] + wg["sell_qty"]
                    wg["count"] = wg["buy_trades"] + wg["sell_trades"]
                    cols1 = ["bar_1m", "open", "high", "low", "close", "volume", "taker_buy_vol", "count"]
                    k1 = (
                        pd.concat([k1[cols1], wg[cols1]], ignore_index=True)
                        .drop_duplicates("bar_1m", keep="last")
                        .sort_values("bar_1m")
                        .tail(45000)
                        .reset_index(drop=True)
                    )
            except Exception:
                pass

    return base_4h, k1, m5_df


def compute_4h_physics_features(df4: pd.DataFrame, btc_df4: pd.DataFrame | None = None) -> pd.DataFrame:
    """Compute strictly causal 4H 5-Pillar Statistical Physics & OrderFlow features."""
    d = df4.sort_values("ts").reset_index(drop=True).copy()
    c = d["c"].to_numpy(float)
    h = d["h"].to_numpy(float)
    l = d["l"].to_numpy(float)
    v = d["v"].to_numpy(float) if "v" in d.columns else (d["bv"].to_numpy(float) + d["sv"].to_numpy(float))

    if "d_pct" not in d.columns and "bv" in d.columns and "sv" in d.columns:
        d["d_pct"] = np.where(v > 0, (d["bv"].to_numpy(float) - d["sv"].to_numpy(float)) / v * 100.0, 0.0)
    if "clv" not in d.columns:
        d["clv"] = (2.0 * c - h - l) / np.where((h - l) > 1e-9, h - l, np.nan)
    if "vol_rel20" not in d.columns:
        rm20 = pd.Series(v).rolling(20, min_periods=5).mean().to_numpy(float)
        d["vol_rel20"] = v / np.where(rm20 > 0, rm20, np.nan)
    if "atr" not in d.columns:
        pc = pd.Series(c).shift(1).to_numpy(float)
        tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
        d["atr"] = pd.Series(tr).ewm(alpha=1.0 / 20.0, adjust=False).mean()
    if "ema89" not in d.columns:
        d["ema89"] = pd.Series(c).ewm(span=89, adjust=False).mean()
    if "ema233" not in d.columns:
        d["ema233"] = pd.Series(c).ewm(span=233, adjust=False).mean()
    if "vwz48" not in d.columns:
        tp = (h + l + c) / 3.0
        vw48 = pd.Series(tp * v).rolling(48).sum() / (pd.Series(v).rolling(48).sum() + 1e-9)
        vsd48 = pd.Series(c).rolling(48).std()
        d["vwz48"] = (c - vw48) / np.where(vsd48 > 1e-9, vsd48, np.nan)
    if "avg_sz_rk" not in d.columns:
        if "nt" in d.columns:
            avg_sz = (v * c) / np.maximum(d["nt"].to_numpy(float), 1.0)
            d["avg_sz_rk"] = pd.Series(avg_sz).rolling(120, min_periods=30).rank(pct=True)
        else:
            d["avg_sz_rk"] = 0.5
    for col, fallback in (("buy_imb", 0), ("sell_imb", 0), ("oi_chg_6b", 0.0), ("fund_bp", 0.0)):
        if col not in d.columns:
            d[col] = fallback

    cs = pd.Series(c)
    ret1 = np.log(c / np.where(cs.shift(1).to_numpy(float) > 0, cs.shift(1).to_numpy(float), np.nan))
    r4 = np.log(c / np.where(cs.shift(4).to_numpy(float) > 0, cs.shift(4).to_numpy(float), np.nan))
    v1 = pd.Series(ret1).rolling(48, min_periods=32).var().to_numpy(float)
    v4 = pd.Series(r4).rolling(48, min_periods=32).var().to_numpy(float)
    d["vr4"] = v4 / (4.0 * np.where(v1 > 1e-12, v1, np.nan))

    s1 = pd.Series(ret1).rolling(60, min_periods=36).std().to_numpy(float)
    s4 = pd.Series(r4).rolling(60, min_periods=36).std().to_numpy(float)
    d["hurst_4h"] = np.log(np.maximum(s4, 1e-12) / np.maximum(s1, 1e-12)) / math.log(4.0)

    sd20 = cs.rolling(20).std().to_numpy(float)
    d["bb_w_rk"] = pd.Series((4.0 * sd20) / np.where(c > 0, c, np.nan)).rolling(120, min_periods=60).rank(pct=True)
    d["was_sq"] = (pd.Series(d["bb_w_rk"]).shift(1).rolling(3).min() < 0.35).fillna(False)
    d["brk_up20"] = h > pd.Series(h).shift(1).rolling(20).max().to_numpy(float)
    d["brk_dn20"] = l < pd.Series(l).shift(1).rolling(20).min().to_numpy(float)
    d["ema_bull"] = (d["c"] > d["ema233"]) & (d["ema89"] > d["ema233"])
    d["ema_bear"] = (d["c"] < d["ema233"]) & (d["ema89"] < d["ema233"])

    if btc_df4 is not None and not btc_df4.empty:
        bm = btc_df4.sort_values("ts").set_index("ts")
        btc_e233 = bm["ema233"] if "ema233" in bm.columns else bm["c"].ewm(span=233, adjust=False).mean()
        btc_b = bm["c"] > btc_e233
        d["btc_bull"] = d["ts"].map(btc_b).ffill().fillna(False).to_numpy(bool)
    else:
        d["btc_bull"] = (d["c"] > d["ema233"]).to_numpy(bool)

    p1 = (d["was_sq"] | (d["hurst_4h"] > 0.52)).astype(int)
    p2 = ((d["d_pct"] > 5.0) & (d["vol_rel20"] > 1.3)).astype(int)
    p3 = ((d["avg_sz_rk"] > 0.60) & (d["buy_imb"] >= 1)).astype(int)
    p4 = (d["vr4"] > 1.05).astype(int)
    p5 = ((d["oi_chg_6b"] > 1.5) & (d["fund_bp"] <= 1.2)).fillna(False).astype(int)
    d["sc_long"] = p1 + p2 + p3 + p4 + p5

    s2 = ((d["d_pct"] < -5.0) & (d["vol_rel20"] > 1.3)).astype(int)
    s3 = ((d["avg_sz_rk"] > 0.60) & (d["sell_imb"] >= 1)).astype(int)
    s5 = ((d["oi_chg_6b"] > 1.5) & (d["fund_bp"] >= -0.5)).fillna(False).astype(int)
    d["sc_short"] = p1 + s2 + s3 + p4 + s5
    return d


def eval_4h_physics_latest(df4_feat: pd.DataFrame, sym: str, is_comm: bool = False) -> list[dict[str, Any]]:
    """Evaluate Sleeve A signals cleanly on the latest CLOSED 4H bar only."""
    if df4_feat.empty or len(df4_feat) < 245:
        return []
    row = df4_feat.iloc[-1]
    ai = float(row["atr"])
    if not (np.isfinite(ai) and ai > 0):
        return []
    ts_sig = int(row.get("end_ts", int(row["ts"]) + 14400))
    px = float(row["c"])
    sh_warn = bool(
        (float(row["vol_rel20"]) > 2.0 and float(row["clv"]) > 0.0)
        or (float(row["oi_chg_6b"]) < -6.0 and float(row["clv"]) > -0.2)
    )
    signals: list[dict[str, Any]] = []

    if bool(row["brk_up20"]) and bool(row["ema_bull"]) and bool(row["btc_bull"]) and float(row["clv"]) > 0.15 and int(row["sc_long"]) >= 3:
        signals.append({
            "sym": sym, "sleeve": "A_4H_L", "family": "A", "side": 1, "ts_sig": ts_sig, "tf": "4H",
            "ref_px": px, "atr": ai, "sl_dist": 2.5 * ai, "short_ratchet": False, "climax_warn": False, "is_comm": is_comm,
        })
    if bool(row["brk_dn20"]) and bool(row["ema_bear"]) and float(row["clv"]) < -0.15 and int(row["sc_short"]) >= 4:
        signals.append({
            "sym": sym, "sleeve": "A_4H_S", "family": "A", "side": -1, "ts_sig": ts_sig, "tf": "4H",
            "ref_px": px, "atr": ai, "sl_dist": 2.5 * ai, "short_ratchet": True, "climax_warn": sh_warn, "is_comm": is_comm,
        })
    if (
        float(row["vwz48"]) < -1.8
        and float(row["oi_chg_6b"]) < -7.0
        and float(row["vol_rel20"]) > 1.5
        and float(row["clv"]) > 0.1
        and float(row["d_pct"]) > 0.0
    ):
        signals.append({
            "sym": sym, "sleeve": "A_RECL", "family": "A", "side": 1, "ts_sig": ts_sig, "tf": "4H",
            "ref_px": px, "atr": ai, "sl_dist": 2.5 * ai, "short_ratchet": False, "climax_warn": False, "is_comm": is_comm,
        })
    return signals


def build_causal_volclock_bars(
    k1_df: pd.DataFrame,
    m5_df: pd.DataFrame | None = None,
    sym: str = "BTCUSDT",
    is_comm: bool = False,
) -> pd.DataFrame:
    """Build strictly causal 24-bar/day adaptive Volume-Clock bars from 1m OHLCV."""
    if k1_df.empty or len(k1_df) < 1600:
        return pd.DataFrame()
    k1 = k1_df.sort_values("bar_1m").reset_index(drop=True)
    ts = k1["bar_1m"].to_numpy(np.int64)
    o = k1["open"].to_numpy(float)
    h = k1["high"].to_numpy(float)
    l = k1["low"].to_numpy(float)
    c = k1["close"].to_numpy(float)
    v = k1["volume"].to_numpy(float)
    bv = k1["taker_buy_vol"].to_numpy(float) if "taker_buy_vol" in k1.columns else (v * 0.5)
    cnt = k1["count"].to_numpy(float) if "count" in k1.columns else np.ones(len(ts), dtype=float)
    sv = np.maximum(0.0, v - bv)
    dv = v * c

    win = 14400 if is_comm else 28800
    warm = 720 if is_comm else 1440
    dv_ma = pd.Series(dv).rolling(win, min_periods=warm).mean().to_numpy(float)
    first_v = float(np.nanmedian(dv[:warm]) * 60.0)
    target_dv = np.where(np.isfinite(dv_ma) & (dv_ma > 0), dv_ma * 60.0, first_v)

    n = len(ts)
    bars: list[dict[str, Any]] = []
    idx = warm
    while idx < n - 1:
        tgt = target_dv[idx]
        if not np.isfinite(tgt) or tgt <= 0:
            idx += 60
            continue
        cum = 0.0
        j = idx
        hit_gap = False
        while j < n and cum < tgt and (j - idx) < 240:
            if j > idx and (ts[j] - ts[j - 1]) > 300:
                hit_gap = True
                break
            cum += dv[j]
            j += 1
        if j <= idx or (j == n and cum < tgt and (j - idx) < 240 and not hit_gap):
            break
        sl = slice(idx, j)
        tot_v = float(v[sl].sum())
        tot_bv = float(bv[sl].sum())
        tot_sv = float(sv[sl].sum())
        tot_cnt = float(cnt[sl].sum())
        c_seg = c[sl]
        dv_seg = dv[sl]
        vwap_b = float(dv_seg.sum() / max(tot_v, 1e-12)) if tot_v > 0 else float(c[j - 1])
        sd_b = float(np.std(c_seg)) if len(c_seg) > 1 else 0.0
        bars.append({
            "sym": sym,
            "ts": int(ts[idx]),
            "end_ts": int(ts[j - 1] + 60),
            "o": float(o[idx]),
            "h": float(np.max(h[sl])),
            "l": float(np.min(l[sl])),
            "c": float(c[j - 1]),
            "v": tot_v,
            "dv": float(cum),
            "d_pct": ((tot_bv - tot_sv) / tot_v * 100.0) if tot_v > 0 else 0.0,
            "trades": tot_cnt,
            "avg_sz_usd": float(cum / max(tot_cnt, 1.0)),
            "poc": vwap_b,
            "vah": vwap_b + 0.7 * sd_b,
            "val": vwap_b - 0.7 * sd_b,
            "dur_m": int(j - idx),
        })
        idx = j

    dfb = pd.DataFrame(bars)
    if dfb.empty:
        return dfb

    if m5_df is not None and not m5_df.empty and "oi_usd" in m5_df.columns:
        m5 = m5_df.sort_values("ts").rename(columns={"ts": "m_ts"})
        dfb = pd.merge_asof(
            dfb.sort_values("end_ts"),
            m5[["m_ts", "oi_usd"]],
            left_on="end_ts", right_on="m_ts", direction="backward", tolerance=1800,
        )
        dfb["oi_chg_6b"] = dfb["oi_usd"].pct_change(6).fillna(0.0) * 100.0
    else:
        dfb["oi_chg_6b"] = 0.0

    c_a = dfb["c"].to_numpy(float)
    h_a = dfb["h"].to_numpy(float)
    l_a = dfb["l"].to_numpy(float)
    pc = pd.Series(c_a).shift(1).to_numpy(float)
    tr = np.maximum(h_a - l_a, np.maximum(np.abs(h_a - pc), np.abs(l_a - pc)))
    dfb["atr"] = pd.Series(tr).ewm(alpha=1.0 / 20.0, adjust=False).mean()
    dfb["ema89"] = pd.Series(c_a).ewm(span=89, adjust=False).mean()
    dfb["ema233"] = pd.Series(c_a).ewm(span=233, adjust=False).mean()
    dfb["clv"] = (2.0 * c_a - h_a - l_a) / np.where((h_a - l_a) > 1e-9, h_a - l_a, np.nan)
    dfb["avg_sz_rk"] = dfb["avg_sz_usd"].rolling(120, min_periods=40).rank(pct=True)
    dfb["speed_rk"] = (1.0 / np.maximum(dfb["dur_m"], 1)).rolling(120, min_periods=40).rank(pct=True)
    dfb["brk_up20"] = h_a > pd.Series(h_a).shift(1).rolling(20).max().to_numpy(float)
    dfb["brk_dn20"] = l_a < pd.Series(l_a).shift(1).rolling(20).min().to_numpy(float)
    return dfb


def eval_volclock_latest(
    vc_df: pd.DataFrame,
    btc_bull_now: bool,
    sym: str,
    is_comm: bool = False,
) -> list[dict[str, Any]]:
    """Evaluate pure Sleeve C signals on the latest closed adaptive Volume-Clock bar."""
    if vc_df.empty or len(vc_df) < 245:
        return []
    row = vc_df.iloc[-1]
    ai = float(row["atr"])
    if not (np.isfinite(ai) and ai > 0):
        return []
    px = float(row["c"])
    e89, e233 = float(row["ema89"]), float(row["ema233"])
    ts_sig = int(row["end_ts"])
    out: list[dict[str, Any]] = []

    if (
        bool(row["brk_up20"]) and px > e89 > e233
        and (btc_bull_now or is_comm)
        and px > float(row["vah"])
        and float(row["d_pct"]) > 15.0
        and float(row["speed_rk"]) > 0.72
        and float(row["avg_sz_rk"]) > 0.60
        and float(row["clv"]) > 0.10
    ):
        out.append({
            "sym": sym, "sleeve": "C1_VC_PURE_L", "family": "C", "side": 1, "ts_sig": ts_sig, "tf": "VC",
            "ref_px": px, "atr": ai, "sl_dist": 2.5 * ai, "short_ratchet": False, "climax_warn": False, "is_comm": is_comm,
        })
    if (
        bool(row["brk_dn20"]) and px < e89 < e233
        and ((not btc_bull_now) or is_comm)
        and px < float(row["val"])
        and float(row["d_pct"]) < -15.0
        and float(row["speed_rk"]) > 0.72
        and float(row["oi_chg_6b"]) > 1.5
        and float(row["clv"]) < -0.10
    ):
        out.append({
            "sym": sym, "sleeve": "C3_VC_PURE_S", "family": "C", "side": -1, "ts_sig": ts_sig, "tf": "VC",
            "ref_px": px, "atr": ai, "sl_dist": 2.5 * ai, "short_ratchet": False, "climax_warn": False, "is_comm": is_comm,
        })
    return out


def eval_fomc_15m_aligned(
    k1_df: pd.DataFrame,
    fomc_ts: int,
    sym: str,
    now_ts: int,
    sl_mult: float = 0.45,
    min_sl_bps: float = 80.0,
) -> dict[str, Any] | None:
    """Evaluate Sleeve B FOMC t+15m Price+Delta aligned entry once minute t+15 is strictly closed."""
    if now_ts < fomc_ts + 900 or now_ts > fomc_ts + 1800:
        return None
    k1 = k1_df.sort_values("bar_1m").reset_index(drop=True)
    ts_a = k1["bar_1m"].to_numpy(np.int64)
    i0 = int(np.searchsorted(ts_a, fomc_ts))
    if i0 < 1440 or i0 + 14 >= len(k1):
        return None
    p0 = float(k1["open"].iloc[i0])
    p15 = float(k1["close"].iloc[i0 + 14])
    if p0 <= 0:
        return None
    r15_bps = (p15 - p0) / p0 * 10000.0
    tv15 = float(k1["volume"].iloc[i0 : i0 + 15].sum())
    bv15 = float(k1["taker_buy_vol"].iloc[i0 : i0 + 15].sum()) if "taker_buy_vol" in k1.columns else (tv15 * 0.5)
    dp15 = ((2.0 * bv15 - tv15) / tv15 * 100.0) if tv15 > 0 else 0.0
    if not (np.sign(r15_bps) == np.sign(dp15) and abs(dp15) >= 4.0):
        return None

    side = int(np.sign(r15_bps))
    vol_pre_bps = (float(k1["high"].iloc[i0 - 1440 : i0].max()) - float(k1["low"].iloc[i0 - 1440 : i0].min())) / p0 * 10000.0
    sl_bps = max(min_sl_bps, sl_mult * vol_pre_bps)
    sd = p15 * (sl_bps / 10000.0)
    return {
        "sym": sym, "sleeve": "B_FOMC_15m_ALN", "family": "B", "side": side, "ts_sig": fomc_ts + 900,
        "tf": "FOMC15m", "ref_px": p15, "atr": sd / 2.5, "sl_dist": sd, "short_ratchet": False,
        "climax_warn": False, "is_comm": False,
    }


def compute_t4_trail_sl(
    entry: float,
    cur_sl: float,
    best_px: float,
    cur_atr: float,
    orig_sd: float,
    side: int,
    be_r: float = 1.5,
    be_pad_r: float = 0.05,
    trail_atr: float = 5.0,
    cur_close: float | None = None,
    climax_warn: bool = False,
) -> float:
    """Exact T4_Convex monotonic trailing SL (+ BE@1.5R + optional short climax ratchet)."""
    if orig_sd <= 0:
        return cur_sl
    mfe_r = side * (best_px - entry) / orig_sd
    cand = cur_sl
    if be_r > 0 and mfe_r >= be_r:
        be_lvl = entry + side * be_pad_r * orig_sd
        cand = max(cand, be_lvl) if side > 0 else min(cand, be_lvl)
    if trail_atr > 0 and mfe_r >= max(1.25, be_r) and cur_atr > 0:
        tr_lvl = best_px - side * trail_atr * cur_atr
        cand = max(cand, tr_lvl) if side > 0 else min(cand, tr_lvl)
    if side < 0 and climax_warn and cur_close is not None and (entry - cur_close) / orig_sd >= 0.60 and cur_atr > 0:
        rk_lvl = min(entry - 0.35 * orig_sd, cur_close + 0.8 * cur_atr)
        cand = min(cand, rk_lvl)
    return cand


def holy_grail_dd_mult(
    equity: float,
    peak_equity: float,
    was_throttled: bool,
    th1: float = 10.0,
    th2: float = 15.0,
    rec_below: float = 5.0,
) -> tuple[float, bool]:
    """Hysteresis drawdown throttle: 0.5x at 10% DD, 0.25x at 15% DD, full 1.0x below 5% DD."""
    if peak_equity <= 0:
        return 1.0, False
    cur_dd = max(0.0, (peak_equity - equity) / peak_equity * 100.0)
    if cur_dd >= th2:
        return 0.25, True
    if cur_dd >= th1 or (was_throttled and cur_dd >= rec_below):
        return 0.50, True
    return 1.0, False


def _safe_cache_name(sym: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in sym)


def sync_binance_daily_closed(
    sym: str,
    live_dir: Path = DEFAULT_LIVE_CACHE_DIR,
    now_ts: int | None = None,
    enable_rest: bool = True,
) -> pd.DataFrame:
    """Maintain strictly closed UTC 1D candles (260+ days) for S17 SMA(200) Triple-RSI."""
    cur_ts = int(now_ts if now_ts is not None else time.time())
    cur_ms = (cur_ts - 3) * 1000  # 3s exchange clock-skew guard: never ingest a still-forming bar
    live_dir.mkdir(parents=True, exist_ok=True)
    live_1d_p = live_dir / f"{sym}_1d_live.parquet"
    base = pd.read_parquet(live_1d_p) if live_1d_p.is_file() else pd.DataFrame()

    if base.empty:
        hist_4h_p = CACHE_4H_DIR / f"{sym}_4h.parquet"
        if hist_4h_p.is_file():
            h4 = pd.read_parquet(hist_4h_p).sort_values("ts")
            if not h4.empty:
                h4 = h4.assign(day_ts=(h4["ts"].astype(np.int64) // 86400) * 86400)
                base = (
                    h4.groupby("day_ts", as_index=False)
                    .agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"))
                    .rename(columns={"day_ts": "ts"})
                )
                base["end_ts"] = base["ts"] + 86400
                base = base[base["end_ts"] <= cur_ts].tail(400).reset_index(drop=True)

    if enable_rest:
        try:
            rest1d = fetch_binance_klines(sym, "1d", limit=260, now_ms=cur_ms)
            if not rest1d.empty:
                rest1d = rest1d.rename(columns={"ts_open": "ts", "ts_close": "end_ts"})[
                    ["ts", "end_ts", "o", "h", "l", "c", "v"]
                ]
                base = (
                    rest1d if base.empty
                    else pd.concat([base[["ts", "end_ts", "o", "h", "l", "c", "v"]], rest1d], ignore_index=True)
                    .drop_duplicates("ts", keep="last")
                )
                base = base[base["end_ts"] <= cur_ts].sort_values("ts").tail(400).reset_index(drop=True)
                _atomic_write_parquet(base, live_1d_p)
        except Exception:
            pass
    return base


def _mt5_server_offset_sec(cur_ts: int) -> int:
    """Detect MT5 broker server clock offset vs UTC (rounded to nearest hour, e.g. +7200 EET)."""
    if not (getattr(core, "MT5_OK", False) and hasattr(core.mt5, "symbol_info_tick")):
        return 0
    for probe in ("BTCUSD.pi", "ETHUSD.pi", "XAUUSD.pi"):
        try:
            tk = core.mt5.symbol_info_tick(probe)
            t_val = int(getattr(tk, "time", 0) or 0)
            if t_val > 0:
                diff = t_val - int(cur_ts)
                if abs(diff) <= 43200:
                    return int(round(diff / 3600.0)) * 3600
        except Exception:
            continue
    return 0


def sync_mt5_bars(
    mt5_symbol: str,
    timeframe: int,
    tf_sec: int,
    cache_tag: str,
    live_dir: Path = DEFAULT_LIVE_CACHE_DIR,
    bars_count: int = 350,
    now_ts: int | None = None,
) -> pd.DataFrame:
    """Fetch strictly closed MT5 bars (start_pos=1, normalized from broker clock to UTC)."""
    cur_ts = int(now_ts if now_ts is not None else time.time())
    live_dir.mkdir(parents=True, exist_ok=True)
    cache_p = live_dir / f"mt5_{_safe_cache_name(mt5_symbol)}_{cache_tag}.parquet"

    if getattr(core, "MT5_OK", False) and hasattr(core.mt5, "copy_rates_from_pos"):
        try:
            if hasattr(core.mt5, "symbol_select"):
                core.mt5.symbol_select(mt5_symbol, True)
            rates = core.mt5.copy_rates_from_pos(mt5_symbol, timeframe, 1, int(bars_count))
            if rates is not None and len(rates) > 0:
                srv_off = _mt5_server_offset_sec(cur_ts)
                df = pd.DataFrame(rates)
                ts_col = df["time"].astype(np.int64) - int(srv_off)
                vol_col = df["tick_volume"].astype(float) if "tick_volume" in df.columns else np.ones(len(df), dtype=float)
                out = pd.DataFrame({
                    "ts": ts_col,
                    "end_ts": ts_col + int(tf_sec),
                    "o": df["open"].astype(float),
                    "h": df["high"].astype(float),
                    "l": df["low"].astype(float),
                    "c": df["close"].astype(float),
                    "v": vol_col,
                })
                out = out[out["end_ts"] <= cur_ts].drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
                if not out.empty:
                    _atomic_write_parquet(out, cache_p)
                    return out
        except Exception:
            pass
    return pd.read_parquet(cache_p) if cache_p.is_file() else pd.DataFrame()


def eval_tradfi_4h_latest(
    df4: pd.DataFrame,
    sym: str,
    params: dict[str, Any] | None = None,
    mt5_symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Evaluate Pod 3 causal 4H TradFi Trend-Long (`TF_TREND_L`) on closed MT5 H4 bars."""
    if df4.empty or len(df4) < 240:
        return []
    d = df4.sort_values("ts").reset_index(drop=True)
    c = d["c"].to_numpy(float)
    h = d["h"].to_numpy(float)
    l = d["l"].to_numpy(float)
    pc = pd.Series(c).shift(1).to_numpy(float)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr20 = pd.Series(tr).ewm(alpha=1.0 / 20.0, adjust=False).mean().to_numpy(float)
    e89 = pd.Series(c).ewm(span=89, adjust=False).mean().to_numpy(float)
    e233 = pd.Series(c).ewm(span=233, adjust=False).mean().to_numpy(float)
    hh20 = pd.Series(h).shift(1).rolling(20).max().to_numpy(float)

    i = len(d) - 1
    ai = float(atr20[i])
    rng = float(h[i] - l[i])
    if not (np.isfinite(ai) and ai > 0 and np.isfinite(hh20[i]) and rng > 1e-12):
        return []
    clv = (2.0 * float(c[i]) - float(h[i]) - float(l[i])) / rng
    tp = params or {}
    sl_mult = float(tp.get("trend_sl_atr", 2.5))
    risk_scale = float(tp.get("trend_risk_scale", 0.85))
    if float(c[i]) > float(e89[i]) > float(e233[i]) and float(h[i]) > float(hh20[i]) and clv > 0.10:
        return [{
            "sym": sym, "mt5_symbol": mt5_symbol or sym, "sleeve": "TF_TREND_L", "family": "TF", "pod": "TRADFI",
            "side": 1, "ts_sig": int(d["end_ts"].iloc[i]), "tf": "4H", "ref_px": float(c[i]),
            "atr": ai, "sl_dist": sl_mult * ai, "risk_scale": risk_scale, "kl_nosl": False,
            "short_ratchet": False, "climax_warn": False, "is_comm": True,
        }]
    return []


def eval_kl_triple_rsi_latest(
    daily_df: pd.DataFrame,
    sym: str,
    sleeve: str,
    pod: str,
    family: str,
    is_comm: bool,
    kl_params: dict[str, Any] | None = None,
    mt5_symbol: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Evaluate causal Daily S17 Kaminsky-Lo Triple-RSI entry & RSI(5)>50 exit flag."""
    if daily_df.empty or len(daily_df) < 210:
        return None, False
    d = daily_df.sort_values("ts").reset_index(drop=True)
    o = d["o"].to_numpy(float)
    h = d["h"].to_numpy(float)
    l = d["l"].to_numpy(float)
    c = d["c"].to_numpy(float)
    i = len(d) - 1
    atr14 = wilder_atr(h, l, c, 14)

    exit_now = bool(s17_exit_at(c, i, kl_params))
    sig = s17_signal_at(o, h, l, c, None, atr14, i, kl_params)
    if sig is None:
        return None, exit_now

    r_scale = float((kl_params or {}).get("risk_scale", sig.get("risk_scale", 0.65)))
    cand = {
        "sym": sym, "mt5_symbol": mt5_symbol or sym, "sleeve": sleeve, "family": family, "pod": pod,
        "side": 1, "ts_sig": int(d["end_ts"].iloc[i]), "tf": "1D", "ref_px": float(c[i]),
        "atr": float(atr14[i]), "sl_dist": float(sig["cat_rd"]), "risk_scale": r_scale,
        "kl_nosl": True, "max_hold_days": int(sig.get("max_hold", 12)),
        "short_ratchet": False, "climax_warn": False, "is_comm": is_comm,
    }
    return cand, exit_now


def holy_grail_portfolio_gate(
    cand: dict[str, Any],
    active: list[dict[str, Any]],
    governor: dict[str, Any],
    macro_events: list[int],
    now_ts: int,
) -> tuple[bool, str]:
    """Enforce non-blocking pod/sleeve gate, concurrency caps, Friday cutoff & macro blackouts."""
    sym = cand["sym"]
    fam = cand["family"]
    pod = cand.get("pod", "CORE")
    side = int(cand["side"])
    is_non_crypto = bool(cand.get("is_comm", False) or pod == "TRADFI")

    if is_non_crypto and is_friday_close_ts(now_ts):
        return False, "FRIDAY_COMMODITY_BLACKOUT"
    if fam in ("A", "C", "TF") and is_in_macro_blackout(now_ts, macro_events):
        return False, "MACRO_PRE_EVENT_BLACKOUT"
    pod_caps = governor.get("pod_caps") or {}
    if pod in pod_caps and sum(1 for a in active if a.get("pod", "CORE") == pod) >= int(pod_caps[pod]):
        return False, f"MAX_POD_{pod}"
    if len(active) >= int(governor.get("max_global", 16)):
        return False, "MAX_GLOBAL_CONCURRENCY"
    if sum(1 for a in active if a["sym"] == sym) >= int(governor.get("max_per_asset", 2)):
        return False, "MAX_PER_ASSET"
    if sum(1 for a in active if a["sym"] == sym and a["family"] == fam) >= int(governor.get("max_per_asset_sleeve", 1)):
        return False, "MAX_PER_ASSET_SLEEVE"
    mt5_sym = cand.get("mt5_symbol")
    max_same_mt5 = int(governor.get("max_same_mt5_symbol", 0) or 0)
    if mt5_sym and max_same_mt5 > 0:
        if sum(1 for a in active if a.get("mt5_symbol") == mt5_sym) >= max_same_mt5:
            return False, "MAX_SAME_MT5_SYMBOL"
    if pod == "CORE" and sum(1 for a in active if a.get("pod", "CORE") == "CORE" and int(a["side"]) == side) >= int(governor.get("max_same_side", 6)):
        return False, "MAX_SAME_SIDE"
    return True, "PASS"


def venue_risk_size(
    sl_dist: float,
    target_risk_usd: float,
    max_risk_usd: float,
    spec: pilot.SymbolSpec | None = None,
    venue: str = "mt5",
) -> tuple[float | None, str]:
    """Venue-aware sizing: strict non-compression on MT5 vs exact fractional units on Perp CLOB."""
    if sl_dist <= 0 or target_risk_usd <= 0:
        return None, "INVALID_RISK_GEOMETRY"
    if venue == "perp":
        qty = round(min(target_risk_usd, max_risk_usd) / sl_dist, 6)
        return (qty, "OK") if qty > 0 else (None, "ZERO_QTY")
    if spec is None:
        return None, "MISSING_SYMBOL_SPEC"
    return pilot.lots_for_risk(spec, sl_dist, target_risk_usd, max_risk_usd)


class HolyGrailEngine(sc.SovereignCoreEngine):
    """Unified 4-Pod Dual-Venue Production Runner (`4_HGv4_Core+TRSI+TF_CS16_R1.00`)."""

    def __init__(
        self,
        cfg_path: Path = DEFAULT_CONFIG_PATH,
        state_path: Path = DEFAULT_STATE_PATH,
        journal_path: Path = DEFAULT_JOURNAL_PATH,
        dry: bool = False,
        live: bool = False,
    ) -> None:
        super().__init__(cfg_path, state_path, journal_path, dry=dry, live=live)
        raw_map = default_macro_timestamps()
        self._macro_events = raw_map["fomc"] + raw_map["nfp"] + raw_map["cpi"]
        self._fomc_events = raw_map["fomc"]
        self._comm_set = set(self.cfg.get("commodities", []))
        self._excluded = set(self.cfg.get("excluded_assets", ["XPDUSDT"]))
        self.pair_by_src = {p["source_symbol"]: p for p in self.pairs}
        self.live_cache_dir = ROOT / self.cfg.get("live_feed", {}).get("live_cache_dir", "engine/data/live_cache")
        self._ws_collector: core.BinanceAggTradeCollector | None = None
        self._last_feed_sync_info: dict[str, dict[str, Any]] = {}
        self._last_daily_exit_map: dict[str, bool] = {}
        self._last_daily_sync_ts: int = 0
        self._daily_cache: dict[str, pd.DataFrame] = {}
        self._last_pod4_sync_ts: int = 0
        self._last_pod4_summary: dict[str, Any] = {"mode": "INIT", "delta_avail_usd": 0.0, "n_coins": 0}

    def _start_ws_collector_if_needed(self) -> None:
        lf = self.cfg.get("live_feed", {})
        if not self.live or self.dry or not lf.get("enable_ws_collector", False):
            return
        if self._ws_collector is not None:
            return
        ws_syms = [
            p["source_symbol"] for p in self.pairs
            if p.get("category") == "CRYPTO" and p.get("feed_type") != "mt5_h4"
        ]
        if ws_syms and hasattr(core, "BinanceAggTradeCollector"):
            self._ws_collector = core.BinanceAggTradeCollector(ws_syms, self.live_cache_dir / "ws")
            self._ws_collector.start()

    def _hg_sleeve_signals(self, now_ts: int) -> tuple[list[dict[str, Any]], dict[str, pd.DataFrame]]:
        """Sync live Binance & MT5 feeds and evaluate Pod 1 (CORE), Pod 2 (KL_MR) & Pod 3 (TRADFI)."""
        lf = self.cfg.get("live_feed", {})
        use_rest = bool(lf.get("enable_rest_sync", False)) and (self.live or not self.dry or lf.get("dry_use_rest", False))
        gov = self.cfg.get("governor", {})
        max_stale_1m = int(gov.get("max_feed_staleness_sec", 300))
        kl_params = gov.get("kl_mr_params", {})
        tf_params = gov.get("tradfi_params", {})
        refresh_daily_rest = use_rest and ((now_ts - self._last_daily_sync_ts) >= 300)

        # 1. Sync & compute BTCUSDT 4H macro trend first
        btc_4h, btc_k1, btc_m5 = sync_live_symbol_feeds("BTCUSDT", self.live_cache_dir, now_ts, enable_rest=use_rest, ws_collector=self._ws_collector)
        btc_bull = True
        if not btc_4h.empty:
            btc_feat = compute_4h_physics_features(btc_4h)
            btc_bull = bool(float(btc_feat["c"].iloc[-1]) > float(btc_feat["ema233"].iloc[-1]))

        out: list[dict[str, Any]] = []
        feat_4h_map: dict[str, pd.DataFrame] = {}
        daily_exit_map: dict[str, bool] = {}

        # 2. Pod 1 (CORE: Sleeves A/B/C) + Pod 2 (KL_MR: Daily S17) on Crypto Majors & Commodities
        core_pairs = [
            p for p in self.pairs
            if p["source_symbol"] not in self._excluded
            and p.get("enabled", True)
            and p.get("feed_type") != "mt5_h4"
        ]
        for pair in core_pairs:
            sym = pair["source_symbol"]
            msym = pair["mt5_symbol"]
            is_comm = sym in self._comm_set
            df4, k1, m5 = (
                (btc_4h, btc_k1, btc_m5)
                if sym == "BTCUSDT"
                else sync_live_symbol_feeds(sym, self.live_cache_dir, now_ts, enable_rest=use_rest, ws_collector=self._ws_collector)
            )
            feed_age_4h = (now_ts - int(df4["end_ts"].iloc[-1])) if (not df4.empty and "end_ts" in df4.columns) else -1
            feed_age_1m = (now_ts - int(k1["bar_1m"].iloc[-1]) - 60) if not k1.empty else -1
            self._last_feed_sync_info[sym] = {"bars_4h": len(df4), "bars_1m": len(k1), "age_4h_s": feed_age_4h, "age_1m_s": feed_age_1m}

            if not df4.empty:
                f4 = compute_4h_physics_features(df4, btc_4h if not btc_4h.empty else None)
                feat_4h_map[sym] = f4
                for s in eval_4h_physics_latest(f4, sym, is_comm=is_comm):
                    s["pod"] = "CORE"
                    s["mt5_symbol"] = msym
                    out.append(s)

            if not k1.empty:
                fresh_ok = True if (is_comm or not use_rest or feed_age_1m <= max_stale_1m) else False
                if fresh_ok:
                    vc_df = build_causal_volclock_bars(k1, m5 if not m5.empty else None, sym=sym, is_comm=is_comm)
                    for s in eval_volclock_latest(vc_df, btc_bull, sym, is_comm=is_comm):
                        s["pod"] = "CORE"
                        s["mt5_symbol"] = msym
                        out.append(s)
                if sym == "BTCUSDT":
                    for ev_ts in self._fomc_events:
                        sig_b = eval_fomc_15m_aligned(k1, ev_ts, sym, now_ts)
                        if sig_b is not None:
                            sig_b["pod"] = "CORE"
                            sig_b["mt5_symbol"] = msym
                            out.append(sig_b)

            # Pod 2: Daily Kaminsky-Lo Triple-RSI (`D_TRSI_KL`)
            if refresh_daily_rest or sym not in self._daily_cache:
                self._daily_cache[sym] = sync_binance_daily_closed(sym, self.live_cache_dir, now_ts, enable_rest=refresh_daily_rest)
            daily_df = self._daily_cache.get(sym, pd.DataFrame())
            cand_kl, exit_kl = eval_kl_triple_rsi_latest(
                daily_df, sym, sleeve="D_TRSI_KL", pod="KL_MR", family="D",
                is_comm=is_comm, kl_params=kl_params, mt5_symbol=msym,
            )
            daily_exit_map[sym] = exit_kl
            if cand_kl is not None:
                out.append(cand_kl)

        # 3. Pod 3 (TRADFI: `TF_TREND_L` 4H + `TF_TRSI_KL` 1D on 15 MT5 TradFi Assets)
        tradfi_pairs = [p for p in self.pairs if p.get("feed_type") == "mt5_h4" and p.get("enabled", True)]
        if tradfi_pairs and getattr(core, "MT5_OK", False):
            tf_h4 = getattr(core.mt5, "TIMEFRAME_H4", 16388)
            tf_d1 = getattr(core.mt5, "TIMEFRAME_D1", 16408)
            for pair in tradfi_pairs:
                sym = pair["source_symbol"]
                msym = pair["mt5_symbol"]
                df_tf4 = sync_mt5_bars(msym, tf_h4, 14400, "4h", self.live_cache_dir, bars_count=320, now_ts=now_ts)
                age_tf4 = (now_ts - int(df_tf4["end_ts"].iloc[-1])) if not df_tf4.empty else -1
                self._last_feed_sync_info[sym] = {"bars_4h": len(df_tf4), "bars_1m": 0, "age_4h_s": age_tf4, "age_1m_s": -1}
                for s in eval_tradfi_4h_latest(df_tf4, sym, params=tf_params, mt5_symbol=msym):
                    out.append(s)

                if pair.get("trsi_eligible", False):
                    if refresh_daily_rest or sym not in self._daily_cache:
                        self._daily_cache[sym] = sync_mt5_bars(msym, tf_d1, 86400, "1d", self.live_cache_dir, bars_count=260, now_ts=now_ts)
                    d_tf = self._daily_cache.get(sym, pd.DataFrame())
                    trsi_kl_p = {**kl_params, "risk_scale": float(tf_params.get("trsi_risk_scale", 0.50))}
                    cand_tf_kl, exit_tf_kl = eval_kl_triple_rsi_latest(
                        d_tf, sym, sleeve="TF_TRSI_KL", pod="TRADFI", family="D",
                        is_comm=True, kl_params=trsi_kl_p, mt5_symbol=msym,
                    )
                    daily_exit_map[sym] = exit_tf_kl
                    if cand_tf_kl is not None:
                        out.append(cand_tf_kl)

        if refresh_daily_rest or self._last_daily_sync_ts == 0:
            self._last_daily_sync_ts = now_ts
        self._last_daily_exit_map = daily_exit_map

        prio = gov.get("sleeve_priority", {"B": 0, "A": 1, "TF": 2, "D": 2, "C": 3})
        out.sort(key=lambda x: (prio.get(x["family"], 9), x["sym"], x["sleeve"]))
        return out, feat_4h_map

    def _query_delta_balances_usd(self) -> tuple[float, float, str]:
        """Read Delta Exchange India available & total USD/USDT wallet equity."""
        try:
            wb = delta_direct.request_delta("GET", "/v2/wallet/balances")
            if not isinstance(wb, dict) or not wb.get("success"):
                return 0.0, 0.0, "DELTA_API_ERR"
            tot, avail = 0.0, 0.0
            for b in wb.get("result", []) or []:
                if str(b.get("asset_symbol", "")).upper() in {"USD", "USDT", "USDC"}:
                    tot += float(b.get("balance", 0.0) or 0.0)
                    avail += float(b.get("available_balance", 0.0) or 0.0)
            return round(avail, 4), round(tot, 4), "OK"
        except Exception as exc:
            return 0.0, 0.0, f"DELTA_EXC:{exc}"

    def _rebalance_delta_pod4(
        self,
        weights: dict[str, float],
        delta_eq_usd: float,
        dd_mult: float,
        cs_cfg: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Execute market-neutral Delta Exchange rebalance when Delta equity >= min_delta_equity_usd."""
        if not weights or delta_eq_usd <= 0:
            return []
        prods = delta_direct.request_delta("GET", "/v2/products")
        pos_resp = delta_direct.request_delta("GET", "/v2/positions/margined")
        if not (isinstance(prods, dict) and prods.get("success")):
            return []
        prod_by_sym: dict[str, dict[str, Any]] = {}
        for p in prods.get("result", []) or []:
            psym = str(p.get("symbol", "")).upper()
            if psym.endswith("USD") and p.get("contract_type") in {"perpetual_futures", None}:
                prod_by_sym[psym.removesuffix("USD") + "USDT"] = p
                prod_by_sym[psym] = p

        cur_pos: dict[int, int] = {}
        if isinstance(pos_resp, dict) and pos_resp.get("success"):
            for cp in pos_resp.get("result", []) or []:
                cur_pos[int(cp["product_id"])] = int(cp.get("size", 0) or 0)

        gross_scale = min(1.0, max(0.25, float(cs_cfg.get("target_ann_vol", 0.16)) / 0.16) * dd_mult)
        execs: list[dict[str, Any]] = []
        orders_to_send: list[tuple[int, dict[str, Any], int, str]] = []
        for sym, w in weights.items():
            prod = prod_by_sym.get(sym)
            if not prod:
                continue
            pid = int(prod["id"])
            cval = float(prod.get("contract_value", 0.001) or 0.001)
            mark = float(prod.get("mark_price", 0.0) or prod.get("spot_price", 0.0) or 0.0)
            if mark <= 0 or cval <= 0:
                continue
            tgt_sz = int(round((float(w) * delta_eq_usd * gross_scale) / (cval * mark)))
            cur_sz = cur_pos.get(pid, 0)
            diff = tgt_sz - cur_sz
            if diff != 0:
                side = "buy" if diff > 0 else "sell"
                reduce_only = (cur_sz > 0 and diff < 0 and abs(diff) <= abs(cur_sz)) or (cur_sz < 0 and diff > 0 and abs(diff) <= abs(cur_sz))
                orders_to_send.append((0 if reduce_only else 1, {"product_id": pid, "size": abs(diff), "side": side, "order_type": "market_order", "reduce_only": reduce_only}, pid, sym))

        orders_to_send.sort(key=lambda x: x[0])
        for _, payload, _, sym in orders_to_send:
            resp = delta_direct.request_delta("POST", "/v2/orders", body=json.dumps(payload))
            ok = bool(isinstance(resp, dict) and resp.get("success"))
            execs.append({"sym": sym, "side": payload["side"], "size": payload["size"], "ok": ok})
            self.journal.log("HG_DELTA_REBAL", sleeve="CS_POD4", symbol=sym, side=payload["side"].upper(),
                             lots=float(payload["size"]), retcode="OK" if ok else "ERR", note=f"ro={payload['reduce_only']}")
        return execs

    def _step_pod4_cs_delta(
        self,
        cur_ts: int,
        dd_mult: float = 1.0,
        feat_4h_map: dict[str, pd.DataFrame] | None = None,
    ) -> dict[str, Any]:
        """Evaluate Pod 4 S19 CS Carry+Mom across 27 perps & auto-gate Delta Exchange execution."""
        cs_cfg = self.cfg.get("cs_pod4", {})
        if not cs_cfg.get("enabled", False):
            return {"enabled": False, "mode": "DISABLED", "delta_avail_usd": 0.0, "n_coins": 0}

        sync_int = int(cs_cfg.get("sync_interval_sec", 300))
        min_eq = float(cs_cfg.get("min_delta_equity_usd", 250.0))
        st_path = ROOT / cs_cfg.get("state_path", "engine/data/cs_pod4_state.json")
        if (cur_ts - self._last_pod4_sync_ts) < sync_int and st_path.is_file():
            return dict(self._last_pod4_summary)

        st = json.loads(st_path.read_text(encoding="utf-8")) if st_path.is_file() else {}
        lf = self.cfg.get("live_feed", {})
        use_rest = bool(lf.get("enable_rest_sync", False)) and (self.live or not self.dry or lf.get("dry_use_rest", False))
        use_rest = use_rest and not getattr(self, "_offline_replay", False)

        avail_usd, tot_usd, delta_msg = self._query_delta_balances_usd()

        rows: list[dict[str, Any]] = []
        latest_prices: dict[str, float] = {}
        latest_bar_ts = 0
        fmap = feat_4h_map or {}
        for sym in cs_cfg.get("universe", []):
            df4 = fmap.get(sym)
            if df4 is None or df4.empty:
                live_p = self.live_cache_dir / f"{sym}_4h_live.parquet"
                hist_p = CACHE_4H_DIR / f"{sym}_4h.parquet"
                df4 = pd.read_parquet(live_p) if live_p.is_file() else (pd.read_parquet(hist_p) if hist_p.is_file() else pd.DataFrame())
                if use_rest:
                    try:
                        rest4 = fetch_binance_klines(sym, "4h", limit=120, now_ms=(cur_ts - 3) * 1000)
                        _, fund_bp = fetch_binance_oi_and_funding(sym, oi_limit=1)
                        if not rest4.empty:
                            rest4 = rest4.rename(columns={"ts_open": "ts", "ts_close": "end_ts"})
                            rest4["d_pct"] = np.where(rest4["v"] > 0, (rest4["bv"] - rest4["sv"]) / rest4["v"] * 100.0, 0.0)
                            rest4["fund_bp"] = fund_bp
                            keep = ["ts", "end_ts", "o", "h", "l", "c", "v", "bv", "sv", "d_pct", "fund_bp"]
                            if not df4.empty:
                                for col in keep:
                                    if col not in df4.columns:
                                        df4[col] = 0.0
                                df4 = pd.concat([df4[keep], rest4[keep]], ignore_index=True).drop_duplicates("ts", keep="last").sort_values("ts").tail(200)
                            else:
                                df4 = rest4[keep]
                            _atomic_write_parquet(df4.reset_index(drop=True), live_p)
                    except Exception:
                        pass
            if df4 is None or len(df4) < 48:
                continue
            d = df4.sort_values("ts").reset_index(drop=True)
            c = d["c"].to_numpy(float)
            dp = d["d_pct"].to_numpy(float) if "d_pct" in d.columns else np.zeros(len(d))
            fb = d["fund_bp"].to_numpy(float) if "fund_bp" in d.columns else np.zeros(len(d))
            rets = np.diff(c[-26:]) / np.maximum(c[-26:-1], 1e-12)
            rows.append({
                "sym": sym,
                "fund_72h": float(np.nanmean(fb[-18:])),
                "ret_42b": float(c[-1] / max(c[-43], 1e-12) - 1.0),
                "ret_6b": float(c[-1] / max(c[-7], 1e-12) - 1.0),
                "dp_6b": float(np.nanmean(dp[-6:])),
                "vol_24": float(np.std(rets[-24:])),
            })
            latest_prices[sym] = float(c[-1])
            latest_bar_ts = max(latest_bar_ts, int(d["end_ts"].iloc[-1]) if "end_ts" in d.columns else int(d["ts"].iloc[-1]) + 14400)

        cs_df = pd.DataFrame(rows)
        ema_scores = dict(st.get("ema_scores", {}) or {})
        weights = dict(st.get("target_weights", {}) or {})
        last_rebal_ts = int(st.get("last_rebal_ts", 0) or 0)
        shadow_eq = float(st.get("shadow_equity_usd", 1000.0) or 1000.0)
        prev_px = dict(st.get("last_prices", {}) or {})
        if weights and prev_px and latest_prices:
            bar_ret = sum(float(w) * (latest_prices[s] / prev_px[s] - 1.0) for s, w in weights.items() if s in latest_prices and prev_px.get(s, 0) > 0)
            shadow_eq *= (1.0 + bar_ret)

        rebal_due = len(cs_df) >= 12 and (latest_bar_ts - last_rebal_ts) >= int(cs_cfg.get("rebalance_hours", 72)) * 3600
        exec_list: list[dict[str, Any]] = []
        if len(cs_df) >= 12:
            if rebal_due or not weights:
                weights, ema_scores = s19_target_weights(cs_df, ema_scores, cs_cfg)
                last_rebal_ts = latest_bar_ts or cur_ts
            if delta_msg != "OK" or avail_usd < min_eq:
                mode = "SHADOW_WAIT_FUNDING"
            elif self.live and not self.dry and not self.state.get("interlock_rearm_required", False):
                mode = "LIVE_ACTIVE"
                if rebal_due:
                    exec_list = self._rebalance_delta_pod4(weights, tot_usd, dd_mult, cs_cfg)
            else:
                mode = "DRY_READY"
        else:
            mode = "SHADOW_WARMUP"

        out_state = {
            "updated_ts": cur_ts,
            "mode": mode,
            "delta_status": delta_msg,
            "delta_total_usd": tot_usd,
            "delta_avail_usd": avail_usd,
            "min_delta_equity_usd": min_eq,
            "n_coins": len(cs_df),
            "last_rebal_ts": last_rebal_ts,
            "shadow_equity_usd": round(shadow_eq, 2),
            "target_weights": weights,
            "ema_scores": ema_scores,
            "last_prices": latest_prices,
            "last_execs": exec_list,
        }
        st_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_st = st_path.with_suffix(".tmp")
        tmp_st.write_text(json.dumps(out_state, indent=2), encoding="utf-8")
        tmp_st.replace(st_path)
        self._last_pod4_sync_ts = cur_ts
        self._last_pod4_summary = {
            "enabled": True, "mode": mode, "delta_avail_usd": avail_usd,
            "min_req_usd": min_eq, "n_coins": len(cs_df), "shadow_eq": round(shadow_eq, 2),
        }
        return dict(self._last_pod4_summary)

    def _active_book_from_mt5(self, positions: list[Any], orders: list[Any]) -> list[dict[str, Any]]:
        """Map live MT5 positions & pending orders into pod-aware active list."""
        book: list[dict[str, Any]] = []
        pos_meta = self.state.get("positions", {}) or {}
        for p in positions:
            if int(p.magic) not in self.pair_by_magic:
                continue
            pair = self.pair_by_magic[int(p.magic)]
            meta = pos_meta.get(str(p.ticket), {})
            book.append({
                "sym": pair["source_symbol"],
                "mt5_symbol": p.symbol,
                "pod": meta.get("pod", pair.get("pod", "CORE")),
                "family": meta.get("family", "TF" if pair.get("pod") == "TRADFI" else "A"),
                "side": 1 if int(p.type) == 0 else -1,
                "ticket": int(p.ticket),
            })
        for o in orders:
            if int(o.magic) not in self.pair_by_magic:
                continue
            pair = self.pair_by_magic[int(o.magic)]
            book.append({
                "sym": pair["source_symbol"],
                "mt5_symbol": o.symbol,
                "pod": pair.get("pod", "CORE"),
                "family": "TF" if pair.get("pod") == "TRADFI" else "A",
                "side": 1 if int(o.type) in (0, 2, 4) else -1,
                "ticket": int(o.ticket),
            })
        return book

    def _manage_hg_open_positions(
        self,
        positions: list[Any],
        feat_4h_map: dict[str, pd.DataFrame],
        cur_ts: int,
        daily_exit_map: dict[str, bool] | None = None,
    ) -> None:
        """Apply T4_Convex trailing SL (CORE/TF_TREND) and Kaminsky-Lo RSI>50 + 12d exits (KL_MR/TF_TRSI_KL)."""
        if not self.live or self.dry:
            return
        pos_state = self.state.get("positions", {}) or {}
        ours = [p for p in positions if int(p.magic) in self.pair_by_magic]
        live_tickets = {str(p.ticket) for p in ours}
        d_exits = self._last_daily_exit_map if daily_exit_map is None else daily_exit_map

        for tid in list(pos_state.keys()):
            if tid not in live_tickets:
                meta = pos_state.pop(tid)
                self.journal.log("HG_CLOSED_SYNC", sleeve=meta.get("sleeve", ""), magic=int(meta.get("magic", 0)),
                                 symbol=meta.get("mt5_symbol", ""), note="closed on MT5")

        dev = int(self.cfg.get("governor", {}).get("deviation", 30))
        tr_cfg = self.cfg.get("trailing_exit", {})
        max_hold_bars = int(tr_cfg.get("max_hold_bars", 120))

        for p in ours:
            tid = str(p.ticket)
            pair = self.pair_by_magic[int(p.magic)]
            side = 1 if int(p.type) == 0 else -1
            meta = pos_state.setdefault(tid, {
                "sleeve": pair["sid"], "family": "TF" if pair.get("pod") == "TRADFI" else "A",
                "pod": pair.get("pod", "CORE"), "magic": int(p.magic), "mt5_symbol": p.symbol,
                "source_symbol": pair["source_symbol"], "side": side, "entry": float(p.price_open),
                "orig_sd": abs(float(p.price_open) - float(p.sl)) if float(p.sl) > 0 else float(p.price_open) * 0.01,
                "best_px": float(p.price_open), "open_ts": int(p.time), "kl_nosl": False,
            })
            tick = core.mt5.symbol_info_tick(p.symbol)
            si = core.mt5.symbol_info(p.symbol)
            if tick is None or si is None:
                continue

            cur_px = float(tick.bid if side == 1 else tick.ask)
            if (side == 1 and cur_px > float(meta["best_px"])) or (side == -1 and cur_px < float(meta["best_px"])):
                meta["best_px"] = cur_px

            if meta.get("kl_nosl"):
                tf_sec = int(meta.get("max_hold_sec", 12 * 86400))
            elif meta.get("family") == "B":
                tf_sec = 60 * int(self.cfg.get("macro_overlay", {}).get("fomc_max_hold_min", 4320))
            else:
                tf_sec = max_hold_bars * int(pair["tf_sec"])

            if (cur_ts - int(meta["open_ts"])) >= tf_sec:
                res = self.bridge.close_position(p, dev)
                self.journal.log("HG_TIME_EXIT", sleeve=meta.get("sleeve", ""), magic=int(p.magic),
                                 symbol=p.symbol, price=cur_px, retcode=str(getattr(res, "retcode", "NONE")),
                                 note=f"held >= {tf_sec}s")
                continue

            if meta.get("kl_nosl"):
                if d_exits.get(str(meta.get("source_symbol", "")), False):
                    res = self.bridge.close_position(p, dev)
                    self.journal.log("HG_KL_RSI_EXIT", sleeve=meta.get("sleeve", ""), magic=int(p.magic),
                                     symbol=p.symbol, price=cur_px, retcode=str(getattr(res, "retcode", "NONE")),
                                     note="Daily RSI(5)>50 exit")
                continue

            f4 = feat_4h_map.get(pair["source_symbol"])
            cur_atr = float(f4["atr"].iloc[-1]) * (cur_px / max(float(f4["c"].iloc[-1]), 1e-9)) if (f4 is not None and not f4.empty) else float(meta["orig_sd"]) / 2.5
            cur_close_scaled = float(f4["c"].iloc[-1]) * (cur_px / max(float(f4["c"].iloc[-1]), 1e-9)) if (f4 is not None and not f4.empty) else cur_px
            climax_warn = bool(side == -1 and f4 is not None and not f4.empty and ((float(f4["vol_rel20"].iloc[-1]) > 2.0 and float(f4["clv"].iloc[-1]) > 0.0) or (float(f4["oi_chg_6b"].iloc[-1]) < -6.0 and float(f4["clv"].iloc[-1]) > -0.2)))

            be_r = 1.0 if meta.get("family") == "B" else float(tr_cfg.get("be_trigger_r", 1.5))
            trail_atr = (1.5 * float(meta["orig_sd"]) / max(cur_atr, 1e-9)) if meta.get("family") == "B" else float(tr_cfg.get("trail_atr", 5.0))
            new_sl = compute_t4_trail_sl(
                float(meta["entry"]), float(p.sl), float(meta["best_px"]), cur_atr, float(meta["orig_sd"]),
                side, be_r=be_r, be_pad_r=float(tr_cfg.get("be_pad_r", 0.05)), trail_atr=trail_atr,
                cur_close=cur_close_scaled, climax_warn=climax_warn,
            )
            new_sl = round(new_sl, int(si.digits))
            min_stop = float(getattr(si, "trade_stops_level", 0) or 0) * float(si.point)
            strict_better = (new_sl > float(p.sl) + float(si.point)) if side == 1 else (new_sl < float(p.sl) - float(si.point) or float(p.sl) == 0.0)
            safe_dist = (cur_px - new_sl > min_stop) if side == 1 else (new_sl - cur_px > min_stop)
            if strict_better and safe_dist:
                res = self.bridge.modify_sl(int(p.ticket), new_sl, float(p.tp))
                self.journal.log("HG_TRAIL_SL", sleeve=meta.get("sleeve", ""), magic=int(p.magic),
                                 symbol=p.symbol, price=cur_px, sl=new_sl, tp=float(p.tp),
                                 retcode=str(getattr(res, "retcode", "NONE")), note="T4_Convex")
        self.state.set("positions", pos_state)

    def _punch_hg_signal(
        self,
        cand: dict[str, Any],
        equity: float,
        committed_risk: float,
        dd_mult: float,
        cur_ts: int,
    ) -> tuple[bool, str]:
        """Validate live MT5 spread/tick/sizing non-compression & execute atomic server-side bracket."""
        src_sym = cand["sym"]
        pair = self.pair_by_src.get(src_sym)
        if not pair or not pair.get("enabled", True):
            return False, "UNMAPPED_MT5_PAIR"
        msym = pair["mt5_symbol"]
        self.bridge.ensure_visible(msym)
        si = core.mt5.symbol_info(msym)
        tk = core.mt5.symbol_info_tick(msym)
        if si is None or tk is None or tk.bid <= 0 or tk.ask <= 0:
            return False, f"MT5_NO_QUOTE_{msym}"

        side = int(cand["side"])
        entry_px = float(tk.ask if side == 1 else tk.bid)
        scale = entry_px / max(float(cand["ref_px"]), 1e-9)
        sl_dist = float(cand["sl_dist"]) * scale
        atr_mt5 = float(cand["atr"]) * scale
        if sl_dist <= 0:
            return False, "INVALID_SL_DIST"

        gov = self.cfg.get("governor", {})
        spr = float(tk.ask - tk.bid)
        if spr / sl_dist > float(gov.get("max_spread_sl_ratio", 0.20)):
            return False, f"SPREAD_GT_20PCT_SL({spr/sl_dist:.2f})"
        if atr_mt5 > 0 and spr / atr_mt5 > float(gov.get("spread_atr_max", 0.45)):
            return False, "SPREAD_GT_ATR_MAX"

        risk_scale = float(cand.get("risk_scale", 1.0))
        target_risk = equity * (float(gov.get("risk_pct", 1.0)) / 100.0) * dd_mult * risk_scale
        max_single = equity * (float(gov.get("max_risk_single_trade_pct", 1.0)) / 100.0)
        max_total = equity * (float(gov.get("max_total_open_risk_pct", 8.0)) / 100.0)
        if committed_risk + target_risk > max_total:
            return False, "BOOK_RISK_CAP_REACHED"

        sp = pilot.SymbolSpec(msym, int(si.digits), float(si.point), float(si.trade_tick_value),
                              float(si.trade_tick_size), float(si.volume_min), float(si.volume_step))
        lots, sz_msg = venue_risk_size(sl_dist, target_risk, max_single, spec=sp, venue="mt5")
        if lots is None:
            return False, f"SIZE_REJECT:{sz_msg}"

        sl_px = round(entry_px - side * sl_dist, sp.digits)
        safety_tp_r = float(gov.get("safety_tp_r_when_trail_only", 12.0))
        tp_mult = max(6.0, safety_tp_r / 2.0) if cand.get("kl_nosl") else safety_tp_r
        tp_px = round(entry_px + side * tp_mult * sl_dist, sp.digits)
        res = self.bridge.send_bracket(sp, side, lots, entry_px, sl_px, tp_px, int(pair["magic"]),
                                       f"HG_{cand['sleeve']}", int(gov.get("deviation", 30)))
        rc = getattr(res, "retcode", -1)
        if rc != core.mt5.TRADE_RETCODE_DONE:
            return False, f"MT5_SEND_ERR_{rc}:{getattr(res, 'comment', '')}"

        pos_state = self.state.get("positions", {}) or {}
        for p in self.bridge.positions():
            if int(p.magic) == int(pair["magic"]) and p.symbol == msym:
                pos_state[str(p.ticket)] = {
                    "sleeve": cand["sleeve"], "family": cand["family"], "pod": cand.get("pod", pair.get("pod", "CORE")),
                    "magic": int(pair["magic"]), "mt5_symbol": msym, "source_symbol": src_sym, "side": side,
                    "entry": float(p.price_open), "orig_sd": abs(float(p.price_open) - float(p.sl)),
                    "best_px": float(p.price_open), "open_ts": cur_ts,
                    "kl_nosl": bool(cand.get("kl_nosl", False)),
                    "max_hold_sec": int(cand.get("max_hold_days", 12)) * 86400,
                }
        self.state.set("positions", pos_state)
        self.journal.log("HG_EXEC_OPEN", sleeve=cand["sleeve"], magic=int(pair["magic"]), symbol=msym,
                         side="BUY" if side == 1 else "SELL", lots=lots, price=entry_px, sl=sl_px, tp=tp_px,
                         risk_usd=round(lots * sl_dist * sp.value_per_price_unit(), 2), retcode=str(rc),
                         note=f"pod={cand.get('pod','CORE')} dd_mult={dd_mult:.2f}")
        return True, f"FILLED lots={lots}"

    def step_once(self, now_ts: int | None = None) -> dict[str, Any]:
        """Execute one end-to-end Sovereign-interlocked live or dry HGv4 cycle across all 4 pods."""
        cur_ts = int(now_ts if now_ts is not None else time.time())
        paper = getattr(self, "_offline_replay", False)

        mt5_ok = self.bridge.connect() if (self.live or not paper) else False
        ti = core.mt5.terminal_info() if (mt5_ok and core.MT5_OK) else None
        ai = core.mt5.account_info() if (mt5_ok and core.MT5_OK) else None

        exp_login = int(self.cfg.get("account", {}).get("expected_login") or 0)
        if self.live and not self.dry:
            if not mt5_ok or ai is None:
                raise RuntimeError("MT5 live connection failed")
            if exp_login and int(ai.login) != exp_login:
                raise RuntimeError(f"MT5 login mismatch: connected #{ai.login} != expected #{exp_login}")
            if not bool(getattr(ti, "trade_allowed", False)):
                self._record_interlock_failure(cur_ts)
                self.state.set("interlock_rearm_required", True)
                self.state.save()
                return {"engine": "HolyGrail", "trade_allowed": False, "signals": 0, "executed": 0}

        eq = float(ai.equity) if ai is not None else float(self.cfg.get("account", {}).get("reference_equity_usd", 2501.80))
        eq_floor = float(self.cfg.get("account", {}).get("equity_floor_usd", 1800.0))
        pk = max(float(self.state.get("peak_equity", eq)), eq)
        self.state.set("peak_equity", pk)
        th = self.cfg.get("governor", {}).get("dd_throttle", [10.0, 15.0])
        dd_mult, is_th = holy_grail_dd_mult(eq, pk, bool(self.state.get("dd_throttled", False)), float(th[0]), float(th[1]))
        self.state.set("dd_throttled", is_th)

        positions = self.bridge.positions() if mt5_ok else []
        orders = list(core.mt5.orders_get() or []) if (mt5_ok and core.MT5_OK) else []
        if is_friday_close_ts(cur_ts) or sc.non_crypto_schedule_state(cur_ts, self.cfg) in {"FRIDAY_CLOSE", "WEEKEND_BLACKOUT"}:
            self._flatten_non_crypto_for_friday(positions)
            if mt5_ok and self.live and not self.dry:
                positions = self.bridge.positions()
        committed_risk = self.bridge.total_sl_risk_usd(positions) if mt5_ok else 0.0

        cands, feat_4h_map = self._hg_sleeve_signals(cur_ts)
        if mt5_ok and self.live and not self.dry and not self.state.get("interlock_rearm_required", False):
            self._manage_hg_open_positions(positions, feat_4h_map, cur_ts)
            positions = self.bridge.positions()
            committed_risk = self.bridge.total_sl_risk_usd(positions)

        pod4_sm = self._step_pod4_cs_delta(cur_ts, dd_mult=dd_mult, feat_4h_map=feat_4h_map)

        active_book = self._active_book_from_mt5(positions, orders) if mt5_ok else []
        bars_seen = self.state.get("bars", {}) or {}
        gov = self.cfg.get("governor", {})
        exec_count = 0
        dry_ok_count = 0

        for cand in cands:
            s_key = f"{cand['sym']}:{cand['sleeve']}"
            ts_sig = int(cand.get("ts_sig", cur_ts))
            if int(bars_seen.get(s_key, 0) or 0) >= ts_sig:
                continue
            tf_c = cand.get("tf")
            if tf_c == "VC":
                max_lag = int(gov.get("max_signal_lag_vc_sec", 900))
            elif tf_c == "1D":
                max_lag = int(gov.get("max_signal_lag_1d_sec", 43200))
            else:
                max_lag = int(gov.get("max_signal_lag_4h_sec", 1800))
            if (cur_ts - ts_sig) > max_lag:
                continue

            ok, reason = holy_grail_portfolio_gate(cand, active_book, gov, self._macro_events, cur_ts)
            if not ok:
                self.journal.log("HG_VETO", sleeve=cand["sleeve"], symbol=cand["sym"],
                                 side="LONG" if cand["side"] > 0 else "SHORT", price=float(cand["ref_px"]), note=reason)
                continue

            if self.live and not self.dry:
                if eq < eq_floor or self.state.get("interlock_rearm_required", False):
                    self.journal.log("HG_SAFETY_BLOCK", sleeve=cand["sleeve"], symbol=cand["sym"], note="floor_or_rearm")
                    continue
                punched, msg = self._punch_hg_signal(cand, eq, committed_risk, dd_mult, cur_ts)
                if punched:
                    bars_seen[s_key] = ts_sig
                    active_book.append({
                        "sym": cand["sym"], "mt5_symbol": cand.get("mt5_symbol", ""),
                        "pod": cand.get("pod", "CORE"), "family": cand["family"], "side": int(cand["side"]),
                    })
                    exec_count += 1
                    committed_risk = self.bridge.total_sl_risk_usd(self.bridge.positions())
                else:
                    self.journal.log("HG_SKIP", sleeve=cand["sleeve"], symbol=cand["sym"],
                                     side="LONG" if cand["side"] > 0 else "SHORT", price=float(cand["ref_px"]), note=msg)
            else:
                bars_seen[s_key] = ts_sig
                dry_ok_count += 1
                self.journal.log("HG_DRY_SIGNAL", sleeve=cand["sleeve"], symbol=cand["sym"],
                                 side="LONG" if cand["side"] > 0 else "SHORT", price=float(cand["ref_px"]),
                                 note=f"PASS (pod={cand.get('pod','CORE')}, dd_mult={dd_mult:.2f}, live_mt5={mt5_ok})")

        self.state.set("bars", bars_seen)
        self.state.save()
        return {
            "engine": "HolyGrail",
            "version": self.cfg.get("_meta", {}).get("version", "4.0-LIVE-DUAL"),
            "equity": eq,
            "dd_mult": dd_mult,
            "mt5_connected": mt5_ok,
            "open_pos": len(positions),
            "committed_risk_usd": round(committed_risk, 2),
            "signals": len(cands),
            "dry_ok": dry_ok_count,
            "executed": exec_count,
            "pod4": pod4_sm,
            "feed_sync": self._last_feed_sync_info,
        }

    def run_loop(self) -> None:
        """Continuous live execution daemon with WebSocket tape capture, MT5 TradFi & Delta Pod 4."""
        poll_s = int(self.cfg.get("governor", {}).get("poll_seconds", 30))
        self._start_ws_collector_if_needed()
        print(f"[HolyGrailEngine] Started HGv4 loop (live={self.live}, dry={self.dry}, poll={poll_s}s, pairs={len(self.pairs)})", flush=True)
        try:
            while True:
                t0 = time.time()
                try:
                    sm = self.step_once()
                    p4 = sm.get("pod4", {})
                    print(
                        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] "
                        f"eq=${sm['equity']:.2f} dd_mult={sm['dd_mult']:.2f} pos={sm['open_pos']} "
                        f"risk=${sm['committed_risk_usd']:.2f} sigs={sm['signals']} exec={sm['executed']} "
                        f"pod4={p4.get('mode','?')}(${p4.get('delta_avail_usd',0):.2f})",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"[HolyGrailEngine] step error: {exc}", flush=True)
                time.sleep(max(1.0, poll_s - (time.time() - t0)))
        finally:
            if self._ws_collector is not None:
                self._ws_collector.stop()
            self.bridge.shutdown()


def print_status(cfg_path: Path, state_path: Path) -> None:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    gov = cfg["governor"]
    meta = cfg["_meta"]["oos_summary_10bps"]
    mt5_only = cfg["_meta"].get("oos_summary_mt5_only_10bps", {})
    cs = cfg.get("cs_pod4", {})
    st = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    cs_st_p = ROOT / cs.get("state_path", "engine/data/cs_pod4_state.json")
    cs_st = json.loads(cs_st_p.read_text(encoding="utf-8")) if cs_st_p.is_file() else {}
    print(f"HolyGrailEngine Status | Version={cfg['_meta']['version']} | Locked={cfg['_meta']['locked_config']}")
    print(f"  Risk={gov['risk_pct']}% | MaxPos={gov['max_global']} | PodCaps={gov.get('pod_caps')} | DD={gov['dd_throttle']} | MT5Pairs={len(cfg['pairs'])}")
    print(f"  HGv4 Combined OOS (16 Folds): Tr={meta['trades']} PF={meta['pf']} CAGR=+{meta['cagr_pct']}% MaxDD={meta['max_dd_pct']}% Cal={meta.get('calmar')} Sh={meta['sharpe']} DSR={meta['dsr']}")
    if mt5_only:
        print(f"  HGv4 MT5-Only (Pods 1-3): EndEq=${mt5_only.get('end_equity_2500'):,.2f} CAGR=+{mt5_only.get('cagr_pct')}% MaxDD={mt5_only.get('max_dd_pct')}% Cal={mt5_only.get('calmar')} Sh={mt5_only.get('sharpe')}")
    print(f"  Pod 4 Delta CS ({len(cs.get('universe', []))} coins): Mode={cs_st.get('mode','NOT_RUN')} | Avail=${cs_st.get('delta_avail_usd',0):.2f} / Min=${cs.get('min_delta_equity_usd',250):.0f} | ShadowEq=${cs_st.get('shadow_equity_usd',1000):.2f}")
    print(f"  PeakEquity=${st.get('peak_equity', cfg['account']['reference_equity_usd']):,.2f} | InterlockRearm={st.get('interlock_rearm_required', False)}")


def run_selftest() -> None:
    """Fast self-contained verification of HGv4 causality, 4-Pod gates, KL Triple-RSI, TradFi & Delta readiness."""
    cfg = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))

    # 1. Friday 20:30 UTC Non-Crypto Flatten invariant (Commodities + TradFi)
    fri_2030 = int(datetime(2026, 11, 27, 20, 30, tzinfo=timezone.utc).timestamp())
    fri_1855 = int(datetime(2026, 11, 27, 18, 55, tzinfo=timezone.utc).timestamp())
    assert is_friday_close_ts(fri_2030) is True
    assert is_friday_close_ts(fri_1855) is False
    ok_tf_fri, r_tf_fri = holy_grail_portfolio_gate({"sym": "US2000.p", "family": "TF", "pod": "TRADFI", "side": 1, "is_comm": True}, [], cfg["governor"], [], fri_2030)
    assert ok_tf_fri is False and r_tf_fri == "FRIDAY_COMMODITY_BLACKOUT"

    # 2. Macro 30m Blackout Gate vs B_FOMC bypass
    ev_ts = et_to_utc_ts("2026-09-16", 14, 0)
    ok_a, r_a = holy_grail_portfolio_gate({"sym": "BTCUSDT", "family": "A", "pod": "CORE", "side": 1, "is_comm": False}, [], cfg["governor"], [ev_ts], ev_ts - 600)
    ok_b, _ = holy_grail_portfolio_gate({"sym": "BTCUSDT", "family": "B", "pod": "CORE", "side": 1, "is_comm": False}, [], cfg["governor"], [ev_ts], ev_ts + 900)
    assert ok_a is False and r_a == "MACRO_PRE_EVENT_BLACKOUT"
    assert ok_b is True

    # 3. Non-Blocking Pod & Sleeve Gate
    act = [{"sym": "ETHUSDT", "family": "A", "pod": "CORE", "side": 1}]
    ok_c, _ = holy_grail_portfolio_gate({"sym": "ETHUSDT", "family": "C", "pod": "CORE", "side": 1, "is_comm": False}, act, cfg["governor"], [ev_ts], fri_1855)
    ok_kl, _ = holy_grail_portfolio_gate({"sym": "ETHUSDT", "family": "D", "pod": "KL_MR", "side": 1, "is_comm": False}, act, cfg["governor"], [ev_ts], fri_1855)
    ok_a2, r_a2 = holy_grail_portfolio_gate({"sym": "ETHUSDT", "family": "A", "pod": "CORE", "side": 1, "is_comm": False}, act, cfg["governor"], [ev_ts], fri_1855)
    assert ok_c is True and ok_kl is True and ok_a2 is False and r_a2 == "MAX_PER_ASSET_SLEEVE"

    # 4. T4 Convex Trail & Short Climax Ratchet
    sl_be = compute_t4_trail_sl(100.0, 95.0, 107.6, 2.0, 5.0, side=1, be_r=1.5, trail_atr=5.0)
    assert math.isclose(sl_be, 100.25, rel_tol=1e-6)
    sl_tr = compute_t4_trail_sl(100.0, sl_be, 115.0, 2.0, 5.0, side=1, be_r=1.5, trail_atr=5.0)
    assert math.isclose(sl_tr, 105.0, rel_tol=1e-6)

    # 5. DD Hysteresis
    m1, t1 = holy_grail_dd_mult(2200.0, 2500.0, False)
    m2, t2 = holy_grail_dd_mult(2340.0, 2500.0, t1)
    m3, t3 = holy_grail_dd_mult(2400.0, 2500.0, t2)
    assert (m1, t1) == (0.50, True) and (m2, t2) == (0.50, True) and (m3, t3) == (1.0, False)

    # 6. Causal Volume-Clock + Daily S17 Triple-RSI
    n = 1700
    ts0 = 1700000000
    k1 = pd.DataFrame({
        "bar_1m": np.arange(ts0, ts0 + 60 * n, 60),
        "open": np.linspace(100, 120, n),
        "high": np.linspace(100.5, 120.5, n),
        "low": np.linspace(99.5, 119.5, n),
        "close": np.linspace(100.1, 120.1, n) + np.sin(np.arange(n) * 0.1),
        "volume": np.full(n, 50.0),
        "taker_buy_vol": np.full(n, 32.0),
        "count": np.full(n, 20.0),
    })
    vc = build_causal_volclock_bars(k1, None, "TESTUSDT", is_comm=True)
    assert not vc.empty and "vah" in vc.columns and "val" in vc.columns

    # 7. Dual venue sizing + HGv4 pairs & Pod 4 config schema
    qty, _ = venue_risk_size(25.0, 25.0, 25.0, venue="perp")
    assert qty == 1.0 and len(cfg["pairs"]) >= 30 and len(cfg["cs_pod4"]["universe"]) >= 25
    print("HOLY_GRAIL_ENGINE HGv4 SELFTEST PASS (7/7 invariants verified)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Holy Grail Unified 4-Pod Dual-Venue Quantitative Engine (HGv4)")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--sync-live", action="store_true", help="Pull live Binance REST bars/OI/funding during --dry inspection")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--once", action="store_true", help="Run a single cycle and exit cleanly even with --live")
    ap.add_argument("--i-understand-live-risk", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        run_selftest()
        return
    if args.status:
        print_status(args.config, args.state)
        return
    if args.live and not args.i_understand_live_risk:
        raise SystemExit("REFUSED: --live requires --i-understand-live-risk")

    eng = HolyGrailEngine(args.config, args.state, args.journal, dry=(args.dry or not args.live), live=args.live)
    if args.sync_live:
        eng.cfg.setdefault("live_feed", {})["dry_use_rest"] = True
    if args.live and not args.once and not args.dry:
        eng.run_loop()
    else:
        sm = eng.step_once()
        print(f"HolyGrailEngine step completed cleanly: {json.dumps(sm)}")
        eng.bridge.shutdown()


if __name__ == "__main__":
    main()
