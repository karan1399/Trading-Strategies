"""Multi-Year (2021-2026) 1m-Exact Institutional Order-Flow + 20K Monte Carlo Portfolio Engine.

Hardened against backtest-vs-reality gaps:
  1. Causal execution latency (`entry_delay_mins=1` default: fills 60s after bar close).
  2. Asymmetric realistic fees & volatility-spike SL slippage (TP maker 8.5bp, Trail 15bp, SL 21.5-26.5bp).
  3. Exact Binance 8h historical funding cashflows (`BTCUSDT_funding_8h.parquet`, directional sign).
  4. Causal scale-invariant adaptive footprint tick sizing (`_ladder_round(o * bps / 10000)`).
  5. Dynamic closed-bar ATR Chandelier trailing (`dyn_atr=True`, `be_r=0` on 4H trend sleeves).
  6. Directional stacking guard (`max_same_side=2`) preventing 3x correlated single-side leverage.
  7. Sample-verified sleeves only (`n >= 30` across multi-year history; void `n<30` sleeves pruned).
  8. Multi-window OOS (`2021..2026` annual, `IS 21-23` vs `OOS 24-26`, rolling WF, regimes, Crash-Stress MC).
"""

from __future__ import annotations

from datetime import datetime, timezone
import glob
import math
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from footprint_Chart.engines.coinglass_parity import _ladder_round, compute_cg_signals
from strategies.core.fp_features import build_fp_context
from strategies.core.indicators import rsi_wilder_arr
from strategies.s02_rsi2_meanrev.strategy import signal_at as s02_signal_at
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd
from strategies.s09_donchian_trend_cta.strategy import signal_at as s09_signal_at
from strategies.s10_ema_pullback.strategy import signal_at as s10_signal_at

# mtime/size-keyed caches
_TF_CACHE: dict[str, tuple[tuple[int, ...], pd.DataFrame, pd.DataFrame, pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
_MY_CACHE: dict[str, tuple[tuple[int, ...], pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
_FUND_CACHE: dict[str, tuple[int, int, np.ndarray, np.ndarray]] = {}


def _load_funding_arrays(symbol: str = "BTCUSDT", data_dir: Path = DATA_DIR) -> tuple[np.ndarray, np.ndarray]:
    sym = symbol.upper()
    fpath = data_dir / f"{sym}_funding_8h.parquet"
    if not fpath.is_file():
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    st = fpath.stat()
    cached = _FUND_CACHE.get(sym)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2], cached[3]
    df = pq.read_table(fpath, columns=["ts", "funding_rate"]).to_pandas().sort_values("ts")
    ts_a = df["ts"].to_numpy(dtype=np.int64)
    rt_a = df["funding_rate"].to_numpy(dtype=np.float64)
    _FUND_CACHE[sym] = (st.st_mtime_ns, st.st_size, ts_a, rt_a)
    return ts_a, rt_a


def _agg_tf_bars_and_raw(
    df1: pd.DataFrame,
    m1: pd.DataFrame,
    tf_sec: int,
    bps_tick: float,
    adaptive_tick: bool = True,
    fixed_tick: float = 40.0,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Aggregate 1m price-bucket rows into TF OHLCV + structural footprint metadata + raw CG ladder."""
    b = (df1["bar_1m"].to_numpy() // tf_sec) * tf_sec
    g = m1.assign(ts=(m1["bar_1m"] // tf_sec) * tf_sec).groupby("ts", sort=True).agg(
        o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"),
        bv=("bv", "sum"), sv=("sv", "sum"), v=("v", "sum"), nt=("nt", "sum"), bt=("bt", "sum"), st=("st", "sum"),
    ).reset_index()
    g["end_ts"] = g["ts"] + tf_sec
    g["dur_m"] = tf_sec // 60

    if adaptive_tick:
        floor_tick = 5.0 if float(g["o"].median()) >= 10000.0 else 1e-6
        ticks_map = {int(r.ts): max(floor_tick, _ladder_round(float(r.o) * bps_tick / 10000.0)) for r in g.itertuples(index=False)}
    else:
        ticks_map = {int(r.ts): float(fixed_tick) for r in g.itertuples(index=False)}
    bars_map = {int(r.ts): (float(r.o), float(r.h), float(r.l), float(r.c)) for r in g.itertuples(index=False)}

    sub = df1[["price_bucket", "buy_qty", "sell_qty", "v"]].copy()
    sub["ts"] = b
    sub["tk"] = sub["ts"].map(ticks_map)
    sub["pb"] = np.floor(sub["price_bucket"] / sub["tk"]) * sub["tk"]
    lvl = sub.groupby(["ts", "pb"], sort=True, as_index=False).agg(bv=("buy_qty", "sum"), sv=("sell_qty", "sum"), v=("v", "sum"))

    ts_l = lvl["ts"].to_numpy()
    pb_l = lvl["pb"].to_numpy()
    bv_l = lvl["bv"].to_numpy()
    sv_l = lvl["sv"].to_numpy()
    splits = np.flatnonzero(ts_l[1:] != ts_l[:-1]) + 1
    starts = np.concatenate(([0], splits))
    ends = np.concatenate((splits, [len(ts_l)]))
    meta: dict[int, tuple[float, bool, bool, int, int]] = {}
    raw: list[dict[str, Any]] = []
    for s_i, e_i in zip(starts, ends):
        ts_val = int(ts_l[s_i])
        tick = ticks_map[ts_val]
        lo = float(pb_l[s_i])
        hi_p = float(pb_l[e_i - 1])
        nb = max(1, round((hi_p - lo) / tick) + 1)
        ba = [0.0] * nb
        sa = [0.0] * nb
        for k in range(s_i, e_i):
            idx = max(0, min(nb - 1, round((pb_l[k] - lo) / tick)))
            ba[idx] += float(bv_l[k])
            sa[idx] += float(sv_l[k])
        tot = [ba[k] + sa[k] for k in range(nb)]
        poc = lo + int(np.argmax(tot)) * tick
        ztop = (sa[-1] == 0 and ba[-1] > 0)
        zbot = (ba[0] == 0 and sa[0] > 0)
        bi = sum(1 for k in range(1, nb) if ba[k] >= 1.0 and sa[k - 1] > 0 and ba[k] >= 3.0 * sa[k - 1])
        si = sum(1 for k in range(0, nb - 1) if sa[k] >= 1.0 and ba[k + 1] > 0 and sa[k] >= 3.0 * ba[k + 1])
        meta[ts_val] = (poc, ztop, zbot, bi, si)
        if ts_val in bars_map:
            o_, h_, l_, c_ = bars_map[ts_val]
            raw.append({"ts": ts_val, "o": o_, "h": h_, "l": l_, "c": c_, "fp": {"tick": tick, "lo": lo, "b": ba, "s": sa}})

    g["poc"] = g["ts"].map(lambda x: meta.get(x, (np.nan, False, False, 0, 0))[0])
    g["zero_top"] = g["ts"].map(lambda x: meta.get(x, (np.nan, False, False, 0, 0))[1])
    g["zero_bot"] = g["ts"].map(lambda x: meta.get(x, (np.nan, False, False, 0, 0))[2])
    g["buy_imb"] = g["ts"].map(lambda x: meta.get(x, (np.nan, False, False, 0, 0))[3])
    g["sell_imb"] = g["ts"].map(lambda x: meta.get(x, (np.nan, False, False, 0, 0))[4])
    return g, raw


def _finalize_tf_indicators(g: pd.DataFrame, raw: list[dict[str, Any]]) -> pd.DataFrame:
    """Compute causal rolling technicals, order-flow ratios, and Coinglass signals on aggregated TF bars."""
    g = g.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    g["delta"] = g["bv"] - g["sv"]
    g["d_pct"] = np.where(g["v"] > 0, g["delta"] / g["v"] * 100.0, 0.0)
    g["avg_sz"] = np.where(g["nt"] > 0, g["v"] / g["nt"], 0.0)
    g["avg_sz_rk"] = g["avg_sz"].rolling(100).rank(pct=True)
    g["tr"] = np.maximum(g["h"] - g["l"], np.maximum((g["h"] - g["c"].shift(1)).abs(), (g["l"] - g["c"].shift(1)).abs()))
    g["atr"] = g["tr"].rolling(20).mean()
    g["clv"] = np.where(g["h"] > g["l"], ((g["c"] - g["l"]) - (g["h"] - g["c"])) / (g["h"] - g["l"]), 0.0)
    g["ema89"] = g["c"].ewm(span=89, adjust=False).mean()
    g["ema233"] = g["c"].ewm(span=233, adjust=False).mean()
    cvd = g["delta"].cumsum()
    g["cvd_z24"] = (cvd - cvd.rolling(24).mean()) / cvd.rolling(24).std().replace(0, np.nan)
    g["vpin12"] = g["delta"].abs().rolling(12).sum() / g["v"].rolling(12).sum().replace(0, np.nan)
    g["vpin_rank"] = g["vpin12"].rolling(100).rank(pct=True)
    g["vpm_rk"] = (g["v"] / g["dur_m"].clip(lower=1)).rolling(50).rank(pct=True)
    vw = (g["c"] * g["v"]).rolling(48).sum() / g["v"].rolling(48).sum().replace(0, np.nan)
    sd = g["c"].rolling(48).std().replace(0, np.nan)
    g["vwz48"] = (g["c"] - vw) / sd

    raw_sorted = sorted({r["ts"]: r for r in raw}.values(), key=lambda x: x["ts"])
    ts_to_i = {int(ts): idx for idx, ts in enumerate(g["ts"])}
    cg_sigs = compute_cg_signals(raw_sorted)
    for t_key in ("abs", "div", "exh", "trap", "sdiv"):
        g[f"cg_{t_key}_b"] = False
        g[f"cg_{t_key}_s"] = False
    for s in cg_sigs:
        t_k = s["t"]
        c_idx = s["j"] if t_k == "trap" else (s["i"] + 3 if t_k == "sdiv" else ts_to_i.get(s["ts"], -1))
        if 0 <= c_idx < len(g):
            g.at[c_idx, f"cg_{t_k}_{'b' if s['side'] == 'buy' else 's'}"] = True
    return g


def _build_tf_from_bucket_df(
    df1: pd.DataFrame,
    m1: pd.DataFrame,
    tf_sec: int,
    bps_tick: float,
    adaptive_tick: bool = True,
    fixed_tick: float = 40.0,
) -> pd.DataFrame:
    g, raw = _agg_tf_bars_and_raw(df1, m1, tf_sec, bps_tick, adaptive_tick=adaptive_tick, fixed_tick=fixed_tick)
    return _finalize_tf_indicators(g, raw)


def _load_1y_tf_frames(symbol: str = "BTCUSDT", data_dir: Path = DATA_DIR, adaptive_tick: bool = True):
    sym = symbol.upper()
    path = data_dir / f"{sym}_agg_footprint.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"Missing 1Y footprint parquet: {path}")
    monthly_paths = sorted(Path(p) for p in glob.glob(str(data_dir / "monthly" / f"{sym}_*.parquet")))
    f_path = data_dir / f"{sym}_funding_8h.parquet"
    f_st = f_path.stat() if f_path.is_file() else None
    st = path.stat()
    key = (
        st.st_mtime_ns, st.st_size, len(monthly_paths), sum(p.stat().st_size for p in monthly_paths),
        f_st.st_mtime_ns if f_st else 0, f_st.st_size if f_st else 0, int(adaptive_tick),
    )
    cached = _TF_CACHE.get(sym)
    if cached and cached[0] == key:
        return cached[1:]

    cols = ["bar_1m", "price_bucket", "buy_trades", "sell_trades", "buy_qty", "sell_qty", "open", "high", "low", "close"]
    parts = [pq.read_table(p, columns=cols).to_pandas() for p in monthly_paths]
    parts.append(pq.read_table(path, columns=cols).to_pandas())
    df1 = pd.concat(parts, ignore_index=True)
    if len(monthly_paths) > 0:
        df1 = df1.drop_duplicates(subset=["bar_1m", "price_bucket"], keep="last").sort_values(["bar_1m", "price_bucket"]).reset_index(drop=True)

    df1["v"] = df1["buy_qty"] + df1["sell_qty"]
    df1["nt"] = df1["buy_trades"] + df1["sell_trades"]
    m1 = df1.groupby("bar_1m", sort=True).agg(
        o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"),
        bv=("buy_qty", "sum"), sv=("sell_qty", "sum"), v=("v", "sum"),
        bt=("buy_trades", "sum"), st=("sell_trades", "sum"), nt=("nt", "sum"),
    ).reset_index()

    m1_ts = m1["bar_1m"].to_numpy()
    m1_o = m1["o"].to_numpy()
    m1_h = m1["h"].to_numpy()
    m1_l = m1["l"].to_numpy()
    m1_c = m1["c"].to_numpy()

    g1 = _build_tf_from_bucket_df(df1, m1, 3600, 6.5, adaptive_tick=adaptive_tick, fixed_tick=40.0)
    g2 = _build_tf_from_bucket_df(df1, m1, 7200, 10.0, adaptive_tick=adaptive_tick, fixed_tick=60.0)
    g4 = _build_tf_from_bucket_df(df1, m1, 14400, 16.0, adaptive_tick=adaptive_tick, fixed_tick=100.0)
    f_ts, f_rt = _load_funding_arrays(sym, data_dir=data_dir)
    if len(f_ts) > 0:
        idx_f = np.searchsorted(f_ts, g4["end_ts"].to_numpy(), side="right") - 1
        g4["fund_bp"] = np.where(idx_f >= 0, f_rt[np.maximum(0, idx_f)] * 10000.0, 0.0)
    else:
        g4["fund_bp"] = 0.0
    _TF_CACHE[sym] = (key, g1, g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c)
    return g1, g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c


def load_asset_tfs_cached(
    symbol: str,
    data_dir: Path = DATA_DIR,
    adaptive_tick: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load (or build & disk-cache) 2H + 4H footprint bars + 1m OHLC arrays for any MT5-Binance asset."""
    import pyarrow as pa

    sym = symbol.upper()
    path = data_dir / f"{sym}_agg_footprint.parquet"
    k_path = data_dir / f"{sym}_klines_1m.parquet"
    monthly_paths = sorted(Path(p) for p in glob.glob(str(data_dir / "monthly" / f"{sym}_*.parquet")))
    if not monthly_paths and not path.is_file():
        raise FileNotFoundError(f"No footprint parquets found for {sym} in {data_dir}")

    f_path = data_dir / f"{sym}_funding_8h.parquet"
    f_st = f_path.stat() if f_path.is_file() else None
    p_st = path.stat() if path.is_file() else None
    k_st = k_path.stat() if k_path.is_file() else None
    sig = {
        "m_cnt": len(monthly_paths),
        "m_bytes": sum(p.stat().st_size for p in monthly_paths),
        "m_mtime": max((p.stat().st_mtime_ns for p in monthly_paths), default=0),
        "a_size": p_st.st_size if p_st else 0,
        "a_mtime": p_st.st_mtime_ns if p_st else 0,
        "k_size": k_st.st_size if k_st else 0,
        "k_mtime": k_st.st_mtime_ns if k_st else 0,
        "f_size": f_st.st_size if f_st else 0,
        "adaptive": int(adaptive_tick),
        "ver": 2,
    }
    cache_dir = data_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta_p = cache_dir / f"{sym}_meta.json"
    g2_p = cache_dir / f"{sym}_2h.parquet"
    g4_p = cache_dir / f"{sym}_4h.parquet"
    m1_p = cache_dir / f"{sym}_m1.parquet"

    hit = False
    if meta_p.is_file() and g2_p.is_file() and g4_p.is_file() and m1_p.is_file():
        try:
            import json
            hit = json.loads(meta_p.read_text(encoding="utf-8")) == sig
        except Exception:
            hit = False

    if hit:
        g2 = pq.read_table(g2_p).to_pandas()
        g4 = pq.read_table(g4_p).to_pandas()
        m1 = pq.read_table(m1_p).to_pandas()
        return (
            g2, g4,
            m1["bar_1m"].to_numpy(dtype=np.int64),
            m1["o"].to_numpy(dtype=np.float64),
            m1["h"].to_numpy(dtype=np.float64),
            m1["l"].to_numpy(dtype=np.float64),
            m1["c"].to_numpy(dtype=np.float64),
        )

    m1_slim = pd.DataFrame()
    if monthly_paths or p_st is not None:
        from footprint_Chart.backtesting.fast_kernels import parallel_build_asset_tf_arrays

        m1_d, g2_d, g4_d = parallel_build_asset_tf_arrays(
            [str(p) for p in monthly_paths],
            str(path) if p_st is not None else None,
            adaptive_tick=adaptive_tick,
            max_workers=6,
        )
        m1_slim = pd.DataFrame(m1_d)

        def _finish_tf(d: dict[str, Any]) -> pd.DataFrame:
            gf = pd.DataFrame(d)
            gf = gf.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
            gf["delta"] = gf["bv"] - gf["sv"]
            gf["d_pct"] = np.where(gf["v"] > 0, gf["delta"] / gf["v"] * 100.0, 0.0)
            gf["avg_sz"] = np.where(gf["nt"] > 0, gf["v"] / gf["nt"], 0.0)
            gf["avg_sz_rk"] = gf["avg_sz"].rolling(100).rank(pct=True)
            gf["tr"] = np.maximum(
                gf["h"] - gf["l"],
                np.maximum((gf["h"] - gf["c"].shift(1)).abs(), (gf["l"] - gf["c"].shift(1)).abs()),
            )
            gf["atr"] = gf["tr"].rolling(20).mean()
            gf["clv"] = np.where(
                gf["h"] > gf["l"],
                ((gf["c"] - gf["l"]) - (gf["h"] - gf["c"])) / (gf["h"] - gf["l"]),
                0.0,
            )
            gf["ema89"] = gf["c"].ewm(span=89, adjust=False).mean()
            gf["ema233"] = gf["c"].ewm(span=233, adjust=False).mean()
            cvd = gf["delta"].cumsum()
            gf["cvd_z24"] = (cvd - cvd.rolling(24).mean()) / cvd.rolling(24).std().replace(0, np.nan)
            gf["vpin12"] = gf["delta"].abs().rolling(12).sum() / gf["v"].rolling(12).sum().replace(0, np.nan)
            gf["vpin_rank"] = gf["vpin12"].rolling(100).rank(pct=True)
            gf["vpm_rk"] = (gf["v"] / gf["dur_m"].clip(lower=1)).rolling(50).rank(pct=True)
            vw = (gf["c"] * gf["v"]).rolling(48).sum() / gf["v"].rolling(48).sum().replace(0, np.nan)
            sd = gf["c"].rolling(48).std().replace(0, np.nan)
            gf["vwz48"] = (gf["c"] - vw) / sd
            return gf

        g2 = _finish_tf(g2_d)
        g4 = _finish_tf(g4_d)
        f_ts, f_rt = _load_funding_arrays(sym, data_dir=data_dir)
        if len(f_ts) > 0:
            idx_f = np.searchsorted(f_ts, g4["end_ts"].to_numpy(), side="right") - 1
            g4["fund_bp"] = np.where(idx_f >= 0, f_rt[np.maximum(0, idx_f)] * 10000.0, 0.0)
        else:
            g4["fund_bp"] = 0.0
    else:
        raise FileNotFoundError(f"Empty footprint data for {sym}")

    import json
    pq.write_table(pa.Table.from_pandas(g2, preserve_index=False), g2_p, compression="snappy")
    pq.write_table(pa.Table.from_pandas(g4, preserve_index=False), g4_p, compression="snappy")
    pq.write_table(pa.Table.from_pandas(m1_slim, preserve_index=False), m1_p, compression="snappy")
    meta_p.write_text(json.dumps(sig), encoding="utf-8")
    return (
        g2, g4,
        m1_slim["bar_1m"].to_numpy(dtype=np.int64),
        m1_slim["o"].to_numpy(dtype=np.float64),
        m1_slim["h"].to_numpy(dtype=np.float64),
        m1_slim["l"].to_numpy(dtype=np.float64),
        m1_slim["c"].to_numpy(dtype=np.float64),
    )


def _load_multiyear_frames(symbol: str = "BTCUSDT", data_dir: Path = DATA_DIR):
    """Load 2021-2026 seamless 1m order-flow + funding + 4H frame (falls back to agg_footprint if klines_1m missing)."""
    sym = symbol.upper()
    k_path = data_dir / f"{sym}_klines_1m.parquet"
    a_path = data_dir / f"{sym}_agg_footprint.parquet"
    if not k_path.is_file():
        g1, g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = _load_1y_tf_frames(sym, data_dir=data_dir)
        return g4, m1_ts, m1_o, m1_h, m1_l, m1_c

    st_k = k_path.stat()
    st_a = a_path.stat() if a_path.is_file() else None
    key = (st_k.st_mtime_ns, st_k.st_size, st_a.st_mtime_ns if st_a else 0, st_a.st_size if st_a else 0)
    cached = _MY_CACHE.get(sym)
    if cached and cached[0] == key:
        return cached[1:]

    k = pq.read_table(k_path, columns=["bar_1m", "open", "high", "low", "close", "volume", "count", "taker_buy_vol"]).to_pandas()
    m1 = pd.DataFrame({
        "bar_1m": k["bar_1m"].to_numpy(dtype=np.int64),
        "o": k["open"].to_numpy(dtype=np.float64),
        "h": k["high"].to_numpy(dtype=np.float64),
        "l": k["low"].to_numpy(dtype=np.float64),
        "c": k["close"].to_numpy(dtype=np.float64),
        "bv": k["taker_buy_vol"].to_numpy(dtype=np.float64),
        "v": k["volume"].to_numpy(dtype=np.float64),
        "nt": k["count"].to_numpy(dtype=np.float64),
    })
    m1["sv"] = np.maximum(0.0, m1["v"] - m1["bv"])

    if a_path.is_file():
        last_k_ts = int(m1["bar_1m"].max())
        a_tbl = pq.read_table(
            a_path,
            columns=["bar_1m", "buy_trades", "sell_trades", "buy_qty", "sell_qty", "open", "high", "low", "close"],
            filters=[("bar_1m", ">", last_k_ts)],
        ).to_pandas()
        if not a_tbl.empty:
            a_tbl["v"] = a_tbl["buy_qty"] + a_tbl["sell_qty"]
            a_tbl["nt"] = a_tbl["buy_trades"] + a_tbl["sell_trades"]
            tail_m1 = a_tbl.groupby("bar_1m", sort=True).agg(
                o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"),
                bv=("buy_qty", "sum"), sv=("sell_qty", "sum"), v=("v", "sum"), nt=("nt", "sum"),
            ).reset_index()
            m1 = pd.concat([m1, tail_m1], ignore_index=True).drop_duplicates("bar_1m", keep="last").sort_values("bar_1m").reset_index(drop=True)

    m1_ts = m1["bar_1m"].to_numpy(dtype=np.int64)
    m1_o = m1["o"].to_numpy(dtype=np.float64)
    m1_h = m1["h"].to_numpy(dtype=np.float64)
    m1_l = m1["l"].to_numpy(dtype=np.float64)
    m1_c = m1["c"].to_numpy(dtype=np.float64)

    tf_sec = 14400
    g4 = m1.assign(ts=(m1["bar_1m"] // tf_sec) * tf_sec).groupby("ts", sort=True).agg(
        o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"),
        bv=("bv", "sum"), sv=("sv", "sum"), v=("v", "sum"), nt=("nt", "sum"),
    ).reset_index()
    g4["end_ts"] = g4["ts"] + tf_sec
    g4["dur_m"] = tf_sec // 60
    g4["delta"] = g4["bv"] - g4["sv"]
    g4["d_pct"] = np.where(g4["v"] > 0, g4["delta"] / g4["v"] * 100.0, 0.0)
    g4["avg_sz"] = np.where(g4["nt"] > 0, g4["v"] / g4["nt"], 0.0)
    g4["avg_sz_rk"] = g4["avg_sz"].rolling(100).rank(pct=True)
    g4["tr"] = np.maximum(g4["h"] - g4["l"], np.maximum((g4["h"] - g4["c"].shift(1)).abs(), (g4["l"] - g4["c"].shift(1)).abs()))
    g4["atr"] = g4["tr"].rolling(20).mean()
    g4["clv"] = np.where(g4["h"] > g4["l"], ((g4["c"] - g4["l"]) - (g4["h"] - g4["c"])) / (g4["h"] - g4["l"]), 0.0)
    g4["ema89"] = g4["c"].ewm(span=89, adjust=False).mean()
    g4["ema233"] = g4["c"].ewm(span=233, adjust=False).mean()
    cvd = g4["delta"].cumsum()
    g4["cvd_z24"] = (cvd - cvd.rolling(24).mean()) / cvd.rolling(24).std().replace(0, np.nan)
    g4["vpin12"] = g4["delta"].abs().rolling(12).sum() / g4["v"].rolling(12).sum().replace(0, np.nan)
    g4["vpin_rank"] = g4["vpin12"].rolling(100).rank(pct=True)
    g4["vpm_rk"] = (g4["v"] / 240.0).rolling(50).rank(pct=True)
    vw = (g4["c"] * g4["v"]).rolling(48).sum() / g4["v"].rolling(48).sum().replace(0, np.nan)
    sd_p = g4["c"].rolling(48).std().replace(0, np.nan)
    g4["vwz48"] = (g4["c"] - vw) / sd_p

    f_ts, f_rt = _load_funding_arrays(sym, data_dir=data_dir)
    if len(f_ts) > 0:
        idx_f = np.searchsorted(f_ts, g4["end_ts"].to_numpy(), side="right") - 1
        g4["fund_bp"] = np.where(idx_f >= 0, f_rt[np.maximum(0, idx_f)] * 10000.0, 0.0)
    else:
        g4["fund_bp"] = 0.0

    _MY_CACHE[sym] = (key, g4, m1_ts, m1_o, m1_h, m1_l, m1_c)
    return g4, m1_ts, m1_o, m1_h, m1_l, m1_c


def _sim_sleeve_1m_exact(
    df: pd.DataFrame,
    ml: pd.Series | np.ndarray,
    ms: pd.Series | np.ndarray,
    tf_sec: int,
    sleeve: str,
    m1_ts: np.ndarray,
    m1_o: np.ndarray,
    m1_h: np.ndarray,
    m1_l: np.ndarray,
    m1_c: np.ndarray,
    sl_atr: float = 2.0,
    tp_r: float = 0.0,
    trail_atr: float = 2.5,
    be_r: float = 1.0,
    be_pad: float = 0.05,
    step_lock: bool = True,
    max_hold: int = 96,
    cost_bps: float = 12.0,
    fund_bps_8h: float = 1.0,
    entry_delay_mins: int = 1,
    realistic_costs: bool = True,
    dyn_atr: bool = True,
    f_ts: np.ndarray | None = None,
    f_rt: np.ndarray | None = None,
    mt5_cost: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Replay trades minute-by-minute via Numba-compiled kernel with latency, asymmetric slippage, ATR trail & swap/funding."""
    from footprint_Chart.backtesting.fast_kernels import sim_1m_trades_nb

    end_ts = df["end_ts"].to_numpy(dtype=np.int64)
    ts = df["ts"].to_numpy(dtype=np.int64)
    atr = df["atr"].to_numpy(dtype=np.float64)
    ema89_a = df["ema89"].to_numpy(dtype=np.float64) if "ema89" in df else np.full(len(df), np.nan, dtype=np.float64)
    ema233_a = df["ema233"].to_numpy(dtype=np.float64) if "ema233" in df else np.full(len(df), np.nan, dtype=np.float64)
    c_a = df["c"].to_numpy(dtype=np.float64)
    n = len(df)
    la = ml.fillna(False).to_numpy(dtype=bool) if isinstance(ml, pd.Series) else np.asarray(ml, dtype=bool)
    sa = ms.fillna(False).to_numpy(dtype=bool) if isinstance(ms, pd.Series) else np.asarray(ms, dtype=bool)
    side_arr = np.where(la, 1, np.where(ms_arr := sa, -1, 0)).astype(np.int8)
    del ms_arr

    use_mt5 = mt5_cost is not None
    sym_str = str(mt5_cost.get("symbol", "")) if mt5_cost else ""
    sp_bps = float(mt5_cost.get("sp_bps", 12.0)) if mt5_cost else 0.0
    roll3d = int(mt5_cost.get("roll3d", 5)) if mt5_cost else 5
    sw_l_bps = float(mt5_cost.get("sw_l_bps", 0.0)) if mt5_cost else 0.0
    sw_s_bps = float(mt5_cost.get("sw_s_bps", 0.0)) if mt5_cost else 0.0
    fts_a = np.asarray(f_ts, dtype=np.int64) if f_ts is not None else np.empty(0, dtype=np.int64)
    frt_a = np.asarray(f_rt, dtype=np.float64) if f_rt is not None else np.empty(0, dtype=np.float64)

    (
        o_bi, o_ti, o_to, o_sd, o_rsn, o_ent, o_exp, o_r, o_fr, o_reg,
    ) = sim_1m_trades_nb(
        ts, end_ts, atr, ema89_a, ema233_a, c_a, side_arr,
        np.asarray(m1_ts, dtype=np.int64), np.asarray(m1_o, dtype=np.float64),
        np.asarray(m1_h, dtype=np.float64), np.asarray(m1_l, dtype=np.float64), np.asarray(m1_c, dtype=np.float64),
        int(tf_sec), float(sl_atr), float(tp_r), float(trail_atr), float(be_r), float(be_pad),
        bool(step_lock), int(max_hold), max(0, int(entry_delay_mins)), bool(dyn_atr),
        bool(use_mt5), sp_bps, bool(realistic_costs), float(cost_bps), float(fund_bps_8h),
        roll3d, sw_l_bps, sw_s_bps, fts_a, frt_a,
    )

    rsn_map = ("TIME", "SL", "TRAIL", "TP")
    reg_map = ("TRANSITION", "BULL", "BEAR")
    trades: list[dict[str, Any]] = []
    for k in range(len(o_bi)):
        bi_k = int(o_bi[k])
        ti_k = int(o_ti[k])
        ent = float(o_ent[k])
        exp = float(o_exp[k])
        px_r = 4 if ent < 100.0 else 2
        trades.append({
            "symbol": sym_str,
            "sleeve": sleeve,
            "bar_i": bi_k,
            "ts_in": ti_k,
            "ts_out": int(o_to[k]),
            "year": int(datetime.fromtimestamp(ti_k, tz=timezone.utc).year),
            "regime": reg_map[int(o_reg[k])],
            "side": "BUY" if int(o_sd[k]) == 1 else "SELL",
            "entry": round(ent, px_r),
            "exit": round(exp, px_r),
            "reason": rsn_map[int(o_rsn[k])],
            "r": float(o_r[k]),
            "fee_r": float(o_fr[k]),
            "q": min(3, (bi_k * 4) // n),
        })
    return trades


def _run_20k_monte_carlo(arr: np.ndarray, start_eq: float, risk_pct: float, years_span: float = 1.0, n_mc: int = 20000) -> dict[str, Any]:
    if len(arr) < 5:
        return {}
    rng = np.random.default_rng(2026)
    rp = risk_pct / 100.0
    # Normalize block simulation to a 1-year horizon (or full span if ~1Y) so P(<$5K) and DD95 are comparable across spans
    horizon_len = len(arr) if years_span <= 1.6 else max(20, round(len(arr) / years_span))

    dds_iid = np.empty(n_mc)
    for b0 in range(0, n_mc, 2000):
        bs = min(2000, n_mc - b0)
        idx = rng.integers(0, len(arr), size=(bs, horizon_len))
        eq_m = start_eq * np.cumprod(1.0 + arr[idx] * rp, axis=1)
        eq_f = np.hstack([np.full((bs, 1), start_eq), eq_m])
        pk_m = np.maximum.accumulate(eq_f, axis=1)
        dds_iid[b0 : b0 + bs] = ((pk_m - eq_f) / pk_m * 100.0).max(axis=1)

    blk = 8
    n_blk = (horizon_len + blk - 1) // blk
    starts_b = rng.integers(0, max(1, len(arr) - blk + 1), size=(n_mc, n_blk))
    flat_idx = (starts_b[:, :, None] + np.arange(blk)[None, None, :]).reshape(n_mc, -1)[:, :horizon_len]
    samp = arr[flat_idx]
    eq_b = start_eq * np.cumprod(1.0 + samp * rp, axis=1)
    eq_bf = np.hstack([np.full((n_mc, 1), start_eq), eq_b])
    pk_b = np.maximum.accumulate(eq_bf, axis=1)
    dds_blk = ((pk_b - eq_bf) / pk_b * 100.0).max(axis=1)
    ends_blk = eq_b[:, -1]

    # Crash-Stress MC: inject one correlated 3-trade -4.5R flash-crash shock cluster into each synthetic 1Y path
    samp_cr = samp.copy()
    if horizon_len >= 6:
        cpos = rng.integers(0, horizon_len - 3, size=n_mc)
        rows = np.arange(n_mc)
        samp_cr[rows, cpos] = -1.5
        samp_cr[rows, cpos + 1] = -1.5
        samp_cr[rows, cpos + 2] = -1.5
    eq_cr = start_eq * np.cumprod(1.0 + samp_cr * rp, axis=1)
    eq_crf = np.hstack([np.full((n_mc, 1), start_eq), eq_cr])
    pk_cr = np.maximum.accumulate(eq_crf, axis=1)
    dds_cr = ((pk_cr - eq_crf) / pk_cr * 100.0).max(axis=1)

    return {
        "runs": int(n_mc),
        "horizon_trades": int(horizon_len),
        "iid_dd_p50": round(float(np.percentile(dds_iid, 50)), 1),
        "iid_dd_p95": round(float(np.percentile(dds_iid, 95)), 1),
        "iid_dd_p99": round(float(np.percentile(dds_iid, 99)), 1),
        "blk_eq_p5": round(float(np.percentile(ends_blk, 5)), 0),
        "blk_eq_p50": round(float(np.percentile(ends_blk, 50)), 0),
        "blk_eq_p95": round(float(np.percentile(ends_blk, 95)), 0),
        "blk_dd_p95": round(float(np.percentile(dds_blk, 95)), 1),
        "p_loss_pct": round(float((ends_blk < start_eq).mean() * 100.0), 2),
        "crash_dd_p95": round(float(np.percentile(dds_cr, 95)), 1),
        "crash_p_loss_pct": round(float((eq_cr[:, -1] < start_eq).mean() * 100.0), 2),
    }


def _build_institutional_4h_trades(
    g4: pd.DataFrame,
    m1_ts: np.ndarray,
    m1_o: np.ndarray,
    m1_h: np.ndarray,
    m1_l: np.ndarray,
    m1_c: np.ndarray,
    cost_bps: float,
    funding_bps_8h: float,
    entry_delay_mins: int,
    realistic_costs: bool,
    f_ts: np.ndarray | None,
    f_rt: np.ndarray | None,
    mt5_cost: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build 6 sample-verified, cross-year OOS-validated 4H Order-Flow sleeves (2021-2026)."""
    sim_kw = dict(
        cost_bps=cost_bps, fund_bps_8h=funding_bps_8h,
        entry_delay_mins=entry_delay_mins, realistic_costs=realistic_costs,
        f_ts=f_ts, f_rt=f_rt, mt5_cost=mt5_cost,
    )
    m1_args = (m1_ts, m1_o, m1_h, m1_l, m1_c)

    hi22 = g4["h"].shift(1).rolling(22).max()
    lo22 = g4["l"].shift(1).rolling(22).min()
    up233 = g4["c"] > g4["ema233"]
    dn233 = g4["c"] < g4["ema233"]
    stkL = (g4["ema89"] > g4["ema233"]) & (g4["ema89"] > g4["ema89"].shift(3)) & up233
    stkS = (g4["ema89"] < g4["ema233"]) & (g4["ema89"] < g4["ema89"].shift(3)) & dn233

    la09 = (g4["c"] > hi22) & up233 & (g4["clv"] > 0.30) & (g4["d_pct"] > 0.0)
    sa09 = (g4["c"] < lo22) & dn233 & (g4["clv"] < -0.30) & (g4["d_pct"] < 0.0)
    if "buy_imb" in g4 and (g4["buy_imb"] > 0).any():
        # Apply imbalance confirmation strictly on bars where full-bucket parquet exists
        has_b = (g4["buy_imb"] + g4["sell_imb"]) > 0
        la09 = la09 & (~has_b | (g4["buy_imb"] >= g4["sell_imb"]))
        sa09 = sa09 & (~has_b | (g4["sell_imb"] >= g4["buy_imb"]))

    la10 = stkL & (g4["l"] <= g4["ema89"] + 0.25 * g4["atr"]) & (g4["c"] > g4["ema89"]) & (g4["d_pct"] > 5.0) & (g4["clv"] > 0.25)
    sa10 = stkS & (g4["h"] >= g4["ema89"] - 0.25 * g4["atr"]) & (g4["c"] < g4["ema89"]) & (g4["d_pct"] < -5.0) & (g4["clv"] < -0.25)

    whL = up233 & (g4["avg_sz_rk"] > 0.70) & (g4["d_pct"] > 8.0) & (g4["clv"] > 0.40) & (g4["vpm_rk"] > 0.50)
    whS = dn233 & (g4["avg_sz_rk"] > 0.70) & (g4["d_pct"] < -8.0) & (g4["clv"] < -0.40) & (g4["vpm_rk"] > 0.50)

    lo10 = g4["l"].shift(1).rolling(10).min()
    hi10 = g4["h"].shift(1).rolling(10).max()
    swpL = up233 & (g4["l"] < lo10) & (g4["c"] > lo10) & (g4["clv"] > 0.35) & (g4["d_pct"] > 3.0)
    swpS = dn233 & (g4["h"] > hi10) & (g4["c"] < hi10) & (g4["clv"] < -0.35) & (g4["d_pct"] < -3.0)

    cvdL = (g4["l"] < lo10) & (g4["cvd_z24"] > 0.5) & (g4["clv"] > 0.25) & (g4["d_pct"] > 0.0)
    cvdS = (g4["h"] > hi10) & (g4["cvd_z24"] < -0.5) & (g4["clv"] < -0.25) & (g4["d_pct"] < 0.0)

    fcL = (g4["fund_bp"] <= -0.5) & (g4["vwz48"] < -1.5) & (g4["d_pct"] > 0.0) & (g4["clv"] > 0.2)
    fcS = (g4["fund_bp"] >= 3.0) & (g4["vwz48"] > 1.5) & (g4["d_pct"] < 0.0) & (g4["clv"] < -0.2)

    return (
        _sim_sleeve_1m_exact(g4, la09, sa09, 14400, "S09 Donchian22 + OF (4h)", *m1_args, 2.0, 0.0, 3.5, 0.0, 0.05, False, 96, dyn_atr=True, **sim_kw)
        + _sim_sleeve_1m_exact(g4, la10, sa10, 14400, "S10 EMA89 Pullback + OF (4h)", *m1_args, 2.0, 0.0, 3.5, 0.0, 0.05, False, 96, dyn_atr=True, **sim_kw)
        + _sim_sleeve_1m_exact(g4, whL, whS, 14400, "Whale TradeSize p70 + OF (4h)", *m1_args, 2.0, 0.0, 3.5, 0.0, 0.05, False, 96, dyn_atr=True, **sim_kw)
        + _sim_sleeve_1m_exact(g4, swpL, swpS, 14400, "Liquidity Sweep10 + EMA233 (4h)", *m1_args, 2.0, 3.5, 0.0, 0.0, 0.2, False, 48, dyn_atr=False, **sim_kw)
        + _sim_sleeve_1m_exact(g4, cvdL, cvdS, 14400, "CVD Z24 Divergence (4h)", *m1_args, 2.5, 3.5, 0.0, 0.0, 0.2, False, 48, dyn_atr=False, **sim_kw)
        + _sim_sleeve_1m_exact(g4, fcL, fcS, 14400, "Funding Crowd + VWAP Fade (4h)", *m1_args, 2.5, 4.0, 0.0, 1.0, 0.2, False, 60, dyn_atr=False, **sim_kw)
    )


def _build_hardened_fp_trades(
    g1: pd.DataFrame,
    g4: pd.DataFrame,
    m1_ts: np.ndarray,
    m1_o: np.ndarray,
    m1_h: np.ndarray,
    m1_l: np.ndarray,
    m1_c: np.ndarray,
    cost_bps: float,
    funding_bps_8h: float,
    entry_delay_mins: int,
    realistic_costs: bool,
    f_ts: np.ndarray | None,
    f_rt: np.ndarray | None,
) -> list[dict[str, Any]]:
    """Hardened 7-Sleeve Full-Bucket Footprint Portfolio (pruned n<30 void sleeves & 1H churn)."""
    fp1 = build_fp_context(g1)
    fp4 = build_fp_context(g4)
    sim_kw = dict(
        cost_bps=cost_bps, fund_bps_8h=funding_bps_8h,
        entry_delay_mins=entry_delay_mins, realistic_costs=realistic_costs,
        f_ts=f_ts, f_rt=f_rt,
    )

    def _run_sig_fn(df: pd.DataFrame, fp: dict[str, Any], fn, params: dict[str, Any], tf_sec: int, name: str, trail_atr: float, be_r: float, dyn_atr: bool):
        o, h, l, c = fp["o"], fp["h"], fp["l"], fp["c"]
        tm = (df["ts"].to_numpy() // 60).astype(np.int64)
        atr = fp["atr20"]
        n = len(df)
        ml = np.zeros(n, dtype=bool)
        ms = np.zeros(n, dtype=bool)
        extra = {"fp": fp}
        p_cfg = {**params, "fp_mode": True}
        sig_meta: dict[str, Any] = {}
        for i in range(235, n - 3):
            sig = fn(o, h, l, c, tm, atr, i, p_cfg, extra)
            if sig is not None:
                if sig["side"] == 1:
                    ml[i] = True
                elif sig["side"] == -1:
                    ms[i] = True
                if not sig_meta:
                    sig_meta = sig
        sl_atr = float(p_cfg.get("fp_sl_atr", 2.0))
        tp_r = float(sig_meta.get("exit_r", 0.0))
        max_hold = int(sig_meta.get("max_hold", 96))
        return _sim_sleeve_1m_exact(
            df, pd.Series(ml, index=df.index), pd.Series(ms, index=df.index), tf_sec, name,
            m1_ts, m1_o, m1_h, m1_l, m1_c, sl_atr, tp_r, trail_atr, be_r, 0.2 if tp_r > 0 else 0.05,
            False, max_hold, dyn_atr=dyn_atr, **sim_kw,
        )

    m1_args = (m1_ts, m1_o, m1_h, m1_l, m1_c)
    whL = (g4["c"] > g4["ema233"]) & (g4["avg_sz_rk"] > 0.70) & (g4["d_pct"] > 8.0) & (g4["clv"] > 0.40) & (g4["vpm_rk"] > 0.50)
    whS = (g4["c"] < g4["ema233"]) & (g4["avg_sz_rk"] > 0.70) & (g4["d_pct"] < -8.0) & (g4["clv"] < -0.40) & (g4["vpm_rk"] > 0.50)
    lo10 = g4["l"].shift(1).rolling(10).min()
    hi10 = g4["h"].shift(1).rolling(10).max()
    swpL = (g4["c"] > g4["ema233"]) & (g4["l"] < lo10) & (g4["c"] > lo10) & (g4["clv"] > 0.35) & (g4["d_pct"] > 3.0)
    swpS = (g4["c"] < g4["ema233"]) & (g4["h"] > hi10) & (g4["c"] < hi10) & (g4["clv"] < -0.35) & (g4["d_pct"] < -3.0)
    cvdL = (g4["l"] < lo10) & (g4["cvd_z24"] > 0.5) & (g4["clv"] > 0.25) & (g4["d_pct"] > 0.0)
    cvdS = (g4["h"] > hi10) & (g4["cvd_z24"] < -0.5) & (g4["clv"] < -0.25) & (g4["d_pct"] < 0.0)
    if f_ts is not None and f_rt is not None and len(f_ts) > 0:
        idx_f = np.searchsorted(f_ts, g4["end_ts"].to_numpy(), side="right") - 1
        fbp = pd.Series(np.where(idx_f >= 0, f_rt[np.maximum(0, idx_f)] * 10000.0, 0.0), index=g4.index)
    else:
        fbp = pd.Series(0.0, index=g4.index)
    fcL = (fbp <= -0.5) & (g4["vwz48"] < -1.5) & (g4["d_pct"] > 0.0) & (g4["clv"] > 0.2)
    fcS = (fbp >= 3.0) & (g4["vwz48"] > 1.5) & (g4["d_pct"] < 0.0) & (g4["clv"] < -0.2)

    return (
        _run_sig_fn(g4, fp4, s09_signal_at, {"fp_sl_atr": 2.0}, 14400, "S09-FP Donch22 + Imb (4h)", 3.5, 0.0, True)
        + _run_sig_fn(g4, fp4, s10_signal_at, {"fp_sl_atr": 2.0, "fp_zero_branch": False}, 14400, "S10-FP EMA89 + OF (4h)", 3.5, 0.0, True)
        + _sim_sleeve_1m_exact(g4, whL, whS, 14400, "Whale TradeSize p70 (4h)", *m1_args, 2.0, 0.0, 3.5, 0.0, 0.05, False, 96, dyn_atr=True, **sim_kw)
        + _sim_sleeve_1m_exact(g4, swpL, swpS, 14400, "Liquidity Sweep10 (4h)", *m1_args, 2.0, 3.5, 0.0, 0.0, 0.2, False, 48, dyn_atr=False, **sim_kw)
        + _sim_sleeve_1m_exact(g4, cvdL, cvdS, 14400, "CVD Z24 Divergence (4h)", *m1_args, 2.5, 3.5, 0.0, 0.0, 0.2, False, 48, dyn_atr=False, **sim_kw)
        + _sim_sleeve_1m_exact(g4, g4["cg_exh_b"] & (g4["c"] > g4["ema89"]), g4["cg_exh_s"] & (g4["c"] < g4["ema89"]), 14400, "CG Exhaustion + EMA89 (4h)", *m1_args, 2.0, 0.0, 3.5, 0.0, 0.05, False, 96, dyn_atr=True, **sim_kw)
        + _sim_sleeve_1m_exact(g4, fcL, fcS, 14400, "Funding Crowd + VWAP Fade (4h)", *m1_args, 2.5, 4.0, 0.0, 1.0, 0.2, False, 60, dyn_atr=False, **sim_kw)
    )


def _build_optimal_8s_trades(
    g2: pd.DataFrame,
    g4: pd.DataFrame,
    m1_ts: np.ndarray,
    m1_o: np.ndarray,
    m1_h: np.ndarray,
    m1_l: np.ndarray,
    m1_c: np.ndarray,
    cost_bps: float,
    funding_bps_8h: float,
    entry_delay_mins: int,
    realistic_costs: bool,
    f_ts: np.ndarray | None,
    f_rt: np.ndarray | None,
    mt5_cost: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Optimal 8-Sleeve Full-Bucket Order-Flow Stack: Base 6 (4H) + Imb2@EMA233 (2H) + RSI2+OF (4H)."""
    sim_kw = dict(
        cost_bps=cost_bps, fund_bps_8h=funding_bps_8h,
        entry_delay_mins=entry_delay_mins, realistic_costs=realistic_costs,
        f_ts=f_ts, f_rt=f_rt, mt5_cost=mt5_cost,
    )
    m1_args = (m1_ts, m1_o, m1_h, m1_l, m1_c)
    base6 = _build_institutional_4h_trades(
        g4, m1_ts, m1_o, m1_h, m1_l, m1_c,
        cost_bps, funding_bps_8h, entry_delay_mins, realistic_costs, f_ts, f_rt, mt5_cost=mt5_cost,
    )

    t233_L2 = (g2["l"] <= g2["ema233"] + 0.3 * g2["atr"]) & (g2["c"] > g2["ema233"]) & (g2["c"].shift(2) > g2["ema233"].shift(2))
    t233_S2 = (g2["h"] >= g2["ema233"] - 0.3 * g2["atr"]) & (g2["c"] < g2["ema233"]) & (g2["c"].shift(2) < g2["ema233"].shift(2))
    imb2_trs = _sim_sleeve_1m_exact(
        g2,
        (g2["buy_imb"] >= 2) & (g2["clv"] > 0.25) & t233_L2,
        (g2["sell_imb"] >= 2) & (g2["clv"] < -0.25) & t233_S2,
        7200, "Plugin_S03_EMA233+Imb2 (2h)", *m1_args,
        sl_atr=2.5, tp_r=3.5, trail_atr=0.0, be_r=1.0, be_pad=0.2,
        step_lock=False, max_hold=72, dyn_atr=False, **sim_kw,
    )

    r2_4 = pd.Series(rsi_wilder_arr(g4["c"].to_numpy(dtype=float), 2), index=g4.index)
    of_b4 = g4["cg_abs_b"] | g4["cg_exh_b"] | g4["cg_div_b"] | ((g4["d_pct"] > 5.0) & (g4["clv"] > 0.25))
    of_s4 = g4["cg_abs_s"] | g4["cg_exh_s"] | g4["cg_div_s"] | ((g4["d_pct"] < -5.0) & (g4["clv"] < -0.25))
    ok4 = g4["vpin_rank"] < 0.65
    s02_trs = _sim_sleeve_1m_exact(
        g4,
        ok4 & (r2_4 < 15.0) & (g4["c"] > g4["ema233"]) & of_b4,
        ok4 & (r2_4 > 85.0) & (g4["c"] < g4["ema233"]) & of_s4,
        14400, "Plugin_S02_RSI2+OF (4h)", *m1_args,
        sl_atr=2.0, tp_r=3.5, trail_atr=0.0, be_r=1.0, be_pad=0.05,
        step_lock=True, max_hold=48, dyn_atr=True, **sim_kw,
    )
    return base6 + imb2_trs + s02_trs


def _dsr_pct(arr: np.ndarray, k_trials: float) -> float:
    if len(arr) <= 10 or k_trials < 2.0:
        return 0.0
    sr = float(arr.mean() / (arr.std() + 1e-9))
    sk = float(pd.Series(arr).skew())
    ku = float(pd.Series(arr).kurt() + 3.0)
    gamma = 0.5772156649
    z_k = (1 - gamma) * math.sqrt(2 * math.log(k_trials)) + gamma * math.sqrt(2 * math.log(k_trials) - math.log(math.log(k_trials)) - math.log(4 * math.pi))
    z_dsr = ((sr - z_k / math.sqrt(len(arr))) * math.sqrt(len(arr) - 1)) / math.sqrt(max(1e-9, 1.0 - sk * sr + ((ku - 1.0) / 4.0) * sr * sr))
    return round(0.5 * (1.0 + math.erf(z_dsr / math.sqrt(2.0))) * 100.0, 1)


def run_multi_sleeve_portfolio(
    symbol: str = "BTCUSDT",
    start_equity: float = 5000.0,
    risk_pct: float = 1.25,
    max_concurrent: int = 3,
    fee_bps: float = 10.0,
    slippage_bps: float = 2.0,
    funding_bps_8h: float = 1.0,
    be_r: float = 1.0,
    trail_atr: float = 2.5,
    mc_runs: int = 20000,
    data_dir: Path = DATA_DIR,
    profile: str = "optimal_8s",
    entry_delay_mins: int = 1,
    realistic_costs: bool = True,
    use_real_funding: bool = True,
    max_same_side: int = 2,
) -> dict[str, Any]:
    """Simulate 1m-exact compounding portfolio across multi-year or full-bucket footprint datasets."""
    sym = symbol.upper()
    cost_bps = max(0.0, float(fee_bps) + float(slippage_bps))
    f_ts, f_rt = _load_funding_arrays(sym, data_dir=data_dir) if use_real_funding else (None, None)

    if profile in ("optimal_8s", "best_optimal", "institutional_8s_optimal"):
        g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = load_asset_tfs_cached(sym, data_dir=data_dir, adaptive_tick=True)
        g_ref = g4
        raw_trades = _build_optimal_8s_trades(
            g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c,
            cost_bps, funding_bps_8h, entry_delay_mins, realistic_costs, f_ts, f_rt,
        )
        vof_tf = "H4"
    elif profile == "institutional_4h_6y":
        g_ref, m1_ts, m1_o, m1_h, m1_l, m1_c = _load_multiyear_frames(sym, data_dir=data_dir)
        raw_trades = _build_institutional_4h_trades(
            g_ref, m1_ts, m1_o, m1_h, m1_l, m1_c,
            cost_bps, funding_bps_8h, entry_delay_mins, realistic_costs, f_ts, f_rt,
        )
        vof_tf = "H4"
    elif profile in ("s01_s11_fp", "unified_9", "hardened_fp"):
        g1, g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = _load_1y_tf_frames(sym, data_dir=data_dir, adaptive_tick=True)
        g_ref = g4
        raw_trades = _build_hardened_fp_trades(
            g1, g4, m1_ts, m1_o, m1_h, m1_l, m1_c,
            cost_bps, funding_bps_8h, entry_delay_mins, realistic_costs, f_ts, f_rt,
        )
        vof_tf = "H4"
    else:
        g1, g2, g4, m1_ts, m1_o, m1_h, m1_l, m1_c = _load_1y_tf_frames(sym, data_dir=data_dir, adaptive_tick=True)
        g_ref = g1
        up233_1 = g1["c"] > g1["ema233"]
        dn233_1 = g1["c"] < g1["ema233"]
        stk_L1 = (g1["ema89"] > g1["ema233"]) & (g1["ema89"] > g1["ema89"].shift(3)) & up233_1
        stk_S1 = (g1["ema89"] < g1["ema233"]) & (g1["ema89"] < g1["ema89"].shift(3)) & dn233_1
        hi22 = g1["h"].shift(1).rolling(22).max()
        lo22 = g1["l"].shift(1).rolling(22).min()
        sA_L = (g1["c"] > hi22) & (g1["clv"] > 0.30) & (g1["d_pct"] > 0) & (g1["buy_imb"] >= g1["sell_imb"]) & up233_1
        sA_S = (g1["c"] < lo22) & (g1["clv"] < -0.30) & (g1["d_pct"] < 0) & (g1["sell_imb"] >= g1["buy_imb"]) & dn233_1
        t89_L = (g1["l"] <= g1["ema89"] + 0.25 * g1["atr"]) & (g1["c"] > g1["ema89"]) & stk_L1
        t89_S = (g1["h"] >= g1["ema89"] - 0.25 * g1["atr"]) & (g1["c"] < g1["ema89"]) & stk_S1
        sB_L = t89_L & (g1["d_pct"] > 5) & (g1["clv"] > 0.25)
        sB_S = t89_S & (g1["d_pct"] < -5) & (g1["clv"] < -0.25)
        sC_L = g1["zero_bot"] & stk_L1 & (g1["vpm_rk"] > 0.40)
        sC_S = g1["zero_top"] & stk_S1 & (g1["vpm_rk"] > 0.40)
        t233_L2 = (g2["l"] <= g2["ema233"] + 0.3 * g2["atr"]) & (g2["c"] > g2["ema233"]) & (g2["c"].shift(2) > g2["ema233"].shift(2))
        t233_S2 = (g2["h"] >= g2["ema233"] - 0.3 * g2["atr"]) & (g2["c"] < g2["ema233"]) & (g2["c"].shift(2) < g2["ema233"].shift(2))
        sG_L = (g2["vwz48"] < -2.0) & ((g2["d_pct"] > 0) | (g2["clv"] > 0.2)) & (g2["vpin_rank"] < 0.60)
        sG_S = (g2["vwz48"] > 2.0) & ((g2["d_pct"] < 0) | (g2["clv"] < -0.2)) & (g2["vpin_rank"] < 0.60)
        m1_args = (m1_ts, m1_o, m1_h, m1_l, m1_c)
        sim_kw = dict(cost_bps=cost_bps, fund_bps_8h=funding_bps_8h, entry_delay_mins=entry_delay_mins, realistic_costs=realistic_costs, f_ts=f_ts, f_rt=f_rt)
        raw_trades = (
            _sim_sleeve_1m_exact(g1, sA_L, sA_S, 3600, "1h OF Breakout + EMA233", *m1_args, 2.0, 0.0, trail_atr, be_r, 0.05, True, 96, dyn_atr=True, **sim_kw)
            + _sim_sleeve_1m_exact(g1, sB_L, sB_S, 3600, "1h EMA89 Rejection + OF", *m1_args, 2.0, 0.0, trail_atr, be_r, 0.05, True, 96, dyn_atr=True, **sim_kw)
            + _sim_sleeve_1m_exact(g1, sC_L, sC_S, 3600, "1h Zero + EMA89/233 + VPM", *m1_args, 2.0, 0.0, trail_atr, be_r, 0.05, True, 96, dyn_atr=True, **sim_kw)
            + _sim_sleeve_1m_exact(g2, (g2["buy_imb"] >= 2) & (g2["clv"] > 0.25) & t233_L2, (g2["sell_imb"] >= 2) & (g2["clv"] < -0.25) & t233_S2, 7200, "2h Imb Stack @ EMA233", *m1_args, 2.5, 3.5, 0.0, be_r, 0.2, False, 72, dyn_atr=False, **sim_kw)
            + _sim_sleeve_1m_exact(g2, g2["cg_exh_b"] & (g2["c"] > g2["ema89"]), g2["cg_exh_s"] & (g2["c"] < g2["ema89"]), 7200, "2h Exhaustion + EMA89", *m1_args, 2.0, 0.0, trail_atr, be_r, 0.2, True, 96, dyn_atr=True, **sim_kw)
            + _sim_sleeve_1m_exact(g2, sG_L, sG_S, 7200, "2h VWAP 2sd + Low VPIN", *m1_args, 2.5, 5.0, 0.0, 1.2, 0.2, False, 48, dyn_atr=False, **sim_kw)
        )
        vof_tf = "H1"

    sorted_t = sorted(raw_trades, key=lambda x: x["ts_in"])
    active: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    for t in sorted_t:
        active = [x for x in active if x["ts_out"] > t["ts_in"]]
        same_side = sum(1 for x in active if x["side"] == t["side"])
        if len(active) < max_concurrent and same_side < max(1, int(max_same_side)):
            executed.append(t)
            active.append(t)

    eq = float(start_equity)
    pk = float(start_equity)
    max_dd_pct = 0.0
    max_dd_usd = 0.0
    rp = float(risk_pct) / 100.0

    curve: list[dict[str, Any]] = [{"ts": int(g_ref["ts"].iloc[0]), "equity": round(eq, 2)}]
    q_r = [0.0, 0.0, 0.0, 0.0]
    by_sleeve: dict[str, dict[str, Any]] = {}
    by_year: dict[int, list[float]] = {}
    by_regime: dict[str, list[float]] = {}
    r_vals: list[float] = []
    fee_r_total = 0.0

    c_ref_a = g_ref["c"].to_numpy(dtype=float)
    end_ref_a = g_ref["end_ts"].to_numpy()
    dp_ref_a = g_ref["d_pct"].to_numpy(dtype=float)
    bi_col = g_ref["buy_imb"].to_numpy(dtype=int) if "buy_imb" in g_ref else np.zeros(len(g_ref), dtype=int)
    si_col = g_ref["sell_imb"].to_numpy(dtype=int) if "sell_imb" in g_ref else np.zeros(len(g_ref), dtype=int)
    imb_ref_a = bi_col - si_col
    vp_ref_a = g_ref["vpin_rank"].to_numpy(dtype=float)

    use_vof = profile in ("optimal_8s", "best_optimal", "institutional_8s_optimal", "institutional_4h_6y", "s01_s11_fp", "unified_9", "hardened_fp")
    for t in executed:
        eff_r = t["r"]
        if use_vof:
            idx_r = max(0, int(np.searchsorted(end_ref_a, t["ts_in"], side="right")) - 1)
            base_usd = eq * rp
            s_sign = 1 if t["side"] == "BUY" else -1
            imb_val = int(imb_ref_a[idx_r]) if (bi_col[idx_r] + si_col[idx_r]) > 0 else s_sign
            scaled_usd = vol_of_conviction_risk_usd(
                c_ref_a[max(0, idx_r - 245) : idx_r + 1],
                vof_tf,
                s_sign,
                float(dp_ref_a[idx_r]),
                imb_val,
                float(vp_ref_a[idx_r]),
                base_usd,
                base_usd * 0.65,
                base_usd * 1.25,
                target_ann_vol=0.55 if vof_tf == "H4" else 0.50,
                lookback=240,
            )
            scale = (scaled_usd / base_usd) if base_usd > 0 else 1.0
            eff_r = t["r"] * scale
            pnl = scaled_usd * t["r"]
        else:
            pnl = eq * rp * t["r"]
        eq += pnl
        pk = max(pk, eq)
        dd_usd = pk - eq
        dd_pct = (dd_usd / pk * 100.0) if pk > 0 else 0.0
        max_dd_usd = max(max_dd_usd, dd_usd)
        max_dd_pct = max(max_dd_pct, dd_pct)
        r_vals.append(eff_r)
        fee_r_total += t["fee_r"]
        q_r[t["q"]] += eff_r
        by_year.setdefault(t["year"], []).append(eff_r)
        by_regime.setdefault(t["regime"], []).append(eff_r)
        t["pnl_usd"] = round(pnl, 2)
        t["equity"] = round(eq, 2)
        t["r"] = round(eff_r, 3)
        curve.append({"ts": t["ts_out"], "equity": round(eq, 2)})
        sv = by_sleeve.setdefault(t["sleeve"], {"trades": 0, "wins": 0, "sum_r": 0.0, "pnl_usd": 0.0})
        sv["trades"] += 1
        if eff_r > 0:
            sv["wins"] += 1
        sv["sum_r"] += eff_r
        sv["pnl_usd"] += pnl

    span_sec = max(86400, int(g_ref["ts"].iloc[-1]) - int(g_ref["ts"].iloc[0]))
    years_span = max(0.25, span_sec / (365.25 * 86400.0))

    arr = np.array(r_vals) if r_vals else np.array([0.0])
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    full_wins = arr[arr >= 1.5]
    wr = float((len(wins) / len(arr) * 100.0) if r_vals else 0.0)
    gw = float(wins.sum()) if len(wins) else 0.0
    gl = float(abs(losses.sum())) if len(losses) else 0.0
    pf = (gw / gl) if gl > 0 else (99.99 if gw > 0 else 0.0)
    avg_rr = float(wins.mean() / abs(losses.mean())) if (len(wins) and len(losses)) else 0.0
    full_rr = float(full_wins.mean() / abs(losses.mean())) if (len(full_wins) and len(losses)) else avg_rr
    ret_pct = (eq / float(start_equity) - 1.0) * 100.0
    cagr_pct = ((eq / float(start_equity)) ** (1.0 / years_span) - 1.0) * 100.0 if eq > 0 else -100.0
    calmar = (cagr_pct / max_dd_pct) if max_dd_pct > 0 else 99.0
    sharpe = float(arr.mean() / (arr.std() + 1e-9) * math.sqrt(len(arr) / years_span)) if len(arr) > 1 else 0.0
    t_stat = float(arr.mean() / (arr.std() / math.sqrt(len(arr)) + 1e-9)) if len(arr) > 1 else 0.0
    p_val = 0.5 * math.erfc(t_stat / math.sqrt(2.0))

    downside = arr[arr < 0]
    down_std = float(np.sqrt(np.mean(downside ** 2))) if len(downside) else 1e-9
    sortino = float(arr.mean() / max(down_std, 1e-9) * math.sqrt(len(arr) / years_span)) if len(arr) > 1 else 0.0
    eq_vals = np.array([pt["equity"] for pt in curve], dtype=float)
    pk_vals = np.maximum.accumulate(eq_vals)
    dd_vals = np.where(pk_vals > 0, (pk_vals - eq_vals) / pk_vals * 100.0, 0.0)
    ulcer_index = float(np.sqrt(np.mean(dd_vals ** 2))) if len(dd_vals) else 0.0
    var95_r = float(np.percentile(arr, 5)) if len(arr) > 5 else 0.0
    cvar95_r = float(arr[arr <= var95_r].mean()) if len(arr) > 5 and np.any(arr <= var95_r) else var95_r
    skew_r = float(pd.Series(arr).skew()) if len(arr) > 3 else 0.0
    ex_kurt_r = float(pd.Series(arr).kurt()) if len(arr) > 3 else 0.0

    # Walk-Forward: IS (<= 2023) vs OOS (>= 2024) when multi-year, else H1/H2
    is_arr = np.array([t["r"] for t in executed if t["year"] <= 2023], dtype=float)
    oos_arr = np.array([t["r"] for t in executed if t["year"] >= 2024], dtype=float)
    h1_m, h2_m = arr[: len(arr) // 2], arr[len(arr) // 2 :]
    wf_h1_sr = float(h1_m.mean() / (h1_m.std() + 1e-9) * math.sqrt(len(h1_m) / (years_span * 0.5))) if len(h1_m) > 2 else 0.0
    wf_h2_sr = float(h2_m.mean() / (h2_m.std() + 1e-9) * math.sqrt(len(h2_m) / (years_span * 0.5))) if len(h2_m) > 2 else 0.0

    annual_oos = []
    for yr in sorted(by_year.keys()):
        ya = np.array(by_year[yr], dtype=float)
        yeq = 1.0
        ypk = 1.0
        ymdd = 0.0
        for rv in ya:
            yeq *= (1.0 + rv * rp)
            ypk = max(ypk, yeq)
            ymdd = max(ymdd, (ypk - yeq) / ypk * 100.0)
        annual_oos.append({
            "year": int(yr),
            "trades": int(len(ya)),
            "wr": round(float((ya > 0).mean() * 100.0), 1),
            "sum_r": round(float(ya.sum()), 2),
            "ret_pct": round(float((yeq - 1.0) * 100.0), 1),
            "max_dd_pct": round(float(ymdd), 1),
            "oos": bool(yr >= 2024),
        })

    regime_breakdown = {
        rg: {
            "trades": int(len(vals)),
            "wr": round(float((np.array(vals) > 0).mean() * 100.0), 1),
            "sum_r": round(float(sum(vals)), 2),
        }
        for rg, vals in sorted(by_regime.items())
    }

    wf_splits = {
        "is_21_23_r": round(float(is_arr.sum()), 2) if len(is_arr) else 0.0,
        "is_21_23_trades": int(len(is_arr)),
        "oos_24_26_r": round(float(oos_arr.sum()), 2) if len(oos_arr) else 0.0,
        "oos_24_26_trades": int(len(oos_arr)),
        "h1_sr": round(wf_h1_sr, 2),
        "h2_sr": round(wf_h2_sr, 2),
    }

    dsr_pct = _dsr_pct(arr, 40.0)
    dsr_k6_pct = _dsr_pct(arr, max(2.0, float(len(by_sleeve))))
    mc_20k = _run_20k_monte_carlo(arr, float(start_equity), float(risk_pct), years_span=years_span, n_mc=int(mc_runs))

    return {
        "symbol": sym,
        "profile": profile,
        "span_start": datetime.fromtimestamp(int(g_ref["ts"].iloc[0]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "span_end": datetime.fromtimestamp(int(g_ref["ts"].iloc[-1]), tz=timezone.utc).strftime("%Y-%m-%d"),
        "years_span": round(years_span, 2),
        "execution_mode": "1m_exact_realistic_dyn_atr",
        "reality_fixes": {
            "entry_delay_mins": int(entry_delay_mins),
            "realistic_costs": bool(realistic_costs),
            "use_real_funding": bool(use_real_funding and f_ts is not None and len(f_ts) > 0),
            "adaptive_tick": True,
            "dyn_atr": True,
            "max_same_side": int(max_same_side),
            "pruned_void_sleeves": True,
        },
        "metrics": {
            "start_equity": round(float(start_equity), 2),
            "end_equity": round(eq, 2),
            "total_return_pct": round(ret_pct, 1),
            "cagr_pct": round(cagr_pct, 1),
            "net_pnl_usd": round(eq - float(start_equity), 2),
            "total_trades": len(executed),
            "win_rate": round(wr, 1),
            "total_r": round(float(arr.sum()), 2),
            "total_fees_r": round(fee_r_total, 2),
            "profit_factor": round(pf, 2),
            "avg_win_loss_r": round(avg_rr, 2),
            "full_win_loss_r": round(full_rr, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "max_dd_usd": round(max_dd_usd, 2),
            "calmar_ratio": round(calmar, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "ulcer_index": round(ulcer_index, 2),
            "var95_r": round(var95_r, 2),
            "cvar95_r": round(cvar95_r, 2),
            "skew_r": round(skew_r, 2),
            "ex_kurt_r": round(ex_kurt_r, 2),
            "wf_h1_sr": round(wf_h1_sr, 2),
            "wf_h2_sr": round(wf_h2_sr, 2),
            "t_stat": round(t_stat, 2),
            "p_value": round(p_val, 4),
            "dsr_pct": dsr_pct,
            "dsr_k6_pct": dsr_k6_pct,
            "cost_bps": round(cost_bps, 1) if not realistic_costs else 16.5,
            "funding_bps_8h": round(float(funding_bps_8h), 2),
            "risk_pct": round(float(risk_pct), 2),
            "max_concurrent": int(max_concurrent),
            "max_same_side": int(max_same_side),
            "quarters_r": [round(x, 2) for x in q_r],
            "annual_oos": annual_oos,
            "wf_splits": wf_splits,
            "regime_breakdown": regime_breakdown,
            "dd_curve_pct": [round(float(x), 2) for x in dd_vals],
            "mc_20k": mc_20k,
        },
        "sleeves": [
            {
                "name": k,
                "trades": v["trades"],
                "win_rate": round(v["wins"] / v["trades"] * 100.0, 1) if v["trades"] else 0.0,
                "sum_r": round(v["sum_r"], 2),
                "pnl_usd": round(v["pnl_usd"], 2),
            }
            for k, v in by_sleeve.items()
        ],
        "equity_curve": curve,
        "recent_trades": executed[-60:],
    }
