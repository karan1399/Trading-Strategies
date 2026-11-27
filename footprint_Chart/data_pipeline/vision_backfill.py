"""Continuous 1-year Binance Vision aggTrades backfill into footprint parquet.

Downloads daily ZIPs from https://data.binance.vision (CloudFront CDN, no fapi 418 limits),
streams CSV in-memory without temp disk bloat, and aggregates exact trade counts
(last_trade_id - first_trade_id + 1), coin qty, and USD notional into 1m x tick_bucket.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import time
import urllib.request
import zipfile

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR, compute_adaptive_tick_bucket

STATUS_PATH = DATA_DIR / "backfill_status.json"
VISION_BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"


def _write_status(status: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")


def read_backfill_status() -> dict:
    if STATUS_PATH.is_file():
        try:
            return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"days_done": 0, "days_target": 365, "running": False, "last_day": None}


def aggregate_day_zip(symbol: str, date_str: str, tick_bucket: float | None = None) -> pd.DataFrame:
    """Download one day's aggTrades ZIP from Binance Vision and aggregate to 1m x tick_bucket."""
    url = f"{VISION_BASE}/{symbol}/{symbol}-aggTrades-{date_str}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "footprint_Chart/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw_bytes = resp.read()

    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as f:
            # Check header
            first_line = f.readline().decode("utf-8", errors="ignore").strip()
            has_header = first_line.startswith("agg_trade_id")
            f.seek(0)
            df = pd.read_csv(
                f,
                header=0 if has_header else None,
                names=None if has_header else [
                    "agg_trade_id", "price", "quantity", "first_trade_id",
                    "last_trade_id", "transact_time", "is_buyer_maker",
                ],
                usecols=[
                    "price", "quantity", "first_trade_id",
                    "last_trade_id", "transact_time", "is_buyer_maker",
                ],
                dtype={
                    "price": "float64",
                    "quantity": "float64",
                    "first_trade_id": "int64",
                    "last_trade_id": "int64",
                    "transact_time": "int64",
                },
            )

    if df.empty:
        return pd.DataFrame()

    df["bar_1m"] = (df["transact_time"] // 60000) * 60
    df["usd"] = df["price"] * df["quantity"]
    df["trades"] = (df["last_trade_id"] - df["first_trade_id"] + 1).clip(lower=1)
    is_maker = df["is_buyer_maker"].astype(str).str.lower().isin(["true", "1"])
    df["is_buy"] = ~is_maker
    eff_bucket = float(tick_bucket) if (tick_bucket is not None and tick_bucket > 0) else (
        5.0 if symbol.upper() == "BTCUSDT" else compute_adaptive_tick_bucket(float(df["price"].median()))
    )
    df["price_bucket"] = (df["price"] // eff_bucket) * eff_bucket

    ohlc = (
        df.groupby("bar_1m", as_index=False)
        .agg(open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"))
    )

    df["buy_trades"] = df["trades"].where(df["is_buy"], 0)
    df["sell_trades"] = df["trades"].where(~df["is_buy"], 0)
    df["buy_qty"] = df["quantity"].where(df["is_buy"], 0.0)
    df["sell_qty"] = df["quantity"].where(~df["is_buy"], 0.0)
    df["buy_usd"] = df["usd"].where(df["is_buy"], 0.0)
    df["sell_usd"] = df["usd"].where(~df["is_buy"], 0.0)

    cells = (
        df.groupby(["bar_1m", "price_bucket"], as_index=False)
        .agg(
            buy_trades=("buy_trades", "sum"),
            sell_trades=("sell_trades", "sum"),
            buy_qty=("buy_qty", "sum"),
            sell_qty=("sell_qty", "sum"),
            buy_usd=("buy_usd", "sum"),
            sell_usd=("sell_usd", "sum"),
        )
    )
    merged = cells.merge(ohlc, on="bar_1m", how="left")
    merged["symbol"] = symbol
    return merged.sort_values(["bar_1m", "price_bucket"]).reset_index(drop=True)


def run_backfill(symbol: str = "BTCUSDT", days: int = 365, tick_bucket: float | None = None) -> None:
    """Continuously backfill up to `days` days newest-first into <symbol>_agg_footprint.parquet."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{symbol}_agg_footprint.parquet"

    today_utc = datetime.now(timezone.utc).date()
    existing_days: set[str] = set()
    if out_path.is_file():
        t0 = pq.read_table(out_path, columns=["bar_1m"]).to_pandas()
        if not t0.empty:
            existing_days = set(pd.to_datetime(t0["bar_1m"], unit="s", utc=True).dt.strftime("%Y-%m-%d").unique())

    done_cnt = 0
    for offset in range(1, days + 1):
        d = (today_utc - timedelta(days=offset)).strftime("%Y-%m-%d")
        if d in existing_days and offset > 1:
            done_cnt += 1
            continue
        _write_status({"symbol": symbol, "days_done": done_cnt, "days_target": days, "running": True, "current_day": d})
        try:
            day_df = aggregate_day_zip(symbol, d, tick_bucket=tick_bucket)
            if not day_df.empty:
                # Re-read latest parquet right before write so live WS bars are never overwritten
                latest = pq.read_table(out_path).to_pandas() if out_path.is_file() else pd.DataFrame()
                if not latest.empty:
                    # Keep existing rows over older Vision duplicates so live intraday bars remain intact
                    merged = (
                        pd.concat([day_df, latest], ignore_index=True)
                        .drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last")
                        .sort_values(["bar_1m", "price_bucket"])
                        .reset_index(drop=True)
                    )
                else:
                    merged = day_df
                tmp_path = out_path.with_name(f".{symbol}_agg_footprint.tmp.{os.getpid()}.parquet")
                pq.write_table(pa.Table.from_pandas(merged, preserve_index=False), tmp_path, compression="snappy")
                os.replace(tmp_path, out_path)
                existing_days.add(d)
                done_cnt += 1
                print(f"[vision-backfill] {symbol} {d} OK | total_rows={len(merged)} | days={done_cnt}/{days}", flush=True)
        except Exception as exc:
            print(f"[vision-backfill] {symbol} {d} skip: {exc}", flush=True)
        time.sleep(0.2)

    _write_status({"symbol": symbol, "days_done": done_cnt, "days_target": days, "running": False, "last_day": (today_utc - timedelta(days=days)).strftime("%Y-%m-%d")})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--tick-bucket", type=float, default=0.0)
    args = ap.parse_args()
    run_backfill(symbol=args.symbol.upper(), days=args.days, tick_bucket=(args.tick_bucket or None))
