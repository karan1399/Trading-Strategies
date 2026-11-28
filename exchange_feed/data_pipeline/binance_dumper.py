"""Binance Futures aggTrades historical dumper -> fine-grained footprint parquet.

Uses Binance Futures public REST API (/fapi/v1/aggTrades):
  - m == False -> buyer is taker (Ask / Buy side)
  - m == True  -> seller is taker (Bid / Sell side)
  - trade_count = (l - f + 1) exact individual trades per aggTrade
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FAPI_AGG_URL = "https://fapi.binance.com/fapi/v1/aggTrades"


def compute_adaptive_tick_bucket(ref_price: float, bps: float = 0.65) -> float:
    """Choose a scale-invariant price ladder step (~0.65 bps) rounded cleanly to {1,2,2.5,5,10}*10^k."""
    if not (ref_price > 0.0 and math.isfinite(ref_price)):
        return 1.0
    raw = max(1e-6, ref_price * bps / 10000.0)
    t = 10.0 ** math.floor(math.log10(raw))
    s = raw / t
    mult = 1.0 if s <= 1.0 else (2.0 if s <= 2.0 else (2.5 if s <= 2.5 else (5.0 if s <= 5.0 else 10.0)))
    return float(round(mult * t, 10))


def _fetch_json(url: str, timeout: float = 10.0) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "exchange_feed/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_agg_trades(
    symbol: str = "BTCUSDT",
    hours: float = 3.0,
    max_requests: int = 120,
    tick_bucket: float | None = None,
) -> pd.DataFrame:
    """Fetch recent historical aggTrades from Binance Futures and aggregate to 1m x tick_bucket."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(hours * 3600 * 1000)

    # First request anchored at startTime to get initial aggTradeId
    q0 = urllib.parse.urlencode({"symbol": symbol, "startTime": start_ms, "limit": 1000})
    batch = _fetch_json(f"{FAPI_AGG_URL}?{q0}")
    if not batch:
        return pd.DataFrame()

    rows: list[dict] = list(batch)
    last_id = int(batch[-1]["a"])
    last_ts = int(batch[-1]["T"])

    for _ in range(max_requests - 1):
        if last_ts >= end_ms:
            break
        q = urllib.parse.urlencode({"symbol": symbol, "fromId": last_id + 1, "limit": 1000})
        try:
            nxt = _fetch_json(f"{FAPI_AGG_URL}?{q}")
        except Exception:
            time.sleep(0.4)
            continue
        if not nxt:
            break
        rows.extend(nxt)
        new_last_id = int(nxt[-1]["a"])
        last_ts = int(nxt[-1]["T"])
        if new_last_id <= last_id or len(nxt) < 1000:
            break
        last_id = new_last_id
        time.sleep(0.04)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df[df["T"].astype("int64") <= end_ms].copy()
    df["ts_ms"] = df["T"].astype("int64")
    df["bar_1m"] = (df["ts_ms"] // 60000) * 60
    df["price"] = df["p"].astype("float64")
    df["qty"] = df["q"].astype("float64")
    df["usd"] = df["price"] * df["qty"]
    df["trades"] = (df["l"].astype("int64") - df["f"].astype("int64") + 1).clip(lower=1)
    df["is_buy"] = ~df["m"].astype(bool)  # m=False => buyer is taker
    eff_bucket = float(tick_bucket) if (tick_bucket is not None and tick_bucket > 0) else (
        5.0 if symbol.upper() == "BTCUSDT" else compute_adaptive_tick_bucket(float(df["price"].median()))
    )
    df["price_bucket"] = (df["price"] // eff_bucket) * eff_bucket

    # Per 1m OHLC
    ohlc = (
        df.sort_values("ts_ms")
        .groupby("bar_1m", as_index=False)
        .agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
        )
    )

    df["buy_trades"] = df["trades"].where(df["is_buy"], 0)
    df["sell_trades"] = df["trades"].where(~df["is_buy"], 0)
    df["buy_qty"] = df["qty"].where(df["is_buy"], 0.0)
    df["sell_qty"] = df["qty"].where(~df["is_buy"], 0.0)
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


def dump_binance_footprint(
    symbol: str = "BTCUSDT",
    hours: float = 3.0,
    max_requests: int = 120,
    tick_bucket: float | None = None,
) -> Path:
    """Dump Binance aggTrades footprint parquet to exchange_feed/data/."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{symbol}_agg_footprint.parquet"
    df = fetch_agg_trades(
        symbol=symbol,
        hours=hours,
        max_requests=max_requests,
        tick_bucket=tick_bucket,
    )
    if df.empty:
        raise RuntimeError(f"No aggTrades returned from Binance for {symbol}")

    # Merge with existing parquet if present so history accumulates cleanly
    if out_path.is_file():
        old = pq.read_table(out_path).to_pandas()
        if not old.empty:
            df = (
                pd.concat([old, df], ignore_index=True)
                .drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last")
                .sort_values(["bar_1m", "price_bucket"])
                .reset_index(drop=True)
            )

    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, out_path, compression="snappy")
    return out_path


if __name__ == "__main__":
    p = dump_binance_footprint("BTCUSDT", hours=3.0, max_requests=120, tick_bucket=5.0)
    tbl = pq.read_table(p).to_pandas()
    print(
        f"Dumped {p} | rows={len(tbl)} | 1m_bars={tbl['bar_1m'].nunique()} | "
        f"total_trades={int(tbl['buy_trades'].sum() + tbl['sell_trades'].sum())}"
    )
