"""Footprint aggregation, imbalance, Value Area, VPVR, and zero-auction engine."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR

TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}


def list_datasets(data_dir: Path = DATA_DIR) -> list[str]:
    """List available footprint symbols in data_dir."""
    if not data_dir.is_dir():
        return []
    return sorted(p.name.replace("_agg_footprint.parquet", "") for p in data_dir.glob("*_agg_footprint.parquet"))


def auto_row_size(df_1m: pd.DataFrame, timeframe: str) -> float:
    """Choose a readable Coinglass-style price step based on recent bar range."""
    if df_1m.empty:
        return 20.0
    px = float(df_1m["close"].iloc[-1])
    if px >= 20000:
        return {"1m": 5.0, "5m": 10.0, "15m": 20.0, "30m": 25.0, "1h": 40.0, "4h": 100.0}.get(timeframe, 20.0)
    if px >= 1000:
        return {"1m": 0.5, "5m": 1.0, "15m": 2.0, "30m": 4.0, "1h": 5.0, "4h": 10.0}.get(timeframe, 2.0)
    return max(round(px * 0.0005, 4), 0.0001)


def _compute_va(levels: list[dict[str, Any]], val_key: str, pct: float = 0.70) -> tuple[float, float, float]:
    """Compute POC, VAL, VAH covering 70% of bar total."""
    if not levels:
        return 0.0, 0.0, 0.0
    sorted_lvls = sorted(levels, key=lambda x: x["price"])
    poc_idx = max(range(len(sorted_lvls)), key=lambda i: sorted_lvls[i][val_key])
    poc = sorted_lvls[poc_idx]["price"]
    total = sum(x[val_key] for x in sorted_lvls)
    if total <= 0:
        return poc, sorted_lvls[0]["price"], sorted_lvls[-1]["price"]

    target = total * pct
    acc = sorted_lvls[poc_idx][val_key]
    lo = hi = poc_idx
    while acc < target and (lo > 0 or hi < len(sorted_lvls) - 1):
        left_val = sorted_lvls[lo - 1][val_key] if lo > 0 else -1.0
        right_val = sorted_lvls[hi + 1][val_key] if hi < len(sorted_lvls) - 1 else -1.0
        if right_val >= left_val and hi < len(sorted_lvls) - 1:
            hi += 1
            acc += sorted_lvls[hi][val_key]
        elif lo > 0:
            lo -= 1
            acc += sorted_lvls[lo][val_key]
        else:
            break
    return poc, sorted_lvls[lo]["price"], sorted_lvls[hi]["price"]


def build_footprint_bars(
    symbol: str = "BTCUSDT",
    timeframe: str = "15m",
    unit: str = "trades",  # "trades" | "coin" | "usd"
    row_size: float | None = None,
    imbalance_ratio: float = 3.0,
    limit: int = 80,
    data_dir: Path = DATA_DIR,
) -> dict[str, Any]:
    """Load fine-grained parquet and aggregate into Coinglass-style footprint bars."""
    path = data_dir / f"{symbol}_agg_footprint.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"Footprint parquet not found: {path}")

    raw = pq.read_table(path).to_pandas()
    if raw.empty:
        return {"symbol": symbol, "timeframe": timeframe, "unit": unit, "row_size": 20.0, "bars": [], "vpvr": {}}

    tf_sec = TF_SECONDS.get(timeframe, 900)
    eff_row = float(row_size) if (row_size is not None and row_size > 0) else auto_row_size(raw, timeframe)

    raw = raw.copy()
    raw["bar_ts"] = (raw["bar_1m"].astype("int64") // tf_sec) * tf_sec
    raw["p_lvl"] = (np.floor(raw["price_bucket"].astype("float64") / eff_row) * eff_row).round(6)

    # Bar OHLC from 1m OHLC
    ohlc_1m = raw[["bar_1m", "bar_ts", "open", "high", "low", "close"]].drop_duplicates("bar_1m").sort_values("bar_1m")
    last_minute = ohlc_1m.groupby("bar_ts")["bar_1m"].max()
    bar_ohlc = (
        ohlc_1m.groupby("bar_ts", as_index=False)
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        .sort_values("bar_ts")
    )
    if limit > 0 and len(bar_ohlc) > limit:
        keep_ts = set(bar_ohlc["bar_ts"].iloc[-limit:].tolist())
        bar_ohlc = bar_ohlc[bar_ohlc["bar_ts"].isin(keep_ts)].reset_index(drop=True)
        raw = raw[raw["bar_ts"].isin(keep_ts)].copy()

    cells_df = (
        raw.groupby(["bar_ts", "p_lvl"], as_index=False)
        .agg(
            buy_trades=("buy_trades", "sum"),
            sell_trades=("sell_trades", "sum"),
            buy_qty=("buy_qty", "sum"),
            sell_qty=("sell_qty", "sum"),
            buy_usd=("buy_usd", "sum"),
            sell_usd=("sell_usd", "sum"),
        )
        .sort_values(["bar_ts", "p_lvl"])
    )

    buy_col, sell_col = {
        "trades": ("buy_trades", "sell_trades"),
        "coin": ("buy_qty", "sell_qty"),
        "usd": ("buy_usd", "sell_usd"),
    }.get(unit, ("buy_trades", "sell_trades"))

    bars: list[dict[str, Any]] = []
    vpvr_map: dict[float, dict[str, float]] = {}

    for ohlc_row in bar_ohlc.itertuples(index=False):
        bts = int(ohlc_row.bar_ts)
        sub = cells_df[cells_df["bar_ts"] == bts]
        levels: list[dict[str, Any]] = []
        for r in sub.itertuples(index=False):
            b_val = float(getattr(r, buy_col))
            s_val = float(getattr(r, sell_col))
            tot = b_val + s_val
            dlt = b_val - s_val
            p = float(r.p_lvl)
            levels.append(
                {
                    "price": p,
                    "buy": round(b_val, 3) if unit == "coin" else round(b_val, 2),
                    "sell": round(s_val, 3) if unit == "coin" else round(s_val, 2),
                    "total": round(tot, 3) if unit == "coin" else round(tot, 2),
                    "delta": round(dlt, 3) if unit == "coin" else round(dlt, 2),
                    "buy_trades": int(r.buy_trades),
                    "sell_trades": int(r.sell_trades),
                    "buy_imb": False,
                    "sell_imb": False,
                    "zero_top": False,
                    "zero_bot": False,
                }
            )
            vp = vpvr_map.setdefault(p, {"price": p, "buy": 0.0, "sell": 0.0, "total": 0.0})
            vp["buy"] += b_val
            vp["sell"] += s_val
            vp["total"] += tot

        levels.sort(key=lambda x: x["price"])

        # Diagonal imbalances (Coinglass 300% default: buy[i] vs sell[i-1], sell[i] vs buy[i+1])
        buy_imb_cnt = 0
        sell_imb_cnt = 0
        for idx in range(len(levels)):
            if idx > 0:
                prev_sell = levels[idx - 1]["sell"]
                if levels[idx]["buy"] > 0 and (
                    (prev_sell > 0 and levels[idx]["buy"] >= imbalance_ratio * prev_sell)
                    or (prev_sell == 0 and levels[idx]["buy"] >= 5)
                ):
                    levels[idx]["buy_imb"] = True
                    buy_imb_cnt += 1
            if idx < len(levels) - 1:
                next_buy = levels[idx + 1]["buy"]
                if levels[idx]["sell"] > 0 and (
                    (next_buy > 0 and levels[idx]["sell"] >= imbalance_ratio * next_buy)
                    or (next_buy == 0 and levels[idx]["sell"] >= 5)
                ):
                    levels[idx]["sell_imb"] = True
                    sell_imb_cnt += 1

        # Finished Auction Zero detection at top & bottom (0 | X at top, X | 0 at bottom)
        zero_top = False
        zero_bot = False
        if levels:
            top = levels[-1]
            bot = levels[0]
            if top["sell"] == 0 and top["buy"] > 0:
                top["zero_top"] = True
                zero_top = True
            if bot["buy"] == 0 and bot["sell"] > 0:
                bot["zero_bot"] = True
                zero_bot = True

        poc, val, vah = _compute_va(levels, "total")
        cum_d = 0.0
        max_d = 0.0
        min_d = 0.0
        # Order-of-level delta stats
        bar_buy = sum(x["buy"] for x in levels)
        bar_sell = sum(x["sell"] for x in levels)
        bar_vol = bar_buy + bar_sell
        bar_delta = bar_buy - bar_sell
        delta_pct = (bar_delta / bar_vol * 100.0) if bar_vol > 0 else 0.0
        bar_trades = int(sub["buy_trades"].sum() + sub["sell_trades"].sum())
        bar_coin = float(sub["buy_qty"].sum() + sub["sell_qty"].sum())
        avg_size = (bar_coin / bar_trades) if bar_trades > 0 else 0.0

        for lv in levels:
            cum_d += lv["delta"]
            max_d = max(max_d, lv["delta"])
            min_d = min(min_d, lv["delta"])

        px_dec = 4 if float(ohlc_row.close) < 100.0 else 2
        bars.append(
            {
                "time": bts,
                "end_ts": bts + tf_sec if last_minute[bts] == bts + tf_sec - 60 else None,
                "open": round(float(ohlc_row.open), px_dec),
                "high": round(float(ohlc_row.high), px_dec),
                "low": round(float(ohlc_row.low), px_dec),
                "close": round(float(ohlc_row.close), px_dec),
                "volume": round(bar_vol, 3 if unit == "coin" else 2),
                "buy_vol": round(bar_buy, 3 if unit == "coin" else 2),
                "sell_vol": round(bar_sell, 3 if unit == "coin" else 2),
                "delta": round(bar_delta, 3 if unit == "coin" else 2),
                "delta_pct": round(delta_pct, 1),
                "max_delta": round(max_d, 2),
                "min_delta": round(min_d, 2),
                "trades": bar_trades,
                "avg_size": round(avg_size, 4),
                "poc": poc,
                "val": val,
                "vah": vah,
                "buy_imbalances": buy_imb_cnt,
                "sell_imbalances": sell_imb_cnt,
                "zero_top": zero_top,
                "zero_bot": zero_bot,
                "unfinished_top": bool(levels and levels[-1]["sell"] > 0 and levels[-1]["buy"] > 0),
                "unfinished_bot": bool(levels and levels[0]["sell"] > 0 and levels[0]["buy"] > 0),
                "levels": levels,
            }
        )

    vpvr_levels = sorted(vpvr_map.values(), key=lambda x: x["price"])
    vp_poc, vp_val, vp_vah = _compute_va(vpvr_levels, "total")

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "unit": unit,
        "row_size": eff_row,
        "bars": bars,
        "vpvr": {
            "poc": vp_poc,
            "val": vp_val,
            "vah": vp_vah,
            "levels": [
                {
                    "price": lv["price"],
                    "buy": round(lv["buy"], 2),
                    "sell": round(lv["sell"], 2),
                    "total": round(lv["total"], 2),
                }
                for lv in vpvr_levels
            ],
        },
    }
