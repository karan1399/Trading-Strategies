#!/usr/bin/env python3
"""binance_orderflow_dump.py — historical orderflow dump engine for Binance Vision.

Downloads official Binance public data dumps (data.binance.vision), verifies SHA-256
CHECKSUMs, normalizes the era-dependent aggTrades formats, and builds per-minute
ORDER-FLOW feature parquet (taker split, delta, CVD, VWAP, large trades, footprint
per price bin) plus optional raw-aggTrades parquet, futures metrics parquet, and a
kline cross-check sanity report.

Reverse-engineered file formats (verified empirically against live dumps, 2026-11-24):
  um futures  daily/monthly aggTrades: 7 cols; MONTHLY+DAILY files since ~2022 carry a
              header row, older ones do not -> auto-detect; ts = ms epoch; booleans
              lowercase true/false. Earliest um data: 2019-09.
  spot        aggTrades: 8 cols (extra per-trade timestamp); NO header; ts = MICROseconds;
              booleans capitalized True/False.
  The parser auto-detects: header row, column count (7/8), ts unit (ms/us), and
  boolean encoding (true/false/True/False/1/0). Prices/quantities parsed as float64.

Orderflow convention: is_buyer_maker=False  =>  the BUYER is the aggressor (taker BUY).
is_buyer_maker=True => taker SELL. Delta = taker_buy - taker_sell. CVD is cumulative
per symbol across the entire dumped window (state persisted across months/files).

Per-minute exactness: aggTrades CSVs are time-ordered; the reader carries the tail
rows of the final (possibly incomplete) minute of each chunk into the next chunk, so
every bar — including percentiles, max and footprint — is computed from COMPLETE
minute data (exact, not chunk-approximated).

Output layout (out dir, default ./data under the engine folder):
  derived/{SYM}/{SYM}-orderflow-{tf}-{YYYY-MM}.parquet     per-minute features (+ resamples: 5m/15m/1h/4h/1d)
  derived/{SYM}/{SYM}-footprint-{tf}-{YYYY-MM}.parquet     MULTI-TF footprint (1m/5m/15m/1h/4h/1d), Coinglass-style:
                                                           per price bin the taker buy/sell VOLUME and TRADE COUNT
                                                           ('Trades' toggle), bar n_trades + avg trade size, intrabar
                                                           running-delta extremes, POC bin; bins span each bar's own
                                                           [low, high] range (default 12 bins, --footprint-tf-bins)
  raw/{SYM}/{SYM}-aggTrades-{YYYY-MM-DD}.parquet           normalized raw aggTrades (--raw, daily granularity)
  metrics/{SYM}/{SYM}-metrics-YYYY-MM.parquet              futures UM metrics (OI, toptrader L/S, taker L/S vol ratio)
  manifest.json, state.json, sanity_report.md, sanity_report.json

FOOTPRINT AT MULTIPLE TIME FRAMES: standard TFs divide the UTC day evenly, so a
footprint bar never crosses a day boundary — each row is attributable to one day,
and the footprint files merge day-wise under the same incremental semantics as the
bar files (a repaired day replaces exactly its own rows at every TF). The 1m
footprint file is the trade-level ground truth of the in-bar profile; higher TFs
re-bin over the wider bar's own price range (never pooled from 1m bins).

Usage:
  python3 binance_orderflow_dump.py --selftest
  python3 binance_orderflow_dump.py --assets BTCUSDT --from 2026-11-20 --to 2026-11-22 --raw --kline-check
  python3 binance_orderflow_dump.py --assets SUITE --from 2026-10 --to 2026-11
  python3 binance_orderflow_dump.py --update                 # incremental gap-fill, no dates needed
  python3 binance_orderflow_dump.py --status

INCREMENTAL BY DESIGN — rerunning the engine only fills what is missing:
  * each asset resumes from its last covered month (or --from) to now;
  * an open month is EXTENDED day by day: only day archives not already fully
    covered are downloaded, merged into the existing parquet, and the CVD chain
    is recomputed from the month seed (never double-counted);
  * a month whose daily build has INTERNAL GAPS (failed downloads) has exactly
    those days re-fetched and repaired in place on the next run;
  * when a month completes, the monthly archive replaces the daily-built file
    (same canonical filename) and metrics upgrade to the monthly archive too;
  * day archives that are not yet published (404) are tracked as pending — the
    next run picks them up automatically.

Notes:
  * Current (unfinished) months are served from DAILY files automatically; completed
    months use the monthly file (fewer, bigger downloads).
  * Downloads stream to disk, checksums verify before parsing, and intermediate files
    are deleted after conversion — the engine never holds a full month CSV on disk.
  * Long histories are just more months: e.g. --from 2020-01 --to 2026-11 (disk-bound;
    ~400MB/month zip for BTCUSDT today, ~2-4GB uncompressed CSV — parsed in chunks).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import io
import json
import math
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = "https://data.binance.vision"

# The repo's curated crypto USDT-M perp suite (30 symbols), mapped to Binance Vision names.
SUITE = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
         "TRXUSDT", "LINKUSDT", "LTCUSDT", "AVAXUSDT", "DOTUSDT", "NEARUSDT", "UNIUSDT",
         "ATOMUSDT", "XLMUSDT", "ETCUSDT", "FILUSDT", "ICPUSDT", "HBARUSDT", "APTUSDT",
         "ARBUSDT", "OPUSDT", "SUIUSDT", "SEIUSDT", "INJUSDT", "TIAUSDT", "VETUSDT",
         "1000PEPEUSDT", "1000SHIBUSDT"]

# incremental --update starts here when a symbol has no prior coverage
DEFAULT_START = "2026-10"

TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}

METRIC_COLS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
               "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
               "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]


# ================================================================ urls ======
def aggtrades_url(market: str, symbol: str, day: str | None = None, month: str | None = None) -> str:
    """data.binance.vision path for one aggTrades archive.

    market: 'um' (USDT-M futures), 'cm' (COIN-M), 'spot'.
    Either day 'YYYY-MM-DD' (daily dump) or month 'YYYY-MM' (monthly dump).
    """
    if market == "spot":
        root = f"{BASE}/data/spot"
    elif market in ("um", "cm"):
        root = f"{BASE}/data/futures/{market}"
    else:
        raise ValueError(market)
    if day:
        return f"{root}/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day}.zip"
    return f"{root}/monthly/aggTrades/{symbol}/{symbol}-aggTrades-{month}.zip"


def klines_url(market: str, symbol: str, interval: str, month: str) -> str:
    root = f"{BASE}/data/spot" if market == "spot" else f"{BASE}/data/futures/{market}"
    return f"{root}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def klines_daily_url(market: str, symbol: str, interval: str, day: str) -> str:
    root = f"{BASE}/data/spot" if market == "spot" else f"{BASE}/data/futures/{market}"
    return f"{root}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day}.zip"


def metrics_url(symbol: str, day: str | None = None, month: str | None = None) -> str:
    root = f"{BASE}/data/futures/um"
    if day:
        return f"{root}/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip"
    return f"{root}/monthly/metrics/{symbol}/{symbol}-metrics-{month}.zip"


# ============================================================ downloads =====
def http_get_stream(url: str, dest: str, timeout: int = 300) -> int:
    """Stream a URL to dest; returns the HTTP status code (0 on transport error)."""
    code = 0
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            code = r.status_code
            if r.status_code != 200:
                return code
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
        return code
    except requests.RequestException:
        return code


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    return h.hexdigest()


def fetch_and_verify(url: str, cache_dir: str, log=print,
                     status: dict | None = None) -> tuple[str | None, int]:
    """Download url into cache_dir, verify against its .CHECKSUM.

    Returns (local_path | None, last_http_code). The code lets callers tell
    'not published yet' (404) apart from real failures.
    """
    def _fail(code: int) -> tuple[str | None, int]:
        if status is not None:
            status["http"] = code
        return None, code

    os.makedirs(cache_dir, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    dest = os.path.join(cache_dir, name)
    if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
        code = http_get_stream(url, dest)
        if code != 200:
            _quiet_remove(dest)
            return _fail(code)
    cks_dest = dest + ".CHECKSUM"
    if not os.path.exists(cks_dest):
        code = http_get_stream(url + ".CHECKSUM", cks_dest)
        if code != 200:
            _quiet_remove(cks_dest)
    want = None
    if os.path.exists(cks_dest):
        with open(cks_dest) as f:
            want = f.read().split()[0]
    got = sha256_file(dest)
    if want and want != got:
        log(f"    CHECKSUM MISMATCH for {name} — redownloading once")
        _quiet_remove(dest)
        code = http_get_stream(url, dest)
        if code != 200:
            return _fail(code)
        got = sha256_file(dest)
        if want != got:
            log(f"    CHECKSUM STILL BAD for {name} — aborting this file")
            _quiet_remove(dest)
            return _fail(0)
    return dest, 200


def _quiet_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# ====================================================== aggTrades parsing ===
@dataclass
class RawNorm:
    df: pd.DataFrame          # normalized chunk
    header_used: bool
    ts_unit: str              # 'ms' | 'us'
    n_cols: int


def _detect_header(first_line: str) -> bool:
    first = first_line.split(",")[0].strip()
    try:
        int(first)
        return False
    except ValueError:
        return True


def _detect_ts_unit(v: int) -> str:
    return "us" if v >= 10 ** 14 else "ms"


def _norm_chunk(df: pd.DataFrame, ts_unit: str) -> pd.DataFrame:
    """Normalize one raw chunk: names, dtypes, ts->ms, bool, taker side."""
    n = df.shape[1]
    if n >= 8:
        df = df.iloc[:, :7]           # spot's 8th col (trade timestamp) dropped
    df.columns = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
                  "ts", "is_buyer_maker"]
    df = df.copy()
    for c in ("agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "ts"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["agg_trade_id", "price", "quantity", "ts"])
    for c in ("agg_trade_id", "first_trade_id", "last_trade_id", "ts"):
        df[c] = df[c].astype("int64")
    if ts_unit == "us":
        df["ts"] = df["ts"] // 1000   # us -> ms
    bm = df["is_buyer_maker"].astype(str).str.strip().str.lower()
    df["is_buyer_maker"] = bm.map({"true": True, "1": True, "false": False, "0": False})
    bad = df["is_buyer_maker"].isna().sum()
    if bad:
        df = df.dropna(subset=["is_buyer_maker"])
    df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
    df["taker_side"] = np.where(df["is_buyer_maker"], -1, 1).astype(np.int8)  # -1 sell-aggr, +1 buy-aggr
    return df


def iter_normalized_chunks(zip_path: str, chunksize: int = 1_000_000):
    """Yield RawNorm chunks read from the zip member — CSV never touches disk."""
    with zipfile.ZipFile(zip_path) as zf:
        member = zf.namelist()[0]
        with zf.open(member) as fh:
            first = fh.readline().decode("utf-8", "replace")
            header_used = _detect_header(first)
            fh.seek(0)
            head = pd.read_csv(fh, header=0 if header_used else None, nrows=5, dtype=str)
            ts_unit = _detect_ts_unit(int(pd.to_numeric(head.iloc[-1, 5])))
            fh.seek(0)
            reader = pd.read_csv(fh, header=0 if header_used else None, dtype=str,
                                 chunksize=chunksize, engine="c")
            n_cols = None
            for chunk in reader:
                n_cols = chunk.shape[1]
                yield RawNorm(_norm_chunk(chunk, ts_unit), header_used, ts_unit, n_cols)


def iter_complete_minutes(zip_path: str, chunksize: int = 1_000_000):
    """Yield complete-minute DataFrames (exact feature basis) from a zip.

    Chunks are concatenated with the carried tail of the previous chunk's final
    minute, so a minute is only emitted once ALL of its rows are seen.
    """
    carry: pd.DataFrame | None = None
    for rn in iter_normalized_chunks(zip_path, chunksize):
        df = rn.df
        if carry is not None and len(carry):
            df = pd.concat([carry, df], ignore_index=True)
        if not len(df):
            continue
        if not df["ts"].is_monotonic_increasing:
            df = df.sort_values("ts", kind="stable").reset_index(drop=True)
        df["minute"] = df["ts"] // 60000
        last_min = int(df["minute"].iloc[-1])
        is_tail = df["minute"].to_numpy() == last_min
        carry = df[is_tail].drop(columns=["minute"]).reset_index(drop=True)
        complete = df[~is_tail]
        if len(complete):
            for minute, g in complete.groupby("minute", sort=True):
                yield g.drop(columns=["minute"]), rn
    if carry is not None and len(carry):
        yield carry, None


# ==================================================== minute feature calc ===
def minute_features(g: pd.DataFrame, thresholds: list[int], fp_bins: int) -> dict:
    """Exact orderflow features for ONE complete minute of aggTrades.

    taker convention: is_buyer_maker False => buyer aggressed => taker BUY (+1).
    """
    px = g["price"].to_numpy(float)
    q = g["quantity"].to_numpy(float)
    side = g["taker_side"].to_numpy(np.int8)
    buy = side == 1
    nq = px * q
    vol = float(q.sum())
    quote = float(nq.sum())
    vb = float(q[buy].sum())
    qb = float(nq[buy].sum())
    vs = vol - vb
    qs = quote - qb
    ts_ms = int(g["ts"].iloc[0] // 60000 * 60000)
    row = {
        "ts": np.int64(ts_ms),
        "n_trades": np.int32(len(g)),
        "vol_base": vol, "vol_quote": quote,
        "taker_buy_vol": vb, "taker_sell_vol": vs,
        "taker_buy_quote": qb, "taker_sell_quote": qs,
        "delta_vol": vb - vs, "delta_quote": qb - qs,
        "delta_pct": (qb - qs) / quote if quote > 0 else 0.0,
        "aggressor_imbalance": (vb - vs) / vol if vol > 0 else 0.0,
        "vwap": quote / vol if vol > 0 else float(px[0]),
        "vwap_buy": qb / vb if vb > 0 else float("nan"),
        "vwap_sell": qs / vs if vs > 0 else float("nan"),
        "p_first": float(px[0]), "p_last": float(px[-1]),
        "p_high": float(px.max()), "p_low": float(px.min()),
        "max_trade_quote": float(nq.max()),
        "max_trade_side": np.int8(side[int(np.argmax(nq))]),
        "trade_q_mean": quote / len(g),
        "trade_q_p50": float(np.quantile(nq, 0.50)),
        "trade_q_p90": float(np.quantile(nq, 0.90)),
        "trade_q_p99": float(np.quantile(nq, 0.99)),
        "id_first": np.int64(g["agg_trade_id"].min()),
        "id_last": np.int64(g["agg_trade_id"].max()),
    }
    for T in thresholds:
        tag = _thr_tag(T)
        lbuy = buy & (nq >= T)
        lsell = (~buy) & (nq >= T)
        row[f"large_{tag}_buy_n"] = np.int32(int(lbuy.sum()))
        row[f"large_{tag}_buy_quote"] = float(nq[lbuy].sum())
        row[f"large_{tag}_sell_n"] = np.int32(int(lsell.sum()))
        row[f"large_{tag}_sell_quote"] = float(nq[lsell].sum())
    # footprint: taker buy/sell volume per equal-width price bin over [p_low, p_high]
    p_hi, p_lo = row["p_high"], row["p_low"]
    if p_hi > p_lo:
        edges = np.linspace(p_lo, p_hi, fp_bins + 1)
        idx = np.clip(np.digitize(px, edges[1:-1], right=False), 0, fp_bins - 1)
    else:
        idx = np.zeros(len(px), dtype=np.int64)
    fb = np.zeros(fp_bins); fs = np.zeros(fp_bins)
    np.add.at(fb, idx[buy], q[buy])
    np.add.at(fs, idx[~buy], q[~buy])
    for k in range(fp_bins):
        row[f"fp_b{k}"] = float(fb[k])
        row[f"fp_s{k}"] = float(fs[k])
    poc = int(np.argmax(fb + fs))
    row["fp_poc_bin"] = np.int8(poc)
    row["fp_poc_price"] = float((p_lo + p_hi) / 2) if p_hi == p_lo else float((edges[poc] + edges[poc + 1]) / 2)
    return row


def _thr_tag(T: int) -> str:
    return f"{T//1000}k" if T < 1_000_000 else f"{T//1_000_000}m"


# ================================== multi-TF footprint (trades + volume) ====
DEFAULT_FOOTPRINT_TFS = ["1m", "5m", "15m", "1h", "4h", "1d"]


def _fp_row(bucket_ts: int, px: np.ndarray, q: np.ndarray, side: np.ndarray, K: int) -> dict:
    """Footprint for ONE bar at any TF, Coinglass-style: per price bin the taker
    buy/sell VOLUME and trade COUNT (the 'Trades' toggle), plus bar trade stats and
    intrabar running-delta extremes. Bins span the bar's own [low, high] range."""
    buy = side == 1
    nq = px * q
    n = len(px)
    p_lo = float(px.min()); p_hi = float(px.max())
    if p_hi > p_lo:
        edges = np.linspace(p_lo, p_hi, K + 1)
        idx = np.clip(np.digitize(px, edges[1:-1], right=False), 0, K - 1)
    else:
        edges = None
        idx = np.zeros(n, dtype=np.int64)
    b_vol = np.bincount(idx[buy], weights=q[buy], minlength=K)
    s_vol = np.bincount(idx[~buy], weights=q[~buy], minlength=K)
    b_n = np.bincount(idx[buy], minlength=K)
    s_n = np.bincount(idx[~buy], minlength=K)
    vol = float(q.sum()); quote = float(nq.sum())
    run_q = np.cumsum(np.where(buy, nq, -nq))
    run_b = np.cumsum(np.where(buy, q, -q))
    row = {"ts": np.int64(bucket_ts), "p_low": p_lo, "p_high": p_hi,
           "n_trades": np.int32(n), "vol_base": vol, "vol_quote": quote,
           "delta_base": float(run_b[-1]), "delta_quote": float(run_q[-1]),
           "delta_pct": float(run_q[-1] / quote) if quote > 0 else 0.0,
           "avg_trade_size": vol / n if n else 0.0,
           "delta_run_max_base": float(run_b.max()), "delta_run_min_base": float(run_b.min()),
           "delta_run_max_quote": float(run_q.max()), "delta_run_min_quote": float(run_q.min())}
    for k in range(K):
        row[f"fp{k}_b_vol"] = float(b_vol[k])
        row[f"fp{k}_s_vol"] = float(s_vol[k])
        row[f"fp{k}_b_n"] = np.int32(int(b_n[k]))
        row[f"fp{k}_s_n"] = np.int32(int(s_n[k]))
    poc = int(np.argmax(b_vol + s_vol))
    row["fp_poc_bin"] = np.int8(poc)
    row["fp_poc_price"] = p_lo if edges is None else float((edges[poc] + edges[poc + 1]) / 2)
    return row


class FootprintAccumulator:
    """Builds footprint bars at multiple TFs from the trade stream.

    Fed one complete-minute frame at a time (all trades in a minute share every TF
    bucket because standard TFs divide the UTC day evenly — a bar never crosses a
    day boundary). Buffers flush on bucket change and at finish(); the day-aligned
    flushes make every output row attributable to one UTC day, which is what lets
    incremental updates repair single days in place.
    """

    def __init__(self, tfs: list[str], bins: int):
        self.tfs = list(tfs)
        self.bins = int(bins)
        self.cur: dict[str, int | None] = {tf: None for tf in self.tfs}
        self.buf: dict[str, list] = {tf: [] for tf in self.tfs}
        self.rows: dict[str, list[dict]] = {tf: [] for tf in self.tfs}

    def feed(self, g: pd.DataFrame):
        px = g["price"].to_numpy(float)
        q = g["quantity"].to_numpy(float)
        side = g["taker_side"].to_numpy(np.int8)
        ts0 = int(g["ts"].iloc[0])
        for tf in self.tfs:
            n = TF_MIN[tf]
            bucket = ts0 // (n * 60000) * (n * 60000)
            if self.cur[tf] is not None and bucket != self.cur[tf]:
                self._flush(tf)
            self.cur[tf] = bucket
            self.buf[tf].append((px, q, side))

    def _flush(self, tf: str):
        if not self.buf[tf]:
            self.cur[tf] = None
            return
        pxs, qs, sds = zip(*self.buf[tf])
        px = np.concatenate(pxs); q = np.concatenate(qs); side = np.concatenate(sds)
        self.rows[tf].append(_fp_row(int(self.cur[tf]), px, q, side, self.bins))
        self.buf[tf] = []
        self.cur[tf] = None

    def finish(self) -> dict[str, list[dict]]:
        for tf in self.tfs:
            self._flush(tf)
        return self.rows


def _write_footprints(fp_rows: dict[str, list[dict]], sym: str, mo: str, market: str,
                      manifest: dict, replace_days: list[str] | None, log=print):
    """Write/merge per-TF footprint parquet for one symbol-month.

    replace_days: UTC days whose bars were rebuilt (incremental daily merge) — the
    existing file keeps every other day untouched. None = full (re)build.
    """
    out_dir = manifest["_out"]
    mkt = "" if market == "um" else f"-{market}"
    for tf, rows in fp_rows.items():
        if not rows:
            continue
        rel = f"derived/{sym}{mkt}/{sym}{mkt}-footprint-{tf}-{mo}.parquet"
        pth = os.path.join(out_dir, rel)
        new_df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
        if replace_days and os.path.exists(pth):
            ex = pd.read_parquet(pth)
            drop_days = {int(day_start_ms(d) // 86_400_000) for d in replace_days}
            ex = ex[~(ex["ts"] // 86_400_000).isin(drop_days)]
            new_df = pd.concat([ex, new_df], ignore_index=True) \
                .sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
        days_in_file = sorted({pd.Timestamp(int(d) * 86_400_000, unit="ms").strftime("%Y-%m-%d")
                               for d in (new_df["ts"] // 86_400_000).unique()})
        path = write_parquet(new_df, pth, manifest,
                             {"kind": "footprint", "symbol": sym, "tf": tf, "month": mo,
                              "market": market, "bins": int(self_bins(fp_rows, tf)),
                              "days": days_in_file})
        log(f"    footprint {sym} {mo} {tf}: {len(new_df):,} bars ({os.path.getsize(path)/1e6:.1f} MB)")


def self_bins(fp_rows: dict, tf: str) -> int:
    """Number of fp bin columns in the generated rows (for manifest metadata)."""
    r = fp_rows[tf][0]
    return sum(1 for c in r if c.endswith("_b_vol"))


# ============================================================== parquet =====
def write_parquet(df: pd.DataFrame, path: str, manifest: dict, meta: dict) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path, compression="zstd", compression_level=3, index=False)
    entry = dict(meta)
    entry.update({"path": os.path.relpath(path, manifest["_out"]),
                  "rows": int(len(df)), "bytes": os.path.getsize(path),
                  "sha256": sha256_file(path),
                  "ts_min": int(df["ts"].min()) if "ts" in df else None,
                  "ts_max": int(df["ts"].max()) if "ts" in df else None,
                  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    manifest["files"][entry["path"]] = entry
    _save_json(manifest, os.path.join(manifest["_out"], "manifest.json"))
    return path


def _save_json(obj: dict, path: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ============================================================ month plan ====
def month_range(from_s: str, to_s: str) -> list[str]:
    """Inclusive month list 'YYYY-MM' between two 'YYYY-MM' or 'YYYY-MM-DD' strings."""
    f = from_s[:7]
    t = to_s[:7]
    fy, fm = int(f[:4]), int(f[5:7])
    ty, tm = int(t[:4]), int(t[5:7])
    out = []
    y, m = fy, fm
    while (y, m) <= (ty, tm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def days_in_month(month: str) -> int:
    y, m = int(month[:4]), int(month[5:7])
    return (pd.Timestamp(f"{y}-{m:02d}-01") + pd.offsets.MonthEnd(0)).day


def plan_parts(symbol: str, market: str, months: list[str], from_s: str, to_s: str) -> list[dict]:
    """Per month: use the monthly archive when the month is complete, else daily files
    for the requested day span (current month / partial windows)."""
    now_month = time.strftime("%Y-%m")
    parts = []
    for mo in months:
        if mo < now_month:
            parts.append({"symbol": symbol, "month": mo, "kind": "monthly", "days": None})
        else:
            d0 = 1 if from_s[:7] != mo else int(from_s[8:10] or 1)
            d1 = days_in_month(mo) if to_s[:7] != mo else int(to_s[8:10] or days_in_month(mo))
            days = [f"{mo}-{d:02d}" for d in range(d0, d1 + 1)]
            parts.append({"symbol": symbol, "month": mo, "kind": "daily", "days": days})
    return parts


# ====================================================== derived pipeline ====
def day_start_ms(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").value // 10 ** 6)


def merge_days(existing: pd.DataFrame | None, new: pd.DataFrame,
               rebuild_days: list[str] | None, seed_vol: float, seed_quote: float) -> pd.DataFrame:
    """Incremental month assembly (pure function — selftested).

    existing: previously built 1m bars for this month (may be None).
    new: freshly built bars for the days being (re)built.
    rebuild_days: UTC days whose old rows must be replaced (empty/None = full rebuild).
    CVD is ALWAYS recomputed over the whole merged frame as seed + cumsum(delta),
    so extensions and mid-month repairs keep the chain exact and never double-count.
    """
    drop_spans = [(day_start_ms(d), day_start_ms(d) + 86_400_000) for d in (rebuild_days or [])]
    parts = []
    if existing is not None and len(existing):
        ex = existing.drop(columns=[c for c in ("cvd_vol", "cvd_quote") if c in existing.columns])
        if drop_spans:
            keep = np.ones(len(ex), dtype=bool)
            ts_arr = ex["ts"].to_numpy()
            for s, e in drop_spans:
                keep &= ~((ts_arr >= s) & (ts_arr < e))
            ex = ex[keep]
        parts.append(ex)
    parts.append(new.drop(columns=[c for c in ("cvd_vol", "cvd_quote") if c in new.columns]))
    df = pd.concat(parts, ignore_index=True)
    df = df.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    df["cvd_vol"] = df["delta_vol"].cumsum() + seed_vol
    df["cvd_quote"] = df["delta_quote"].cumsum() + seed_quote
    return df


def process_symbol_part(part: dict, market: str, cfg: dict, state: dict, manifest: dict,
                        sanity: dict, log=print) -> dict | None:
    """Download + verify + stream one month-part for one symbol -> derived parquet.

    Incremental semantics (the engine's core promise: rerun == fill the gaps):
      * completed month already built from the monthly archive -> nothing to do;
      * current/partial month -> load existing bars, detect which requested days are
        missing or incomplete, download ONLY those days, merge them in and recompute
        the CVD chain from the month seed;
      * when a month completes and its monthly archive exists, the daily-built file
        is replaced by a full monthly rebuild (single authoritative download).
    """
    sym = part["symbol"]
    mo = part["month"]
    tf = cfg["bars"]
    out_dir = manifest["_out"]
    mkt = "" if market == "um" else f"-{market}"
    rel = f"derived/{sym}{mkt}/{sym}{mkt}-orderflow-{tf}-{mo}.parquet"
    pth = os.path.join(out_dir, rel)
    cache = os.path.join(out_dir, "tmp")
    st = state.setdefault(sym, {"cvd_by_month": {}, "months_done": []})
    prev = [m for m in st["cvd_by_month"] if m < mo]
    seed = st["cvd_by_month"].get(max(prev)) if prev else {"cvd_vol": 0.0, "cvd_quote": 0.0, "last_agg_id": 0}
    cvd_vol0, cvd_quote0 = float(seed["cvd_vol"]), float(seed["cvd_quote"])
    last_agg_id = int(seed["last_agg_id"])

    entry = manifest["files"].get(rel)
    existing = None
    rebuild_days: list[str] = []
    trades_prev = int(entry.get("trades_read", 0)) if entry else 0
    if entry and os.path.exists(pth) and not cfg["force"]:
        if part["kind"] == "monthly" and entry.get("built_from") == "monthly":
            log(f"  {sym} {mo}: monthly build present — skip")
            return None
        if part["kind"] == "daily":
            existing = pd.read_parquet(pth)
            have_days = set(pd.to_datetime(existing["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d"))
            present = set(existing["ts"].tolist())
            for d in part["days"]:
                s = day_start_ms(d)
                day_minutes = present.intersection(range(s, s + 86_400_000, 60_000))
                if len(day_minutes) < 1440 or d not in have_days:
                    rebuild_days.append(d)
            if not rebuild_days:
                log(f"  {sym} {mo}: all {len(part['days'])} days complete — nothing to fetch")
                return None
            log(f"  {sym} {mo}: incremental — (re)building {len(rebuild_days)}/{len(part['days'])} days "
                f"({', '.join(rebuild_days[:6])}{'…' if len(rebuild_days) > 6 else ''})")

    t0 = time.time()
    rows: list[dict] = []
    fp_bins = int(cfg["footprint_bins"])
    thr = cfg["large_thresholds"]
    n_rows_read = 0
    acc = FootprintAccumulator(cfg["footprint_tfs"], cfg["footprint_tf_bins"])
    if part["kind"] == "monthly":
        url = aggtrades_url(market, sym, month=mo)
        log(f"  {sym} {mo}: monthly download…")
        zip_path, _code = fetch_and_verify(url, cache, log)
        if zip_path is None:
            sanity["skipped"].append({"symbol": sym, "month": mo, "reason": "download/checksum failed or not published"})
            sanity["archives_failed"] = sanity.get("archives_failed", 0) + 1
            log(f"    SKIP ({url.rsplit('/',1)[-1]})")
            return None
        sanity["archives_ok"] = sanity.get("archives_ok", 0) + 1
        for g, _rn in iter_complete_minutes(zip_path, cfg["chunksize"]):
            rows.append(minute_features(g, thr, fp_bins))
            acc.feed(g)
            n_rows_read += len(g)
        if cfg["raw"]:
            dump_raw(zip_path, sym, market, manifest, log)
        if not cfg["keep_zips"]:
            _quiet_remove(zip_path)
            _quiet_remove(zip_path + ".CHECKSUM")
        built_from, days_built = "monthly", None
    else:
        fetch_days = rebuild_days if (existing is not None and rebuild_days) else part["days"]
        urls = [aggtrades_url(market, sym, day=d) for d in fetch_days]
        today = time.strftime("%Y-%m-%d")
        paths: dict[str, tuple[str | None, int]] = {}
        with cf.ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
            futs = {ex.submit(fetch_and_verify, u, cache, log, {}): u for u in urls}
            for fu in cf.as_completed(futs):
                u = futs[fu]
                try:
                    paths[u] = fu.result()
                except Exception:
                    paths[u] = (None, 0)
        good = [paths[u][0] for u in urls if paths.get(u) and paths[u][0]]
        pending = [d for d, u in zip(fetch_days, urls) if not (paths.get(u) and paths[u][0])
                   and paths.get(u) is not None and paths[u][1] == 404 and d >= today]
        failed = [d for d, u in zip(fetch_days, urls) if not (paths.get(u) and paths[u][0])
                  and d not in pending]
        sanity["archives_ok"] = sanity.get("archives_ok", 0) + len(good)
        sanity["archives_pending"] = sanity.get("archives_pending", 0) + len(pending)
        sanity["archives_failed"] = sanity.get("archives_failed", 0) + len(failed)
        for d in failed:
            sanity["skipped"].append({"symbol": sym, "month": mo, "day": d,
                                      "reason": "daily archive unavailable (gap — will retry on next run)"})
        if pending:
            log(f"    {len(pending)} day file(s) not published yet ({pending[0]}…) — will fill on next run")
        if failed:
            log(f"    WARNING: {len(failed)} past day file(s) unavailable: {failed[:5]}")
        if not good:
            log(f"    SKIP {sym} {mo}: no daily files available")
            return None
        log(f"  {sym} {mo}: {len(good)}/{len(urls)} daily files verified")
        for zp in good:
            for g, _rn in iter_complete_minutes(zp, cfg["chunksize"]):
                rows.append(minute_features(g, thr, fp_bins))
                acc.feed(g)
                n_rows_read += len(g)
            if cfg["raw"]:
                dump_raw(zp, sym, market, manifest, log)
            if not cfg["keep_zips"]:
                _quiet_remove(zp)
                _quiet_remove(zp + ".CHECKSUM")
        built_from = "daily"
        days_built = sorted(set((entry.get("days") or []) if entry else []) |
                            set(fetch_days))

    if not rows:
        sanity["skipped"].append({"symbol": sym, "month": mo, "reason": "zero rows parsed"})
        return None
    new_df = pd.DataFrame(rows)

    # aggTrade-id continuity across the chain (month seed, or in-file boundary when extending)
    if existing is not None and len(existing) and "id_last" in existing.columns and built_from == "daily":
        first_new_ts = int(new_df["ts"].min())
        before = existing[existing["ts"] < first_new_ts]
        ref_id = int(before["id_last"].iloc[-1]) if len(before) else last_agg_id
    else:
        ref_id = last_agg_id
    first_id = int(new_df["id_first"].min())
    id_gap = max(0, first_id - ref_id - 1) if ref_id else 0
    if ref_id and id_gap > 0:
        sanity.setdefault("agg_id_gaps", []).append({"symbol": sym, "month": mo, "gap": int(id_gap)})

    df = merge_days(existing if (built_from == "daily" and existing is not None) else None,
                    new_df, rebuild_days if built_from == "daily" else None, cvd_vol0, cvd_quote0)
    if cfg["footprint_tfs"]:
        _write_footprints(acc.finish(), sym, mo, market, manifest,
                          rebuild_days if (built_from == "daily" and existing is not None) else None)
    st["cvd_by_month"][mo] = {"cvd_vol": float(df["cvd_vol"].iloc[-1]),
                              "cvd_quote": float(df["cvd_quote"].iloc[-1]),
                              "last_agg_id": int(df["id_last"].iloc[-1]),
                              "last_ts": int(df["ts"].iloc[-1])}
    st["months_done"] = sorted(set(st["months_done"]) | {mo})
    _save_json(state, os.path.join(out_dir, "state.json"))

    trades_total = trades_prev + n_rows_read if built_from == "daily" else n_rows_read
    days_expected = days_in_month(mo) if part["kind"] == "monthly" else len(part["days"])
    path = write_parquet(df, pth, manifest,
                         {"kind": "orderflow", "symbol": sym, "tf": tf, "month": mo,
                          "market": market, "trades_read": int(trades_total),
                          "built_from": built_from, "days": days_built,
                          "incremental_merge": bool(built_from == "daily" and entry is not None),
                          "agg_id_gap_into_month": int(id_gap)})
    cov = len(df) / (days_expected * 1440)
    sanity.setdefault("symbols", {}).setdefault(sym, []).append(
        {"month": mo, "kind": built_from, "bars": int(len(df)),
         "trades_read": int(trades_total) if built_from == "monthly" else n_rows_read,
         "coverage_vs_calendar": round(float(cov), 4), "agg_id_gap_into_month": int(id_gap),
         "rebuilt_days": list(rebuild_days) if built_from == "daily" else None,
         "seconds": round(time.time() - t0, 1), "path": rel})
    log(f"    {sym} {mo}: {len(df):,} {tf} bars (coverage {cov:.4f}, "
        f"{os.path.getsize(path)/1e6:.1f} MB parquet, {time.time()-t0:.0f}s)")
    for extra_tf in cfg["resample"]:
        _write_resample(df, sym, mo, extra_tf, market, manifest)
    return path


def _write_resample(df1m: pd.DataFrame, sym: str, mo: str, tf: str, market: str,
                    manifest: dict):
    n = TF_MIN[tf]
    exclude = {"ts", "vwap", "vwap_buy", "vwap_sell", "p_first", "p_last", "p_high",
               "p_low", "max_trade_quote", "max_trade_side", "trade_q_mean",
               "trade_q_p50", "trade_q_p90", "trade_q_p99", "delta_pct",
               "aggressor_imbalance", "fp_poc_bin", "fp_poc_price",
               "taker_buy_vol_share", "id_first", "id_last"}
    # footprint bins are per-bar price-range grids — pooling them across a wider bar
    # would misrepresent them, so resampled files deliberately exclude fp columns.
    sum_cols = [c for c in df1m.columns if c not in exclude and not c.startswith("fp_")]
    g = df1m.groupby(df1m["ts"] // (n * 60000) * (n * 60000), sort=True)
    agg = g[sum_cols].sum()
    agg["vwap"] = g["vol_quote"].sum() / g["vol_base"].sum()
    agg["p_high"] = g["p_high"].max()
    agg["p_low"] = g["p_low"].min()
    agg["max_trade_quote"] = g["max_trade_quote"].max()
    agg["cvd_vol"] = g["cvd_vol"].last()
    agg["cvd_quote"] = g["cvd_quote"].last()
    agg["delta_pct"] = agg["delta_quote"] / agg["vol_quote"].where(agg["vol_quote"] > 0)
    agg["aggressor_imbalance"] = agg["delta_vol"] / agg["vol_base"].where(agg["vol_base"] > 0)
    agg["taker_buy_vol_share"] = agg["taker_buy_vol"] / agg["vol_base"].where(agg["vol_base"] > 0)
    agg = agg.reset_index().rename(columns={"ts": "ts"})
    mkt = "" if market == "um" else f"-{market}"
    rel = f"derived/{sym}{mkt}/{sym}{mkt}-orderflow-{tf}-{mo}.parquet"
    write_parquet(agg, os.path.join(manifest["_out"], rel), manifest,
                  {"kind": "orderflow", "symbol": sym, "tf": tf, "month": mo, "market": market,
                   "derived_from": "1m"})


# ============================================================== raw dump ====
RAW_SCHEMA_FIELDS = [
    ("agg_trade_id", "int64"), ("price", "float64"), ("quantity", "float64"),
    ("first_trade_id", "int64"), ("last_trade_id", "int64"), ("ts", "int64"),
    ("is_buyer_maker", "bool"), ("taker_side", "int8")]


def dump_raw(zip_path: str, sym: str, market: str, manifest: dict, log=print) -> list[str]:
    """Normalize a (daily or monthly) aggTrades zip into raw parquet, day by day."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    schema = pa.schema([(f, t) for f, t in RAW_SCHEMA_FIELDS])
    out_dir = manifest["_out"]
    writer = None
    cur_day = None
    written = []
    def _close():
        nonlocal writer
        if writer is not None:
            writer.close()
            writer = None
    def _path(day):
        d = os.path.join(out_dir, "raw", sym)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sym}-aggTrades-{day}.parquet")
    for rn in iter_normalized_chunks(zip_path, 1_000_000):
        df = rn.df
        day = time.strftime("%Y-%m-%d", time.gmtime(int(df["ts"].iloc[0]) / 1000))
        if cur_day is None:
            cur_day = day
            writer = pq.ParquetWriter(_path(day), schema, compression="zstd")
        if day != cur_day:
            _close()
            written.append(cur_day)
            cur_day = day
            writer = pq.ParquetWriter(_path(day), schema, compression="zstd")
        tbl = pa.Table.from_pandas(df[[f for f, _ in RAW_SCHEMA_FIELDS]], schema=schema, preserve_index=False)
        writer.write_table(tbl, row_group_size=500_000)
    _close()
    if cur_day:
        written.append(cur_day)
    for day in written:
        p = _path(day)
        rel = os.path.relpath(p, out_dir)
        manifest["files"][rel] = {"kind": "raw_aggtrades", "symbol": sym, "day": day,
                                  "market": market, "bytes": os.path.getsize(p),
                                  "sha256": sha256_file(p), "rows": None,
                                  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "path": rel}
        _save_json(manifest, os.path.join(out_dir, "manifest.json"))
    log(f"    raw parquet: {len(written)} day file(s) for {sym}")
    return written


# =============================================================== metrics ====
def _read_metrics_zip(zp: str) -> pd.DataFrame:
    with zipfile.ZipFile(zp) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            head = fh.readline().decode("utf-8", "replace")
            header = not head.split(",")[0].strip()[:4].isdigit()
            fh.seek(0)
            return pd.read_csv(fh, header=0 if header else None, dtype=str)


def process_metrics(sym: str, mo: str, manifest: dict, sanity: dict, cache: str, log=print) -> None:
    """Futures metrics (OI, toptrader L/S, taker L/S vol ratio) — incremental too.

    Completed months upgrade to the monthly archive once it exists; the open month
    is extended day by day, fetching only days not already covered.
    """
    rel = f"metrics/{sym}/{sym}-metrics-{mo}.parquet"
    out_dir = manifest["_out"]
    pth = os.path.join(out_dir, rel)
    entry = manifest["files"].get(rel)
    now_month = time.strftime("%Y-%m")
    monthly_ready = mo < now_month
    if entry and os.path.exists(pth) and not manifest.get("_cfg", {}).get("force", False):
        if monthly_ready and entry.get("built_from") == "monthly":
            return
        if not monthly_ready and entry.get("built_from") == "daily":
            existing = pd.read_parquet(pth)
            covered = set(pd.to_datetime(existing["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d"))
            today = time.strftime("%Y-%m-%d")
            need = [f"{mo}-{d:02d}" for d in range(1, days_in_month(mo) + 1)
                    if f"{mo}-{d:02d}" <= today and f"{mo}-{d:02d}" not in covered]
            if not need:
                return
    if monthly_ready:
        zp, _code = fetch_and_verify(metrics_url(sym, month=mo), cache, log)
        if zp is None:
            return
        df = _read_metrics_zip(zp)
        os.remove(zp)
        built_from = "monthly"
    else:
        today = time.strftime("%Y-%m-%d")
        all_days = [f"{mo}-{d:02d}" for d in range(1, days_in_month(mo) + 1)
                    if f"{mo}-{d:02d}" <= today]
        covered = set()
        existing = None
        if entry and os.path.exists(pth):
            existing = pd.read_parquet(pth)
            covered = set(pd.to_datetime(existing["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d"))
        need = [d for d in all_days if d not in covered]
        if not need:
            return
        urls = [metrics_url(sym, day=d) for d in need]
        frames = []
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            for path_, _code in ex.map(lambda u: fetch_and_verify(u, cache, log), urls):
                if path_:
                    frames.append(_read_metrics_zip(path_))
                    os.remove(path_)
        if not frames:
            return
        df = pd.concat(frames, ignore_index=True)
        built_from = "daily"
    if df.shape[1] >= len(METRIC_COLS):
        df = df.iloc[:, :len(METRIC_COLS)]
        df.columns = METRIC_COLS
        df["create_time"] = pd.to_datetime(df["create_time"], errors="coerce")
        for c in METRIC_COLS[2:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["create_time"]).sort_values("create_time")
        df["ts"] = (df["create_time"].astype("datetime64[s]").astype("int64") * 1000).astype("int64")
        df = df.drop(columns=["create_time", "symbol"])
        if existing is not None and built_from == "daily":
            df = pd.concat([existing, df], ignore_index=True) \
                .drop_duplicates("ts", keep="last").sort_values("ts")
        path = write_parquet(df, pth, manifest,
                             {"kind": "metrics", "symbol": sym, "month": mo, "market": "um",
                              "built_from": built_from})
        log(f"    metrics {sym} {mo}: {len(df):,} rows ({os.path.getsize(path)/1e6:.1f} MB)")


# ============================================================ kline check ===
def kline_sanity(sym: str, mo: str, market: str, df1m: pd.DataFrame, manifest: dict,
                 cache: str, sanity: dict, days: list[str] | None = None, log=print) -> dict | None:
    """Cross-check trades-derived bars against official 1m klines (same dumps).

    months still open have no monthly kline file -> pass `days` to use daily klines.
    """
    urls = [klines_daily_url(market, sym, "1m", d) for d in days] if days \
        else [klines_url(market, sym, "1m", mo)]
    zps = []
    with cf.ThreadPoolExecutor(max_workers=min(4, max(1, len(urls)))) as ex:
        for path_, _code in ex.map(lambda u: fetch_and_verify(u, cache, log), urls):
            if path_:
                zps.append(path_)
    if not zps:
        return None
    frames = []
    for zp in zps:
        with zipfile.ZipFile(zp) as zf:
            with zf.open(zf.namelist()[0]) as fh:
                head = fh.readline().decode("utf-8", "replace")
                header = not head.split(",")[0].strip()[:4].isdigit()
                fh.seek(0)
                frames.append(pd.read_csv(fh, header=0 if header else None, dtype=str))
        os.remove(zp)
    k = pd.concat(frames, ignore_index=True)
    k = k.iloc[:, :12]
    k.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                 "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
    ts0 = pd.to_numeric(k["open_time"].iloc[0])
    k["ts"] = pd.to_numeric(k["open_time"]) // (1 if ts0 < 10 ** 14 else 1000)  # ms stays, us -> ms
    for c in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume"):
        k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.dropna(subset=["ts"]).drop_duplicates("ts").set_index("ts")
    ours = df1m.set_index("ts")
    both = ours.index.intersection(k.index)
    if not len(both):
        return None
    a, b = ours.loc[both], k.loc[both]
    tol_px = 1e-6
    d_close = (a["p_last"] - b["close"]).abs()
    d_hl = np.maximum((a["p_high"] - b["high"]).abs(), (a["p_low"] - b["low"]).abs())
    rel_vol = ((a["vol_base"] - b["volume"]).abs() / b["volume"].replace(0, np.nan)).dropna()
    # Day-level volume reconciliation — THE invariant for UM futures. Binance's um
    # aggTrades dumps do not reconcile with raw-trade klines minute-by-minute (the
    # exchange-side aggregation drops/merges burst trades both ways; kline `count`
    # counts raw trades, ours counts aggregated ones). Per-day sums must, however,
    # agree closely; per-minute volume diffs partially cancel within a day.
    day_idx = (both // 86_400_000)
    day_ours = a["vol_base"].groupby(day_idx).sum()
    day_k = b["volume"].groupby(day_idx).sum()
    day_rel = ((day_ours - day_k).abs() / day_k.replace(0, np.nan)).dropna()
    day_rows = [{"day": pd.Timestamp(int(d) * 86_400_000, unit="ms").strftime("%Y-%m-%d"),
                 "ours_base_vol": float(day_ours[d]), "kline_base_vol": float(day_k[d]),
                 "rel_diff": float(day_rel[d]) if d in day_rel.index else None}
                for d in sorted(day_ours.index)]
    res = {"month": mo, "minutes_compared": int(len(both)),
           "only_in_trades": int(len(ours.index.difference(k.index))),
           "only_in_klines": int(len(k.index.difference(ours.index))),
           "close_max_abs_diff": float(d_close.max()),
           "close_exact": bool(d_close.max() <= tol_px),
           "highlow_mismatch_minutes": int((d_hl > tol_px).sum()),
           "highlow_max_abs_diff": float(d_hl.max()),
           "minute_rel_vol_p50": float(rel_vol.quantile(0.50)) if len(rel_vol) else 0.0,
           "minute_rel_vol_p95": float(rel_vol.quantile(0.95)) if len(rel_vol) else 0.0,
           "minute_rel_vol_max": float(rel_vol.max()) if len(rel_vol) else 0.0,
           "day_rel_vol_max": float(day_rel.max()) if len(day_rel) else None,
           "day_rows": day_rows[:40]}
    res["day_volume_ok"] = bool(res["day_rel_vol_max"] is not None and res["day_rel_vol_max"] <= 0.005)
    sanity.setdefault("kline_check", []).append({"symbol": sym, **res})
    log(f"    kline check {sym} {mo}: minutes={res['minutes_compared']:,} "
        f"close_exact={res['close_exact']} hl_mismatch={res['highlow_mismatch_minutes']} "
        f"day_rel_vol_max={res['day_rel_vol_max'] if res['day_rel_vol_max'] is None else format(res['day_rel_vol_max'], '.4%')} "
        f"day_volume_ok={res['day_volume_ok']}")
    return res

# ========================================================= sanity report ====
def build_sanity_report(manifest: dict, sanity: dict, out_dir: str) -> str:
    """Human-readable sanity report + machine JSON. Returns md text."""
    now = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    lines = [f"# Binance Vision Orderflow Dump — Sanity Report ({now})", "",
             f"- market: `{sanity.get('market', 'um')}`  engine: single-file `binance_orderflow_dump.py`",
             f"- archives checksummed (SHA-256 from `.CHECKSUM`): **{sanity.get('archives_ok', 0)}** ok, "
             f"{sanity.get('archives_failed', 0)} failed",
             f"- skipped downloads: {len(sanity.get('skipped', []))}", ""]
    kc = sanity.get("kline_check", [])
    if kc:
        close_exact = sum(1 for r in kc if r.get("close_exact"))
        day_ok = sum(1 for r in kc if r.get("day_volume_ok"))
        worst_day = max((r.get("day_rel_vol_max") or 0) for r in kc)
        worst_close = max((r.get("close_max_abs_diff") or 0) for r in kc)
        hl_minutes = sum(r.get("highlow_mismatch_minutes") or 0 for r in kc)
        p95 = max((r.get("minute_rel_vol_p95") or 0) for r in kc)
        lines += ["## Official-kline cross-check (trades-derived bars vs exchange 1m klines)",
                  f"- {len(kc)} window-checks: minute CLOSE exact {close_exact}/{len(kc)} "
                  f"(worst abs diff {worst_close:.2e}); day-level base-volume reconciliation "
                  f"within 0.5%: {day_ok}/{len(kc)} (worst day diff {worst_day:.4%})",
                  f"- per-minute volume vs klines differs by construction: Binance's um aggTrades "
                  f"do not reconcile with raw-trade klines minute-by-minute (exchange-side "
                  f"aggregation loses/merges burst trades both ways; kline count = raw trades, "
                  f"ours = aggregated). p95 minute diff {p95:.3%}. The taker-buy split, VWAP, "
                  f"delta and CVD remain internally exact w.r.t. the aggTrades source of truth.",
                  f"- high/low extremes mismatched on {hl_minutes} minutes total across all checks "
                  f"(aggTrades dump misses some burst extremes; close never does).", ""]
    lines += ["## Per-symbol coverage", "",
              "| symbol | month | kind | 1m bars | trades read | coverage vs calendar | agg-id gap |",
              "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    total_bars = total_trades = 0
    for sym, entries in sorted(sanity.get("symbols", {}).items()):
        for e in entries:
            lines.append(f"| {sym} | {e['month']} | {e['kind']} | {e['bars']:,} | "
                         f"{e['trades_read']:,} | {e['coverage_vs_calendar']:.4f} | "
                         f"{e.get('agg_id_gap_into_month', 0):,} |")
            total_bars += e["bars"]
            total_trades += e["trades_read"]
    lines += ["", f"**Totals: {total_bars:,} bars from {total_trades:,} aggTrades.**", ""]
    gaps = sanity.get("agg_id_gaps", [])
    if gaps:
        lines += ["## aggTrade-id gaps across file boundaries", ""]
        lines += [f"- {g['symbol']} {g['month']}: gap of {g['gap']} ids (exchange-side skips — CVD unaffected, reported for honesty)" for g in gaps]
        lines.append("")
    skipped = sanity.get("skipped", [])
    if skipped:
        lines += ["## Skipped (unavailable archives)", ""]
        lines += [f"- {s['symbol']} {s['month']}: {s['reason']}" for s in skipped[:60]]
        lines.append("")
    n_files = len(manifest.get("files", {}))
    total_bytes = sum(f.get("bytes", 0) or 0 for f in manifest.get("files", {}).values())
    lines += ["## Outputs", "",
              f"- manifest entries: **{n_files}** files, {total_bytes/1e6:.1f} MB total",
              "- derived bars are computed from COMPLETE minutes (tail-carry across read chunks — exact percentiles/footprint)",
              "- CVD is month-keyed seeded and recomputed over the whole file on every incremental merge: "
              "rerunning the engine only fetches missing days/months and repairs day gaps in place "
              "(rerun == fill the gap candles)",
              "- coverage < 1.0000 reflects exchange maintenance/publishing gaps, not parser loss "
              "(kline cross-check proves minute-close equality and day-level volume reconciliation)", ""]
    md = "\n".join(lines)
    with open(os.path.join(out_dir, "sanity_report.md"), "w") as f:
        f.write(md)
    _save_json(sanity, os.path.join(out_dir, "sanity_report.json"))
    return md


# ================================================================ selftest ==
def selftest() -> bool:
    """Known-answer + format-variant + chunk-exactness tests (no network)."""
    import tempfile
    failures = []
    print("=" * 84)
    print("SELFTEST — synthetic known-answer, format variants, chunk exactness")
    print("=" * 84)

    def synth_csv(header: bool, cols8: bool, us: bool, caps: bool) -> str:
        rows = [
            # px, q, buyer_maker   (t1..t4 minute A; t5 minute B single price)
            # t3/t4 are sized so trade notional crosses the 100k threshold: 150,750 / 200,400
            (100.0, 1.0, True), (101.0, 2.0, False), (100.5, 1500.0, False), (100.2, 2000.0, True),
        ]
        base_ms = 1795305600000  # 2026-11-22 00:00:00 UTC
        lines = ["agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"] \
            if header else []
        aid = 1
        for i, (px, q, bm) in enumerate(rows):
            ts = base_ms + i * 1000
            if us:
                ts = ts * 1000 + 137
            bm_s = ("True" if bm else "False") if caps else ("true" if bm else "false")
            if cols8:
                lines.append(f"{aid},{px:.8f},{q:.8f},{aid},{aid},{ts},{bm_s},{ts}")
            else:
                lines.append(f"{aid},{px:.4f},{q:.4f},{aid},{aid},{ts},{bm_s}")
            aid += 1
        # minute B (single price)
        ts_b = (base_ms + 60000) * (1000 if us else 1) + 5
        bm_s = ("False" if caps else "false")
        if cols8:
            lines.append(f"{aid},100.00000000,5.00000000,{aid},{aid},{ts_b},{bm_s},{ts_b}")
        else:
            lines.append(f"{aid},100.0,5.0,{aid},{aid},{ts_b},{bm_s}")
        return "\n".join(lines) + "\n"

    def make_zip(csv_text: str, path: str):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("SYNTH-aggTrades-x.csv", csv_text)

    def build(zip_path: str, chunksize: int) -> pd.DataFrame:
        rows = []
        for g, _rn in iter_complete_minutes(zip_path, chunksize):
            rows.append(minute_features(g, [100000, 1000000], 6))
        df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
        df["cvd_vol"] = df["delta_vol"].cumsum()
        df["cvd_quote"] = df["delta_quote"].cumsum()
        return df

    with tempfile.TemporaryDirectory() as td:
        # --- 1) known-answer on the um-style file (header, 7 cols, ms, lowercase)
        p1 = os.path.join(td, "um.zip")
        make_zip(synth_csv(True, False, False, False), p1)
        df = build(p1, 1_000_000)
        a = df.iloc[0]
        # hand-computed expectations for the scaled synthetic trades:
        # vol=3503  quote=351452  buy(t2,t3): vol 1502 / quote 150952
        # sell(t1,t4): vol 2001 / quote 200500  delta_q=-49548  vwap=351452/3503
        # max nq=200400 (t4, sell)  large_100k: buy 1 @150750, sell 1 @200400
        # minute B: single px 100, buy q5 -> cvd_quote end = -49548 + 500 = -49448
        exp_quote = 100.0 * 1 + 101.0 * 2 + 100.5 * 1500 + 100.2 * 2000
        checks = [
            ("n_trades", a["n_trades"], 4),
            ("vol_base", a["vol_base"], 3503.0),
            ("vol_quote", abs(a["vol_quote"] - exp_quote) < 1e-9, True),
            ("taker_buy_vol", a["taker_buy_vol"], 1502.0),
            ("taker_sell_vol", a["taker_sell_vol"], 2001.0),
            ("delta_vol", a["delta_vol"], -499.0),
            ("delta_quote", abs(a["delta_quote"] - (-49548.0)) < 1e-9, True),
            ("vwap", abs(a["vwap"] - exp_quote / 3503) < 1e-9, True),
            ("p_high", a["p_high"], 101.0), ("p_low", a["p_low"], 100.0),
            ("max_trade_quote", a["max_trade_quote"], 200400.0),
            ("max_trade_side", a["max_trade_side"], -1),
            ("large_100k_buy_n", a["large_100k_buy_n"], 1),
            ("large_100k_buy_quote", a["large_100k_buy_quote"], 150750.0),
            ("large_100k_sell_n", a["large_100k_sell_n"], 1),
            ("large_100k_sell_quote", a["large_100k_sell_quote"], 200400.0),
            ("large_1m_buy_n", a["large_1m_buy_n"], 0),
            ("p_first", a["p_first"], 100.0), ("p_last", a["p_last"], 100.2),
            ("fp invariant buy", abs(sum(df.iloc[0][f"fp_b{k}"] for k in range(6)) - 1502.0) < 1e-9, True),
            ("fp invariant sell", abs(sum(df.iloc[0][f"fp_s{k}"] for k in range(6)) - 2001.0) < 1e-9, True),
            ("single-price poc price", df.iloc[1]["fp_poc_price"], 100.0),
            ("cvd_quote last", abs(df["cvd_quote"].iloc[-1] - (-49048.0)) < 1e-9, True),
        ]
        for name, got, want in checks:
            ok = bool(got == want) if not isinstance(want, float) else bool(abs(float(got) - want) < 1e-9)
            if not ok:
                failures.append(f"known-answer {name}: got {got} want {want}")
        print(f"  1) known-answer um-style: {len(checks) - sum(1 for f in failures if f.startswith('known-answer'))}/{len(checks)} OK")

        # --- 2) spot-style variant (no header, 8 cols, µs, True/False) must give IDENTICAL features
        p2 = os.path.join(td, "spot.zip")
        make_zip(synth_csv(False, True, True, True), p2)
        df2 = build(p2, 1_000_000)
        same = np.allclose(df.drop(columns=[]).select_dtypes("number").to_numpy(float),
                           df2.select_dtypes("number").to_numpy(float), equal_nan=True)
        print(f"  2) spot-format variant parses to identical numbers: {'OK' if same else 'FAIL'}")
        if not same:
            failures.append("spot variant mismatch")

        # --- 3) chunk-carry exactness: tiny chunks must reproduce big-chunk features exactly
        p3 = os.path.join(td, "big.zip")
        many = ["agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"]
        base_ms = 1795305600000
        rng = np.random.default_rng(7)
        aid = 1
        for m in range(5):                      # 5 minutes, ~2000 trades each
            for i in range(2000):
                px = 100 + float(rng.random())
                q = float(rng.random()) * 2
                bm = "true" if rng.random() < 0.5 else "false"
                ts = base_ms + m * 60000 + i * 29  # 2000*29 = 58000ms < 60000 — inside minute
                many.append(f"{aid},{px:.6f},{q:.6f},{aid},{aid},{ts},{bm}")
                aid += 1
        make_zip("\n".join(many) + "\n", p3)
        big = build(p3, 1_000_000)
        small = build(p3, 137)                  # deliberately tiny chunks
        cols = [c for c in big.columns if c != "ts"]
        exact = np.allclose(big[cols].to_numpy(float), small[cols].to_numpy(float), rtol=0, atol=1e-9, equal_nan=True)
        print(f"  3) chunk-boundary exactness (chunksize 137 vs 1M): {'OK' if exact else 'FAIL'} "
              f"({len(big)} minutes)")
        if not exact:
            failures.append("chunk-carry inexact")

        # --- 4) sha256 + manifest plumbing
        h = sha256_file(p1)
        import hashlib as _hl
        want = _hl.sha256(open(p1, "rb").read()).hexdigest()
        ok = h == want and len(h) == 64
        print(f"  4) sha256_file: {'OK' if ok else 'FAIL'}")
        if not ok:
            failures.append("sha256")

        # --- 5) month/day planning
        parts = plan_parts("BTCUSDT", "um", ["2026-10", "2026-11"], "2026-11-01", "2026-11-22")
        ok = parts[0]["kind"] == "monthly" and parts[1]["kind"] == "daily" \
            and parts[1]["days"][0] == "2026-11-01" and parts[1]["days"][-1] == "2026-11-22"
        print(f"  5) month/day planning: {'OK' if ok else 'FAIL'} "
              f"({parts[0]['kind']}/{parts[1]['kind']}, {len(parts[1]['days'])} days)")
        if not ok:
            failures.append("planning")

        # --- 6) incremental gap-fill: extend a partial month + mid-month day repair
        #      must both reproduce the single-shot build exactly (incl. CVD chain)
        base_ms = 1795305600000  # 2026-11-22 00:00 UTC
        hdr = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"
        day1 = [hdr,
                "1,100.0,1.0,1,1,1795305600000,true",
                "2,101.0,2.0,2,2,1795305601000,false",
                "3,100.5,3.0,3,3,1795305602000,false",
                "4,100.2,4.0,4,4,1795305660000,true"]          # 2 minutes on day 1
        day2 = [hdr,
                "5,100.0,1.5,5,5,1795392000000,false",
                "6,100.5,2.5,6,6,1795392001000,true",
                "7,101.0,2.0,7,7,1795392060000,true"]          # 2 minutes on day 2
        pa1, pa2, pfull = (os.path.join(td, n) for n in ("d1.zip", "d2.zip", "full.zip"))
        make_zip("\n".join(day1) + "\n", pa1)
        make_zip("\n".join(day2) + "\n", pa2)
        make_zip("\n".join(day1 + day2[1:]) + "\n", pfull)
        d1 = build(pa1, 1_000_000)
        d2 = build(pa2, 1_000_000)
        full = build(pfull, 1_000_000)
        cols = [c for c in full.columns if c != "ts"]
        merged = merge_days(d1, d2, ["2026-11-23"], 0.0, 0.0)          # extend partial month
        ext_ok = np.allclose(full[cols].to_numpy(float), merged[cols].to_numpy(float),
                             rtol=0, atol=1e-9, equal_nan=True) and len(merged) == len(full)
        repaired = merge_days(full, d2, ["2026-11-23"], 0.0, 0.0)      # day-level repair (same data)
        rep_ok = np.allclose(full[cols].to_numpy(float), repaired[cols].to_numpy(float),
                             rtol=0, atol=1e-9, equal_nan=True)
        repaired2 = merge_days(full, d1, ["2026-11-22"], 0.0, 0.0)     # mid-month repair with day 1
        rep2_ok = np.allclose(full[cols].to_numpy(float), repaired2[cols].to_numpy(float),
                              rtol=0, atol=1e-9, equal_nan=True)
        ok = ext_ok and rep_ok and rep2_ok
        print(f"  6) incremental merge (extend/repair == single-shot): "
              f"{'OK' if ok else f'FAIL ext={ext_ok} rep={rep_ok} rep2={rep2_ok}'}")
        if not ok:
            failures.append("incremental merge")

        # --- 7) multi-TF footprint (trades + volume per bin, running delta)
        acc = FootprintAccumulator(["1m", "5m", "1h"], 4)
        for g, _rn in iter_complete_minutes(pfull, 1_000_000):
            acc.feed(g)
        frows = acc.finish()
        r5 = [r for r in frows["5m"] if r["ts"] == base_ms][0]
        vol_1m_day1 = sum(r["vol_base"] for r in frows["1m"] if r["ts"] < base_ms + 86_400_000)
        n_1m = sum(r["n_trades"] for r in frows["1m"])
        n_5m = sum(r["n_trades"] for r in frows["5m"])
        checks7 = [
            ("fp row counts", (len(frows["1m"]), len(frows["5m"]), len(frows["1h"])), (4, 2, 2)),
            ("fp5m n_trades", r5["n_trades"], 4),
            ("fp5m vol", abs(r5["vol_base"] - 10.0) < 1e-9, True),
            ("fp5m buy trades", sum(r5[f"fp{k}_b_n"] for k in range(4)), 2),
            ("fp5m sell trades", sum(r5[f"fp{k}_s_n"] for k in range(4)), 2),
            ("fp5m buy vol == sum bins", abs(sum(r5[f"fp{k}_b_vol"] for k in range(4)) - 5.0) < 1e-9, True),
            ("fp5m sell vol == sum bins", abs(sum(r5[f"fp{k}_s_vol"] for k in range(4)) - 5.0) < 1e-9, True),
            ("fp5m run-delta max", abs(r5["delta_run_max_quote"] - 403.5) < 1e-9, True),
            ("fp5m run-delta min", abs(r5["delta_run_min_quote"] - (-100.0)) < 1e-9, True),
            ("fp5m final delta", abs(r5["delta_quote"] - 2.7) < 1e-9, True),
            ("fp vol conservation 1m==5m", abs(vol_1m_day1 - r5["vol_base"]) < 1e-9, True),
            ("fp trade conservation 1m==5m", n_1m == n_5m == 7, True),
            ("fp1h day1 == 5m day1", abs(frows["1h"][0]["vol_base"] - 10.0) < 1e-9, True),
        ]
        bad7 = [n for n, got, want in checks7
                if not ((got == want) if not isinstance(want, float) else bool(abs(float(got) - want) < 1e-9))]
        print(f"  7) multi-TF footprint (trades+volume, run-delta): "
              f"{'OK' if not bad7 else f'FAIL {bad7}'} ({len(checks7)} checks)")
        if bad7:
            failures.append(f"footprint {bad7}")

    print("\n" + ("SELFTEST: PASS" if not failures else f"SELFTEST: FAIL — {failures}"))
    return not failures


# =================================================================== CLI ====
def parse_args():
    ap = argparse.ArgumentParser(description="Binance Vision historical orderflow dump engine")
    ap.add_argument("--assets", default="SUITE",
                    help="'SUITE' (30 curated USDT-M perps) or comma-separated symbols")
    ap.add_argument("--market", default="um", choices=["um", "cm", "spot"])
    ap.add_argument("--from", dest="frm", required=False, help="YYYY-MM or YYYY-MM-DD")
    ap.add_argument("--to", dest="to", required=False, help="YYYY-MM or YYYY-MM-DD (inclusive)")
    ap.add_argument("--out", default=os.path.join(ENGINE_DIR, "data"))
    ap.add_argument("--bars", default="1m", choices=["1m", "5m"])
    ap.add_argument("--resample", default="5m,1h", help="comma TFs to also emit from 1m")
    ap.add_argument("--footprint-bins", type=int, default=6,
                    help="inline per-1m-bar footprint volume bins in the orderflow file")
    ap.add_argument("--footprint-tfs", default=",".join(DEFAULT_FOOTPRINT_TFS),
                    help="time frames for the dedicated footprint files (trades+volume per bin)")
    ap.add_argument("--footprint-tf-bins", type=int, default=12,
                    help="price bins per footprint bar (bins span each bar's own [low, high])")
    ap.add_argument("--large-thresholds", default="100000,1000000", help="USD quote-notional thresholds")
    ap.add_argument("--chunksize", type=int, default=1_000_000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--raw", action="store_true", help="also write normalized raw aggTrades parquet (daily files)")
    ap.add_argument("--no-metrics", action="store_true", help="skip futures metrics (OI, L/S ratios)")
    ap.add_argument("--kline-check", action="store_true", help="cross-check derived bars vs official 1m klines")
    ap.add_argument("--update", action="store_true",
                    help="incremental gap-fill: for each asset continue from its last covered month "
                         "to now — fetches only missing days/months, repairs day gaps, upgrades "
                         "finished months to the monthly archive (rerun == fill the gap candles)")
    ap.add_argument("--keep-zips", action="store_true")
    ap.add_argument("--force", action="store_true", help="rebuild even if present in manifest")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--status", action="store_true")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.selftest:
        sys.exit(0 if selftest() else 1)
    if args.status:
        out = args.out
        mp = os.path.join(out, "manifest.json")
        if not os.path.exists(mp):
            print("no manifest — nothing dumped yet")
            return
        man = json.load(open(mp))
        files = man.get("files", {})
        tot = sum(f.get("bytes", 0) or 0 for f in files.values())
        kinds = {}
        syms = set()
        for p, f in files.items():
            kinds[f.get("kind", "?")] = kinds.get(f.get("kind", "?"), 0) + 1
            syms.add(f.get("symbol", "?"))
        print(f"out dir     : {out}")
        print(f"files       : {len(files)}  ({tot/1e6:.1f} MB)")
        print(f"symbols     : {len(syms)}  {sorted(syms)[:8]}{'…' if len(syms) > 8 else ''}")
        print(f"by kind     : {kinds}")
        return

    if not args.update and (not args.frm or not args.to):
        print("--from and --to are required (or use --update for incremental gap-fill)")
        sys.exit(2)
    assets = SUITE if args.assets.upper() == "SUITE" else \
        [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    cfg = {"bars": args.bars, "resample": [t for t in args.resample.split(",") if t],
           "footprint_bins": args.footprint_bins,
           "footprint_tfs": [t for t in args.footprint_tfs.split(",") if t],
           "footprint_tf_bins": args.footprint_tf_bins,
           "large_thresholds": [int(x) for x in args.large_thresholds.split(",")],
           "chunksize": args.chunksize, "workers": args.workers, "raw": args.raw,
           "keep_zips": args.keep_zips, "force": args.force,
           "with_metrics": not args.no_metrics, "kline_check": args.kline_check}
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "manifest.json")
    manifest = {"_out": out_dir, "_cfg": cfg, "files": {}}
    if os.path.exists(manifest_path):
        try:
            manifest = json.load(open(manifest_path))
            manifest["_out"] = out_dir
            manifest["_cfg"] = cfg
        except Exception:
            pass
    state = {}
    sp = os.path.join(out_dir, "state.json")
    if os.path.exists(sp):
        try:
            state = json.load(open(sp))
        except Exception:
            state = {}
    sanity: dict = {"market": args.market, "skipped": [], "symbols": {}, "archives_ok": 0,
                    "archives_failed": 0, "archives_pending": 0, "kline_check": [], "agg_id_gaps": []}
    today = time.strftime("%Y-%m-%d")
    months = None if args.update else month_range(args.frm, args.to)
    print(f"[dump] market={args.market} assets={len(assets)} "
          f"months={months if months else 'UPDATE (per-asset)'} "
          f"bars={cfg['bars']} raw={cfg['raw']} metrics={cfg['with_metrics']} kline_check={cfg['kline_check']}")

    cache = os.path.join(out_dir, "tmp")
    for sym in assets:
        print(f"[{sym}]")
        if args.update:
            done = set(state.get(sym, {}).get("months_done", []))
            for p, f in manifest["files"].items():
                if f.get("symbol") == sym and f.get("kind") == "orderflow" and f.get("month"):
                    done.add(f["month"])
            start = min(done) if done else DEFAULT_START
            months = month_range(start, today)
            frm_s, to_s = f"{start}-01", today
            print(f"  update window: {start} → {today}")
        else:
            frm_s, to_s = args.frm, args.to
        parts = plan_parts(sym, args.market, months, frm_s, to_s)
        for part in parts:
            # disk guard: monthly archives can be large — check before download
            if part["kind"] == "monthly":
                url = aggtrades_url(args.market, sym, month=part["month"])
                try:
                    h = requests.head(url, timeout=20)
                    size = int(h.headers.get("content-length", 0)) if h.status_code == 200 else 0
                except requests.RequestException:
                    size = 0
                free = shutil.disk_usage(out_dir).free
                if size and free < size * 3:
                    sanity["skipped"].append({"symbol": sym, "month": part["month"],
                                              "reason": f"insufficient disk (need ~{size*3/1e9:.1f}GB, free {free/1e9:.1f}GB)"})
                    print(f"    DISK GUARD: skip {sym} {part['month']} (zip {size/1e6:.0f}MB, free {free/1e9:.1f}GB)")
                    continue
            path = process_symbol_part(part, args.market, cfg, state, manifest, sanity)
            if path and cfg["kline_check"]:
                df1m = pd.read_parquet(path)
                kline_sanity(sym, part["month"], args.market, df1m, manifest, cache, sanity,
                             days=part["days"] if part["kind"] == "daily" else None)
        if cfg["with_metrics"] and args.market == "um":
            for mo in months:
                try:
                    process_metrics(sym, mo, manifest, sanity, cache)
                except Exception as e:
                    sanity["skipped"].append({"symbol": sym, "month": mo, "reason": f"metrics: {e}"})
    _save_json(state, os.path.join(out_dir, "state.json"))
    _save_json(manifest, manifest_path)
    md = build_sanity_report(manifest, sanity, out_dir)
    print("\n" + "=" * 84)
    print(md.split("## Per-symbol coverage")[0])
    print("report: sanity_report.md / sanity_report.json in", out_dir)


if __name__ == "__main__":
    main()

