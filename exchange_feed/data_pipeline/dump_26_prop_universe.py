"""26-Asset Prop-Firm Footprint Universe Dumper (20 Crypto + 6 Commodities).

Dumps Binance USD-M Futures historical 8h funding, 1m klines, monthly aggTrades
footprint parquets (2021-01..present for Crypto, inception..present for Commodities),
current-month daily vision tail, builds 2H/4H/1M adaptive-tick cache, and snapshots
live MT5 contract specs + multi-year MT5 H4 commodity OHLC cross-checks.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from exchange_feed.backtesting.portfolio_engine import load_asset_tfs_cached
from exchange_feed.data_pipeline.binance_dumper import DATA_DIR, dump_binance_footprint
from exchange_feed.data_pipeline.history_dumper import run_history_dump
from exchange_feed.data_pipeline.vision_backfill import run_backfill

SPECS_26_PATH = DATA_DIR / "prop26_contract_specs.json"

# 20 Liquid Crypto (uniform 2021-01..2026-11) + 6 Binance FAPI Commodities
UNIVERSE_26: tuple[dict[str, Any], ...] = (
    # --- 10 Existing Crypto (2021-01) ---
    {"binance": "BTCUSDT",  "mt5": "BTCUSD.pi", "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "ETHUSDT",  "mt5": "ETHUSD.pi", "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "SOLUSDT",  "mt5": "SOLUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "BNBUSDT",  "mt5": "BNBUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "UNIUSDT",  "mt5": "UNIUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "AVAXUSDT", "mt5": "AVXUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "BCHUSDT",  "mt5": "BCHUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "AAVEUSDT", "mt5": "AVEUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "XMRUSDT",  "mt5": "XMRUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "ZECUSDT",  "mt5": "ZECUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    # --- 10 New Crypto (2021-01) ---
    {"binance": "XRPUSDT",  "mt5": "XRPUSD.pi", "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "DOGEUSDT", "mt5": None,        "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "ADAUSDT",  "mt5": "ADAUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "LINKUSDT", "mt5": None,        "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "DOTUSDT",  "mt5": "DOTUSD.pi", "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "NEARUSDT", "mt5": None,        "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "LTCUSDT",  "mt5": "LTCUSD.pi", "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "TRXUSDT",  "mt5": "TRXUSD.p",  "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "FILUSDT",  "mt5": None,        "cat": "CRYPTO",    "start_ym": "2021-01"},
    {"binance": "ATOMUSDT", "mt5": None,        "cat": "CRYPTO",    "start_ym": "2021-01"},
    # --- 6 Binance FAPI Commodities (Metals & Energy) ---
    {"binance": "XAUUSDT",  "mt5": "XAUUSD.pi", "cat": "COMMODITY", "start_ym": "2025-12"},
    {"binance": "XAGUSDT",  "mt5": "XAGUSD.pi", "cat": "COMMODITY", "start_ym": "2026-01"},
    {"binance": "XPTUSDT",  "mt5": "XPTUSD.pi", "cat": "COMMODITY", "start_ym": "2026-01"},
    {"binance": "XPDUSDT",  "mt5": "XPDUSD.pi", "cat": "COMMODITY", "start_ym": "2026-01"},
    {"binance": "COPPERUSDT","mt5":"COPPER.p",  "cat": "COMMODITY", "start_ym": "2026-03"},
    {"binance": "CLUSDT",   "mt5": "USWTI.p",   "cat": "COMMODITY", "start_ym": "2026-04"},
)


def snapshot_26_mt5_specs() -> dict[str, dict[str, Any]]:
    """Snapshot live MT5 spreads, swaps, min notional, and multi-year H4 commodity rates."""
    specs: dict[str, dict[str, Any]] = {}
    for row in UNIVERSE_26:
        specs[row["binance"]] = {**row, "sp_bps": 3.5 if row["cat"] == "COMMODITY" else 5.0, "mt5_available": False}

    try:
        import MetaTrader5 as mt5
    except Exception:
        SPECS_26_PATH.write_text(json.dumps(specs, indent=2), encoding="utf-8")
        return specs

    if not mt5.initialize():
        SPECS_26_PATH.write_text(json.dumps(specs, indent=2), encoding="utf-8")
        return specs

    try:
        for row in UNIVERSE_26:
            bn = row["binance"]
            ms = row["mt5"]
            if not ms:
                continue
            s = mt5.symbol_info(ms)
            tk = mt5.symbol_info_tick(ms)
            if not s or not tk or tk.bid <= 0:
                continue
            px = float(tk.bid)
            ask = float(tk.ask) if tk.ask >= px else px
            sp_px = max(0.0, ask - px) if ask >= px else float(s.spread * s.point)
            sp_bps = (sp_px / px * 10000.0) if px > 0 else 5.0
            if s.swap_mode == 1 and px > 0:
                sw_l_bps = float(s.swap_long) * float(s.point) / px * 10000.0
                sw_s_bps = float(s.swap_short) * float(s.point) / px * 10000.0
            elif s.swap_mode == 3:
                sw_l_bps = float(s.swap_long) / 365.0 * 100.0
                sw_s_bps = float(s.swap_short) / 365.0 * 100.0
            else:
                sw_l_bps, sw_s_bps = 0.0, 0.0
            min_notional = px * float(s.trade_contract_size) * float(s.volume_min)
            specs[bn] = {
                **row,
                "mt5_available": True,
                "px": round(px, 6),
                "sp_bps": round(sp_bps, 2),
                "sw_l_bps": round(sw_l_bps, 3),
                "sw_s_bps": round(sw_s_bps, 3),
                "min_notional": round(min_notional, 2),
                "contract_size": float(s.trade_contract_size),
                "volume_min": float(s.volume_min),
                "updated_ts": int(datetime.now(timezone.utc).timestamp()),
            }
            if row["cat"] == "COMMODITY":
                rates = mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H4, 0, 15000)
                if rates is not None and len(rates) > 0:
                    df_m = pd.DataFrame(rates).rename(columns={"time": "ts", "open": "o", "high": "h", "low": "l", "close": "c", "tick_volume": "v"})
                    df_m = df_m[["ts", "o", "h", "l", "c", "v"]].sort_values("ts").reset_index(drop=True)
                    pq.write_table(pa.Table.from_pandas(df_m, preserve_index=False), DATA_DIR / f"{bn}_mt5_h4.parquet", compression="snappy")
                    print(f"[specs] Exported {bn} MT5 H4 cross-check: {len(df_m)} bars", flush=True)
        SPECS_26_PATH.write_text(json.dumps(specs, indent=2), encoding="utf-8")
    finally:
        mt5.shutdown()
    return specs


def dump_all_26(
    only_missing: bool = True,
    workers: int = 5,
    tail_days: int = 29,
    symbols_filter: set[str] | None = None,
) -> dict[str, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_26_mt5_specs()
    results: dict[str, str] = {}

    for row in UNIVERSE_26:
        sym = row["binance"]
        if symbols_filter and sym not in symbols_filter:
            continue
        cache_4h = DATA_DIR / "cache" / f"{sym}_4h.parquet"
        agg_p = DATA_DIR / f"{sym}_agg_footprint.parquet"
        if only_missing and cache_4h.is_file() and agg_p.is_file() and cache_4h.stat().st_size > 50_000:
            results[sym] = "CACHED"
            print(f"[dump26] {sym:10s} -> already cached ({cache_4h.stat().st_size/1e6:.2f}MB), skipping dump", flush=True)
            continue
        try:
            print(f"\n=== [dump26] {sym} ({row['cat']} | start={row['start_ym']}) ===", flush=True)
            run_history_dump(symbol=sym, start_ym=row["start_ym"], end_ym=None, workers=workers)
            run_backfill(symbol=sym, days=tail_days, tick_bucket=None)
            try:
                dump_binance_footprint(symbol=sym, hours=24.0, max_requests=200, tick_bucket=None)
            except Exception as tail_exc:
                print(f"[dump26] {sym} REST tail warn: {tail_exc}", flush=True)
            g2, g4, *_ = load_asset_tfs_cached(sym, DATA_DIR, adaptive_tick=True)
            results[sym] = f"OK (2H={len(g2)}, 4H={len(g4)})"
            print(f"[dump26] {sym} READY -> {results[sym]}", flush=True)
            del g2, g4
            gc.collect()
        except Exception as exc:
            results[sym] = f"ERR: {exc}"
            print(f"[dump26] {sym} FAILED: {exc}", flush=True)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="Optional comma-separated subset")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--tail-days", type=int, default=29)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    flt = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} or None
    summary = dump_all_26(only_missing=not args.force, workers=args.workers, tail_days=args.tail_days, symbols_filter=flt)
    print("\n=== DUMP 26 SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k:12s}: {v}")
