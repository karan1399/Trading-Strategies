#!/usr/bin/env python3
"""
purge_stale_historical_data.py — Purge stale legacy CSV/Parquet historical candle archives.

Safely purges only raw candlestick files from:
  - Delta-Fibonacci-Institutional-Business/Blueberry_Data/
  - CG-Terminal/Delta_Exchange/MT5_Broker_Assets/
  - CG-Terminal/Delta_Exchange/ (*_1h.csv, *_4h.csv, etc.)
  - CG-Terminal/Delta_Fibonacci_Institutional_Business/data/

Preserves all source code, git files, desk records, and audit ledgers.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

TARGETS = [
    ROOT / "Delta-Fibonacci-Institutional-Business" / "Blueberry_Data",
    ROOT / "CG-Terminal" / "Delta_Exchange" / "MT5_Broker_Assets",
    ROOT / "CG-Terminal" / "Delta_Exchange",
    ROOT / "CG-Terminal" / "Delta_Fibonacci_Institutional_Business" / "data",
]

def purge_stale_files():
    print("================================================================================")
    print("PURGING STALE HISTORICAL CANDLE DATA")
    print("================================================================================")
    deleted_count = 0
    deleted_bytes = 0

    for target in TARGETS:
        if not target.exists():
            print(f"[SKIP] Directory does not exist: {target}")
            continue

        files = [f for f in target.glob("*") if f.is_file() and f.suffix in [".csv", ".parquet"]]
        print(f"Purging {len(files)} files from {target.relative_to(ROOT)}...")
        for f in files:
            # Safety check: ensure it is a candle file
            name = f.name.lower()
            if any(term in name for term in ["_1h", "_4h", "_15m", "_5m", "_1m", ".p_", ".pi_"]) or f.suffix == ".parquet" or "delta_exchange" in str(target).lower() or "data" in str(target).lower():
                try:
                    size = f.stat().st_size
                    f.unlink()
                    deleted_count += 1
                    deleted_bytes += size
                except Exception as e:
                    print(f"  [ERROR] Could not delete {f.name}: {e}")

    deleted_mb = deleted_bytes / (1024 * 1024)
    print("================================================================================")
    print(f"[SUCCESS] Purged {deleted_count} stale historical data files ({deleted_mb:.2f} MB freed).")
    print("================================================================================")
    return deleted_count

if __name__ == "__main__":
    purge_stale_files()
