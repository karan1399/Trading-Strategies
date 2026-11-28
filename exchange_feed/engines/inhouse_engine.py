"""100% In-House Binance Footprint, Bar-Type (Time / Vol / Range) & Signal Engine.

Reads the local 1Y Binance Futures aggTrades parquet + millisecond live WS memory
buffer, optionally enriches OI via Binance's public openInterestHist endpoint, and
runs the reverse-engineered Coinglass signal engine (Absorption, Trapped traders,
Divergence, Swing CVD Divergence, Exhaustion) simultaneously across Time, Volume,
and Range bars — zero external Coinglass dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request
from typing import Any

import pyarrow.parquet as pq

from exchange_feed.data_pipeline.binance_dumper import DATA_DIR
from exchange_feed.engines.coinglass_parity import (
    _fmt_qty,
    auto_threshold,
    build_derived_bars,
    compute_cg_signals,
    merge_fp,
)
from exchange_feed.engines.footprint_engine import TF_SECONDS, _compute_va
from exchange_feed.data_pipeline.live_sync import SYNC_STATE, get_live_buffer_df
from exchange_feed.data_pipeline.metrics_store import enrich_raw_bars_with_metrics

_OI_CACHE: dict[str, tuple[float, dict[int, float]]] = {}
# Cache parsed parquet m1 bars keyed by (symbol, mtime_ns, size, lookback_bars)
_PARQUET_M1_CACHE: dict[str, tuple[int, int, int, list[dict[str, Any]]]] = {}


def _fetch_binance_oi(symbol: str, timeframe: str, ttl: float = 30.0) -> dict[int, float]:
    """Fetch recent Binance Futures OI history directly (no Coinglass dependency)."""
    period_map = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h"}
    period = period_map.get(timeframe)
    if not period:
        return {}
    key = f"{symbol}|{period}"
    now = time.time()
    cached = _OI_CACHE.get(key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    url = f"https://fapi.binance.com/futures/data/openInterestHist?{urllib.parse.urlencode({'symbol': symbol, 'period': period, 'limit': 200})}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "exchange_feed/1.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        out = {int(r["timestamp"]) // 1000: float(r["sumOpenInterest"]) for r in rows}
        _OI_CACHE[key] = (now, out)
        return out
    except Exception:
        return cached[1] if cached else {}


def _df_to_m1_bars(df) -> list[dict[str, Any]]:
    if df.empty:
        return []
    df = df.sort_values(["bar_1m", "price_bucket"])
    diffs = df["price_bucket"].diff().dropna()
    pos_diffs = diffs[diffs > 0]
    tick = float(pos_diffs.min()) if not pos_diffs.empty else 5.0

    ts_arr = df["bar_1m"].to_numpy(dtype="int64")
    p_arr = df["price_bucket"].to_numpy(dtype="float64")
    bq_arr = df["buy_qty"].to_numpy(dtype="float64")
    sq_arr = df["sell_qty"].to_numpy(dtype="float64")
    bu_arr = df["buy_usd"].to_numpy(dtype="float64")
    su_arr = df["sell_usd"].to_numpy(dtype="float64")
    bt_arr = df["buy_trades"].to_numpy(dtype="int64")
    st_arr = df["sell_trades"].to_numpy(dtype="int64")
    o_arr = df["open"].to_numpy(dtype="float64")
    h_arr = df["high"].to_numpy(dtype="float64")
    l_arr = df["low"].to_numpy(dtype="float64")
    c_arr = df["close"].to_numpy(dtype="float64")

    import numpy as np
    splits = np.flatnonzero(ts_arr[1:] != ts_arr[:-1]) + 1
    starts = np.concatenate(([0], splits))
    ends = np.concatenate((splits, [len(ts_arr)]))

    out: list[dict[str, Any]] = []
    for s_i, e_i in zip(starts, ends):
        lo = float(p_arr[s_i])
        hi_p = float(p_arr[e_i - 1])
        n = max(1, round((hi_p - lo) / tick) + 1)
        b_l, s_l = [0.0] * n, [0.0] * n
        bu_l, su_l = [0.0] * n, [0.0] * n
        bc_l, sc_l = [0] * n, [0] * n
        for k in range(s_i, e_i):
            idx = max(0, min(n - 1, round((float(p_arr[k]) - lo) / tick)))
            b_l[idx] += float(bq_arr[k])
            s_l[idx] += float(sq_arr[k])
            bu_l[idx] += float(bu_arr[k])
            su_l[idx] += float(su_arr[k])
            bc_l[idx] += int(bt_arr[k])
            sc_l[idx] += int(st_arr[k])
        out.append({
            "ts": int(ts_arr[s_i]),
            "o": float(o_arr[s_i]),
            "h": float(h_arr[s_i:e_i].max()),
            "l": float(l_arr[s_i:e_i].min()),
            "c": float(c_arr[e_i - 1]),
            "fp": {"tick": tick, "lo": lo, "b": b_l, "s": s_l, "bu": bu_l, "su": su_l, "bc": bc_l, "sc": sc_l},
            "oio": None,
            "oic": None,
        })
    return out


def load_inhouse_m1(symbol: str, lookback_bars: int = 4500, data_dir: Path = DATA_DIR) -> list[dict[str, Any]]:
    """Load recent 1m Binance fp bars from parquet (mtime-cached) + live un-flushed WS memory buffer."""
    sym = symbol.upper()
    path = data_dir / f"{sym}_agg_footprint.parquet"
    base_bars: list[dict[str, Any]] = []

    if path.is_file():
        st = path.stat()
        cached = _PARQUET_M1_CACHE.get(sym)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size and cached[2] >= lookback_bars:
            base_bars = cached[3][-lookback_bars:]
        else:
            min_ts = int(time.time()) - lookback_bars * 60
            try:
                df = pq.read_table(path, filters=[("bar_1m", ">=", min_ts)]).to_pandas()
            except Exception:
                df = pq.read_table(path).to_pandas()
                if not df.empty:
                    df = df[df["bar_1m"] >= min_ts]
            if df.empty:
                all_ts = pq.read_table(path, columns=["bar_1m"]).to_pandas()
                if not all_ts.empty:
                    cutoff = int(all_ts["bar_1m"].max()) - lookback_bars * 60
                    df = pq.read_table(path, filters=[("bar_1m", ">=", cutoff)]).to_pandas()
            if not df.empty:
                keep_ts = sorted(df["bar_1m"].unique())[-lookback_bars:]
                df = df[df["bar_1m"].isin(keep_ts)]
            base_bars = _df_to_m1_bars(df)
            _PARQUET_M1_CACHE[sym] = (st.st_mtime_ns, st.st_size, lookback_bars, base_bars)

    live_df = get_live_buffer_df(sym)
    if live_df.empty:
        return list(base_bars)

    live_bars = _df_to_m1_bars(live_df)
    if not live_bars:
        return list(base_bars)
    by_ts = {b["ts"]: b for b in base_bars}
    for lb in live_bars:
        by_ts[lb["ts"]] = lb
    return [by_ts[t] for t in sorted(by_ts)][-lookback_bars:]


def _group_m1_time(m1_bars: list[dict[str, Any]], tf_sec: int, oi_hist: dict[int, float]) -> list[dict[str, Any]]:
    """Aggregate 1m fp bars onto fixed clock timeframe boundaries."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for b in m1_bars:
        groups.setdefault((b["ts"] // tf_sec) * tf_sec, []).append(b)
    oi_sorted = sorted(oi_hist.items())
    out: list[dict[str, Any]] = []
    for bts in sorted(groups):
        gs = groups[bts]
        fp = merge_fp([x["fp"] for x in gs])
        if not fp:
            continue
        oio = oi_hist.get(bts)
        oic = oi_hist.get(bts + tf_sec)
        if oic is None and oi_sorted:
            # Find latest OI point inside this bar
            in_bar = [v for t, v in oi_sorted if bts < t <= bts + tf_sec]
            if in_bar:
                oic = in_bar[-1]
        out.append({
            "ts": bts,
            "o": gs[0]["o"],
            "h": max(x["h"] for x in gs),
            "l": min(x["l"] for x in gs),
            "c": gs[-1]["c"],
            "fp": fp,
            "oio": oio,
            "oic": oic,
            "m1_children": gs,
        })
    return out


def _intrabar_extrema(bar: dict[str, Any], unit: str) -> tuple[float, float] | None:
    children = bar.get("m1_children")
    if not children:
        return None
    cum = mx = mn = 0.0
    for ch in children:
        fp = ch.get("fp")
        if not fp:
            continue
        if unit == "coin":
            d = sum(fp["b"]) - sum(fp["s"])
        elif unit == "usd":
            d = sum(fp["bu"]) - sum(fp["su"])
        else:
            d = float(sum(fp["bc"]) - sum(fp["sc"]))
        cum += d
        if cum > mx:
            mx = cum
        if cum < mn:
            mn = cum
    return mx, mn


def build_all_signals_bundle(
    time_bars: list[dict[str, Any]],
    vol_bars: list[dict[str, Any]],
    range_bars: list[dict[str, Any]],
    timeframe: str,
    vol_thr: float,
    range_thr: float,
    display_limit: int,
) -> dict[str, Any]:
    """Compute cracked Coinglass signals across Time, Volume, and Range bars simultaneously."""
    vol_lbl = f"Volume {_fmt_qty(vol_thr)} BTC"
    rng_lbl = f"Range {int(range_thr) if range_thr >= 1 else range_thr}"

    def _tag(series: list[dict[str, Any]], bt: str, bt_label: str) -> list[dict[str, Any]]:
        sigs = compute_cg_signals(series)
        vis = series[-display_limit:]
        idx_map = {b["ts"]: idx for idx, b in enumerate(vis)}
        tagged = []
        for s in sigs:
            if s["ts"] not in idx_map:
                continue
            tagged.append({**s, "bt": bt, "bt_label": bt_label, "bar_index": idx_map[s["ts"]]})
        return tagged

    t_sigs = _tag(time_bars, "time", timeframe)
    v_sigs = _tag(vol_bars, "vol", vol_lbl)
    r_sigs = _tag(range_bars, "range", rng_lbl)
    combined = sorted(t_sigs + v_sigs + r_sigs, key=lambda x: x["ts"], reverse=True)
    return {
        "time_label": timeframe,
        "vol_label": vol_lbl,
        "range_label": rng_lbl,
        "vol_thr": vol_thr,
        "range_thr": range_thr,
        "counts": {"all": len(combined), "time": len(t_sigs), "vol": len(v_sigs), "range": len(r_sigs)},
        "items": combined,
    }


def format_footprint_series(
    raw_bars: list[dict[str, Any]],
    cg_sigs: list[dict[str, Any]],
    all_signals: dict[str, Any],
    source_name: str,
    symbol: str,
    timeframe: str,
    bar_type: str,
    bar_threshold: float | None,
    unit: str,
    row_size: float | None,
    imbalance_ratio: float,
    limit: int,
) -> dict[str, Any]:
    """Format native fp bars into frontend footprint payload + VPVR + zero-auctions."""
    sliced = raw_bars[-limit:]
    keep_ts = {b["ts"] for b in sliced}
    filtered_sigs = [s for s in cg_sigs if s["ts"] in keep_ts]

    eff_row = float(row_size) if (row_size is not None and row_size > 0) else (
        20.0 if (sliced and sliced[-1]["c"] >= 20000 and timeframe == "15m") else (
            5.0 if (sliced and sliced[-1]["c"] >= 20000) else max(round(sliced[-1]["c"] * 0.0005, 4), 0.01) if sliced else 20.0
        )
    )

    out_bars: list[dict[str, Any]] = []
    vpvr_map: dict[float, dict[str, float]] = {}

    for b in sliced:
        fp = b["fp"]
        buckets_by_lvl: dict[float, dict[str, float]] = {}
        for idx in range(len(fp["b"])):
            p_native = fp["lo"] + idx * fp["tick"]
            p_lvl = round(math.floor(p_native / eff_row + 1e-9) * eff_row, 6)
            cell = buckets_by_lvl.setdefault(p_lvl, {"bc": 0.0, "sc": 0.0, "bu": 0.0, "su": 0.0, "bt": 0, "st": 0})
            cell["bc"] += fp["b"][idx]
            cell["sc"] += fp["s"][idx]
            cell["bu"] += fp["bu"][idx]
            cell["su"] += fp["su"][idx]
            cell["bt"] += fp["bc"][idx]
            cell["st"] += fp["sc"][idx]

        levels: list[dict[str, Any]] = []
        large_cells = 0
        for p_lvl in sorted(buckets_by_lvl):
            c = buckets_by_lvl[p_lvl]
            bv, sv = (c["bc"], c["sc"]) if unit == "coin" else ((c["bu"], c["su"]) if unit == "usd" else (float(c["bt"]), float(c["st"])))
            if bv == 0 and sv == 0:
                continue
            tot = bv + sv
            dlt = bv - sv
            if (c["bu"] + c["su"]) >= 1_000_000:
                large_cells += 1
            levels.append({
                "price": p_lvl,
                "buy": round(bv, 3 if unit == "coin" else 2),
                "sell": round(sv, 3 if unit == "coin" else 2),
                "total": round(tot, 3 if unit == "coin" else 2),
                "delta": round(dlt, 3 if unit == "coin" else 2),
                "buy_trades": int(c["bt"]),
                "sell_trades": int(c["st"]),
                "buy_imb": False,
                "sell_imb": False,
                "zero_top": False,
                "zero_bot": False,
            })
            vp = vpvr_map.setdefault(p_lvl, {"price": p_lvl, "buy": 0.0, "sell": 0.0, "total": 0.0})
            vp["buy"] += bv
            vp["sell"] += sv
            vp["total"] += tot

        buy_imb_cnt = sell_imb_cnt = 0
        for idx, lv in enumerate(levels):
            if idx > 0:
                ps = levels[idx - 1]["sell"]
                if lv["buy"] > 0 and ((ps > 0 and lv["buy"] >= imbalance_ratio * ps) or (ps == 0 and lv["buy"] >= 5)):
                    lv["buy_imb"] = True
                    buy_imb_cnt += 1
            if idx < len(levels) - 1:
                nb = levels[idx + 1]["buy"]
                if lv["sell"] > 0 and ((nb > 0 and lv["sell"] >= imbalance_ratio * nb) or (nb == 0 and lv["sell"] >= 5)):
                    lv["sell_imb"] = True
                    sell_imb_cnt += 1

        zero_top = bool(levels and levels[-1]["sell"] == 0 and levels[-1]["buy"] > 0)
        zero_bot = bool(levels and levels[0]["buy"] == 0 and levels[0]["sell"] > 0)
        if zero_top:
            levels[-1]["zero_top"] = True
        if zero_bot:
            levels[0]["zero_bot"] = True

        poc, val, vah = _compute_va(levels, "total")
        bar_buy = sum(x["buy"] for x in levels)
        bar_sell = sum(x["sell"] for x in levels)
        bar_vol = bar_buy + bar_sell
        bar_delta = bar_buy - bar_sell
        ext = _intrabar_extrema(b, unit)
        max_cum_d = max(ext[0], bar_delta, 0.0) if ext else max(bar_delta, 0.0)
        min_cum_d = min(ext[1], bar_delta, 0.0) if ext else min(bar_delta, 0.0)
        coin_vol = sum(fp["b"]) + sum(fp["s"])
        bar_trades = sum(fp["bc"]) + sum(fp["sc"])
        avg_size = (coin_vol / bar_trades) if bar_trades > 0 else 0.0
        oi_chg = round(float(b["oic"]) - float(b["oio"]), 1) if (b.get("oic") is not None and b.get("oio") is not None) else None

        out_bars.append({
            "time": b["ts"],
            "open": round(b["o"], 2),
            "high": round(b["h"], 2),
            "low": round(b["l"], 2),
            "close": round(b["c"], 2),
            "volume": round(bar_vol, 3 if unit == "coin" else 2),
            "buy_vol": round(bar_buy, 3 if unit == "coin" else 2),
            "sell_vol": round(bar_sell, 3 if unit == "coin" else 2),
            "delta": round(bar_delta, 3 if unit == "coin" else 2),
            "delta_pct": round((bar_delta / bar_vol * 100.0) if bar_vol > 0 else 0.0, 1),
            "max_delta": round(max_cum_d, 2),
            "min_delta": round(min_cum_d, 2),
            "trades": int(bar_trades),
            "avg_size": round(avg_size, 4),
            "oi_close": round(float(b["oic"]), 1) if b.get("oic") is not None else None,
            "oi_change": oi_chg,
            "funding_rate": b.get("funding_rate"),
            "top_ls": b.get("top_ls"),
            "global_ls": b.get("global_ls"),
            "basis_bps": b.get("basis_bps"),
            "large_cells": large_cells,
            "poc": poc,
            "val": val,
            "vah": vah,
            "buy_imbalances": buy_imb_cnt,
            "sell_imbalances": sell_imb_cnt,
            "zero_top": zero_top,
            "zero_bot": zero_bot,
            "levels": levels,
        })

    ts_to_idx = {b["time"]: idx for idx, b in enumerate(out_bars)}
    for s in filtered_sigs:
        s["bar_index"] = ts_to_idx.get(s["ts"], -1)

    vpvr_levels = sorted(vpvr_map.values(), key=lambda x: x["price"])
    vp_poc, vp_val, vp_vah = _compute_va(vpvr_levels, "total")
    return {
        "source": source_name,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "bar_type": bar_type,
        "bar_threshold": bar_threshold,
        "unit": unit,
        "row_size": eff_row,
        "updated_at": int(time.time()),
        "bars": out_bars,
        "cg_signals": [s for s in filtered_sigs if s["bar_index"] >= 0],
        "all_signals": all_signals,
        "vpvr": {"poc": vp_poc, "val": vp_val, "vah": vp_vah, "levels": vpvr_levels},
    }


def load_inhouse_footprint(
    symbol: str = "BTCUSDT",
    timeframe: str = "15m",
    bar_type: str = "time",
    unit: str = "coin",
    row_size: float | None = None,
    imbalance_ratio: float = 3.0,
    limit: int = 150,
    data_dir: Path = DATA_DIR,
) -> dict[str, Any]:
    """Build 100% in-house Binance footprint bars + all 3 bar-type signals (Time / Vol / Range)."""
    tf_sec = TF_SECONDS.get(timeframe, 900)
    # Enough 1m history for 300 bars on selected TF plus 240+ bars for vol/range lookback
    lookback_1m = max(4500, (tf_sec // 60) * max(limit, 300)) if tf_sec <= 3600 else (tf_sec // 60) * max(limit, 320)
    lookback_1m = min(lookback_1m, 90_000)

    m1_bars = load_inhouse_m1(symbol, lookback_bars=lookback_1m, data_dir=data_dir)
    if not m1_bars:
        raise FileNotFoundError(f"No in-house Binance footprint parquet for {symbol}")

    oi_hist = _fetch_binance_oi(symbol.upper(), timeframe)
    time_bars = _group_m1_time(m1_bars, tf_sec, oi_hist)[-max(limit, 300):]

    m1_derived_pool = m1_bars[-4500:]
    vol_thr = auto_threshold(m1_derived_pool, "vol")
    range_thr = auto_threshold(m1_derived_pool, "range")
    vol_bars = build_derived_bars(m1_derived_pool, "vol", vol_thr)[-max(limit, 300):]
    range_bars = build_derived_bars(m1_derived_pool, "range", range_thr)[-max(limit, 300):]

    all_signals = build_all_signals_bundle(
        time_bars=time_bars,
        vol_bars=vol_bars,
        range_bars=range_bars,
        timeframe=timeframe,
        vol_thr=vol_thr,
        range_thr=range_thr,
        display_limit=limit,
    )

    if bar_type == "vol":
        active_bars, thr = vol_bars, vol_thr
    elif bar_type == "range":
        active_bars, thr = range_bars, range_thr
    else:
        active_bars, thr = time_bars, None

    live_snap = {
        "oi_btc": SYNC_STATE.get("live_oi"),
        "funding_rate": SYNC_STATE.get("live_funding") / 100.0 if SYNC_STATE.get("live_funding") is not None else None,
        "basis_bps": SYNC_STATE.get("live_basis_bps"),
    }
    enrich_raw_bars_with_metrics(
        active_bars,
        symbol=symbol,
        bar_type=bar_type,
        tf_sec=tf_sec,
        live_snap=live_snap,
        data_dir=data_dir,
    )

    active_sigs = compute_cg_signals(active_bars)
    return format_footprint_series(
        raw_bars=active_bars,
        cg_sigs=active_sigs,
        all_signals=all_signals,
        source_name="inhouse_binance",
        symbol=symbol,
        timeframe=timeframe,
        bar_type=bar_type,
        bar_threshold=thr,
        unit=unit,
        row_size=row_size,
        imbalance_ratio=imbalance_ratio,
        limit=limit,
    )
