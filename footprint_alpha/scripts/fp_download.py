#!/usr/bin/env python3
"""Binance Vision aggTrades/klines downloader -> compact footprint store.

Per day, emits into footprint/processed/:
  bars_<date>.parquet   one row per 5m bar: ts, o/h/l/c, buy_v, sell_v, n_tr
  edges_<date>.parquet top-12 + bottom-12 TRADED price levels per bar at RAW
                       tick precision (price, buy_v, sell_v, n_tr) - this is
                       where the 0-print 'auction complete' signal lives
  grid_<date>.parquet  1-USD bucket footprint grid (bar, bucket_px, buy_v,
                       sell_v, n_tr) for POC / absorption / imbalance features

Modes: full (all 120 days) | pilot (last day) | klines (5m klines for
cross-validation of taker volumes).
Raw zips are never stored in full mode (disk discipline).
"""
import io
import os
import sys
import time
import urllib.request
import zipfile

import numpy as np
import pandas as pd

BASE = "/home/z/my-project/footprint"
PROC = os.path.join(BASE, "processed")
KL = os.path.join(BASE, "klines")
for d in (PROC, KL):
    os.makedirs(d, exist_ok=True)

SYMBOL = "BTCUSDT"
START, END = "2026-01-01", "2026-11-22"   # ~11 months
BAR_SEC = 300                              # 5m footprint bars
TICK = 0.10                                # BTCUSDT futures tick
K_EDGE = 12                                # levels per side kept in edges
KEY_SCALE = 10_000_000                     # bar*KEY_SCALE + tick_index


def dates_range():
    return [d.date() for d in pd.date_range(START, END)]


def fetch(url, tries=3):
    for t in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return r.read()
        except Exception:
            if t == tries - 1:
                raise
            time.sleep(3 * (t + 1))


def read_csv_skipheader(zf, **kw):
    name = zf.namelist()[0]
    with zf.open(name) as f:
        head = f.readline().decode()
        skip = 0
        try:
            float(head.split(",")[0].strip())
        except ValueError:
            skip = 1  # header row present
    with zf.open(name) as f:
        return pd.read_csv(f, header=None, skiprows=skip, **kw)


def process_day(blob):
    """aggTrades zip -> (bars, edges, grid) tables for one day."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    df = read_csv_skipheader(
        z, names=["aid", "price", "qty", "ftid", "ltid", "ts", "bm"],
        usecols=["price", "qty", "ts", "bm"],
        dtype={"price": np.float64, "qty": np.float64, "ts": np.int64,
               "bm": "boolean"})
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    ts = df["ts"].to_numpy(np.int64)
    px = df["price"].to_numpy(np.float64)
    qty = df["qty"].to_numpy(np.float64)
    buys = ~df["bm"].to_numpy(bool)   # is_buyer_maker=False -> taker BOUGHT
    bar = ts // (BAR_SEC * 1000)
    tick = np.rint(px / TICK).astype(np.int64)

    # ---- bars: OHLC + per-bar totals (bar is sorted non-decreasing)
    ub = np.unique(bar)
    nb = len(ub)
    bidx = np.searchsorted(ub, bar)          # gap-proof bar index 0..nb-1
    starts = np.searchsorted(bar, ub, "left")
    ends = np.searchsorted(bar, ub, "right")
    bars = pd.DataFrame({
        "ts": pd.to_datetime(ub * BAR_SEC, unit="s", utc=True),
        "open": px[starts],
        "high": np.maximum.reduceat(px, starts),
        "low": np.minimum.reduceat(px, starts),
        "close": px[ends - 1],
        "n_tr": (ends - starts),
        "buy_v": np.bincount(bidx, weights=qty * buys, minlength=nb),
        "sell_v": np.bincount(bidx, weights=qty * (~buys), minlength=nb),
    })

    # ---- raw-tick level grid
    key = bar * KEY_SCALE + tick
    uk, inv = np.unique(key, return_inverse=True)
    n = len(uk)
    lvl_bar = uk // KEY_SCALE
    lvl_px = (uk % KEY_SCALE) * TICK
    lv = pd.DataFrame({
        "bar": lvl_bar, "price": lvl_px,
        "buy_v": np.bincount(inv, weights=qty * buys, minlength=n),
        "sell_v": np.bincount(inv, weights=qty * (~buys), minlength=n),
        "n_tr": np.bincount(inv, minlength=n).astype(np.int32),
    })

    # ---- edges: top/bottom K raw-tick traded levels per bar
    order = np.lexsort((lvl_px, lvl_bar))
    lv = lv.iloc[order]
    starts_l = np.searchsorted(lv["bar"].to_numpy(), ub, "left")
    ends_l = np.searchsorted(lv["bar"].to_numpy(), ub, "right")
    lp = lv["price"].to_numpy(); bv = lv["buy_v"].to_numpy()
    sv = lv["sell_v"].to_numpy(); nt = lv["n_tr"].to_numpy()
    rows = []
    for bi in range(nb):
        s, e = starts_l[bi], ends_l[bi]
        k = min(K_EDGE, e - s)
        for r in range(k):
            j = e - 1 - r
            rows.append((bi, r + 1, 1, lp[j], bv[j], sv[j], nt[j]))
        for r in range(k):
            j = s + r
            rows.append((bi, r + 1, -1, lp[j], bv[j], sv[j], nt[j]))
    edges = pd.DataFrame(rows, columns=["bar_i", "rank", "side", "price",
                                        "buy_v", "sell_v", "n_tr"])

    # ---- 1-USD bucket grid
    bk = np.rint(px / 1.0).astype(np.int64) + 500_000   # keep positive
    key2 = bar * 2_000_000 + bk
    uk2, inv2 = np.unique(key2, return_inverse=True)
    m = len(uk2)
    grid = pd.DataFrame({
        "bar_i": np.searchsorted(ub, uk2 // 2_000_000),  # positional, aligns with bars/edges
        "bucket_px": (uk2 % 2_000_000 - 500_000) * 1.0,
        "buy_v": np.bincount(inv2, weights=qty * buys, minlength=m),
        "sell_v": np.bincount(inv2, weights=qty * (~buys), minlength=m),
        "n_tr": np.bincount(inv2, minlength=m).astype(np.int32),
    })
    return bars, edges, grid


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "klines":
        for d in dates_range():
            dst = os.path.join(KL, f"{SYMBOL}-5m-{d}.parquet")
            if os.path.exists(dst):
                continue
            url = (f"https://data.binance.vision/data/futures/um/daily/klines/"
                   f"{SYMBOL}/5m/{SYMBOL}-5m-{d}.zip")
            blob = fetch(url)
            z = zipfile.ZipFile(io.BytesIO(blob))
            k = read_csv_skipheader(
                z, usecols=[0, 1, 2, 3, 4, 5, 8, 9],
                names=["open_ts", "open", "high", "low", "close", "vol", "cnt", "tbv"],
                dtype={"open": np.float64, "high": np.float64, "low": np.float64,
                       "close": np.float64, "vol": np.float64, "cnt": np.int64,
                       "tbv": np.float64})
            k["open_ts"] = pd.to_datetime(k["open_ts"], unit="ms", utc=True)
            k.to_parquet(dst)
        print("klines done:", len(dates_range()), "days", flush=True)
        return

    days = dates_range() if mode == "full" else [pd.Timestamp(END).date()]
    for d in days:
        dstb = os.path.join(PROC, f"bars_{SYMBOL}_{d}.parquet")
        if os.path.exists(dstb):
            print("skip", d, flush=True)
            continue
        url = (f"https://data.binance.vision/data/futures/um/daily/aggTrades/"
               f"{SYMBOL}/{SYMBOL}-aggTrades-{d}.zip")
        t0 = time.time()
        blob = fetch(url)
        bars, edges, grid = process_day(blob)
        bars.to_parquet(dstb)
        edges.to_parquet(os.path.join(PROC, f"edges_{SYMBOL}_{d}.parquet"))
        grid.to_parquet(os.path.join(PROC, f"grid_{SYMBOL}_{d}.parquet"))
        print(f"{d}  bars={len(bars)} edges={len(edges)} "
              f"grid={len(grid)/1e3:.0f}k  t={time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
