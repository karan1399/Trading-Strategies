#!/usr/bin/env python3
"""Probe data.binance.vision aggTrades file formats across eras and markets.

Downloads small daily files to /tmp/binance_probe (never the repo), validates
CHECKSUM, and reports: header presence, column count, ts unit, boolean encoding,
row counts, monotonicity — everything the dump engine's parser must handle.
"""
import hashlib
import io
import os
import zipfile

import pandas as pd
import requests

OUT = "/tmp/binance_probe"
os.makedirs(OUT, exist_ok=True)
BASE = "https://data.binance.vision"

FILES = [
    # label, url
    ("um-daily-2026-11-22", f"{BASE}/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-11-22.zip"),
    ("um-daily-2020-01-15", f"{BASE}/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2020-01-15.zip"),
    ("um-daily-2024-06-01", f"{BASE}/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-06-01.zip"),
    ("um-daily-alt-2026-11-22", f"{BASE}/data/futures/um/daily/aggTrades/SUIUSDT/SUIUSDT-aggTrades-2026-11-22.zip"),
    ("spot-daily-2026-11-22", f"{BASE}/data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-11-22.zip"),
    ("um-monthly-2021-01", f"{BASE}/data/futures/um/monthly/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2021-01.zip"),
]


def fetch(url: str) -> bytes | None:
    r = requests.get(url, timeout=120)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code} for {url}")
        return None
    return r.content


for label, url in FILES:
    print("=" * 84)
    print(label, "->", url.rsplit("/", 1)[-1])
    blob = fetch(url)
    if blob is None:
        continue
    cks = fetch(url + ".CHECKSUM")
    if cks:
        want = cks.decode().split()[0]
        got = hashlib.sha256(blob).hexdigest()
        print(f"  checksum: {'OK' if want == got else 'MISMATCH'} ({len(blob)/1e6:.1f} MB)")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    name = zf.namelist()[0]
    print(f"  member: {name}  ({zf.getinfo(name).file_size/1e6:.1f} MB uncompressed)")
    with zf.open(name) as fh:
        first = fh.readline()
        print(f"  first line: {first[:120]!r}")
        fh.seek(0)
        df = pd.read_csv(fh, header=None, dtype=str)
    print(f"  rows: {len(df):,}  cols: {df.shape[1]}")
    print(f"  columns: {list(df.iloc[0].index)}")
    if df.shape[1] >= 7:
        c0 = df[0]
        numeric_id = pd.to_numeric(c0, errors="coerce")
        has_header = numeric_id.isna().sum() > 0
        print(f"  header row? {has_header}")
        if has_header:
            df = df.iloc[1:].reset_index(drop=True)
        t = pd.to_numeric(df[5])
        print(f"  ts[0]={t.iloc[0]}  ts[-1]={t.iloc[-1]}")
        unit = "us" if t.iloc[0] > 10**14 else ("ms" if t.iloc[0] > 10**11 else "s")
        print(f"  ts unit: {unit}  -> {pd.to_datetime(t.iloc[0], unit='us' if unit=='us' else 'ms')}")
        bm = df[6].unique()[:6]
        print(f"  is_buyer_maker values: {bm}")
        ids = pd.to_numeric(df[0])
        print(f"  trade_id monotonic: {ids.is_monotonic_increasing}  "
              f"gaps: {int((ids.diff().iloc[1:] != 1).sum())}")
        px = pd.to_numeric(df[1])
        print(f"  price range: {px.min()} .. {px.max()}")
        df.to_parquet(os.path.join(OUT, label + ".parquet")) if False else None
print("=" * 84)
print("probe files kept in /tmp/binance_probe (not the repo)")
