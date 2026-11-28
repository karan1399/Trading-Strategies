"""MT5 x Binance Futures Core 12 intersection registry, live MT5 cost snapshot, and multi-asset backfill runner."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from exchange_feed.data_pipeline.binance_dumper import DATA_DIR, dump_binance_footprint
from exchange_feed.data_pipeline.history_dumper import compute_adaptive_tick_bucket, run_history_dump
from exchange_feed.data_pipeline.vision_backfill import run_backfill

MT5_SPECS_PATH = DATA_DIR / "mt5_contract_specs.json"

# Empirically vetted Option-A intersection (MT5 #5126976 x Binance USD-M Futures)
# Sorted by Binance 24h liquidity; excludes high-spread / huge-min-lot unviable contracts.
CORE_12_ASSETS: tuple[dict[str, Any], ...] = (
    {"binance": "BTCUSDT",  "mt5": "BTCUSD.pi", "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 2.7,  "sw_l_bps": -4.25, "sw_s_bps": -0.42, "roll3d": 5, "min_notional": 603.9},
    {"binance": "ETHUSDT",  "mt5": "ETHUSD.pi", "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 17.8, "sw_l_bps": -0.27, "sw_s_bps": -0.12, "roll3d": 5, "min_notional": 16.9},
    {"binance": "XAUUSDT",  "mt5": "XAUUSD.pi", "cat": "METAL",  "start_ym": "2025-12", "sp_bps": 0.3,  "sw_l_bps": -1.82, "sw_s_bps": +0.61, "roll3d": 3, "min_notional": 3568.0},
    {"binance": "ZECUSDT",  "mt5": "ZECUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 7.4,  "sw_l_bps": -0.29, "sw_s_bps": -0.28, "roll3d": 5, "min_notional": 701.4},
    {"binance": "XAGUSDT",  "mt5": "XAGUSD.pi", "cat": "METAL",  "start_ym": "2026-01", "sp_bps": 14.0, "sw_l_bps": -2.04, "sw_s_bps": +0.46, "roll3d": 3, "min_notional": 2075.2},
    {"binance": "SOLUSDT",  "mt5": "SOLUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 31.1, "sw_l_bps": -2.22, "sw_s_bps": -1.37, "roll3d": 5, "min_notional": 70.7},
    {"binance": "BNBUSDT",  "mt5": "BNBUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 58.3, "sw_l_bps": -0.16, "sw_s_bps": -0.04, "roll3d": 5, "min_notional": 5.5},
    {"binance": "XMRUSDT",  "mt5": "XMRUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 5.0,  "sw_l_bps": -4.43, "sw_s_bps": -1.10, "roll3d": 5, "min_notional": 443.8},
    {"binance": "UNIUSDT",  "mt5": "UNIUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 50.3, "sw_l_bps": -0.21, "sw_s_bps": -0.14, "roll3d": 5, "min_notional": 45.6},
    {"binance": "AAVEUSDT", "mt5": "AVEUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 29.1, "sw_l_bps": -3.28, "sw_s_bps": -1.09, "roll3d": 5, "min_notional": 93.0},
    {"binance": "AVAXUSDT", "mt5": "AVXUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 45.6, "sw_l_bps": -0.41, "sw_s_bps": -0.23, "roll3d": 5, "min_notional": 6.6},
    {"binance": "BCHUSDT",  "mt5": "BCHUSD.p",  "cat": "CRYPTO", "start_ym": "2021-01", "sp_bps": 23.1, "sw_l_bps": -1.99, "sw_s_bps": -1.12, "roll3d": 5, "min_notional": 207.6},
)

ASSET_BY_BINANCE: dict[str, dict[str, Any]] = {a["binance"]: a for a in CORE_12_ASSETS}


def snapshot_mt5_contract_specs(data_dir: Path = DATA_DIR, export_metals_h4: bool = True) -> dict[str, dict[str, Any]]:
    """Snapshot live Blueberry MT5 spreads, swap rates, and min-lot contract constraints."""
    specs: dict[str, dict[str, Any]] = {a["binance"]: dict(a) for a in CORE_12_ASSETS}
    try:
        import MetaTrader5 as mt5
    except Exception:
        return specs

    if not mt5.initialize():
        return specs

    try:
        for row in CORE_12_ASSETS:
            bn = row["binance"]
            ms = row["mt5"]
            s = mt5.symbol_info(ms)
            tk = mt5.symbol_info_tick(ms)
            if not s:
                continue
            px = tk.bid if (tk and tk.bid > 0) else 0.0
            ask = tk.ask if (tk and tk.ask > 0) else px
            sp_px = max(0.0, ask - px) if (px > 0 and ask >= px) else (s.spread * s.point)
            sp_bps = (sp_px / px * 10000.0) if px > 0 else float(row["sp_bps"])
            if s.swap_mode == 1 and px > 0:
                sw_l_bps = float(s.swap_long) * float(s.point) / px * 10000.0
                sw_s_bps = float(s.swap_short) * float(s.point) / px * 10000.0
            elif s.swap_mode == 3:
                sw_l_bps = float(s.swap_long) / 365.0 * 100.0
                sw_s_bps = float(s.swap_short) / 365.0 * 100.0
            else:
                sw_l_bps = float(row["sw_l_bps"])
                sw_s_bps = float(row["sw_s_bps"])
            min_notional = px * float(s.trade_contract_size) * float(s.volume_min) if px > 0 else float(row["min_notional"])
            specs[bn] = {
                **row,
                "px": round(px, 6),
                "sp_bps": round(sp_bps, 2),
                "sw_l_bps": round(sw_l_bps, 3),
                "sw_s_bps": round(sw_s_bps, 3),
                "roll3d": int(s.swap_rollover3days),
                "swap_mode": int(s.swap_mode),
                "contract_size": float(s.trade_contract_size),
                "volume_min": float(s.volume_min),
                "volume_step": float(s.volume_step),
                "min_notional": round(min_notional, 2),
                "min_sp_usd": round(sp_px * float(s.trade_contract_size) * float(s.volume_min), 3),
                "updated_ts": int(datetime.now(timezone.utc).timestamp()),
            }
            if export_metals_h4 and row["cat"] == "METAL":
                rates = mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H4, 0, 15000)
                if rates is not None and len(rates) > 0:
                    df_m = pd.DataFrame(rates)
                    df_m = df_m.rename(columns={"time": "ts", "open": "o", "high": "h", "low": "l", "close": "c", "tick_volume": "v"})
                    df_m = df_m[["ts", "o", "h", "l", "c", "v"]].sort_values("ts").reset_index(drop=True)
                    data_dir.mkdir(parents=True, exist_ok=True)
                    pq.write_table(pa.Table.from_pandas(df_m, preserve_index=False), data_dir / f"{bn}_mt5_h4.parquet", compression="snappy")
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "mt5_contract_specs.json").write_text(json.dumps(specs, indent=2), encoding="utf-8")
    finally:
        mt5.shutdown()
    return specs


def load_mt5_contract_specs(data_dir: Path = DATA_DIR) -> dict[str, dict[str, Any]]:
    path = data_dir / "mt5_contract_specs.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {a["binance"]: dict(a) for a in CORE_12_ASSETS}


def dump_universe(
    symbols: list[str] | None = None,
    workers: int = 3,
    tail_days: int = 32,
    include_btc: bool = False,
) -> dict[str, str]:
    """Dump closed monthly history (through latest closed month) + current-month daily + live tail."""
    snapshot_mt5_contract_specs(DATA_DIR, export_metals_h4=True)
    targets = [a for a in CORE_12_ASSETS if (include_btc or a["binance"] != "BTCUSDT")]
    if symbols:
        wanted = {s.upper() for s in symbols}
        targets = [a for a in CORE_12_ASSETS if a["binance"] in wanted]

    summary: dict[str, str] = {}
    for row in targets:
        sym = row["binance"]
        try:
            print(f"\n=== [mt5-universe] Dumping {sym} (MT5 {row['mt5']} | {row['cat']} | start={row['start_ym']}) ===", flush=True)
            run_history_dump(symbol=sym, start_ym=row["start_ym"], end_ym=None, workers=workers)
            run_backfill(symbol=sym, days=tail_days, tick_bucket=None)
            try:
                dump_binance_footprint(symbol=sym, hours=24.0, max_requests=250, tick_bucket=None)
            except Exception as tail_exc:
                print(f"[mt5-universe] {sym} REST tail warn: {tail_exc}", flush=True)
            summary[sym] = "OK"
        except Exception as exc:
            print(f"[mt5-universe] {sym} FAILED: {exc}", flush=True)
            summary[sym] = f"ERR: {exc}"
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="Comma-separated Binance symbols (default: 11 new Core assets)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--tail-days", type=int, default=32)
    ap.add_argument("--include-btc", action="store_true")
    ap.add_argument("--specs-only", action="store_true")
    args = ap.parse_args()
    if args.specs_only:
        sp = snapshot_mt5_contract_specs(DATA_DIR, export_metals_h4=True)
        print("Snapshotted MT5 specs for", len(sp), "assets ->", MT5_SPECS_PATH)
    else:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None
        res = dump_universe(symbols=syms, workers=args.workers, tail_days=args.tail_days, include_btc=args.include_btc)
        print("Universe dump summary:", res)
