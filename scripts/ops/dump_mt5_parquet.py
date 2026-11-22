#!/usr/bin/env python3
"""
dump_mt5_parquet.py — Export high-performance Parquet OHLCV candlestick archives from MetaTrader 5.

Exports 1M, 5M, 15M, 1H, and 4H historical candlestick data for all MT5 assets into:
  C:\\Users\\SIGMA\\Documents\\Trading-Strategies\\historical_parquet\\<timeframe>\\<symbol>.parquet

Features:
  - Clean schema: timestamp (int64), datetime (UTC), open, high, low, close, tick_volume, spread, real_volume
  - Snappy compressed Apache Parquet format via pyarrow
  - Master manifest.json summarizing asset coverage, row counts, and date ranges
"""
from __future__ import annotations

import os
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    print("Error: MetaTrader5 python library not installed.")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent.parent
TARGET_DIR = ROOT / "historical_parquet"

TIMEFRAMES = {
    "1M": (mt5.TIMEFRAME_M1, 5000),
    "5M": (mt5.TIMEFRAME_M5, 5000),
    "15M": (mt5.TIMEFRAME_M15, 5000),
    "1H": (mt5.TIMEFRAME_H1, 5000),
    "4H": (mt5.TIMEFRAME_H4, 5000),
}


def dump_all_mt5_to_parquet():
    t_start = time.time()
    print("================================================================================")
    print("MT5 PARQUET EXPORT ENGINE")
    print(f"Target Directory: {TARGET_DIR}")
    print("================================================================================")

    # Ensure target directories exist
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for tf in TIMEFRAMES:
        (TARGET_DIR / tf).mkdir(parents=True, exist_ok=True)

    # Initialize MT5
    if not mt5.initialize(timeout=15000):
        print(f"[FAIL] mt5.initialize failed: {mt5.last_error()}", file=sys.stderr)
        return False

    symbols = mt5.symbols_get()
    if not symbols:
        print("[FAIL] No symbols returned from MT5 terminal.", file=sys.stderr)
        mt5.shutdown()
        return False

    sym_names = [s.name for s in symbols]
    print(f"[INFO] Discovered {len(sym_names)} symbols in MT5 broker inventory.")

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_symbols": len(sym_names),
        "timeframes": list(TIMEFRAMES.keys()),
        "symbols": {},
        "summary": {
            "total_files": 0,
            "total_rows": 0,
            "total_size_mb": 0.0,
        }
    }

    total_files = 0
    total_rows = 0

    for idx, sym in enumerate(sym_names, 1):
        mt5.symbol_select(sym, True)
        sym_entry = {"timeframes": {}}

        for tf_name, (tf_code, bar_count) in TIMEFRAMES.items():
            try:
                rates = mt5.copy_rates_from_pos(sym, tf_code, 0, bar_count)
                if rates is not None and len(rates) > 0:
                    df = pd.DataFrame(rates)
                    df.rename(columns={"time": "timestamp"}, inplace=True)
                    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
                    
                    cols = ["timestamp", "datetime", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
                    for c in cols:
                        if c not in df.columns:
                            df[c] = 0
                    df = df[cols]

                    file_path = TARGET_DIR / tf_name / f"{sym}.parquet"
                    df.to_parquet(file_path, engine="pyarrow", compression="snappy", index=False)

                    first_dt = df["datetime"].iloc[0].strftime("%Y-%m-%d %H:%M:%S")
                    last_dt = df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S")
                    rows = len(df)
                    size_kb = file_path.stat().st_size / 1024

                    sym_entry["timeframes"][tf_name] = {
                        "rows": rows,
                        "earliest": first_dt,
                        "latest": last_dt,
                        "size_kb": round(size_kb, 1),
                        "file": f"{tf_name}/{sym}.parquet"
                    }
                    total_files += 1
                    total_rows += rows
            except Exception as e:
                pass

        manifest["symbols"][sym] = sym_entry
        if idx % 10 == 0 or idx == len(sym_names):
            print(f"[{idx:>3}/{len(sym_names)}] Processed: {sym:<15} ({len(sym_entry['timeframes'])}/{len(TIMEFRAMES)} TFs saved)")

    mt5.shutdown()

    # Calculate total size
    total_bytes = sum(f.stat().st_size for f in TARGET_DIR.rglob("*.parquet"))
    manifest["summary"]["total_files"] = total_files
    manifest["summary"]["total_rows"] = total_rows
    manifest["summary"]["total_size_mb"] = round(total_bytes / (1024 * 1024), 2)

    manifest_path = TARGET_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    elapsed = time.time() - t_start
    print("================================================================================")
    print(f"[SUCCESS] Export complete in {elapsed:.2f}s!")
    print(f"  Total Parquet Files : {total_files}")
    print(f"  Total Rows Exported : {total_rows:,}")
    print(f"  Total Storage Size  : {manifest['summary']['total_size_mb']} MB")
    print(f"  Manifest Path       : {manifest_path}")
    print("================================================================================")
    return True


if __name__ == "__main__":
    success = dump_all_mt5_to_parquet()
    sys.exit(0 if success else 1)
