"""Startup gap-filler and continuous live WebSocket -> parquet persister."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import websockets

from exchange_feed.data_pipeline.vision_backfill import DATA_DIR, aggregate_day_zip

FAPI_AGG_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
_WRITE_LOCK = threading.Lock()
_LIVE_LOCK = threading.Lock()

SYNC_STATE: dict = {
    "gap_status": "idle",
    "gap_rows_added": 0,
    "ws_connected": False,
    "live_flushes": 0,
    "last_bar_utc": None,
    "live_price": None,
    "live_ts": 0,
    "live_oi": None,
    "live_funding": None,
    "live_basis_bps": None,
}

# In-memory un-flushed WS 1m cells so reads are millisecond-live between parquet flushes
_LIVE_OHLC: dict[int, list[float]] = {}
_LIVE_CELLS: dict[tuple[int, float], list[float]] = {}


def get_live_buffer_df(symbol: str = "BTCUSDT") -> pd.DataFrame:
    """Return a snapshot DataFrame of un-flushed live WS 1m footprint cells."""
    with _LIVE_LOCK:
        if not _LIVE_CELLS:
            return pd.DataFrame()
        rows = []
        for (b_ts, p_b), vals in _LIVE_CELLS.items():
            o, h, l, c = _LIVE_OHLC[b_ts]
            rows.append({
                "bar_1m": b_ts,
                "price_bucket": p_b,
                "buy_trades": int(vals[0]),
                "sell_trades": int(vals[1]),
                "buy_qty": float(vals[2]),
                "sell_qty": float(vals[3]),
                "buy_usd": float(vals[4]),
                "sell_usd": float(vals[5]),
                "open": o, "high": h, "low": l, "close": c,
                "symbol": symbol,
            })
        return pd.DataFrame(rows)


def atomic_merge_parquet(symbol: str, new_df: pd.DataFrame, data_dir: Path = DATA_DIR) -> int:
    """Thread-safe, atomic merge of new footprint rows into <symbol>_agg_footprint.parquet."""
    if new_df.empty:
        return 0
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / f"{symbol}_agg_footprint.parquet"
    tmp_path = data_dir / f".{symbol}_agg_footprint.tmp.{os.getpid()}.parquet"

    with _WRITE_LOCK:
        if out_path.is_file():
            old = pq.read_table(out_path).to_pandas()
            if not old.empty:
                merged = (
                    pd.concat([old, new_df], ignore_index=True)
                    .drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last")
                    .sort_values(["bar_1m", "price_bucket"])
                    .reset_index(drop=True)
                )
            else:
                merged = new_df.sort_values(["bar_1m", "price_bucket"]).reset_index(drop=True)
        else:
            merged = new_df.sort_values(["bar_1m", "price_bucket"]).reset_index(drop=True)

        pq.write_table(pa.Table.from_pandas(merged, preserve_index=False), tmp_path, compression="snappy")
        for attempt in range(5):
            try:
                os.replace(tmp_path, out_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.15)
        max_ts = int(merged["bar_1m"].max())
        SYNC_STATE["last_bar_utc"] = datetime.fromtimestamp(max_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return len(merged)


def _agg_trades_to_df(rows: list[dict], symbol: str, end_ms: int, tick_bucket: float) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[df["T"].astype("int64") <= end_ms].copy()
    if df.empty:
        return pd.DataFrame()
    df["ts_ms"] = df["T"].astype("int64")
    df["bar_1m"] = (df["ts_ms"] // 60000) * 60
    df["price"] = df["p"].astype("float64")
    df["qty"] = df["q"].astype("float64")
    df["usd"] = df["price"] * df["qty"]
    df["trades"] = (df["l"].astype("int64") - df["f"].astype("int64") + 1).clip(lower=1)
    df["is_buy"] = ~df["m"].astype(bool)
    df["price_bucket"] = (df["price"] // tick_bucket) * tick_bucket

    ohlc = df.sort_values("ts_ms").groupby("bar_1m", as_index=False).agg(
        open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last")
    )
    df["buy_trades"] = df["trades"].where(df["is_buy"], 0)
    df["sell_trades"] = df["trades"].where(~df["is_buy"], 0)
    df["buy_qty"] = df["qty"].where(df["is_buy"], 0.0)
    df["sell_qty"] = df["qty"].where(~df["is_buy"], 0.0)
    df["buy_usd"] = df["usd"].where(df["is_buy"], 0.0)
    df["sell_usd"] = df["usd"].where(~df["is_buy"], 0.0)

    cells = df.groupby(["bar_1m", "price_bucket"], as_index=False).agg(
        buy_trades=("buy_trades", "sum"), sell_trades=("sell_trades", "sum"),
        buy_qty=("buy_qty", "sum"), sell_qty=("sell_qty", "sum"),
        buy_usd=("buy_usd", "sum"), sell_usd=("sell_usd", "sum"),
    )
    merged = cells.merge(ohlc, on="bar_1m", how="left")
    merged["symbol"] = symbol
    return merged.sort_values(["bar_1m", "price_bucket"]).reset_index(drop=True)


def _fetch_fapi_gap(symbol: str, start_ms: int, end_ms: int, tick_bucket: float = 5.0, max_requests: int = 350, data_dir: Path = DATA_DIR) -> int:
    """Rate-safe fetch of missing intraday aggTrades from Binance Futures, flushing incrementally."""
    if start_ms >= end_ms - 30_000:
        return 0
    q0 = urllib.parse.urlencode({"symbol": symbol, "startTime": start_ms, "limit": 1000})
    batch = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{FAPI_AGG_URL}?{q0}", headers={"User-Agent": "exchange_feed/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            if "429" in str(exc) or "418" in str(exc):
                time.sleep(8.0 * (attempt + 1))
            else:
                print(f"[live-sync] fapi gap fetch paused ({exc}); live WS will continue.", flush=True)
                return 0
    if not batch:
        return 0

    rows = list(batch)
    last_id = int(batch[-1]["a"])
    last_ts = int(batch[-1]["T"])
    total_added = 0

    for step in range(max_requests - 1):
        if last_ts >= end_ms:
            break
        time.sleep(0.55)  # ~1.8 req/s (< 2400 weight/min limit, prevents 429)
        q = urllib.parse.urlencode({"symbol": symbol, "fromId": last_id + 1, "limit": 1000})
        nxt = None
        for attempt in range(3):
            try:
                r2 = urllib.request.Request(f"{FAPI_AGG_URL}?{q}", headers={"User-Agent": "exchange_feed/1.0"})
                with urllib.request.urlopen(r2, timeout=10) as resp:
                    nxt = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:
                if "429" in str(exc) or "418" in str(exc):
                    time.sleep(8.0 * (attempt + 1))
                else:
                    break
        if not nxt:
            break
        rows.extend(nxt)
        new_id = int(nxt[-1]["a"])
        last_ts = int(nxt[-1]["T"])
        if len(rows) >= 25_000:
            chunk_df = _agg_trades_to_df(rows, symbol, end_ms, tick_bucket)
            if not chunk_df.empty:
                atomic_merge_parquet(symbol, chunk_df, data_dir=data_dir)
                total_added += len(chunk_df)
            rows = []
        if new_id <= last_id or len(nxt) < 1000:
            break
        last_id = new_id

    if rows:
        chunk_df = _agg_trades_to_df(rows, symbol, end_ms, tick_bucket)
        if not chunk_df.empty:
            atomic_merge_parquet(symbol, chunk_df, data_dir=data_dir)
            total_added += len(chunk_df)
    return total_added


def fill_startup_gap(symbol: str = "BTCUSDT", tick_bucket: float = 5.0, data_dir: Path = DATA_DIR) -> int:
    """Fill ALL gaps between historical Vision days / today's missing bars and current live UTC time."""
    SYNC_STATE["gap_status"] = "checking"
    out_path = data_dir / f"{symbol}_agg_footprint.parquet"
    now_utc = datetime.now(timezone.utc)
    now_ms = int(now_utc.timestamp() * 1000)

    existing_ts: list[int] = []
    if out_path.is_file():
        tbl = pq.read_table(out_path, columns=["bar_1m"]).to_pandas()
        if not tbl.empty:
            existing_ts = sorted(int(x) for x in tbl["bar_1m"].unique())

    max_bar_sec = existing_ts[-1] if existing_ts else int((now_utc - timedelta(days=1)).timestamp())
    added_rows = 0

    # 1. Fill any missing full UTC days up to yesterday via Vision CDN
    last_dt = datetime.fromtimestamp(max_bar_sec, tz=timezone.utc).date()
    yesterday = (now_utc - timedelta(days=1)).date()
    cur_d = last_dt
    while cur_d <= yesterday:
        d_str = cur_d.strftime("%Y-%m-%d")
        try:
            day_df = aggregate_day_zip(symbol, d_str, tick_bucket=tick_bucket)
            if not day_df.empty:
                atomic_merge_parquet(symbol, day_df, data_dir=data_dir)
                added_rows += len(day_df)
                print(f"[live-sync] gap Vision day {d_str} merged (+{len(day_df)} rows)", flush=True)
        except Exception:
            pass
        cur_d += timedelta(days=1)

    # 2. Fill recent intraday tail gap up to now FIRST so live signals have immediate continuity
    tail_start_ms = max(max_bar_sec * 1000, int(datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp()) * 1000)
    if now_ms - tail_start_ms > 90_000:
        tail_added = _fetch_fapi_gap(symbol, tail_start_ms, now_ms, tick_bucket=tick_bucket, max_requests=400, data_dir=data_dir)
        added_rows += tail_added
        if tail_added:
            print(f"[live-sync] intraday tail gap merged (+{tail_added} rows)", flush=True)

    # 3. Also repair any missing internal gaps today (e.g. 00:00 UTC -> earliest today bar)
    today_0_sec = int(datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp())
    today_ts = [x for x in existing_ts if x >= today_0_sec]
    missing_windows: list[tuple[int, int]] = []
    if not today_ts:
        missing_windows.append((today_0_sec * 1000, tail_start_ms))
    else:
        if today_ts[0] - today_0_sec > 120:
            missing_windows.append((today_0_sec * 1000, today_ts[0] * 1000))
        for i in range(len(today_ts) - 1):
            if today_ts[i + 1] - today_ts[i] > 120:
                missing_windows.append(((today_ts[i] + 60) * 1000, today_ts[i + 1] * 1000))

    for g_s, g_e in missing_windows:
        if g_e > g_s + 60_000:
            mid_added = _fetch_fapi_gap(symbol, g_s, g_e, tick_bucket=tick_bucket, max_requests=500, data_dir=data_dir)
            added_rows += mid_added
            if mid_added:
                print(f"[live-sync] intraday internal gap merged (+{mid_added} rows)", flush=True)

    SYNC_STATE["gap_status"] = "done"
    SYNC_STATE["gap_rows_added"] = added_rows
    return added_rows


async def run_live_ws_persister(symbol: str = "BTCUSDT", tick_bucket: float = 5.0, data_dir: Path = DATA_DIR) -> None:
    """Continuously stream wss://fstream.binance.com aggTrade and flush 1m cells to parquet every 15s."""
    url = f"wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade"
    ohlc_buf: dict[int, list[float]] = {}  # bar_1m -> [o, h, l, c]
    cell_buf: dict[tuple[int, float], list[float]] = {}  # (bar_1m, p_lvl) -> [bt, st, bq, sq, bu, su]
    last_flush = time.time()

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                SYNC_STATE["ws_connected"] = True
                print(f"[live-sync] WS connected: {url}", flush=True)
                async for raw in ws:
                    msg = json.loads(raw)
                    px = float(msg["p"])
                    qty = float(msg["q"])
                    usd = px * qty
                    trades = int(msg["l"]) - int(msg["f"]) + 1
                    is_buy = not bool(msg["m"])
                    SYNC_STATE["live_price"] = px
                    SYNC_STATE["live_ts"] = int(time.time() * 1000)
                    bar_1m = (int(msg["T"]) // 60000) * 60
                    p_lvl = float((px // tick_bucket) * tick_bucket)

                    with _LIVE_LOCK:
                        if bar_1m not in _LIVE_OHLC:
                            _LIVE_OHLC[bar_1m] = [px, px, px, px]
                        else:
                            ob = _LIVE_OHLC[bar_1m]
                            ob[1] = max(ob[1], px)
                            ob[2] = min(ob[2], px)
                            ob[3] = px

                        key = (bar_1m, p_lvl)
                        if key not in _LIVE_CELLS:
                            _LIVE_CELLS[key] = [0, 0, 0.0, 0.0, 0.0, 0.0]
                        cb = _LIVE_CELLS[key]
                        if is_buy:
                            cb[0] += trades
                            cb[2] += qty
                            cb[4] += usd
                        else:
                            cb[1] += trades
                            cb[3] += qty
                            cb[5] += usd

                    if time.time() - last_flush >= 15.0:
                        df_flush = get_live_buffer_df(symbol)
                        if not df_flush.empty:
                            atomic_merge_parquet(symbol, df_flush, data_dir=data_dir)
                            SYNC_STATE["live_flushes"] += 1
                            last_flush = time.time()
                            cur_bar = (int(time.time()) // 60) * 60
                            with _LIVE_LOCK:
                                for b in [b for b in _LIVE_OHLC if b < cur_bar - 120]:
                                    _LIVE_OHLC.pop(b, None)
                                for k in [k for k in _LIVE_CELLS if k[0] < cur_bar - 120]:
                                    _LIVE_CELLS.pop(k, None)
        except asyncio.CancelledError:
            SYNC_STATE["ws_connected"] = False
            raise
        except Exception as exc:
            SYNC_STATE["ws_connected"] = False
            print(f"[live-sync] WS reconnect in 3s ({exc})", flush=True)
            await asyncio.sleep(3.0)


async def run_live_metrics_and_materializer(symbol: str = "BTCUSDT", data_dir: Path = DATA_DIR) -> None:
    """Continuously poll live OI/Funding/Basis (10s) and append metrics + 15m/Vol/Range parquets (60s)."""
    from exchange_feed.data_pipeline.metrics_store import (
        fetch_live_derivatives_snapshot,
        materialize_bar_parquets,
        sync_binance_metrics,
    )

    last_materialize = 0.0
    while True:
        try:
            snap = await asyncio.to_thread(fetch_live_derivatives_snapshot, symbol)
            if snap.get("oi_btc") is not None:
                SYNC_STATE["live_oi"] = snap["oi_btc"]
            if snap.get("funding_rate") is not None:
                SYNC_STATE["live_funding"] = round(float(snap["funding_rate"]) * 100.0, 4)
            if snap.get("basis_bps") is not None:
                SYNC_STATE["live_basis_bps"] = snap["basis_bps"]

            now = time.time()
            if now - last_materialize >= 60.0:
                await asyncio.to_thread(sync_binance_metrics, symbol, 3, data_dir)
                counts = await asyncio.to_thread(materialize_bar_parquets, symbol, None, data_dir)
                SYNC_STATE["materialized"] = counts
                last_materialize = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[live-sync] metrics loop warning: {exc}", flush=True)
        await asyncio.sleep(10.0)
