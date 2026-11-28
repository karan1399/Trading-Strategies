"""Dump genuine, unaltered Binance USD-M Futures Vision data for viable MT5-mutual symbols.

Streams official https://data.binance.vision monthly/daily aggTrades, 1m klines, and 8h funding
archives directly via history_dumper.py & vision_backfill.py without synthetic alterations, then
snapshots live Blueberry MT5 contract specs & H4 spreads into data/mt5_mutual_specs.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import json
from pathlib import Path
import sys
from typing import Any

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exchange_feed.backtesting.portfolio_engine import load_asset_tfs_cached
from exchange_feed.data_pipeline.binance_dumper import DATA_DIR
from exchange_feed.data_pipeline.dump_26_prop_universe import UNIVERSE_26
from exchange_feed.data_pipeline.history_dumper import run_history_dump
from exchange_feed.data_pipeline.vision_backfill import run_backfill

MUTUAL_SPECS_PATH = DATA_DIR / "mt5_mutual_specs.json"

# Tight / borderline viable mutual MT5 <-> Binance crypto symbols not yet in UNIVERSE_26
EXTRA_MUTUAL_CRYPTO: tuple[dict[str, str], ...] = (
    {"binance": "DASHUSDT", "mt5": "DSHUSD.p", "cat": "CRYPTO", "start_ym": "2021-01"},
    {"binance": "YFIUSDT",  "mt5": "YFIUSD.p", "cat": "CRYPTO", "start_ym": "2021-01"},
    {"binance": "XLMUSDT",  "mt5": "XLMUSD.p", "cat": "CRYPTO", "start_ym": "2021-01"},
    {"binance": "VETUSDT",  "mt5": "VETUSD.p", "cat": "CRYPTO", "start_ym": "2021-01"},
    {"binance": "GRTUSDT",  "mt5": "GRTUSD.p", "cat": "CRYPTO", "start_ym": "2021-01"},
    {"binance": "NEOUSDT",  "mt5": "NEOUSD.p", "cat": "CRYPTO", "start_ym": "2021-01"},
    {"binance": "ZRXUSDT",  "mt5": "ZRXUSD.p", "cat": "CRYPTO", "start_ym": "2021-01"},
)


def snapshot_all_mutual_specs() -> dict[str, dict[str, Any]]:
    """Snapshot live Blueberry MT5 spreads, min-lots, tick values, and H4 ATR once cleanly."""
    import MetaTrader5 as mt5  # type: ignore

    # Also map DOGE/LINK aliases when MT5 carries DOGUSD.p / LNKUSD.p
    mt5_override = {"DOGEUSDT": "DOGUSD.p", "LINKUSDT": "LNKUSD.p"}
    items = [r for r in UNIVERSE_26 if r.get("mt5") or r["binance"] in mt5_override] + list(EXTRA_MUTUAL_CRYPTO)
    out: dict[str, dict[str, Any]] = {}
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        for r in items:
            bn = r["binance"]
            ms = mt5_override.get(bn, r["mt5"])
            if not ms:
                continue
            mt5.symbol_select(ms, True)
            inf = mt5.symbol_info(ms)
            tk = mt5.symbol_info_tick(ms)
            if inf is None or tk is None or tk.bid <= 0 or tk.ask < tk.bid:
                out[bn] = {**r, "mt5": ms, "mt5_available": False}
                continue
            mid = 0.5 * (tk.ask + tk.bid)
            sp_bps = round(((tk.ask - tk.bid) / mid) * 10000.0, 2)
            rates = mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H4, 0, 60)
            atr4 = float((rates["high"] - rates["low"]).mean()) if (rates is not None and len(rates) >= 10) else 0.0
            unit = (inf.trade_tick_value / inf.trade_tick_size) if inf.trade_tick_size > 0 else 0.0
            min_sl_25_usd = round((2.5 * atr4) * inf.volume_min * unit, 2) if atr4 > 0 else 0.0
            out[bn] = {
                "binance": bn,
                "mt5": ms,
                "cat": r["cat"],
                "mt5_available": True,
                "mid": round(float(mid), 6),
                "sp_bps": sp_bps,
                "vol_min": float(inf.volume_min),
                "vol_step": float(inf.volume_step),
                "tick_value": float(inf.trade_tick_value),
                "tick_size": float(inf.trade_tick_size),
                "h4_atr": round(atr4, 6),
                "min_lot_25atr_sl_usd": min_sl_25_usd,
                "swap_long": float(inf.swap_long),
                "swap_short": float(inf.swap_short),
                "updated_ts": int(datetime.now(timezone.utc).timestamp()),
            }
        MUTUAL_SPECS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    finally:
        mt5.shutdown()
    return out


def dump_extra_mutual(workers: int = 4, tail_days: int = 30, force: bool = False, skip_specs: bool = False) -> None:
    if skip_specs and MUTUAL_SPECS_PATH.is_file():
        specs = json.loads(MUTUAL_SPECS_PATH.read_text(encoding="utf-8"))
        print(f"[mutual-dump] Using cached {len(specs)} specs -> {MUTUAL_SPECS_PATH}", flush=True)
    else:
        specs = snapshot_all_mutual_specs()
    avail = [k for k, v in specs.items() if v.get("mt5_available")]
    print(f"[mutual-dump] Snapshotted {len(avail)}/{len(specs)} live MT5 specs -> {MUTUAL_SPECS_PATH}", flush=True)

    for row in EXTRA_MUTUAL_CRYPTO:
        sym = row["binance"]
        sp = specs.get(sym, {})
        if not sp.get("mt5_available"):
            print(f"[mutual-dump] SKIP {sym} ({row['mt5']}) — unavailable on MT5", flush=True)
            continue
        cache_4h = DATA_DIR / "cache" / f"{sym}_4h.parquet"
        agg_p = DATA_DIR / f"{sym}_agg_footprint.parquet"
        if not force and cache_4h.is_file() and agg_p.is_file() and cache_4h.stat().st_size > 50_000:
            print(f"[mutual-dump] {sym:10s} CACHED ({cache_4h.stat().st_size/1e6:.2f}MB)", flush=True)
            continue
        print(
            f"\n=== [mutual-dump] {sym} <-> {sp['mt5']} | sp={sp['sp_bps']}bp | 0.01_SL=${sp['min_lot_25atr_sl_usd']} ===",
            flush=True,
        )
        try:
            run_history_dump(symbol=sym, start_ym=row["start_ym"], end_ym=None, workers=workers)
            run_backfill(symbol=sym, days=tail_days, tick_bucket=None)
            g2, g4, *_ = load_asset_tfs_cached(sym, DATA_DIR, adaptive_tick=True)
            print(f"[mutual-dump] {sym} READY -> 2H={len(g2)} bars | 4H={len(g4)} bars | range={g4['ts'].iloc[0]}..{g4['end_ts'].iloc[-1]}", flush=True)
            del g2, g4
        except Exception as exc:
            import traceback
            print(f"[mutual-dump] ERROR {sym}: {exc}", flush=True)
            traceback.print_exc()
        gc.collect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tail-days", type=int, default=30)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--specs-only", action="store_true")
    ap.add_argument("--skip-specs", action="store_true")
    args = ap.parse_args()
    if args.specs_only:
        s = snapshot_all_mutual_specs()
        print("Specs saved:", len(s))
    else:
        dump_extra_mutual(workers=args.workers, tail_days=args.tail_days, force=args.force, skip_specs=args.skip_specs)
