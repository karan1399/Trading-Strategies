"""Multi-year Binance Vision historical dumper (2021+): fundingRate, 1m klines, and monthly aggTrades footprint."""

from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import gc
import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR, compute_adaptive_tick_bucket

MONTHLY_DIR = DATA_DIR / "monthly"
HIST_STATUS_PATH = DATA_DIR / "history_backfill_status.json"
VISION_ROOT = "https://data.binance.vision/data/futures/um/monthly"


def _month_list(start_ym: str = "2021-01", end_ym: str | None = None) -> list[str]:
    if end_ym is None:
        now = datetime.now(timezone.utc)
        # Monthly zip for current month isn't published until month closes
        y, m = now.year, now.month - 1
        if m == 0:
            y -= 1
            m = 12
        end_ym = f"{y}-{m:02d}"
    sy, sm = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _download_bytes(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "footprint_Chart/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _download_file(url: str, dst: Path, timeout: float = 120.0, retries: int = 5) -> None:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "footprint_Chart/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(dst, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            return
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (400, 404):
                raise FileNotFoundError(f"HTTP {exc.code} for {url}") from exc
        except Exception as exc:
            last_exc = exc
        if dst.is_file():
            try:
                dst.unlink()
            except OSError:
                pass
        if attempt + 1 < retries:
            wait_s = min(60.0, 5.0 * (2 ** attempt))
            print(f"[hist-dump] retry {attempt+1}/{retries} in {wait_s:.0f}s for {dst.name}: {last_exc}", flush=True)
            time.sleep(wait_s)
    raise RuntimeError(f"Failed download after {retries} attempts: {last_exc}")


def dump_funding_history(symbol: str = "BTCUSDT", start_ym: str = "2021-01", data_dir: Path = DATA_DIR) -> Path:
    """Download all available monthly 8h funding rate files from Binance Vision."""
    sym = symbol.upper()
    months = _month_list(start_ym)
    # Also try current open month just in case
    cur_ym = datetime.now(timezone.utc).strftime("%Y-%m")
    if cur_ym not in months:
        months.append(cur_ym)

    def _one(ym: str) -> pd.DataFrame | None:
        url = f"{VISION_ROOT}/fundingRate/{sym}/{sym}-fundingRate-{ym}.zip"
        try:
            raw = _download_bytes(url, timeout=15.0)
        except Exception:
            return None
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            with zf.open(zf.namelist()[0]) as f:
                first = f.readline().decode("utf-8", errors="ignore").lower()
                has_hdr = "calc_time" in first or "funding" in first
                f.seek(0)
                df = pd.read_csv(
                    f,
                    header=0 if has_hdr else None,
                    names=None if has_hdr else ["calc_time", "funding_interval_hours", "last_funding_rate"],
                )
        df.columns = [str(c).strip() for c in df.columns]
        ts_col = "calc_time" if "calc_time" in df.columns else df.columns[0]
        rate_col = "last_funding_rate" if "last_funding_rate" in df.columns else df.columns[-1]
        out = pd.DataFrame({
            "ts": (df[ts_col].astype("int64") // 1000).astype("int64"),
            "funding_rate": df[rate_col].astype("float64"),
        })
        return out

    frames: list[pd.DataFrame] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(_one, months):
            if res is not None and not res.empty:
                frames.append(res)

    if not frames:
        raise RuntimeError(f"No fundingRate files fetched for {sym}")

    full = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["ts"], keep="last")
        .sort_values("ts")
        .reset_index(drop=True)
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / f"{sym}_funding_8h.parquet"
    tmp_path = out_path.with_name(f".{sym}_funding_8h.tmp.{os.getpid()}.parquet")
    pq.write_table(pa.Table.from_pandas(full, preserve_index=False), tmp_path, compression="snappy")
    os.replace(tmp_path, out_path)
    return out_path


def dump_klines_1m_history(symbol: str = "BTCUSDT", start_ym: str = "2021-01", data_dir: Path = DATA_DIR) -> Path:
    """Download all available monthly 1m klines (with taker buy volume) from Binance Vision."""
    sym = symbol.upper()
    months = _month_list(start_ym)

    def _one(ym: str) -> pd.DataFrame | None:
        url = f"{VISION_ROOT}/klines/{sym}/1m/{sym}-1m-{ym}.zip"
        try:
            raw = _download_bytes(url, timeout=25.0)
        except Exception:
            return None
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            with zf.open(zf.namelist()[0]) as f:
                first = f.readline().decode("utf-8", errors="ignore").lower()
                has_hdr = "open_time" in first
                f.seek(0)
                df = pd.read_csv(
                    f,
                    header=0 if has_hdr else None,
                    names=None if has_hdr else [
                        "open_time", "open", "high", "low", "close", "volume",
                        "close_time", "quote_volume", "count", "taker_buy_volume",
                        "taker_buy_quote_volume", "ignore",
                    ],
                    usecols=["open_time", "open", "high", "low", "close", "volume", "count", "taker_buy_volume"],
                )
        out = pd.DataFrame({
            "bar_1m": (df["open_time"].astype("int64") // 1000).astype("int64"),
            "open": df["open"].astype("float64"),
            "high": df["high"].astype("float64"),
            "low": df["low"].astype("float64"),
            "close": df["close"].astype("float64"),
            "volume": df["volume"].astype("float64"),
            "count": df["count"].astype("int64"),
            "taker_buy_vol": df["taker_buy_volume"].astype("float64"),
        })
        return out

    frames: list[pd.DataFrame] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(_one, months):
            if res is not None and not res.empty:
                frames.append(res)

    if not frames:
        raise RuntimeError(f"No 1m klines fetched for {sym}")

    full = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["bar_1m"], keep="last")
        .sort_values("bar_1m")
        .reset_index(drop=True)
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / f"{sym}_klines_1m.parquet"
    tmp_path = out_path.with_name(f".{sym}_klines_1m.tmp.{os.getpid()}.parquet")
    pq.write_table(pa.Table.from_pandas(full, preserve_index=False), tmp_path, compression="snappy")
    os.replace(tmp_path, out_path)
    return out_path


def aggregate_month_aggtrades(
    symbol: str,
    ym: str,
    out_dir: Path = MONTHLY_DIR,
    tick_bucket: float | None = None,
) -> Path:
    """Download one monthly aggTrades ZIP to temp file, stream-parse in chunks, and write monthly footprint parquet."""
    sym = symbol.upper()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{sym}_{ym}.parquet"
    if out_path.is_file() and out_path.stat().st_size > 100_000:
        return out_path

    if tick_bucket is None and sym == "BTCUSDT":
        yr = int(ym.split("-")[0])
        tick_bucket = 2.5 if yr <= 2023 else 5.0

    url = f"{VISION_ROOT}/aggTrades/{sym}/{sym}-aggTrades-{ym}.zip"
    tmp_zip = out_dir / f".tmp_{sym}_{ym}_{os.getpid()}.zip"
    t0 = time.time()
    try:
        try:
            _download_file(url, tmp_zip, timeout=300.0)
        except FileNotFoundError:
            return out_path
        dl_sec = time.time() - t0
        zip_mb = tmp_zip.stat().st_size / 1e6

        cell_chunks: list[pd.DataFrame] = []
        ohlc_chunks: list[pd.DataFrame] = []
        eff_bucket = float(tick_bucket) if tick_bucket is not None else 0.0

        with zipfile.ZipFile(tmp_zip) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                first_line = f.readline().decode("utf-8", errors="ignore").strip()
                has_header = first_line.startswith("agg_trade_id")
                f.seek(0)
                reader = pd.read_csv(
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
                    chunksize=1_500_000,
                )
                for chunk in reader:
                    if chunk.empty:
                        continue
                    bar_1m = (chunk["transact_time"].to_numpy(dtype=np.int64) // 60000) * 60
                    px = chunk["price"].to_numpy(dtype=np.float64)
                    if eff_bucket <= 0.0:
                        eff_bucket = compute_adaptive_tick_bucket(float(np.median(px)))
                    qty = chunk["quantity"].to_numpy(dtype=np.float64)
                    usd = px * qty
                    trades = np.maximum(1, chunk["last_trade_id"].to_numpy(dtype=np.int64) - chunk["first_trade_id"].to_numpy(dtype=np.int64) + 1)
                    ibm_col = chunk["is_buyer_maker"]
                    if ibm_col.dtype == bool:
                        is_buy = ~ibm_col.to_numpy(dtype=bool)
                    else:
                        is_buy = ~ibm_col.astype(str).str.lower().isin(["true", "1"]).to_numpy(dtype=bool)
                    pb = np.floor(px / eff_bucket) * eff_bucket

                    df_o = pd.DataFrame({"bar_1m": bar_1m, "price": px}).groupby("bar_1m", as_index=False).agg(
                        open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last")
                    )
                    ohlc_chunks.append(df_o)

                    df_c = pd.DataFrame({
                        "bar_1m": bar_1m,
                        "price_bucket": pb,
                        "buy_trades": np.where(is_buy, trades, 0),
                        "sell_trades": np.where(~is_buy, trades, 0),
                        "buy_qty": np.where(is_buy, qty, 0.0),
                        "sell_qty": np.where(~is_buy, qty, 0.0),
                        "buy_usd": np.where(is_buy, usd, 0.0),
                        "sell_usd": np.where(~is_buy, usd, 0.0),
                    }).groupby(["bar_1m", "price_bucket"], as_index=False).sum()
                    cell_chunks.append(df_c)
                    del chunk, df_o, df_c

        if not cell_chunks:
            raise RuntimeError(f"Empty aggTrades month {ym}")

        ohlc_all = pd.concat(ohlc_chunks, ignore_index=True).groupby("bar_1m", as_index=False).agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last")
        )
        cells_all = pd.concat(cell_chunks, ignore_index=True).groupby(["bar_1m", "price_bucket"], as_index=False).sum()
        merged = cells_all.merge(ohlc_all, on="bar_1m", how="left")
        merged["symbol"] = sym
        merged = merged.sort_values(["bar_1m", "price_bucket"]).reset_index(drop=True)

        tmp_parq = out_path.with_name(f".{sym}_{ym}.tmp.{os.getpid()}.parquet")
        pq.write_table(pa.Table.from_pandas(merged, preserve_index=False), tmp_parq, compression="snappy")
        os.replace(tmp_parq, out_path)
        tot_sec = time.time() - t0
        print(f"[hist-dump] {sym} {ym} OK | zip={zip_mb:.1f}MB dl={dl_sec:.1f}s tot={tot_sec:.1f}s | rows={len(merged):,} bars={ohlc_all['bar_1m'].nunique():,}", flush=True)
        del ohlc_chunks, cell_chunks, ohlc_all, cells_all, merged
        pa.default_memory_pool().release_unused()
        gc.collect()
        return out_path
    finally:
        if tmp_zip.is_file():
            try:
                tmp_zip.unlink()
            except OSError:
                pass


def run_history_dump(symbol: str = "BTCUSDT", start_ym: str = "2021-01", end_ym: str | None = None, workers: int = 3) -> None:
    sym = symbol.upper()
    print(f"[hist-dump] Dumping 8h funding & 1m klines for {sym} ({start_ym}..present)...", flush=True)
    f_path = dump_funding_history(sym, start_ym=start_ym)
    print(f"[hist-dump] Funding saved -> {f_path} ({len(pq.read_table(f_path)):,} rows)", flush=True)
    k_path = dump_klines_1m_history(sym, start_ym=start_ym)
    print(f"[hist-dump] 1m Klines saved -> {k_path} ({len(pq.read_table(k_path)):,} rows)", flush=True)

    months = list(reversed(_month_list(start_ym, end_ym)))  # newest first: latest closed month -> start_ym
    done = 0
    total = len(months)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(aggregate_month_aggtrades, sym, ym): ym for ym in months}
        for fut in concurrent.futures.as_completed(futs):
            ym = futs[fut]
            try:
                fut.result()
                done += 1
            except Exception as exc:
                print(f"[hist-dump] ERROR {sym} {ym}: {exc}", flush=True)
            HIST_STATUS_PATH.write_text(json.dumps({
                "symbol": sym, "done": done, "total": total, "last_month": ym, "updated": int(time.time())
            }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--start-ym", default="2021-01")
    ap.add_argument("--end-ym", default=None)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--meta-only", action="store_true", help="Only dump funding + 1m klines")
    ap.add_argument("--test-month", default="", help="Test a single month e.g. 2025-11")
    args = ap.parse_args()
    if args.meta_only:
        f_p = dump_funding_history(args.symbol, args.start_ym)
        k_p = dump_klines_1m_history(args.symbol, args.start_ym)
        print("Funding:", f_p, len(pq.read_table(f_p)), "Klines 1m:", k_p, len(pq.read_table(k_p)))
    elif args.test_month:
        aggregate_month_aggtrades(args.symbol, args.test_month)
    else:
        run_history_dump(args.symbol, args.start_ym, args.end_ym, args.workers)
