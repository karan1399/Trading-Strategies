"""Direct Binance Futures Metrics Store (OI, Funding, L/S Ratios, Basis) & Materialized Bar Parquets.

Zero Coinglass dependency:
  - Backfills up to 30 days of 5m OI, Top Trader L/S, Global L/S, Taker L/S, and 8h Funding
    from Binance Futures public endpoints into <SYMBOL>_metrics_5m.parquet.
  - Enriches Time, Volume, and Range bars with oi_open, oi_close, oi_change, funding_rate,
    top_ls, global_ls, and basis_bps via fast vectorized timestamp search.
  - Materializes and incrementally appends <SYMBOL>_bars_15m.parquet,
    <SYMBOL>_bars_vol_<THR>.parquet, and <SYMBOL>_bars_range_<THR>.parquet.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from exchange_feed.data_pipeline.binance_dumper import DATA_DIR
from exchange_feed.engines.coinglass_parity import auto_threshold, build_derived_bars, merge_fp
from exchange_feed.engines.footprint_engine import TF_SECONDS, _compute_va

_METRICS_LOCK = threading.Lock()
_METRICS_CACHE: dict[str, tuple[int, int, pd.DataFrame]] = {}
_LIVE_OI_SERIES: dict[str, list[tuple[int, float]]] = {}  # sym -> [(ts_sec, oi_btc), ...]

FAPI_BASE = "https://fapi.binance.com"


def _fetch_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "exchange_feed/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_live_derivatives_snapshot(symbol: str = "BTCUSDT") -> dict[str, Any]:
    """Fetch instant live OI (BTC) + Mark/Index/Funding snapshot directly from Binance Futures."""
    sym = symbol.upper()
    out: dict[str, Any] = {"symbol": sym, "ts": int(time.time())}
    try:
        oi = _fetch_json(f"{FAPI_BASE}/fapi/v1/openInterest?symbol={sym}", timeout=4.0)
        oi_btc = float(oi["openInterest"])
        ts_sec = int(oi.get("time", int(time.time() * 1000))) // 1000
        out["oi_btc"] = oi_btc
        out["oi_ts"] = ts_sec
        hist = _LIVE_OI_SERIES.setdefault(sym, [])
        if not hist or ts_sec > hist[-1][0]:
            hist.append((ts_sec, oi_btc))
            if len(hist) > 720:
                del hist[:-720]
    except Exception:
        pass

    try:
        pm = _fetch_json(f"{FAPI_BASE}/fapi/v1/premiumIndex?symbol={sym}", timeout=4.0)
        mark = float(pm["markPrice"])
        idx = float(pm["indexPrice"])
        out["mark_price"] = mark
        out["index_price"] = idx
        out["funding_rate"] = float(pm["lastFundingRate"])
        out["basis_bps"] = round(((mark - idx) / idx) * 10000.0, 2) if idx > 0 else 0.0
    except Exception:
        pass
    return out


def _fetch_futures_data_paginated(path: str, symbol: str, period: str, days: int, val_map: dict[str, str]) -> pd.DataFrame:
    """Fetch paginated /futures/data/* 5m history backwards from now (max ~30d supported by Binance)."""
    now_ms = int(time.time() * 1000)
    earliest_ms = now_ms - days * 86400 * 1000
    end_ms = now_ms
    rows_all: list[dict[str, Any]] = []
    seen_ts: set[int] = set()

    for _ in range(25):
        params = {"symbol": symbol, "period": period, "limit": 500, "endTime": end_ms}
        url = f"{FAPI_BASE}{path}?{urllib.parse.urlencode(params)}"
        try:
            batch = _fetch_json(url, timeout=8.0)
        except Exception:
            break
        if not isinstance(batch, list) or not batch:
            break
        batch.sort(key=lambda r: int(r["timestamp"]))
        added = 0
        for r in batch:
            ts_ms = int(r["timestamp"])
            if ts_ms in seen_ts or ts_ms < earliest_ms:
                continue
            seen_ts.add(ts_ms)
            rec = {"ts": ts_ms // 1000}
            for src_k, dst_k in val_map.items():
                if src_k in r and r[src_k] is not None:
                    rec[dst_k] = float(r[src_k])
            rows_all.append(rec)
            added += 1
        oldest_ms = int(batch[0]["timestamp"])
        if oldest_ms <= earliest_ms or len(batch) < 400 or added == 0:
            break
        end_ms = oldest_ms - 1
        time.sleep(0.08)

    if not rows_all:
        return pd.DataFrame()
    return pd.DataFrame(rows_all).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def _fetch_funding_history(symbol: str, days: int = 30) -> pd.DataFrame:
    start_ms = int(time.time() * 1000) - days * 86400 * 1000
    url = f"{FAPI_BASE}/fapi/v1/fundingRate?{urllib.parse.urlencode({'symbol': symbol, 'startTime': start_ms, 'limit': 1000})}"
    try:
        rows = _fetch_json(url, timeout=8.0)
    except Exception:
        return pd.DataFrame()
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    df = pd.DataFrame([
        {
            "ts": (int(r["fundingTime"]) // 300000) * 300,
            "funding_rate": float(r["fundingRate"]),
            "mark_price": float(r["markPrice"]) if r.get("markPrice") else np.nan,
        }
        for r in rows
    ])
    return df.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)


def _atomic_write_parquet(df: pd.DataFrame, out_path: Path, dedup_cols: list[str], sort_cols: list[str]) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.stem}.tmp.{os.getpid()}.parquet")
    with _METRICS_LOCK:
        if out_path.is_file():
            old = pq.read_table(out_path).to_pandas()
            if not old.empty:
                df = pd.concat([old, df], ignore_index=True).drop_duplicates(subset=dedup_cols, keep="last")
        df = df.sort_values(sort_cols).reset_index(drop=True)
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp_path, compression="snappy")
        for attempt in range(5):
            try:
                os.replace(tmp_path, out_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.15)
        return len(df)


def sync_binance_metrics(symbol: str = "BTCUSDT", days: int = 3, data_dir: Path = DATA_DIR) -> int:
    """Sync Binance 5m OI, Top L/S, Global L/S, Taker L/S, and Funding into <SYMBOL>_metrics_5m.parquet."""
    sym = symbol.upper()
    out_path = data_dir / f"{sym}_metrics_5m.parquet"
    # Backfill 29d if file doesn't exist yet; otherwise incremental 2d refresh
    fetch_days = 29 if not out_path.is_file() else days

    oi_df = _fetch_futures_data_paginated(
        "/futures/data/openInterestHist", sym, "5m", fetch_days,
        {"sumOpenInterest": "oi_btc", "sumOpenInterestValue": "oi_usd"},
    )
    if oi_df.empty:
        return 0

    top_df = _fetch_futures_data_paginated(
        "/futures/data/topLongShortPositionRatio", sym, "5m", fetch_days,
        {"longShortRatio": "top_ls"},
    )
    glb_df = _fetch_futures_data_paginated(
        "/futures/data/globalLongShortAccountRatio", sym, "5m", fetch_days,
        {"longShortRatio": "global_ls"},
    )
    tkr_df = _fetch_futures_data_paginated(
        "/futures/data/takerlongshortRatio", sym, "5m", fetch_days,
        {"buySellRatio": "taker_ls"},
    )
    fnd_df = _fetch_funding_history(sym, days=fetch_days)

    merged = oi_df
    for extra in (top_df, glb_df, tkr_df):
        if not extra.empty:
            merged = merged.merge(extra, on="ts", how="left")
    if not fnd_df.empty:
        merged = pd.merge_asof(
            merged.sort_values("ts"),
            fnd_df.sort_values("ts"),
            on="ts",
            direction="backward",
        )
    else:
        merged["funding_rate"] = np.nan
        merged["mark_price"] = np.nan

    # Overlay latest live snapshot onto current 5m slot
    snap = fetch_live_derivatives_snapshot(sym)
    if "oi_btc" in snap:
        cur_5m = (int(time.time()) // 300) * 300
        live_row = {
            "ts": cur_5m,
            "oi_btc": snap["oi_btc"],
            "oi_usd": snap["oi_btc"] * snap.get("mark_price", 0.0) if snap.get("mark_price") else np.nan,
            "top_ls": float(merged["top_ls"].iloc[-1]) if "top_ls" in merged and not merged["top_ls"].dropna().empty else np.nan,
            "global_ls": float(merged["global_ls"].iloc[-1]) if "global_ls" in merged and not merged["global_ls"].dropna().empty else np.nan,
            "taker_ls": float(merged["taker_ls"].iloc[-1]) if "taker_ls" in merged and not merged["taker_ls"].dropna().empty else np.nan,
            "funding_rate": snap.get("funding_rate", np.nan),
            "mark_price": snap.get("mark_price", np.nan),
            "basis_bps": snap.get("basis_bps", np.nan),
        }
        merged["basis_bps"] = np.nan
        merged = pd.concat([merged, pd.DataFrame([live_row])], ignore_index=True).drop_duplicates("ts", keep="last")
    else:
        merged["basis_bps"] = np.nan

    for col in ("oi_btc", "oi_usd", "top_ls", "global_ls", "taker_ls", "funding_rate", "mark_price", "basis_bps"):
        if col not in merged.columns:
            merged[col] = np.nan
    merged["symbol"] = sym
    return _atomic_write_parquet(merged, out_path, dedup_cols=["ts"], sort_cols=["ts"])


def load_metrics_df(symbol: str = "BTCUSDT", data_dir: Path = DATA_DIR) -> pd.DataFrame:
    sym = symbol.upper()
    path = data_dir / f"{sym}_metrics_5m.parquet"
    if not path.is_file():
        return pd.DataFrame()
    st = path.stat()
    cached = _METRICS_CACHE.get(sym)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    df = pq.read_table(path).to_pandas().sort_values("ts").reset_index(drop=True)
    _METRICS_CACHE[sym] = (st.st_mtime_ns, st.st_size, df)
    return df


def enrich_raw_bars_with_metrics(
    bars: list[dict[str, Any]],
    symbol: str = "BTCUSDT",
    bar_type: str = "time",
    tf_sec: int = 900,
    live_snap: dict[str, Any] | None = None,
    data_dir: Path = DATA_DIR,
) -> None:
    """Attach oio, oic, funding_rate, top_ls, global_ls, basis_bps to raw bars in-place."""
    if not bars:
        return
    df = load_metrics_df(symbol, data_dir=data_dir)
    if df.empty:
        return

    ts_arr = df["ts"].to_numpy(dtype="int64")
    oi_arr = df["oi_btc"].to_numpy(dtype="float64")
    top_arr = df["top_ls"].to_numpy(dtype="float64") if "top_ls" in df else np.full(len(ts_arr), np.nan)
    glb_arr = df["global_ls"].to_numpy(dtype="float64") if "global_ls" in df else np.full(len(ts_arr), np.nan)
    fnd_arr = df["funding_rate"].to_numpy(dtype="float64") if "funding_rate" in df else np.full(len(ts_arr), np.nan)
    bas_arr = df["basis_bps"].to_numpy(dtype="float64") if "basis_bps" in df else np.full(len(ts_arr), np.nan)

    def _lookup_oi(target_ts: int) -> float | None:
        idx = int(np.searchsorted(ts_arr, target_ts, side="right")) - 1
        if idx < 0:
            return None
        val = float(oi_arr[idx])
        return val if math.isfinite(val) else None

    for i, b in enumerate(bars):
        b_start = int(b["ts"])
        b_end = int(bars[i + 1]["ts"]) if i + 1 < len(bars) else (b_start + tf_sec if bar_type == "time" else int(time.time()))
        oio = _lookup_oi(b_start)
        oic = _lookup_oi(b_end - 1) if i + 1 < len(bars) else _lookup_oi(int(time.time()))
        if oio is not None:
            b["oio"] = oio
        if oic is not None:
            b["oic"] = oic

        idx_m = max(0, int(np.searchsorted(ts_arr, b_end, side="right")) - 1)
        if idx_m < len(ts_arr):
            if math.isfinite(float(top_arr[idx_m])):
                b["top_ls"] = round(float(top_arr[idx_m]), 2)
            if math.isfinite(float(glb_arr[idx_m])):
                b["global_ls"] = round(float(glb_arr[idx_m]), 2)
            if math.isfinite(float(fnd_arr[idx_m])):
                b["funding_rate"] = round(float(fnd_arr[idx_m]) * 100.0, 4)  # %
            if math.isfinite(float(bas_arr[idx_m])):
                b["basis_bps"] = round(float(bas_arr[idx_m]), 2)

    # Apply millisecond-live OI / Funding / Basis snapshot to tip forming bar
    if live_snap and bars:
        last = bars[-1]
        if live_snap.get("oi_btc"):
            last["oic"] = float(live_snap["oi_btc"])
            if last.get("oio") is None:
                hist = _LIVE_OI_SERIES.get(symbol.upper(), [])
                for t_s, oi_v in hist:
                    if t_s >= int(last["ts"]):
                        last["oio"] = oi_v
                        break
        if live_snap.get("funding_rate") is not None:
            last["funding_rate"] = round(float(live_snap["funding_rate"]) * 100.0, 4)
        if live_snap.get("basis_bps") is not None:
            last["basis_bps"] = round(float(live_snap["basis_bps"]), 2)


def _bars_to_materialized_df(bars: list[dict[str, Any]], row_size: float, symbol: str) -> pd.DataFrame:
    """Convert raw bars with fp & metrics into a compact parquet DataFrame for instant loading/backtesting."""
    records: list[dict[str, Any]] = []
    for b in bars:
        fp = b.get("fp")
        if not fp:
            continue
        buckets: dict[float, list[float]] = {}
        for idx in range(len(fp["b"])):
            p_native = fp["lo"] + idx * fp["tick"]
            p_lvl = round(math.floor(p_native / row_size + 1e-9) * row_size, 6)
            c = buckets.setdefault(p_lvl, [0.0, 0.0, 0.0, 0.0])
            c[0] += fp["b"][idx]
            c[1] += fp["s"][idx]
            c[2] += fp["bc"][idx]
            c[3] += fp["sc"][idx]

        levels = []
        for p_lvl in sorted(buckets):
            bv, sv, bt, st = buckets[p_lvl]
            if bv == 0 and sv == 0:
                continue
            levels.append([p_lvl, round(bv, 4), round(sv, 4), int(bt), int(st)])
        if not levels:
            continue
        bv_tot = sum(x[1] for x in levels)
        sv_tot = sum(x[2] for x in levels)
        vol = bv_tot + sv_tot
        delta = bv_tot - sv_tot
        trades = sum(x[3] + x[4] for x in levels)
        poc, val, vah = _compute_va([{"price": x[0], "total": x[1] + x[2]} for x in levels], "total")
        z_top = levels[-1][2] == 0 and levels[-1][1] > 0
        z_bot = levels[0][1] == 0 and levels[0][2] > 0
        oi_chg = round(float(b["oic"]) - float(b["oio"]), 1) if (b.get("oic") is not None and b.get("oio") is not None) else np.nan
        records.append({
            "ts": int(b["ts"]),
            "open": float(b["o"]),
            "high": float(b["h"]),
            "low": float(b["l"]),
            "close": float(b["c"]),
            "volume": round(vol, 4),
            "buy_vol": round(bv_tot, 4),
            "sell_vol": round(sv_tot, 4),
            "delta": round(delta, 4),
            "delta_pct": round((delta / vol * 100.0) if vol > 0 else 0.0, 2),
            "trades": int(trades),
            "avg_size": round(vol / trades, 5) if trades > 0 else 0.0,
            "poc": poc,
            "val": val,
            "vah": vah,
            "zero_top": bool(z_top),
            "zero_bot": bool(z_bot),
            "oi_open": float(b["oio"]) if b.get("oio") is not None else np.nan,
            "oi_close": float(b["oic"]) if b.get("oic") is not None else np.nan,
            "oi_change": oi_chg,
            "funding_rate": float(b["funding_rate"]) if b.get("funding_rate") is not None else np.nan,
            "top_ls": float(b["top_ls"]) if b.get("top_ls") is not None else np.nan,
            "global_ls": float(b["global_ls"]) if b.get("global_ls") is not None else np.nan,
            "basis_bps": float(b["basis_bps"]) if b.get("basis_bps") is not None else np.nan,
            "row_size": row_size,
            "levels_json": json.dumps(levels, separators=(",", ":")),
            "symbol": symbol.upper(),
        })
    return pd.DataFrame(records)


def materialize_bar_parquets(
    symbol: str = "BTCUSDT",
    m1_bars: list[dict[str, Any]] | None = None,
    data_dir: Path = DATA_DIR,
) -> dict[str, int]:
    """Build and incrementally append 15m, Volume, and Range bar parquet files."""
    from exchange_feed.engines.inhouse_engine import _group_m1_time, load_inhouse_m1

    sym = symbol.upper()
    if m1_bars is None:
        m1_bars = load_inhouse_m1(sym, lookback_bars=43_200, data_dir=data_dir)  # up to 30d
    if not m1_bars:
        return {}

    snap = fetch_live_derivatives_snapshot(sym)

    # 1. 15m Time Bars
    time_15m = _group_m1_time(m1_bars, TF_SECONDS["15m"], {})
    enrich_raw_bars_with_metrics(time_15m, sym, bar_type="time", tf_sec=900, live_snap=snap, data_dir=data_dir)
    df_15m = _bars_to_materialized_df(time_15m, row_size=20.0, symbol=sym)
    n_15m = _atomic_write_parquet(df_15m, data_dir / f"{sym}_bars_15m.parquet", ["ts"], ["ts"]) if not df_15m.empty else 0

    # 2. Volume Bars (Auto threshold, e.g. 1000 BTC)
    m1_recent = m1_bars[-15_000:]
    vol_thr = auto_threshold(m1_recent, "vol")
    vol_bars = build_derived_bars(m1_recent, "vol", vol_thr)
    enrich_raw_bars_with_metrics(vol_bars, sym, bar_type="vol", tf_sec=900, live_snap=snap, data_dir=data_dir)
    df_vol = _bars_to_materialized_df(vol_bars, row_size=20.0, symbol=sym)
    vol_tag = int(vol_thr) if vol_thr >= 1 else str(vol_thr).replace(".", "_")
    n_vol = _atomic_write_parquet(df_vol, data_dir / f"{sym}_bars_vol_{vol_tag}.parquet", ["ts"], ["ts"]) if not df_vol.empty else 0

    # 3. Range Bars (Auto threshold, e.g. 200)
    rng_thr = auto_threshold(m1_recent, "range")
    rng_bars = build_derived_bars(m1_recent, "range", rng_thr)
    enrich_raw_bars_with_metrics(rng_bars, sym, bar_type="range", tf_sec=900, live_snap=snap, data_dir=data_dir)
    df_rng = _bars_to_materialized_df(rng_bars, row_size=20.0, symbol=sym)
    rng_tag = int(rng_thr) if rng_thr >= 1 else str(rng_thr).replace(".", "_")
    n_rng = _atomic_write_parquet(df_rng, data_dir / f"{sym}_bars_range_{rng_tag}.parquet", ["ts"], ["ts"]) if not df_rng.empty else 0

    return {
        "metrics_5m": len(load_metrics_df(sym, data_dir=data_dir)),
        "bars_15m": n_15m,
        f"bars_vol_{vol_tag}": n_vol,
        f"bars_range_{rng_tag}": n_rng,
    }


def list_materialized_parquets(symbol: str = "BTCUSDT", data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Return lightweight inventory of persisted metrics and multi-bar parquet archives."""
    sym = symbol.upper()
    out: dict[str, Any] = {}
    for p in sorted(data_dir.glob(f"{sym}_*.parquet")):
        try:
            meta = pq.read_metadata(p)
            out[p.name] = {"rows": meta.num_rows, "mb": round(p.stat().st_size / 1_048_576, 2)}
        except Exception:
            pass
    return out
