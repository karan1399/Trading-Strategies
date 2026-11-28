"""Dump ALL live Futures contracts (262+) from Delta Exchange India into Delta_Exchange_Data/.

Fetches product catalog + live turnover/spread snapshot from api.india.delta.exchange,
then paginates /v2/history/candles backwards in 3,800-bar chunks across 4h, 1h, and 1d
for every live perpetual/dated future contract, saving clean Parquet OHLCV files + CSV catalog.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.request

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

BASE_URL = "https://api.india.delta.exchange"
OUT_DIR = Path(r"C:\Users\SIGMA\Documents\Trading-Strategies\historical_data\delta_exchange")
HEADERS = {"User-Agent": "QuantResearch/1.0"}

TF_SEC = {"1h": 3600, "4h": 14400, "1d": 86400}


def _get_json(url: str, retries: int = 4) -> dict[str, Any]:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            if he.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.0 * (2 ** attempt))
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise
    return {}


def fetch_catalog() -> list[dict[str, Any]]:
    prods = _get_json(f"{BASE_URL}/v2/products").get("result", [])
    tickers = _get_json(f"{BASE_URL}/v2/tickers").get("result", [])
    tk_map = {str(t.get("symbol")): t for t in tickers}

    futs: list[dict[str, Any]] = []
    for p in prods:
        ct = str(p.get("contract_type", "")).lower()
        st = str(p.get("state", "")).lower()
        if "futures" not in ct or st != "live":
            continue
        sym = str(p["symbol"])
        tk = tk_map.get(sym, {})
        q = tk.get("quotes", {}) or {}
        best_bid = float(q.get("best_bid") or 0.0)
        best_ask = float(q.get("best_ask") or 0.0)
        mark = float(tk.get("mark_price") or 0.0)
        mid = 0.5 * (best_bid + best_ask) if (best_bid > 0 and best_ask >= best_bid) else mark
        sp_bps = ((best_ask - best_bid) / mid * 10000.0) if (mid > 0 and best_ask >= best_bid and best_bid > 0) else 8.0
        futs.append({
            "symbol": sym,
            "id": p.get("id"),
            "contract_type": p.get("contract_type"),
            "description": p.get("description"),
            "launch_time": p.get("launch_time"),
            "contract_value": float(p.get("contract_value") or 1.0),
            "tick_size": float(p.get("tick_size") or 0.0),
            "taker_fee_bps": float(p.get("taker_commission_rate") or 0.0005) * 10000.0,
            "maker_fee_bps": float(p.get("maker_commission_rate") or 0.0002) * 10000.0,
            "mark_price": mark,
            "turnover_usd": float(tk.get("turnover_usd") or 0.0),
            "oi_usd": float(tk.get("oi_value_usd") or 0.0),
            "live_sp_bps": round(sp_bps, 2),
        })
    futs.sort(key=lambda x: x["turnover_usd"], reverse=True)
    return futs


def fetch_paginated_candles(symbol: str, resolution: str, min_ts: int, max_ts: int) -> pd.DataFrame:
    step = TF_SEC[resolution]
    chunk_span = 3800 * step
    cur_end = max_ts
    batches: list[list[dict[str, Any]]] = []

    for _ in range(12):  # up to ~45,600 candles
        cur_start = max(min_ts, cur_end - chunk_span)
        if cur_start >= cur_end:
            break
        url = f"{BASE_URL}/v2/history/candles?resolution={resolution}&symbol={symbol}&start={cur_start}&end={cur_end}"
        try:
            rows = _get_json(url).get("result", []) or []
        except Exception:
            break
        if not rows:
            break
        batches.append(rows)
        earliest = min(int(r["time"]) for r in rows)
        if len(rows) < 200 or earliest <= min_ts:
            break
        cur_end = earliest - 1
        time.sleep(0.03)

    if not batches:
        return pd.DataFrame(columns=["timestamp", "datetime", "open", "high", "low", "close", "volume"])
    flat = [r for b in batches for r in b]
    df = pd.DataFrame(flat)
    df = df.rename(columns={"time": "timestamp"})[["timestamp", "open", "high", "low", "close", "volume"]]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("int64")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
    return df[["timestamp", "datetime", "open", "high", "low", "close", "volume"]]


def dump_one_symbol(prod: dict[str, Any], resolutions: tuple[str, ...] = ("4h", "1h", "1d")) -> dict[str, Any]:
    sym = prod["symbol"]
    now_s = int(time.time())
    floor_s = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp())
    lt = prod.get("launch_time")
    if lt:
        try:
            lt_s = int(datetime.fromisoformat(str(lt).replace("Z", "+00:00")).timestamp())
            floor_s = max(floor_s, lt_s - 86400)
        except Exception:
            pass

    counts: dict[str, int] = {}
    span_info: dict[str, str] = {}
    for res in resolutions:
        sub_dir = OUT_DIR / f"ohlc_{res}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        out_p = sub_dir / f"{sym}.parquet"
        df = fetch_paginated_candles(sym, res, floor_s, now_s)
        if not df.empty:
            pq.write_table(pa.Table.from_pandas(df, preserve_index=False), out_p, compression="zstd")
            counts[res] = len(df)
            span_info[res] = f"{df['datetime'].iloc[0][:10]}..{df['datetime'].iloc[-1][:10]}"
        else:
            counts[res] = 0
            span_info[res] = "-"
    return {
        **prod,
        "bars_4h": counts.get("4h", 0),
        "bars_1h": counts.get("1h", 0),
        "bars_1d": counts.get("1d", 0),
        "span_4h": span_info.get("4h", "-"),
        "span_1h": span_info.get("1h", "-"),
    }


def run_dump(workers: int = 10, limit: int = 0) -> list[dict[str, Any]]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    futs = fetch_catalog()
    if limit > 0:
        futs = futs[:limit]
    print(f"[delta-dump] Discovered {len(futs)} live futures on {BASE_URL}. Dumping 4h/1h/1d OHLC to {OUT_DIR}...", flush=True)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fmap = {pool.submit(dump_one_symbol, p): p["symbol"] for p in futs}
        done = 0
        for fut in as_completed(fmap):
            done += 1
            sym = fmap[fut]
            try:
                res = fut.result()
                results.append(res)
            except Exception as exc:
                print(f"[delta-dump] ERR {sym}: {exc}", flush=True)
            if done % 25 == 0 or done == len(futs):
                ok_4h = sum(1 for r in results if r["bars_4h"] > 0)
                print(f"[delta-dump] Progress {done}/{len(futs)} (4H valid={ok_4h})", flush=True)

    results.sort(key=lambda x: x["turnover_usd"], reverse=True)
    (OUT_DIR / "delta_products_catalog.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    pd.DataFrame(results).to_csv(OUT_DIR / "catalog_summary.csv", index=False)
    print(f"[delta-dump] COMPLETE: {len(results)} futures cataloged; 4H non-empty={sum(1 for r in results if r['bars_4h']>0)}; 1H non-empty={sum(1 for r in results if r['bars_1h']>0)}", flush=True)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    run_dump(workers=args.workers, limit=args.limit)
