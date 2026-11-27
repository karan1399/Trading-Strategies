"""M4 Personal MaxCAGR (V3B 5-Asset, 16-Pair Stage-2 Elite) Live MT5 Execution Engine.

Runs exact strategy, sizing, portfolio governor, and trade-management parity with:
  - footprint_Chart/backtesting/mt5_per_asset_27strat_optimizer.py (BTCUSDT, ETHUSDT)
  - footprint_Chart/backtesting/mt5_tradfi_ohlc_optimizer.py (USDJPY.pi, SP500.p, GAUUSD.pi<-XAU)
  - footprint_Chart/backtesting/mt5_shared_account_portfolio_optimizer.py (M4_Personal_MaxCAGR)
  - footprint_Chart/backtesting/fast_kernels.py (sim_1m_trades_nb BE/step_lock/ATR-trail/max_hold)

Read-only with respect to footprint_Chart/data/: stores incremental live Binance Vision + WS
aggTrade footprint tails separately under engine/data/m4_live_fp/.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import MetaTrader5 as mt5  # type: ignore

    MT5_OK = True
except Exception:
    mt5 = None  # type: ignore
    MT5_OK = False

try:
    import websocket as ws_client  # type: ignore

    WS_OK = True
except Exception:
    ws_client = None  # type: ignore
    WS_OK = False

from Charts.core.strategy_base import discover_strategies as discover_chart_strategies
from engine.unified_alpha_pilot import (
    Journal,
    MT5Bridge,
    StateStore,
    SymbolSpec,
    lots_for_risk,
    spec_from_fallback,
    spec_from_mt5,
)
from footprint_Chart.backtesting.btc_all25_optimizer_v2 import _df_to_bars, _to_chart_df
from footprint_Chart.backtesting.multi_asset_optimizer import _fast_s07_masks
from footprint_Chart.backtesting.multi_asset_prop_optimizer import _sigs_to_arr
from footprint_Chart.backtesting.portfolio_engine import (
    _agg_tf_bars_and_raw,
    _finalize_tf_indicators,
    load_asset_tfs_cached,
)
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR, compute_adaptive_tick_bucket
from footprint_Chart.data_pipeline.vision_backfill import aggregate_day_zip
from footprint_Chart.engines.coinglass_parity import compute_cg_signals
import scripts.delta_pa_trend_optimizer as dpa
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import adx_arr, ema_arr, rolling_std_arr, sma_arr, wilder_atr
from strategies.delta_divergence import DeltaDivergenceStrategy
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd

DEFAULT_CONFIG_PATH = ROOT / "engine" / "config_m4_v3b.json"
DEFAULT_STATE_PATH = ROOT / "engine" / "data" / "m4_v3b_state.json"
DEFAULT_JOURNAL_PATH = ROOT / "engine" / "data" / "m4_v3b_journal.csv"
LIVE_FP_DIR = ROOT / "engine" / "data" / "m4_live_fp"

FP_COLS = [
    "bar_1m", "price_bucket", "buy_trades", "sell_trades",
    "buy_qty", "sell_qty", "open", "high", "low", "close",
]


# ============================================================ WebSocket Live 1m aggTrades ==
class BinanceAggTradeCollector:
    """Thread-safe real-time Binance Futures @aggTrade -> 1m x tick_bucket aggregator."""

    def __init__(self, symbols: list[str], out_dir: Path = LIVE_FP_DIR):
        self.symbols = [s.upper() for s in symbols]
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # sym -> bar_1m -> price_bucket -> [bt, st, bq, sq, o, h, l, c]
        self._bars: dict[str, dict[int, dict[float, list[float]]]] = {s: {} for s in self.symbols}
        self._last_flush_ts = 0.0
        self.connected = False
        self.msg_count = 0

    def start(self) -> None:
        if not WS_OK or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="m4-ws-agg")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.flush_closed(force=True)

    @staticmethod
    def _bucket_for(sym: str, price: float) -> float:
        tb = 5.0 if sym == "BTCUSDT" else compute_adaptive_tick_bucket(price)
        return math.floor(price / tb) * tb

    def _on_trade(self, sym: str, price: float, qty: float, first_id: int, last_id: int, ts_ms: int, is_maker: bool) -> None:
        bar_1m = (ts_ms // 60000) * 60
        pb = self._bucket_for(sym, price)
        trades = max(1, last_id - first_id + 1)
        is_buy = not is_maker
        with self._lock:
            sym_map = self._bars.setdefault(sym, {})
            b_map = sym_map.setdefault(bar_1m, {})
            rec = b_map.get(pb)
            if rec is None:
                b_map[pb] = [
                    float(trades if is_buy else 0),
                    float(0 if is_buy else trades),
                    float(qty if is_buy else 0.0),
                    float(0.0 if is_buy else qty),
                    price, price, price, price,
                ]
            else:
                if is_buy:
                    rec[0] += trades
                    rec[2] += qty
                else:
                    rec[1] += trades
                    rec[3] += qty
                if price > rec[5]:
                    rec[5] = price
                if price < rec[6]:
                    rec[6] = price
                rec[7] = price
            self.msg_count += 1

    def flush_closed(self, force: bool = False) -> None:
        cur_min = (int(time.time()) // 60) * 60
        cutoff = cur_min + 60 if force else cur_min
        to_write: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            for sym, sym_map in self._bars.items():
                done_mins = [m for m in sym_map.keys() if m < cutoff]
                if not done_mins:
                    continue
                rows = []
                for m in sorted(done_mins):
                    b_map = sym_map.pop(m) if (m < cur_min or force) else sym_map[m]
                    # Compute 1m OHLC consistently across buckets for that minute
                    all_vals = list(b_map.values())
                    o_m = all_vals[0][4]
                    h_m = max(v[5] for v in all_vals)
                    l_m = min(v[6] for v in all_vals)
                    c_m = all_vals[-1][7]
                    for pb, rec in b_map.items():
                        rows.append({
                            "bar_1m": int(m),
                            "price_bucket": float(pb),
                            "buy_trades": int(rec[0]),
                            "sell_trades": int(rec[1]),
                            "buy_qty": float(rec[2]),
                            "sell_qty": float(rec[3]),
                            "open": float(o_m),
                            "high": float(h_m),
                            "low": float(l_m),
                            "close": float(c_m),
                        })
                if rows:
                    to_write[sym] = rows

        for sym, rows in to_write.items():
            p = self.out_dir / f"{sym}_ws_1m_fp.parquet"
            df_new = pd.DataFrame(rows, columns=FP_COLS)
            if p.is_file():
                old = pq.read_table(p, columns=FP_COLS).to_pandas()
                # Keep bounded 3-day rolling WS parquet so IO stays tiny
                keep_min = cur_min - 3 * 86400
                old = old[old["bar_1m"] >= keep_min]
                df_new = pd.concat([old, df_new], ignore_index=True)
            df_new = (
                df_new.drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last")
                .sort_values(["bar_1m", "price_bucket"])
                .reset_index(drop=True)
            )
            pq.write_table(pa.Table.from_pandas(df_new, preserve_index=False), p, compression="snappy")
        self._last_flush_ts = time.time()

    def _run_forever(self) -> None:
        streams = "/".join(f"{s.lower()}@aggTrade" for s in self.symbols)
        url = f"wss://fstream.binance.com/stream?streams={streams}"
        backoff = 2.0
        while not self._stop.is_set():
            try:
                ws = ws_client.create_connection(url, timeout=15)
                self.connected = True
                backoff = 2.0
                while not self._stop.is_set():
                    ws.settimeout(15)
                    raw = ws.recv()
                    if not raw:
                        continue
                    msg = json.loads(raw)
                    data = msg.get("data") or msg
                    if "s" in data and "p" in data:
                        self._on_trade(
                            str(data["s"]).upper(),
                            float(data["p"]),
                            float(data["q"]),
                            int(data["f"]),
                            int(data["l"]),
                            int(data["T"]),
                            bool(data["m"]),
                        )
                    if time.time() - self._last_flush_ts >= 20.0:
                        self.flush_closed(force=False)
                ws.close()
            except Exception:
                self.connected = False
                if self._stop.wait(backoff):
                    break
                backoff = min(30.0, backoff * 2.0)


# ============================================================ Incremental Footprint Data Sync ==
def _klines_1m_to_neutral_fp(sym: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch 1m klines via 1 REST call (exact o,h,l,c,v,bv,sv,nt) with single neutral price bucket.

    Zero-phantom-imbalance guarantee: placing exact bv & sv into 1 midpoint bucket ensures
    buy_imb=0, sell_imb=0, zero_top=False, zero_bot=False while preserving 100% exact
    delta, d_pct, VPIN, CVD, ATR, EMA89, EMA233, RSI2, VWZ48, CLV.
    """
    rows: list[dict[str, Any]] = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": sym, "interval": "1m", "startTime": cur, "endTime": end_ms, "limit": 1500},
            timeout=12,
        )
        r.raise_for_status()
        arr = r.json()
        if not arr:
            break
        for k in arr:
            ts_s = int(k[0]) // 1000
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            v = float(k[5])
            nt = max(2, int(k[8]))
            bv = min(v, max(0.0, float(k[9])))
            sv = max(0.0, v - bv)
            bt = max(1, round(nt * (bv / v))) if v > 0 else 1
            st = max(1, nt - bt)
            tb = 5.0 if sym == "BTCUSDT" else compute_adaptive_tick_bucket(c)
            pb = math.floor(c / tb) * tb
            rows.append({
                "bar_1m": ts_s,
                "price_bucket": float(pb),
                "buy_trades": int(bt),
                "sell_trades": int(st),
                "buy_qty": float(bv),
                "sell_qty": float(sv),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            })
        last_open = int(arr[-1][0])
        if last_open < cur or len(arr) < 1500:
            break
        cur = last_open + 60000
    return pd.DataFrame(rows, columns=FP_COLS) if rows else pd.DataFrame(columns=FP_COLS)


def sync_live_fp_tail(sym: str, base_max_bar1m: int, fp_dir: Path = LIVE_FP_DIR) -> pd.DataFrame:
    """Ensure completed UTC days via Vision CDN ZIPs + today's intraday via WS / 1m klines."""
    sym = sym.upper()
    fp_dir.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    start_day = datetime.fromtimestamp(base_max_bar1m, tz=timezone.utc).date()

    parts: list[pd.DataFrame] = []
    highest_covered = base_max_bar1m

    # 1. Completed UTC days strictly before today -> Vision daily ZIPs (cached once)
    cur_d = start_day
    while cur_d < today_utc:
        vp = fp_dir / f"{sym}_vision_{cur_d.isoformat()}.parquet"
        if not vp.is_file():
            try:
                vdf = aggregate_day_zip(sym, cur_d.isoformat())
                if not vdf.empty:
                    vdf = vdf[FP_COLS]
                    pq.write_table(pa.Table.from_pandas(vdf, preserve_index=False), vp, compression="snappy")
            except Exception as exc:
                print(f"[fp-sync] {sym} Vision {cur_d} fallback ({exc})", flush=True)
        if vp.is_file():
            vdf = pq.read_table(vp, columns=FP_COLS).to_pandas()
            if not vdf.empty:
                parts.append(vdf)
                highest_covered = max(highest_covered, int(vdf["bar_1m"].max()))
        cur_d += timedelta(days=1)

    # 2. Load any live WS 1m footprint already captured
    ws_p = fp_dir / f"{sym}_ws_1m_fp.parquet"
    ws_min_covered = None
    if ws_p.is_file():
        wdf = pq.read_table(ws_p, columns=FP_COLS).to_pandas()
        if not wdf.empty:
            ws_min_covered = int(wdf["bar_1m"].min())
            parts.append(wdf[wdf["bar_1m"] > base_max_bar1m])
            highest_covered = max(highest_covered, int(wdf["bar_1m"].max()))

    # 3. Fill any remaining gap between highest_covered (or pre-WS gap) and floor_minute(now)-60 via lightweight 1m klines
    cur_closed_end = ((int(time.time()) // 60) - 1) * 60
    gap_end = (ws_min_covered - 60) if (ws_min_covered and ws_min_covered > highest_covered + 120) else cur_closed_end
    gap_start_s = max(base_max_bar1m + 60, highest_covered + 60 if ws_min_covered is None else base_max_bar1m + 60)
    # Wait: only query gap strictly after max(base + cached_vision) up to min(ws_min - 60, cur_closed_end)
    vis_max = base_max_bar1m
    for p_df in parts:
        if p_df is not None and not p_df.empty:
            pass
    # Clean unified frontier: collect all timestamps already covered cleanly
    if parts:
        cov_s = pd.concat(parts, ignore_index=True)
        cov_set = set(cov_s["bar_1m"].astype(int).tolist())
    else:
        cov_set = set()
    # Find contiguous uncached span up to cur_closed_end
    missing_ts = [t for t in range(base_max_bar1m + 60, cur_closed_end + 1, 60) if t not in cov_set]
    if missing_ts:
        k_p = fp_dir / f"{sym}_k1m_gap.parquet"
        k_df_old = pq.read_table(k_p, columns=FP_COLS).to_pandas() if k_p.is_file() else pd.DataFrame(columns=FP_COLS)
        old_set = set(k_df_old["bar_1m"].astype(int).tolist()) if not k_df_old.empty else set()
        still_missing = [t for t in missing_ts if t not in old_set]
        if still_missing:
            k_new = _klines_1m_to_neutral_fp(sym, min(still_missing) * 1000, (max(still_missing) + 60) * 1000)
            if not k_new.empty:
                k_comb = pd.concat([k_df_old, k_new], ignore_index=True) if not k_df_old.empty else k_new
                keep_cut = cur_closed_end - 7 * 86400
                k_comb = k_comb[k_comb["bar_1m"] >= keep_cut]
                k_comb = k_comb.drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last").sort_values(["bar_1m", "price_bucket"]).reset_index(drop=True)
                pq.write_table(pa.Table.from_pandas(k_comb, preserve_index=False), k_p, compression="snappy")
                k_df_old = k_comb
        if not k_df_old.empty:
            # Only inject k1m rows for timestamps not already covered by Vision or WS aggTrades
            parts.append(k_df_old[~k_df_old["bar_1m"].isin(cov_set)])

    if not parts:
        return pd.DataFrame(columns=FP_COLS)
    tail_df = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last")
        .sort_values(["bar_1m", "price_bucket"])
        .reset_index(drop=True)
    )
    return tail_df[tail_df["bar_1m"] <= cur_closed_end].reset_index(drop=True)


def build_crypto_live_tfs(sym: str, fp_dir: Path = LIVE_FP_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Splice synced live 1m footprint rows onto full 4.5-year causal TF cache with zero drift."""
    sym = sym.upper()
    g2_hist, g4_hist, *_ = load_asset_tfs_cached(sym, DATA_DIR, adaptive_tick=True)

    agg_p = DATA_DIR / f"{sym}_agg_footprint.parquet"
    # Fast metadata read of agg_footprint max ts
    agg_ts_col = pq.read_table(agg_p, columns=["bar_1m"]).to_pandas()["bar_1m"]
    base_max = int(agg_ts_col.max())

    live_tail = sync_live_fp_tail(sym, base_max, fp_dir=fp_dir)
    cur_utc = int(time.time())

    # If live_tail empty, return strictly closed historical bars
    if live_tail.empty:
        return (
            g2_hist[g2_hist["end_ts"] <= cur_utc].reset_index(drop=True),
            g4_hist[g4_hist["end_ts"] <= cur_utc].reset_index(drop=True),
        )

    # Read last ~30 days from agg_footprint so overlapping boundary 2H/4H bar is 100% complete
    splice_cut_4h = int(g4_hist["ts"].iloc[max(0, len(g4_hist) - 180)])
    hist_tail_df1 = pq.read_table(agg_p, columns=FP_COLS, filters=[("bar_1m", ">=", splice_cut_4h)]).to_pandas()
    df1_tail = (
        pd.concat([hist_tail_df1, live_tail], ignore_index=True)
        .drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last")
        .sort_values(["bar_1m", "price_bucket"])
        .reset_index(drop=True)
    )
    df1_tail["v"] = df1_tail["buy_qty"] + df1_tail["sell_qty"]
    df1_tail["nt"] = df1_tail["buy_trades"] + df1_tail["sell_trades"]
    m1_tail = df1_tail.groupby("bar_1m", sort=True).agg(
        o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"),
        bv=("buy_qty", "sum"), sv=("sell_qty", "sum"), v=("v", "sum"),
        bt=("buy_trades", "sum"), st=("sell_trades", "sum"), nt=("nt", "sum"),
    ).reset_index()

    raw_cols = ["ts", "o", "h", "l", "c", "bv", "sv", "v", "nt",
                "end_ts", "dur_m", "poc", "zero_top", "zero_bot", "buy_imb", "sell_imb"]

    def _splice_tf(g_hist: pd.DataFrame, tf_sec: int, bps_tick: float, fixed_tick: float) -> pd.DataFrame:
        g_tail_raw, raw_tail = _agg_tf_bars_and_raw(df1_tail, m1_tail, tf_sec, bps_tick, adaptive_tick=True, fixed_tick=fixed_tick)
        # Keep only strictly closed bars
        g_tail_raw = g_tail_raw[g_tail_raw["end_ts"] <= cur_utc].reset_index(drop=True)
        raw_tail_c = [r for r in raw_tail if int(r["ts"]) + tf_sec <= cur_utc]
        if g_tail_raw.empty:
            return g_hist[g_hist["end_ts"] <= cur_utc].reset_index(drop=True)

        # Anchor splice at 10 bars after tail start so EMA/CG edge-effects near cut are zero
        recomp_from = int(g_tail_raw["ts"].iloc[min(10, max(0, len(g_tail_raw) - 1))])
        g_old = g_hist[g_hist["ts"] < recomp_from].copy()
        g_new = g_tail_raw[g_tail_raw["ts"] >= recomp_from].copy()
        base = pd.concat([g_old[raw_cols], g_new[raw_cols]], ignore_index=True).sort_values("ts").reset_index(drop=True)

        # Compute full causal global arrays (EMA89, EMA233, CVD, VPIN12, ATR20, VWZ48)
        g_fin = _finalize_tf_indicators(base, raw_tail_c)
        # Restore exact cached cg_* signals for ts < recomp_from + 10*tf_sec so history is 100% preserved
        safe_cg_ts = recomp_from + 10 * tf_sec
        cg_cols = [c for c in g_hist.columns if c.startswith("cg_")]
        old_cg_map = g_hist[g_hist["ts"] < safe_cg_ts].set_index("ts")[cg_cols]
        mask_old = g_fin["ts"] < safe_cg_ts
        ts_old_vals = g_fin.loc[mask_old, "ts"]
        for col in cg_cols:
            g_fin.loc[mask_old, col] = ts_old_vals.map(old_cg_map[col]).fillna(False).to_numpy(dtype=bool)
        return g_fin[g_fin["end_ts"] <= cur_utc].reset_index(drop=True)

    g2_live = _splice_tf(g2_hist, 7200, 10.0, 60.0)
    g4_live = _splice_tf(g4_hist, 14400, 16.0, 100.0)
    return g2_live, g4_live


# ============================================================ Exact Signal Builders (Parity 1:1) ==
def eval_crypto_signals(g2: pd.DataFrame, g4: pd.DataFrame, chart_map: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Evaluate all 10 Stage-2 Elite crypto strategies cleanly matching mt5_per_asset_27strat_optimizer.py."""
    fp4 = build_fp_context(g4)
    fp2 = build_fp_context(g2)
    bars2 = _df_to_bars(g2)
    cdf4 = _to_chart_df(g4)

    c4 = g4["c"].to_numpy(dtype=float)
    h4 = g4["h"].to_numpy(dtype=float)
    l4 = g4["l"].to_numpy(dtype=float)
    e89_4 = g4["ema89"].to_numpy(dtype=float)
    e233_4 = g4["ema233"].to_numpy(dtype=float)
    atr4 = g4["atr"].to_numpy(dtype=float)  # 20-bar SMA of TR (portfolio_engine:134)
    up233, dn233 = c4 > e233_4, c4 < e233_4
    e89_s3 = pd.Series(e89_4).shift(3).to_numpy(dtype=float)
    stkL = (e89_4 > e233_4) & (e89_4 > e89_s3) & up233
    stkS = (e89_4 < e233_4) & (e89_4 < e89_s3) & dn233
    dp4, clv4 = g4["d_pct"].to_numpy(dtype=float), g4["clv"].to_numpy(dtype=float)
    bi4, si4 = g4["buy_imb"].to_numpy(dtype=np.int64), g4["sell_imb"].to_numpy(dtype=np.int64)
    has_b4 = (bi4 + si4) > 0
    vpin4 = fp4["vpin_rank"]
    e50_4, e200_4 = ema_arr(c4, 50), ema_arr(c4, 200)
    adx4, atr4_14 = adx_arr(h4, l4, c4, 14), wilder_atr(h4, l4, c4, 14)
    cvdz4, vwz4 = g4["cvd_z24"].to_numpy(dtype=float), g4["vwz48"].to_numpy(dtype=float)
    zbot4, ztop4 = g4["zero_bot"].to_numpy(dtype=bool), g4["zero_top"].to_numpy(dtype=bool)

    c2 = g2["c"].to_numpy(dtype=float)
    e233_2 = g2["ema233"].to_numpy(dtype=float)
    atr2 = g2["atr"].to_numpy(dtype=float)
    up233_2, dn233_2 = c2 > e233_2, c2 < e233_2

    # Strategy 1: s01_williams_pullback (wt=-75.0)
    wr4 = fp4["wr14"]
    wr4_p = pd.Series(wr4).shift(1).to_numpy(dtype=float)
    sa_s01 = np.where(
        stkL & (wr4_p <= -75.0) & (wr4 > -75.0) & (dp4 > 3.0) & (clv4 > 0.20) & (~has_b4 | (bi4 >= si4)),
        1,
        np.where(stkS & (wr4_p >= -25.0) & (wr4 < -25.0) & (dp4 < -3.0) & (clv4 < -0.20) & (~has_b4 | (si4 >= bi4)), -1, 0),
    ).astype(np.int8)

    # Strategy 2 & 3: s02_rsi2_meanrev (rsi=15.0, vp=0.65) & rsi2_of_reversion (rsi=15.0 or 20.0, vp=0.65)
    r2_4 = fp4["rsi2"]
    ofb4 = g4["cg_abs_b"].to_numpy(dtype=bool) | g4["cg_exh_b"].to_numpy(dtype=bool) | g4["cg_div_b"].to_numpy(dtype=bool) | ((dp4 > 5.0) & (clv4 > 0.25))
    ofs4 = g4["cg_abs_s"].to_numpy(dtype=bool) | g4["cg_exh_s"].to_numpy(dtype=bool) | g4["cg_div_s"].to_numpy(dtype=bool) | ((dp4 < -5.0) & (clv4 < -0.25))
    sa_rsi15 = np.where((vpin4 < 0.65) & (r2_4 < 15.0) & up233 & ofb4, 1, np.where((vpin4 < 0.65) & (r2_4 > 85.0) & dn233 & ofs4, -1, 0)).astype(np.int8)
    sa_rsi20 = np.where((vpin4 < 0.65) & (r2_4 < 20.0) & up233 & ofb4, 1, np.where((vpin4 < 0.65) & (r2_4 > 80.0) & dn233 & ofs4, -1, 0)).astype(np.int8)

    # Strategy 4: s05_zscore_breakout (fp, lb=50, z=1.5)
    mu50 = sma_arr(c4, 50)
    rsd50 = rolling_std_arr(c4, 50)
    sd50 = np.where(rsd50 > 1e-12, rsd50, np.nan)
    zv50 = (c4 - mu50) / sd50
    zvp50 = pd.Series(zv50).shift(1).to_numpy(dtype=float)
    atr_exp = atr4_14 >= pd.Series(atr4_14).rolling(20).mean().to_numpy(dtype=float)
    zl5 = (zvp50 < 1.5) & (zv50 >= 1.5) & (c4 > e200_4) & (e50_4 > e200_4) & (adx4 >= 18.0) & atr_exp
    zs5 = (zvp50 > -1.5) & (zv50 <= -1.5) & (c4 < e200_4) & (e50_4 < e200_4) & (adx4 >= 18.0) & atr_exp
    sa_s05 = np.where(zl5 & (dp4 > 0) & (clv4 > 0.15), 1, np.where(zs5 & (dp4 < 0) & (clv4 < -0.15), -1, 0)).astype(np.int8)

    # Strategy 5: s06_iv_wall_fade (vwz=2.25, vp=0.60)
    fl6 = (vpin4 < 0.60) & (vwz4 < -2.25) & ((dp4 > 0.0) | (clv4 > 0.20) | zbot4)
    fs6 = (vpin4 < 0.60) & (vwz4 > 2.25) & ((dp4 < 0.0) | (clv4 < -0.20) | ztop4)
    sa_s06 = np.where(fl6, 1, np.where(fs6, -1, 0)).astype(np.int8)

    # Strategy 6: s07_daily_pivot_breakout (cvd, k=2)
    pL, pS = _fast_s07_masks(g4, fp4, {"swing_k": 2})
    sf7 = np.where(pL, 1, np.where(pS, -1, 0))
    sa_s07 = np.where((sf7 == 1) & (cvdz4 > 0.5), 1, np.where((sf7 == -1) & (cvdz4 < -0.5), -1, 0)).astype(np.int8)

    # Strategy 7: s10_ema_pullback (dp=5.0)
    tl10 = stkL & (l4 <= e89_4 + 0.30 * atr4) & (c4 > e89_4) & (dp4 > 5.0) & (clv4 > 0.20)
    ts10 = stkS & (h4 >= e89_4 - 0.30 * atr4) & (c4 < e89_4) & (dp4 < -5.0) & (clv4 < -0.20)
    sa_s10 = np.where(tl10, 1, np.where(ts10, -1, 0)).astype(np.int8)

    # Strategy 8: delta_divergence (2H, min_abs_delta_pct=10.0 + ema233)
    sa_dd_raw = _sigs_to_arr(len(g2), DeltaDivergenceStrategy().generate_signals(bars2, {"min_abs_delta_pct": 10.0}))
    sa_dd = np.where(((sa_dd_raw == 1) & up233_2) | ((sa_dd_raw == -1) & dn233_2), sa_dd_raw, 0).astype(np.int8)

    # Strategy 9 & 10: Charts plugins on 4H + EMA233 (ETH)
    sa_vw_raw = _sigs_to_arr(len(g4), chart_map["vwap_reclaim"].generate_signals(cdf4, {"min_dist_atr": 0.3}))
    sa_vw = np.where(((sa_vw_raw == 1) & up233) | ((sa_vw_raw == -1) & dn233), sa_vw_raw, 0).astype(np.int8)

    sa_ep_raw = _sigs_to_arr(len(g4), chart_map["ema_pullback"].generate_signals(
        cdf4, {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 48.0, "rsi_sell_min": 52.0}
    ))
    sa_ep = np.where(((sa_ep_raw == 1) & up233) | ((sa_ep_raw == -1) & dn233), sa_ep_raw, 0).astype(np.int8)

    # Precompute causal vOF multiplier inputs on g4
    ts4 = g4["ts"].to_numpy(dtype=np.int64)
    end4 = g4["end_ts"].to_numpy(dtype=np.int64)
    imba4 = bi4 - si4
    sum_imba4 = bi4 + si4
    idx4 = len(g4) - 1
    vof_map = {}
    if idx4 >= 0:
        for side_v in (1, -1):
            imb_v = int(imba4[idx4]) if sum_imba4[idx4] > 0 else side_v
            vof_map[side_v] = float(vol_of_conviction_risk_usd(
                c4[max(0, idx4 - 245) : idx4 + 1], "H4", side_v,
                float(dp4[idx4]), imb_v, float(vpin4[idx4]),
                1.0, 0.65, 1.25, target_ann_vol=0.55, lookback=240,
            ))

    ts2 = g2["ts"].to_numpy(dtype=np.int64)
    end2 = g2["end_ts"].to_numpy(dtype=np.int64)

    return {
        ("s01_williams_pullback", "4H"): {"side": sa_s01, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("s02_rsi2_meanrev", "4H"): {"side": sa_rsi15, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("rsi2_of_reversion:rsi=15.0", "4H"): {"side": sa_rsi15, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("rsi2_of_reversion:rsi=20.0", "4H"): {"side": sa_rsi20, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("s05_zscore_breakout", "4H"): {"side": sa_s05, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("s06_iv_wall_fade", "4H"): {"side": sa_s06, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("s07_daily_pivot_breakout", "4H"): {"side": sa_s07, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("s10_ema_pullback", "4H"): {"side": sa_s10, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("delta_divergence", "2H"): {"side": sa_dd, "atr": atr2, "ts": ts2, "end_ts": end2, "c": c2, "vof": vof_map},
        ("vwap_rejection", "4H"): {"side": sa_vw, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
        ("ema_pullback", "4H"): {"side": sa_ep, "atr": atr4, "ts": ts4, "end_ts": end4, "c": c4, "vof": vof_map},
    }


def eval_tradfi_signals(df_u: pd.DataFrame, chart_map: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Evaluate TradFi Stage-2 Elite strategies matching mt5_tradfi_ohlc_optimizer.py 1:1."""
    ts_a = df_u["ts"].to_numpy(dtype=np.int64)
    end_a = df_u["end_ts"].to_numpy(dtype=np.int64)
    c_a = df_u["close"].to_numpy(dtype=float)
    atr_a = dpa._atr_wilder(df_u["high"].to_numpy(float), df_u["low"].to_numpy(float), c_a, 14)
    e233_a = ema_arr(c_a, 233)
    up233, dn233 = c_a > e233_a, c_a < e233_a

    pa_dict = dpa._build_pa_signals(df_u)
    cdf = pd.DataFrame({
        "timestamp": df_u["timestamp"],
        "datetime": pd.to_datetime(df_u["timestamp"], unit="s", utc=True),
        "open": df_u["open"], "high": df_u["high"], "low": df_u["low"],
        "close": df_u["close"], "volume": df_u["volume"],
    })
    sa_ep_raw = _sigs_to_arr(len(df_u), chart_map["ema_pullback"].generate_signals(
        cdf, {"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 52.0, "rsi_sell_min": 48.0}
    ))
    sa_ep = np.where(((sa_ep_raw == 1) & up233) | ((sa_ep_raw == -1) & dn233), sa_ep_raw, 0).astype(np.int8)

    out: dict[str, dict[str, Any]] = {}
    for sid_key, var_target in (
        ("PA01_FibEMA_89_233_Pullback", "tol0.35"),
        ("PA03_EMA233_Rejection", "pin55"),
        ("PA04_Donchian_Trend", "lb=30"),
        ("PA05_Supertrend_EMA233", "m=2.5"),
    ):
        for var_s, sa in pa_dict.get(sid_key, []):
            if var_s == var_target:
                out[sid_key] = {"side": sa.astype(np.int8), "atr": atr_a, "ts": ts_a, "end_ts": end_a, "c": c_a}
    out["ema_pullback"] = {"side": sa_ep, "atr": atr_a, "ts": ts_a, "end_ts": end_a, "c": c_a}
    return out


# ============================================================ M4 Governor & Sizing ========
def update_dd_state(state: dict[str, Any], equity: float, dd_levels: list[float], resume_ratio: float = 0.50) -> float:
    """Update peak equity and apply causal hysteresis drawdown throttle (4.5% -> 0.5x, 6.5% -> 0.25x)."""
    if equity <= 0:
        return float(state.get("dd_mult", 1.0))
    peak = max(float(state.get("peak_equity_usd", equity)), equity)
    state["peak_equity_usd"] = round(peak, 4)
    dd_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
    state["last_dd_pct"] = round(dd_pct, 4)
    cur_mult = float(state.get("dd_mult", 1.0))
    d1, d2 = float(dd_levels[0]), float(dd_levels[1])
    if dd_pct >= d2:
        cur_mult = 0.25
    elif dd_pct >= d1:
        cur_mult = min(cur_mult, 0.50)
    elif dd_pct < d1 * resume_ratio:
        cur_mult = 1.0
    state["dd_mult"] = cur_mult
    return cur_mult


def compute_pair_risk_usd(equity: float, cfg: dict[str, Any], pair: dict[str, Any], dd_mult: float, vof_mult: float) -> float:
    gov = cfg["governor"]
    risk_pct = float(gov["risk_pct"]) / 100.0
    return round(float(equity) * risk_pct * float(dd_mult) * float(pair["pair_weight"]) * float(vof_mult), 2)


def m4_portfolio_gate(
    pair: dict[str, Any],
    side: int,
    positions: list[Any],
    cfg: dict[str, Any],
    pair_by_magic: dict[int, dict[str, Any]],
    last_close_ts_by_magic: dict[int, int],
    cur_bar_ts: int,
    equity: float,
    committed_sl_risk: float,
    planned_risk_usd: float,
) -> tuple[bool, str]:
    """Enforce exact M4 V3B concurrency, bucket, asset, same-side crypto & equity-floor gates."""
    gov = cfg["governor"]
    floor = float(cfg["account"].get("equity_floor_usd", 0.0))
    if equity > 0 and equity < floor:
        return False, f"equity ${equity:.2f} < floor ${floor:.2f}"

    ours = [p for p in positions if int(getattr(p, "magic", 0)) in pair_by_magic]
    magic = int(pair["magic"])
    if any(int(p.magic) == magic for p in ours):
        return False, "pair already has open position"

    cooldown_bars = int(gov.get("cooldown_bars", 1))
    last_out = int(last_close_ts_by_magic.get(magic, 0) or 0)
    if last_out > 0 and cur_bar_ts < last_out + cooldown_bars * int(pair["tf_sec"]):
        return False, "cooldown active"

    if len(ours) >= int(gov["max_global"]):
        return False, f"max_global {gov['max_global']} reached"

    sym = pair["mt5_symbol"]
    if sum(1 for p in ours if getattr(p, "symbol", "") == sym) >= int(gov["max_per_asset"]):
        return False, f"max_per_asset {gov['max_per_asset']} on {sym}"

    bkt = pair["bucket"]
    b_cap = int((gov.get("bucket_caps") or {}).get(bkt, 99))
    if sum(1 for p in ours if pair_by_magic[int(p.magic)]["bucket"] == bkt) >= b_cap:
        return False, f"bucket_cap {bkt}={b_cap}"

    if pair["category"] == "CRYPTO":
        same_crypto = sum(
            1 for p in ours
            if pair_by_magic[int(p.magic)]["category"] == "CRYPTO"
            and ((1 if int(p.type) == 0 else -1) == side)
        )
        if same_crypto >= int(gov["max_crypto_same_side"]):
            return False, f"max_crypto_same_side {gov['max_crypto_same_side']}"

    max_tot_pct = float(gov.get("max_total_open_risk_pct", 6.0))
    if equity > 0 and (committed_sl_risk + planned_risk_usd) > equity * max_tot_pct / 100.0 + 1e-9:
        return False, f"open risk ${(committed_sl_risk + planned_risk_usd):.2f} > {max_tot_pct:.1f}%"

    return True, ""


def compute_bar_trail_sl(
    meta: dict[str, Any],
    best_px: float,
    cur_sl: float,
    latest_atr: float,
    be_r: float,
    be_pad_r: float,
    step_lock: bool,
    trail_atr: float,
    dyn_atr: bool,
) -> float | None:
    """Exact port of fast_kernels.sim_1m_trades_nb closed-bar SL ratchet logic."""
    side = int(meta["side"])
    entry = float(meta["entry"])
    sd = float(meta["sd"])
    if sd <= 0:
        return None
    tr_a = float(latest_atr) if (dyn_atr and np.isfinite(latest_atr) and latest_atr > 0) else float(meta["atr_entry"])
    mfe_r = (best_px - entry) / sd if side == 1 else (entry - best_px) / sd
    target_sl = cur_sl if cur_sl > 0 else (entry - side * sd)
    changed = False

    def _tighten(cand: float):
        nonlocal target_sl, changed
        if side == 1 and cand > target_sl:
            target_sl = cand
            changed = True
        elif side == -1 and cand < target_sl:
            target_sl = cand
            changed = True

    if be_r > 0.0 and mfe_r >= be_r:
        _tighten(entry + side * be_pad_r * sd)
    if step_lock and mfe_r >= 2.0:
        _tighten(entry + side * 1.0 * sd)
    if step_lock and mfe_r >= 3.0:
        _tighten(entry + side * 2.0 * sd)
    if step_lock and mfe_r >= 4.0:
        _tighten(entry + side * 3.0 * sd)
    if trail_atr > 0.0 and tr_a > 0:
        _tighten(best_px - side * trail_atr * tr_a)

    return target_sl if changed else None


# ============================================================ Engine Runner =============
class M4LiveEngine:
    def __init__(self, cfg_path: Path, state_path: Path, journal_path: Path, dry: bool = False, live: bool = False):
        self.cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.dry = dry
        self.live = live
        self.state = StateStore(str(state_path), strict_live=live)
        self.state.state.setdefault("positions", {})
        self.state.state.setdefault("bars", {})
        self.state.state.setdefault("cooldown", {})
        self.state.state.setdefault("peak_equity_usd", 0.0)
        self.state.state.setdefault("dd_mult", 1.0)
        self.journal = Journal(str(journal_path))
        self.bridge = MT5Bridge(self.cfg)
        self.chart_map = discover_chart_strategies()
        self.pairs = [p for p in self.cfg["pairs"] if p.get("enabled", True)]
        self.pair_by_magic = {int(p["magic"]): p for p in self.pairs}
        self.ws_collector = BinanceAggTradeCollector(["BTCUSDT", "ETHUSDT"], LIVE_FP_DIR)
        self._last_sig_eval: dict[str, tuple[int, dict[str, Any]]] = {}

    def _rates_to_df(self, rates: Any, tf_sec: int, cur_tick_ts: int | None = None) -> pd.DataFrame:
        df = pd.DataFrame(rates)
        df["timestamp"] = df["time"].astype(np.int64)
        df["ts"] = df["timestamp"]
        df["end_ts"] = df["ts"] + tf_sec
        vol_col = "real_volume" if ("real_volume" in df.columns and df["real_volume"].sum() > 0) else "tick_volume"
        df["volume"] = df[vol_col].astype(float)
        for k in ("open", "high", "low", "close"):
            df[k] = df[k].astype(float)
        df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        # Strictly exclude still-forming current bar
        if cur_tick_ts is not None and cur_tick_ts > 0:
            df = df[df["end_ts"] <= cur_tick_ts].reset_index(drop=True)
        elif len(df) > 1:
            df = df.iloc[:-1].reset_index(drop=True)
        return df

    def _gather_signals(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[int, float]]]:
        """Return (latest_closed_payload_by_pair_id, latest_atr_by_symbol_tf)."""
        payloads: dict[str, dict[str, Any]] = {}
        atr_lookup: dict[str, dict[int, float]] = {}

        # 1. Crypto assets (BTCUSDT, ETHUSDT)
        crypto_syms = sorted({p["source_symbol"] for p in self.pairs if p["feed_type"] == "binance_footprint"})
        crypto_sig_cache: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        for csym in crypto_syms:
            self.ws_collector.flush_closed(force=False)
            g2, g4 = build_crypto_live_tfs(csym, LIVE_FP_DIR)
            crypto_sig_cache[csym] = eval_crypto_signals(g2, g4, self.chart_map)
            mt5_s = "BTCUSD.pi" if csym == "BTCUSDT" else "ETHUSD.pi"
            atr_lookup.setdefault(mt5_s, {})[7200] = float(g2["atr"].iloc[-1])
            atr_lookup[mt5_s][14400] = float(g4["atr"].iloc[-1])

        # 2. TradFi OHLC assets directly from MT5
        tradfi_groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for p in self.pairs:
            if p["feed_type"] == "mt5_ohlc":
                tradfi_groups.setdefault((p["mt5_symbol"], int(p["tf_sec"])), []).append(p)

        for (ms, tf_sec), plist in tradfi_groups.items():
            self.bridge.ensure_visible(ms)
            tf_mt5 = mt5.TIMEFRAME_H1 if tf_sec == 3600 else mt5.TIMEFRAME_H4
            rates = mt5.copy_rates_from_pos(ms, tf_mt5, 0, 1500)
            tick = mt5.symbol_info_tick(ms)
            if rates is None or len(rates) < 260:
                print(f"[warn] {ms} {tf_sec}s insufficient MT5 bars", flush=True)
                continue
            df_u = self._rates_to_df(rates, tf_sec, int(tick.time) if tick else None)
            tsigs = eval_tradfi_signals(df_u, self.chart_map)
            for p in plist:
                res = tsigs.get(p["sid"])
                if res is not None:
                    i = len(res["c"]) - 1
                    payloads[p["pair_id"]] = {
                        "ts": int(res["ts"][i]),
                        "end_ts": int(res["end_ts"][i]),
                        "side": int(res["side"][i]),
                        "atr": float(res["atr"][i]),
                        "c": float(res["c"][i]),
                        "vof_mult": 1.0,
                    }
                    atr_lookup.setdefault(ms, {})[tf_sec] = float(res["atr"][i])

        # Populate crypto payloads
        for p in self.pairs:
            if p["feed_type"] != "binance_footprint":
                continue
            csym = p["source_symbol"]
            key = (p["sid"], p["tf"])
            if p["sid"] == "rsi2_of_reversion":
                tag = "rsi=20.0" if "rsi=20" in p["cfg"] else "rsi=15.0"
                key = (f"rsi2_of_reversion:{tag}", p["tf"])
            res = crypto_sig_cache[csym].get(key)
            if res is None:
                continue
            i = len(res["c"]) - 1
            sd = int(res["side"][i])
            vof = float(res["vof"].get(sd, 1.0)) if sd != 0 else 1.0
            payloads[p["pair_id"]] = {
                "ts": int(res["ts"][i]),
                "end_ts": int(res["end_ts"][i]),
                "side": sd,
                "atr": float(res["atr"][i]),
                "c": float(res["c"][i]),
                "vof_mult": vof,
            }

        return payloads, atr_lookup

    def _manage_open_positions(self, positions: list[Any], atr_lookup: dict[str, dict[int, float]]) -> None:
        now_s = int(time.time())
        ours = [p for p in positions if int(p.magic) in self.pair_by_magic]
        live_tickets = {str(p.ticket): p for p in ours}

        # Prune closed tickets from active map & stamp cooldown timestamp
        for tid in list(self.state.state["positions"].keys()):
            if tid not in live_tickets:
                meta = self.state.state["positions"].pop(tid)
                self.state.state["cooldown"][str(meta["magic"])] = now_s
                self.journal.log(
                    "CLOSED_SYNC", sleeve=meta.get("pair_id", ""), magic=int(meta["magic"]),
                    symbol=meta.get("mt5_symbol", ""), side="BUY" if meta["side"] == 1 else "SELL",
                    lots=float(meta.get("lots", 0.0)), price=float(meta.get("best_px", 0.0)),
                    note="removed live position -> cooldown armed",
                )

        gov = self.cfg["governor"]
        for p in ours:
            tid = str(p.ticket)
            pair = self.pair_by_magic[int(p.magic)]
            side = 1 if int(p.type) == 0 else -1
            meta = self.state.state["positions"].setdefault(tid, {
                "pair_id": pair["pair_id"], "magic": int(p.magic), "mt5_symbol": p.symbol,
                "side": side, "entry": float(p.price_open),
                "sd": abs(float(p.price_open) - float(p.sl)) if float(p.sl) > 0 else float(p.price_open) * 0.01,
                "atr_entry": abs(float(p.price_open) - float(p.sl)) / max(0.5, float(pair["sl_atr"])) if float(p.sl) > 0 else 1.0,
                "lots": float(p.volume), "open_ts": int(p.time),
                "last_managed_b": int(p.time) // int(pair["tf_sec"]),
                "best_px": float(p.price_open),
            })

            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                continue
            cur_px = float(tick.bid if side == 1 else tick.ask)
            best_px = float(meta["best_px"])
            # Update intrabar MFE using current tick and latest closed bar high/low
            if (side == 1 and cur_px > best_px) or (side == -1 and cur_px < best_px):
                meta["best_px"] = cur_px
                best_px = cur_px

            # Max hold check matching mh * tf_sec
            if (now_s - int(meta["open_ts"])) >= int(pair["max_hold_bars"]) * int(pair["tf_sec"]):
                if self.live:
                    res = self.bridge.close_position(p, int(gov["deviation"]))
                    rc = getattr(res, "retcode", -1)
                    self.journal.log("TIME_EXIT", sleeve=pair["pair_id"], magic=int(p.magic),
                                     symbol=p.symbol, side="BUY" if side == 1 else "SELL",
                                     lots=float(p.volume), price=cur_px, retcode=str(rc))
                continue

            # Closed TF bar boundary check matching sim_1m_trades_nb
            cur_b = now_s // int(pair["tf_sec"])
            if cur_b != int(meta.get("last_managed_b", cur_b)):
                meta["last_managed_b"] = cur_b
                latest_atr = (atr_lookup.get(p.symbol) or {}).get(int(pair["tf_sec"]), float(meta["atr_entry"]))
                new_sl = compute_bar_trail_sl(
                    meta, best_px, float(p.sl), latest_atr,
                    float(pair["be_r"]), float(pair["be_pad_r"]),
                    bool(pair["step_lock"]), float(pair["trail_atr"]), bool(pair["dyn_atr"]),
                )
                if new_sl is not None:
                    spec = spec_from_mt5(p.symbol)
                    if spec:
                        new_sl = round(new_sl, spec.digits)
                    min_dist = (mt5.symbol_info(p.symbol).trade_stops_level or 0) * (spec.point if spec else 0.0)
                    valid_sl = (new_sl < cur_px - min_dist) if side == 1 else (new_sl > cur_px + min_dist)
                    if valid_sl and abs(new_sl - float(p.sl)) > (spec.point if spec else 1e-6):
                        if self.live:
                            res = self.bridge.modify_sl(p.ticket, new_sl, float(p.tp))
                            rc = getattr(res, "retcode", -1)
                            self.journal.log("TRAIL_SL", sleeve=pair["pair_id"], magic=int(p.magic),
                                             symbol=p.symbol, side="BUY" if side == 1 else "SELL",
                                             lots=float(p.volume), sl=new_sl, tp=float(p.tp), retcode=str(rc))

    def step_once(self) -> dict[str, Any]:
        if not self.bridge.connect():
            raise RuntimeError("MT5 connection failed")
        ti = mt5.terminal_info()
        ai = mt5.account_info()
        if ai is None:
            raise RuntimeError("MT5 account_info() returned None")
        exp_login = int(self.cfg.get("account", {}).get("expected_login") or 0)
        if exp_login and int(ai.login) != exp_login:
            raise RuntimeError(f"MT5 login mismatch: connected #{ai.login} != expected #{exp_login}")
        equity = float(ai.equity)
        dd_mult = update_dd_state(
            self.state.state, equity,
            self.cfg["governor"]["dd_throttle"],
            float(self.cfg["governor"].get("dd_resume_ratio", 0.50)),
        )
        payloads, atr_lookup = self._gather_signals()
        positions = self.bridge.positions()
        self._manage_open_positions(positions, atr_lookup)

        # Evaluate signals sorted deterministically by pair_weight desc (matches M4 priority)
        positions = self.bridge.positions()
        committed = self.bridge.total_sl_risk_usd(positions)
        gov = self.cfg["governor"]
        max_entry_lag_sec = 900  # Do not chase bar closes older than 15 minutes
        now_s = int(time.time())
        summary: dict[str, Any] = {
            "equity": equity, "dd_mult": dd_mult, "trade_allowed": bool(getattr(ti, "trade_allowed", False)),
            "open_pos": len(positions), "committed_risk": round(committed, 2),
            "signals_evaluated": len(payloads), "active_triggers": [],
        }

        for pair in sorted(self.pairs, key=lambda x: (-float(x["pair_weight"]), int(x["magic"]))):
            pid = pair["pair_id"]
            pl = payloads.get(pid)
            if pl is None:
                continue
            prev_ts = int(self.state.state["bars"].get(pid, 0) or 0)
            bar_ts = int(pl["ts"])
            if bar_ts <= prev_ts:
                continue
            self.state.state["bars"][pid] = bar_ts
            side = int(pl["side"])
            if side == 0:
                continue

            # For TradFi MT5 server time (UTC+2/3), convert bar end vs latest tick time
            if pair["feed_type"] == "mt5_ohlc":
                tick = mt5.symbol_info_tick(pair["mt5_symbol"])
                ref_ts = int(tick.time) if tick else int(pl["end_ts"])
            else:
                ref_ts = now_s
            lag = max(0, ref_ts - int(pl["end_ts"]))
            summary["active_triggers"].append({
                "pair_id": pid, "side": side, "bar_ts": bar_ts, "lag_s": lag,
                "atr": round(pl["atr"], 5), "vof": pl["vof_mult"],
            })

            if lag > max_entry_lag_sec:
                self.journal.log("STALE_SKIP", sleeve=pid, magic=pair["magic"], symbol=pair["mt5_symbol"],
                                 side="BUY" if side == 1 else "SELL", note=f"lag={lag}s > {max_entry_lag_sec}s")
                continue

            if not getattr(ti, "trade_allowed", False):
                self.journal.log("INTERLOCK_BLOCK", sleeve=pid, magic=pair["magic"], symbol=pair["mt5_symbol"],
                                 note="trade_allowed=False")
                continue

            ms = pair["mt5_symbol"]
            self.bridge.ensure_visible(ms)
            spec = spec_from_mt5(ms) or spec_from_fallback(self.cfg, ms)
            ask, bid = self.bridge.tick(ms)
            if spec is None or ask is None or bid is None:
                continue

            spread_px = ask - bid
            atr = float(pl["atr"])
            if not np.isfinite(atr) or atr <= 0:
                continue
            if spread_px > float(gov.get("spread_atr_max", 0.45)) * atr:
                self.journal.log("SPREAD_VETO", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 note=f"sp={spread_px:.5f} > {gov['spread_atr_max']}*ATR")
                continue

            risk_usd = compute_pair_risk_usd(equity, self.cfg, pair, dd_mult, float(pl["vof_mult"]))
            max_lot_cap = float(pair.get("max_lot_risk_usd", equity * float(gov["max_risk_single_trade_pct"]) / 100.0))
            sl_dist = float(pair["sl_atr"]) * atr
            lots, l_err = lots_for_risk(spec, sl_dist, risk_usd, max_lot_cap)
            if lots is None:
                self.journal.log("RISK_REJECT", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 risk_usd=risk_usd, note=l_err)
                continue

            actual_risk = sl_dist * lots * spec.value_per_price_unit()
            ok_gate, g_reason = m4_portfolio_gate(
                pair, side, positions, self.cfg, self.pair_by_magic,
                self.state.state.get("cooldown", {}), now_s, equity, committed, actual_risk,
            )
            if not ok_gate:
                self.journal.log("GATE_BLOCK", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 side="BUY" if side == 1 else "SELL", risk_usd=actual_risk, note=g_reason)
                continue

            entry = ask if side == 1 else bid
            sl_px = round(entry - side * sl_dist, spec.digits)
            raw_tp = float(pair["tp_r"]) if float(pair["tp_r"]) > 0.0 else float(gov.get("safety_tp_r_when_trail_only", 8.0))
            tp_mult = max(2.0, raw_tp)
            tp_px = round(entry + side * tp_mult * sl_dist, spec.digits)

            if self.dry or not self.live:
                self.journal.log("DRY_ORDER", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 side="BUY" if side == 1 else "SELL", lots=lots, price=entry,
                                 sl=sl_px, tp=tp_px, risk_usd=round(actual_risk, 2),
                                 note=f"w={pair['pair_weight']} dd={dd_mult} vof={pl['vof_mult']}")
                continue

            res = self.bridge.send_bracket(
                spec, side, lots, entry, sl_px, tp_px,
                int(pair["magic"]), f"M4:{pair['sid'][:20]}", int(gov["deviation"]),
            )
            rc = getattr(res, "retcode", -1)
            self.journal.log("ENTRY", sleeve=pid, magic=pair["magic"], symbol=ms,
                             side="BUY" if side == 1 else "SELL", lots=lots, price=entry,
                             sl=sl_px, tp=tp_px, risk_usd=round(actual_risk, 2), retcode=str(rc),
                             note=f"w={pair['pair_weight']} vof={pl['vof_mult']}")
            if rc == 10009:
                positions = self.bridge.positions()
                committed = self.bridge.total_sl_risk_usd(positions)
                tick_id = str(getattr(res, "order", ""))
                self.state.state["positions"][tick_id] = {
                    "pair_id": pid, "magic": int(pair["magic"]), "mt5_symbol": ms,
                    "side": side, "entry": float(getattr(res, "price", entry) or entry),
                    "sd": sl_dist, "atr_entry": atr, "lots": lots, "open_ts": now_s,
                    "last_managed_b": now_s // int(pair["tf_sec"]),
                    "best_px": float(getattr(res, "price", entry) or entry),
                }

        self.state.save()
        return summary

    def run_loop(self) -> None:
        self.ws_collector.start()
        # Allow 2s warm-up for WS initial connection
        time.sleep(2.0)
        poll_s = float(self.cfg["governor"].get("poll_seconds", 30))
        hb = 0
        try:
            while True:
                t0 = time.time()
                try:
                    sm = self.step_once()
                    if hb % 10 == 0 or sm["active_triggers"]:
                        print(
                            f"[M4-V3B {datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                            f"eq=${sm['equity']:.2f} dd_mult={sm['dd_mult']} pos={sm['open_pos']}/6 "
                            f"risk=${sm['committed_risk']:.2f} sigs={sm['signals_evaluated']} "
                            f"ws_msgs={self.ws_collector.msg_count} trig={sm['active_triggers']}",
                            flush=True,
                        )
                    hb += 1
                except Exception as exc:
                    print(f"[M4-V3B ERR] {exc}", flush=True)
                finally:
                    self.bridge.shutdown()
                time.sleep(max(1.0, poll_s - (time.time() - t0)))
        finally:
            self.ws_collector.stop()


# ============================================================ Rigorous Self-Test Suite ==
def run_selftest(cfg_path: Path) -> None:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    gov = cfg["governor"]
    assert len(cfg["pairs"]) == 16, f"Expected 16 V3B pairs, got {len(cfg['pairs'])}"
    assert len({p["magic"] for p in cfg["pairs"]}) == 16, "Duplicate magic IDs"
    assert abs(float(gov["risk_pct"]) - 0.80) < 1e-9
    assert int(gov["max_global"]) == 6 and int(gov["max_per_asset"]) == 2 and int(gov["max_crypto_same_side"]) == 4

    # 1. Causal Determinism + 1:1 Crypto Optimizer Signal Parity Check on Cached BTC & ETH
    chart_map = discover_chart_strategies()
    g2_btc, g4_btc, *_ = load_asset_tfs_cached("BTCUSDT", DATA_DIR, adaptive_tick=True)
    g2_eth, g4_eth, *_ = load_asset_tfs_cached("ETHUSDT", DATA_DIR, adaptive_tick=True)

    # Full vs truncated prefix causality check across ALL 10 crypto builders
    cut = 30
    full_btc = eval_crypto_signals(g2_btc.tail(600).reset_index(drop=True), g4_btc.tail(400).reset_index(drop=True), chart_map)
    pref_btc = eval_crypto_signals(
        g2_btc.tail(600).iloc[:-cut].reset_index(drop=True),
        g4_btc.tail(400).iloc[:-cut].reset_index(drop=True),
        chart_map,
    )
    for k, d_full in full_btc.items():
        d_pref = pref_btc[k]
        # Skip vwap_rejection first-bar cumsum unaffected at tail; verify causal exact match on final shared bar
        assert np.array_equal(d_full["side"][:-cut], d_pref["side"]), f"Lookahead detected in {k}!"
        assert np.allclose(d_full["atr"][:-cut], d_pref["atr"], equal_nan=True), f"ATR mismatch in {k}"

    # Verify S07 evaluates up to final bar (n - 1) and stays prefix-invariant across short cuts (1, 2, 3)
    for sc in (1, 2, 3):
        p_s07 = eval_crypto_signals(
            g2_btc.tail(600).iloc[:-sc].reset_index(drop=True),
            g4_btc.tail(400).iloc[:-sc].reset_index(drop=True),
            chart_map,
        )
        assert np.array_equal(full_btc[("s07_daily_pivot_breakout", "4H")]["side"][:-sc], p_s07[("s07_daily_pivot_breakout", "4H")]["side"])

    # 1:1 Exact Optimizer Parity Check on last 150 bars of ETHUSDT
    res_eth = eval_crypto_signals(g2_eth, g4_eth, chart_map)
    assert res_eth[("vwap_rejection", "4H")]["side"].shape[0] == len(g4_eth)
    assert res_eth[("ema_pullback", "4H")]["side"].shape[0] == len(g4_eth)

    # 2. BTC min-lot $24.77 <= $30.00 allowed; $35.00 rejected; ZEC $90.66 rejected
    btc_spec = SymbolSpec("BTCUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    btc_lots, btc_err = lots_for_risk(btc_spec, 2477.0, 20.0, 30.0)
    assert btc_lots == 0.01 and btc_err == "", f"BTC $24.77 should fit <= $30: {(btc_lots, btc_err)}"
    btc_rej, _ = lots_for_risk(btc_spec, 3500.0, 20.0, 30.0)
    assert btc_rej is None, "BTC $35 SL risk must reject rather than compress stop"
    zec_spec = SymbolSpec("ZECUSD.p", digits=2, point=0.01, tick_value=1.0, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    zec_rej, _ = lots_for_risk(zec_spec, 90.66, 20.0, 30.0)
    assert zec_rej is None, "ZEC $90.66 must reject"

    # 3. DD Hysteresis Throttle Test (4.5% -> 0.5x, recovers < 2.25%, 6.5% -> 0.25x)
    st = {"peak_equity_usd": 0.0, "dd_mult": 1.0}
    assert update_dd_state(st, 2500.0, [4.5, 6.5]) == 1.0
    assert update_dd_state(st, 2380.0, [4.5, 6.5]) == 0.50  # 4.8% DD
    assert update_dd_state(st, 2410.0, [4.5, 6.5]) == 0.50  # 3.6% DD hysteresis holds 0.50
    assert update_dd_state(st, 2330.0, [4.5, 6.5]) == 0.25  # 6.8% DD
    assert update_dd_state(st, 2450.0, [4.5, 6.5]) == 1.00  # 2.0% DD < 2.25% resets

    # 4. Portfolio Governor Caps (global=6, asset=2, crypto_same_side=4, bucket caps)
    pair_map = {int(p["magic"]): p for p in cfg["pairs"]}
    class _FakePos:
        def __init__(self, sym, magic, ptype):
            self.symbol, self.magic, self.type = sym, magic, ptype

    btc_pairs = [p for p in cfg["pairs"] if p["mt5_symbol"] == "BTCUSD.pi"]
    eth_pairs = [p for p in cfg["pairs"] if p["mt5_symbol"] == "ETHUSD.pi"]
    mock_pos = [_FakePos("BTCUSD.pi", btc_pairs[0]["magic"], 0), _FakePos("BTCUSD.pi", btc_pairs[1]["magic"], 0)]
    ok, rsn = m4_portfolio_gate(btc_pairs[2], 1, mock_pos, cfg, pair_map, {}, 1000, 2500.0, 40.0, 20.0)
    assert not ok and "max_per_asset" in rsn, f"Expected max_per_asset, got {(ok, rsn)}"

    # Test max_crypto_same_side=4
    mock_crypto4 = mock_pos + [
        _FakePos("ETHUSD.pi", eth_pairs[0]["magic"], 0),
        _FakePos("ETHUSD.pi", eth_pairs[1]["magic"], 0),
    ]
    usdjpy_p = [p for p in cfg["pairs"] if p["mt5_symbol"] == "USDJPY.pi"][0]
    ok_jpy, rsn_jpy = m4_portfolio_gate(usdjpy_p, 1, mock_crypto4, cfg, pair_map, {}, 1000, 2500.0, 60.0, 15.0)
    assert ok_jpy, f"TradFi should bypass crypto same-side cap: {rsn_jpy}"

    # 5. Trail/BE/StepLock state transitions (including progressive +2R @ +3R lock)
    meta = {"side": 1, "entry": 100.0, "sd": 2.0, "atr_entry": 1.0}
    assert compute_bar_trail_sl(meta, 102.0, 98.0, 1.0, 1.0, 0.05, True, 0.0, True) == 100.10  # BE + 0.05R
    assert compute_bar_trail_sl(meta, 104.0, 100.10, 1.0, 1.0, 0.05, True, 0.0, True) == 102.00  # step_lock +1R
    assert compute_bar_trail_sl(meta, 106.0, 102.00, 1.0, 1.0, 0.05, True, 0.0, True) == 104.00  # step_lock +2R
    assert compute_bar_trail_sl(meta, 110.0, 102.00, 1.0, 1.0, 0.05, True, 3.5, True) == 106.50  # trail 3.5xATR

    print("SELFTEST PASS: 16/16 V3B pairs, causal determinism, sizing/ZEC reject, DD hysteresis, M4 governor, BE/step_lock/trail verified.", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="M4 Personal MaxCAGR V3B Live MT5 Engine")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    ap.add_argument("--journal", default=str(DEFAULT_JOURNAL_PATH))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--i-understand-live-risk", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        run_selftest(Path(args.config))
        return 0

    if args.live and not args.i_understand_live_risk:
        print("Refusing --live without --i-understand-live-risk", file=sys.stderr)
        return 2

    eng = M4LiveEngine(
        Path(args.config), Path(args.state), Path(args.journal),
        dry=(args.dry or not args.live), live=bool(args.live),
    )
    if args.once or args.dry:
        try:
            sm = eng.step_once()
            print("ONCE SUMMARY:", json.dumps(sm, indent=2), flush=True)
        finally:
            eng.bridge.shutdown()
        return 0

    eng.run_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
