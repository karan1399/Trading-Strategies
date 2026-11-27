"""OI Z-Score + 2D VWAP +-2sigma + High-Volume Pumping Liquidation Sweep Forensics.

Tests strictly causal multi-year (2021-2026) BTCUSDT 5m Binance Futures Open Interest
aligned to 2H/4H Footprint + 1m-exact trade simulation (`sim_1m_trades_nb`), comparing:
  - H0: 2D VWAP +-2sigma + HTF EMA233 baseline (no OI filter)
  - H1: + OI Liquidation Purge (`Z_dOI <= -1.5 / -2.0`)
  - H2: Contrast/Placebo: OI Expansion (`Z_dOI >= +1.5`, initiative buildup)
  - H3: + High Volume Pumping (`Z_vol >= 1.5 / 2.0` on liquidation cascade)
  - H4: + Footprint Absorption/Reclaim (`CLV` / Next-Bar Reclaim) + Fib-Prime EMA89/233
  - H5: Counter-Trend contrast (without HTF EMA233)
And dissects Stop-Loss (SL) failure mechanics + avoidance filters across IS -> Frozen OOS.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
from typing import Any
import urllib.request
import zipfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from footprint_Chart.backtesting.btc_all25_optimizer_v2 import _prep_arrs
from footprint_Chart.backtesting.fast_kernels import sim_1m_trades_nb
from footprint_Chart.backtesting.multi_asset_optimizer import SPLIT_2024_TS, compute_hac_stats
from footprint_Chart.backtesting.portfolio_engine import _load_funding_arrays, load_asset_tfs_cached
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR

METRICS_FULL_PATH = DATA_DIR / "BTCUSDT_metrics_5m_full.parquet"
REPORT_PATH = DATA_DIR / "oi_vwap_sweep_report.json"
PNG_PATH = Path(
    r"C:\Users\SIGMA\.gemini\antigravity\brain\4ed94c81-b1da-4868-8a7c-aec85386790d\oi_vwap_sweep_forensics.png"
)


def _fetch_day_metrics(sym: str, d_str: str) -> pd.DataFrame | None:
    url = f"https://data.binance.vision/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{d_str}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "QuantResearch/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            df = pd.read_csv(zf.open(zf.namelist()[0]))
        if df.empty:
            return None
        ts = (pd.to_datetime(df["create_time"], utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().astype(np.int64)
        return pd.DataFrame({
            "ts": ts.astype(np.int64),
            "oi_coin": pd.to_numeric(df["sum_open_interest"], errors="coerce"),
            "oi_usd": pd.to_numeric(df["sum_open_interest_value"], errors="coerce"),
            "top_ls": pd.to_numeric(df["sum_toptrader_long_short_ratio"], errors="coerce"),
            "global_ls": pd.to_numeric(df["count_long_short_ratio"], errors="coerce"),
            "taker_ls": pd.to_numeric(df["sum_taker_long_short_vol_ratio"], errors="coerce"),
        })
    except Exception:
        return None


def ensure_full_metrics(sym: str = "BTCUSDT", start_d: date = date(2021, 12, 1)) -> pd.DataFrame:
    out_p = DATA_DIR / f"{sym}_metrics_5m_full.parquet"
    if out_p.is_file() and out_p.stat().st_size > 100_000:
        df = pq.read_table(out_p).to_pandas()
        if len(df) > 20_000 and int(df["ts"].iloc[0]) >= 1_600_000_000:
            print(f"[metrics] Using cached {out_p.name} ({len(df):,} rows)", flush=True)
            return df

    end_d = datetime.now(timezone.utc).date() - timedelta(days=1)
    days = []
    cur = start_d
    while cur <= end_d:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    print(f"[metrics] Downloading {len(days)} daily Vision 5m OI zips for {sym}...", flush=True)
    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = {pool.submit(_fetch_day_metrics, sym, d_s): d_s for d_s in days}
        done = 0
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            if res is not None and not res.empty:
                frames.append(res)
            if done % 400 == 0 or done == len(days):
                print(f"[metrics] {sym} Progress {done}/{len(days)} (valid days={len(frames)})", flush=True)

    # Merge tail from live metrics if present
    tail_p = DATA_DIR / f"{sym}_metrics_5m.parquet"
    if tail_p.is_file():
        tdf = pq.read_table(tail_p).to_pandas()
        oi_col = "oi_btc" if "oi_btc" in tdf.columns else ("oi_coin" if "oi_coin" in tdf.columns else "sum_open_interest")
        if oi_col in tdf.columns:
            frames.append(pd.DataFrame({
                "ts": tdf["ts"].astype(np.int64),
                "oi_coin": pd.to_numeric(tdf[oi_col], errors="coerce"),
                "oi_usd": pd.to_numeric(tdf["oi_usd"], errors="coerce") if "oi_usd" in tdf.columns else np.nan,
                "top_ls": pd.to_numeric(tdf["top_ls"], errors="coerce") if "top_ls" in tdf.columns else np.nan,
                "global_ls": pd.to_numeric(tdf["global_ls"], errors="coerce") if "global_ls" in tdf.columns else np.nan,
                "taker_ls": pd.to_numeric(tdf["taker_ls"], errors="coerce") if "taker_ls" in tdf.columns else np.nan,
            }))

    if not frames:
        return pd.DataFrame(columns=["ts", "oi_coin", "oi_usd", "top_ls", "global_ls", "taker_ls"])
    full = pd.concat(frames, ignore_index=True).dropna(subset=["ts", "oi_coin"])
    full = full[full["ts"] >= 1_600_000_000].sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    pq.write_table(pa.Table.from_pandas(full, preserve_index=False), out_p, compression="zstd")
    t0 = datetime.fromtimestamp(int(full["ts"].iloc[0]), tz=timezone.utc).strftime("%Y-%m-%d")
    t1 = datetime.fromtimestamp(int(full["ts"].iloc[-1]), tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"[metrics] Saved {out_p.name}: {len(full):,} rows ({t0} -> {t1})", flush=True)
    return full


def attach_causal_oi_vwap_features(gf: pd.DataFrame, m_df: pd.DataFrame, bar_hours: int) -> pd.DataFrame:
    """Strictly causal as-of alignment (`oi_ts <= bar_end_ts`) + statistical OI/Vol/2D-VWAP Z-scores."""
    df = gf.copy().reset_index(drop=True)
    m = m_df.sort_values("ts").reset_index(drop=True)

    # Exact causal point-in-time alignment: last 5m OI sample at or before bar end_ts
    aligned = pd.merge_asof(
        df[["end_ts"]].rename(columns={"end_ts": "ts"}).sort_values("ts"),
        m,
        on="ts",
        direction="backward",
        tolerance=3600 * bar_hours,
    )
    for col in ("oi_coin", "oi_usd", "top_ls", "global_ls", "taker_ls"):
        df[col] = aligned[col].to_numpy(dtype=np.float64)

    # Also compute minimum/max OI excursion WITHIN the bar to catch intrabar liquidation drops that partially rebuilt!
    m_ts = m["ts"].to_numpy(dtype=np.int64)
    m_oi = m["oi_coin"].to_numpy(dtype=np.float64)
    b_ts = df["ts"].to_numpy(dtype=np.int64)
    b_end = df["end_ts"].to_numpy(dtype=np.int64)
    min_oi_in_bar = np.full(len(df), np.nan, dtype=np.float64)
    max_doi_drop_bar = np.full(len(df), np.nan, dtype=np.float64)
    starts = np.searchsorted(m_ts, b_ts, side="left")
    ends = np.searchsorted(m_ts, b_end, side="right")
    for k in range(len(df)):
        s_i, e_i = int(starts[k]), int(ends[k])
        if e_i > s_i and s_i > 0:
            prev_oi = m_oi[s_i - 1]
            if prev_oi > 0 and np.isfinite(prev_oi):
                seg = m_oi[s_i:e_i]
                mn = float(np.nanmin(seg))
                min_oi_in_bar[k] = mn
                max_doi_drop_bar[k] = (mn - prev_oi) / prev_oi * 100.0

    # Bar-to-bar OI % delta (coin-denominated, immune to spot-price valuation drop)
    oi = pd.Series(df["oi_coin"])
    doi = oi.pct_change(fill_method=None) * 100.0
    doi_2 = oi.pct_change(2, fill_method=None) * 100.0
    win = max(24, 96 // bar_hours)  # 4-day rolling baseline (48 bars on 2H, 24 on 4H)

    df["doi_pct"] = doi.to_numpy(dtype=np.float64)
    df["doi_2_pct"] = doi_2.to_numpy(dtype=np.float64)
    df["max_drop_bar_pct"] = max_doi_drop_bar
    df["z_doi"] = ((doi - doi.rolling(win, min_periods=20).mean()) / doi.rolling(win, min_periods=20).std().replace(0, np.nan)).to_numpy(dtype=np.float64)
    df["z_intrabar_drop"] = ((pd.Series(max_doi_drop_bar) - pd.Series(max_doi_drop_bar).rolling(win, min_periods=20).mean()) / pd.Series(max_doi_drop_bar).rolling(win, min_periods=20).std().replace(0, np.nan)).to_numpy(dtype=np.float64)

    # Modified MAD Z-score (fat-tail immune liquidation detector)
    med_doi = doi.rolling(win, min_periods=20).median()
    mad_doi = (doi - med_doi).abs().rolling(win, min_periods=20).median().replace(0, np.nan)
    df["mad_z_doi"] = (0.6745 * (doi - med_doi) / mad_doi).to_numpy(dtype=np.float64)

    # OI Level Z-score
    df["z_oi_lvl"] = ((oi - oi.rolling(win, min_periods=20).mean()) / oi.rolling(win, min_periods=20).std().replace(0, np.nan)).to_numpy(dtype=np.float64)

    # High Volume Pumping metrics (log-volume Z-score + ratio to 4D SMA)
    vol = pd.Series(np.maximum(df["v"].to_numpy(dtype=np.float64), 1.0))
    lvol = np.log(vol)
    df["z_vol"] = ((lvol - lvol.rolling(win, min_periods=20).mean()) / lvol.rolling(win, min_periods=20).std().replace(0, np.nan)).to_numpy(dtype=np.float64)
    df["vol_ratio"] = (vol / vol.rolling(win, min_periods=20).mean().replace(0, np.nan)).to_numpy(dtype=np.float64)

    # Exact 2-Day (48H) Rolling VWAP & Intrabar Low/High Bands (+-2.0 sigma)
    n_2d = max(6, 48 // bar_hours)  # 24 bars on 2H, 12 bars on 4H = exact 2D = 48H
    tp = (df["h"] + df["l"] + df["c"]) / 3.0
    pv = tp * vol
    vwap_2d = pv.rolling(n_2d, min_periods=n_2d).sum() / vol.rolling(n_2d, min_periods=n_2d).sum()
    sq_diff = ((tp - vwap_2d) ** 2 * vol).rolling(n_2d, min_periods=n_2d).sum() / vol.rolling(n_2d, min_periods=n_2d).sum()
    sd_2d = np.sqrt(np.maximum(sq_diff, 1e-12))
    df["vwap_2d"] = vwap_2d.to_numpy(dtype=np.float64)
    df["sd_2d"] = sd_2d.to_numpy(dtype=np.float64)
    df["vwz2d_close"] = ((df["c"] - vwap_2d) / sd_2d).to_numpy(dtype=np.float64)
    df["vwz2d_low"] = ((df["l"] - vwap_2d) / sd_2d).to_numpy(dtype=np.float64)
    df["vwz2d_high"] = ((df["h"] - vwap_2d) / sd_2d).to_numpy(dtype=np.float64)

    # Top-trader L/S delta
    tls = pd.Series(df["top_ls"])
    df["z_top_ls"] = ((tls - tls.rolling(win, min_periods=20).mean()) / tls.rolling(win, min_periods=20).std().replace(0, np.nan)).to_numpy(dtype=np.float64)
    return df


def _eval_variant(
    name: str,
    df_u: pd.DataFrame,
    arrs: tuple[Any, ...],
    m1_tup: tuple[Any, ...],
    ftup: tuple[Any, ...],
    tf_sec: int,
    side_arr: np.ndarray,
    sl: float,
    tp: float,
    tr: float,
    be: float,
    mh: int,
    oi_start_ts: int,
) -> tuple[dict[str, Any], pd.DataFrame]:
    m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c = m1_tup
    fts_c, frt_c = ftup
    # Mask signals strictly to OI-available epoch
    sa = np.where(df_u["ts"].to_numpy(dtype=np.int64) >= oi_start_ts, side_arr, 0).astype(np.int8)
    res = sim_1m_trades_nb(
        arrs[0], arrs[1], arrs[2], arrs[3], arrs[4], arrs[5], sa,
        m1_ts_c, m1_o_c, m1_h_c, m1_l_c, m1_c_c,
        int(tf_sec), float(sl), float(tp), float(tr), float(be), 0.05, False, int(mh), 1, bool(tr > 0),
        False, 2.65, True, 0.0, 0.0, 5, 0.0, 0.0, fts_c, frt_c,
    )
    o_bi, o_ti, o_to, o_sd, o_rsn, o_ent, o_exp, o_r, o_fr, _ = res
    rsn_map = ("TIME", "SL", "TRAIL", "TP")
    rows = []
    for k in range(len(o_bi)):
        bi = int(o_bi[k])
        dur_h = (int(o_to[k]) - int(o_ti[k])) / 3600.0
        rows.append({
            "bar_i": bi, "ts_in": int(o_ti[k]), "ts_out": int(o_to[k]),
            "dur_h": dur_h, "side": int(o_sd[k]), "reason": rsn_map[int(o_rsn[k])],
            "r": float(o_r[k]), "z_doi": float(df_u["z_doi"].iloc[bi]),
            "z_vol": float(df_u["z_vol"].iloc[bi]), "clv": float(df_u["clv"].iloc[bi]),
            "d_pct": float(df_u["d_pct"].iloc[bi]), "vpin": float(df_u["vpin_rank"].iloc[bi]),
            "vwz2d_c": float(df_u["vwz2d_close"].iloc[bi]),
            "range_atr": float((df_u["h"].iloc[bi] - df_u["l"].iloc[bi]) / max(df_u["atr"].iloc[bi], 1e-9)),
            "fund_bp": float(df_u["fund_bp"].iloc[bi]) if "fund_bp" in df_u else 0.0,
            "buy_imb": int(df_u["buy_imb"].iloc[bi]), "sell_imb": int(df_u["sell_imb"].iloc[bi]),
        })
    tdf = pd.DataFrame(rows)
    if len(tdf) == 0:
        return {"name": name, "n": 0, "r": 0.0, "pf": 0.0, "wr": 0.0, "sl_pct": 0.0, "is_pf": 0.0, "oos_n": 0, "oos_r": 0.0, "oos_pf": 0.0, "t_hac": 0.0}, tdf

    r_all = tdf["r"].to_numpy(dtype=float)
    is_m = tdf["ts_in"].to_numpy(dtype=np.int64) < SPLIT_2024_TS
    oos_m = ~is_m
    ris, roos = r_all[is_m], r_all[oos_m]
    pf_all = float(r_all[r_all > 0].sum() / max(abs(r_all[r_all <= 0].sum()), 1e-9))
    pf_is = float(ris[ris > 0].sum() / max(abs(ris[ris <= 0].sum()), 1e-9)) if len(ris) else 0.0
    pf_oos = float(roos[roos > 0].sum() / max(abs(roos[roos <= 0].sum()), 1e-9)) if len(roos) else 0.0
    t_hac = compute_hac_stats(r_all)["t_hac"] if len(r_all) >= 8 else 0.0
    sl_pct = float((tdf["reason"] == "SL").mean() * 100.0)
    summary = {
        "name": name, "n": len(tdf), "r": round(float(r_all.sum()), 2), "pf": round(pf_all, 2),
        "wr": round(float((r_all > 0).mean() * 100.0), 1), "sl_pct": round(sl_pct, 1),
        "is_n": int(is_m.sum()), "is_r": round(float(ris.sum()), 2), "is_pf": round(pf_is, 2),
        "oos_n": int(oos_m.sum()), "oos_r": round(float(roos.sum()), 2), "oos_pf": round(pf_oos, 2),
        "t_hac": round(float(t_hac), 2),
    }
    return summary, tdf


def run_oi_sweep_research() -> dict[str, Any]:
    m_df = ensure_full_metrics("BTCUSDT")
    # Use valid 2021+ timestamp floor (exclude bad rows if any)
    m_df = m_df[m_df["ts"] >= 1_600_000_000].reset_index(drop=True)
    oi_start_ts = int(m_df["ts"].iloc[0]) + 48 * 7200  # after 48-bar rolling warmup
    print(f"[metrics] Active OI range: {datetime.fromtimestamp(int(m_df['ts'].iloc[0]), timezone.utc):%Y-%m-%d} -> {datetime.fromtimestamp(int(m_df['ts'].iloc[-1]), timezone.utc):%Y-%m-%d} ({len(m_df):,} rows)", flush=True)

    g2_raw, g4_raw, m1_ts, m1_o, m1_h, m1_l, m1_c = load_asset_tfs_cached("BTCUSDT", DATA_DIR, adaptive_tick=True)
    m1_tup = (
        np.ascontiguousarray(m1_ts, dtype=np.int64),
        np.ascontiguousarray(m1_o, dtype=np.float64),
        np.ascontiguousarray(m1_h, dtype=np.float64),
        np.ascontiguousarray(m1_l, dtype=np.float64),
        np.ascontiguousarray(m1_c, dtype=np.float64),
    )
    f_ts, f_rt = _load_funding_arrays("BTCUSDT", DATA_DIR)
    ftup = (np.ascontiguousarray(f_ts, dtype=np.int64), np.ascontiguousarray(f_rt, dtype=np.float64))

    g2 = attach_causal_oi_vwap_features(g2_raw, m_df, bar_hours=2)
    g4 = attach_causal_oi_vwap_features(g4_raw, m_df, bar_hours=4)
    a2, a4 = _prep_arrs(g2), _prep_arrs(g4)

    # Print statistical properties of OI Z-score & Volume Pumping on 2H BTC
    sub2 = g2[g2["ts"] >= oi_start_ts].dropna(subset=["z_doi", "z_vol", "vwz2d_close"])
    liq_evt = sub2[(sub2["z_doi"] <= -1.5) & (sub2["z_vol"] >= 1.5)]
    print(f"\n[stats-2H] OI epoch rows={len(sub2):,} | Events(Z_dOI<=-1.5 & Z_vol>=1.5)={len(liq_evt)} ({len(liq_evt)/len(sub2)*100:.2f}%)")
    print(f"[stats-2H] Outside +-2D VWAP wick={((sub2['vwz2d_low']<=-2.0)|(sub2['vwz2d_high']>=2.0)).sum()} | Close={((sub2['vwz2d_close']<=-2.0)|(sub2['vwz2d_close']>=2.0)).sum()}")
    print(f"[stats-2H] dOI% p01={sub2['doi_pct'].quantile(0.01):.2f}% p05={sub2['doi_pct'].quantile(0.05):.2f}% med={sub2['doi_pct'].median():.3f}% p95={sub2['doi_pct'].quantile(0.95):.2f}% p99={sub2['doi_pct'].quantile(0.99):.2f}%")

    # Align 4H HTF Fib-Prime Trend (EMA 89/233) onto 2H bars strictly causally!
    htf_ref = pd.DataFrame({
        "end_ts": g4["end_ts"].to_numpy(dtype=np.int64),
        "htf_up233": (g4["c"] > g4["ema233"]).to_numpy(dtype=bool),
        "htf_dn233": (g4["c"] < g4["ema233"]).to_numpy(dtype=bool),
        "htf_stkL": ((g4["ema89"] > g4["ema233"]) & (g4["c"] > g4["ema233"])).to_numpy(dtype=bool),
        "htf_stkS": ((g4["ema89"] < g4["ema233"]) & (g4["c"] < g4["ema233"])).to_numpy(dtype=bool),
    }).sort_values("end_ts")
    m_htf = pd.merge_asof(g2[["end_ts"]].sort_values("end_ts"), htf_ref, on="end_ts", direction="backward")
    up233_htf = m_htf["htf_up233"].fillna(False).to_numpy(dtype=bool)
    dn233_htf = m_htf["htf_dn233"].fillna(False).to_numpy(dtype=bool)
    stkL_htf = m_htf["htf_stkL"].fillna(False).to_numpy(dtype=bool)
    stkS_htf = m_htf["htf_stkS"].fillna(False).to_numpy(dtype=bool)

    # Core Signal Components on 2H bars
    vw_low2 = g2["vwz2d_low"].to_numpy(dtype=float) <= -2.0
    vw_high2 = g2["vwz2d_high"].to_numpy(dtype=float) >= 2.0
    vw_c_low2 = g2["vwz2d_close"].to_numpy(dtype=float) <= -2.0
    vw_c_high2 = g2["vwz2d_close"].to_numpy(dtype=float) >= 2.0
    z_doi = g2["z_doi"].to_numpy(dtype=float)
    z_intra = g2["z_intrabar_drop"].to_numpy(dtype=float)
    z_vol = g2["z_vol"].to_numpy(dtype=float)
    clv = g2["clv"].to_numpy(dtype=float)
    dp = g2["d_pct"].to_numpy(dtype=float)
    vpin = g2["vpin_rank"].to_numpy(dtype=float)
    c2 = g2["c"].to_numpy(dtype=float)
    o2 = g2["o"].to_numpy(dtype=float)
    l2 = g2["l"].to_numpy(dtype=float)
    h2 = g2["h"].to_numpy(dtype=float)
    atr2 = g2["atr"].to_numpy(dtype=float)

    # Wait-for-reclaim confirmation: next bar closes > sweep bar close or reclaim inside -2D VWAP
    sw_wick_L = vw_low2 & ((z_doi <= -1.5) | (z_intra <= -1.8)) & (z_vol >= 1.5)
    sw_wick_S = vw_high2 & ((z_doi <= -1.5) | (z_intra <= -1.8)) & (z_vol >= 1.5)
    # Next-bar reclaim confirmation (bar t is reclaim after sweep bar t-1)
    swL_p = pd.Series(sw_wick_L).shift(1).fillna(False).to_numpy(dtype=bool)
    swS_p = pd.Series(sw_wick_S).shift(1).fillna(False).to_numpy(dtype=bool)
    reclaim_L = swL_p & (c2 > o2) & (c2 > pd.Series(c2).shift(1).to_numpy(dtype=float)) & (l2 >= pd.Series(l2).shift(1).to_numpy(dtype=float) - 0.5 * atr2)
    reclaim_S = swS_p & (c2 < o2) & (c2 < pd.Series(c2).shift(1).to_numpy(dtype=float)) & (h2 <= pd.Series(h2).shift(1).to_numpy(dtype=float) + 0.5 * atr2)

    # 4D VWAP (vwz48) Next-Bar Reclaim + OI Purge
    v48_L = (g2["vwz48"].to_numpy(dtype=float) <= -2.0) & (z_doi <= -1.5)
    v48_S = (g2["vwz48"].to_numpy(dtype=float) >= 2.0) & (z_doi <= -1.5)
    v48_Lp = pd.Series(v48_L).shift(1).fillna(False).to_numpy(dtype=bool) & (c2 > o2)
    v48_Sp = pd.Series(v48_S).shift(1).fillna(False).to_numpy(dtype=bool) & (c2 < o2)

    # Anti-cascade cooldown (skip 2nd flush within 6 bars / 12H)
    def _cooldown(mask: np.ndarray, cd: int = 6) -> np.ndarray:
        out = mask.copy()
        last = -999
        for idx in range(len(out)):
            if out[idx]:
                if idx - last <= cd:
                    out[idx] = False
                else:
                    last = idx
        return out

    experiments = [
        ("H0_Base_2DVWAP_Wick_HTF233",      np.where(vw_low2 & up233_htf, 1, np.where(vw_high2 & dn233_htf, -1, 0)), 2.5, 4.0, 0.0, 1.0, 48),
        ("H1_OI_Purge_z<=1.5_HTF233",       np.where(vw_low2 & (z_doi <= -1.5) & up233_htf, 1, np.where(vw_high2 & (z_doi <= -1.5) & dn233_htf, -1, 0)), 2.5, 4.0, 0.0, 1.0, 48),
        ("H2_Contrast_OI_Exp_z>=+1.5",      np.where(vw_low2 & (z_doi >= 1.5) & up233_htf, 1, np.where(vw_high2 & (z_doi >= 1.5) & dn233_htf, -1, 0)), 2.5, 4.0, 0.0, 1.0, 48),
        ("H3_OIPurge+VolPump_Naive",        np.where(vw_low2 & (z_doi <= -1.5) & (z_vol >= 1.5) & up233_htf, 1, np.where(vw_high2 & (z_doi <= -1.5) & (z_vol >= 1.5) & dn233_htf, -1, 0)), 2.5, 4.0, 0.0, 1.0, 48),
        # SL-Avoidance Ablation on 4D VWAP + 2D VWAP + Momentum Short-Squeeze:
        ("B2_4DVWAP_Immediate_NoWait",      np.where((g2["vwz48"] <= -2.0) & (z_doi <= -1.5) & (z_vol >= 1.5) & up233_htf, 1, np.where((g2["vwz48"] >= 2.0) & (z_doi <= -1.5) & (z_vol >= 1.5) & dn233_htf, -1, 0)), 3.0, 4.5, 0.0, 1.5, 72),
        ("B3_4DVWAP_Reclaim+3ATR+Trail",    np.where(v48_Lp & up233_htf, 1, np.where(v48_Sp & dn233_htf, -1, 0)), 3.0, 0.0, 4.0, 1.5, 96),
        ("A2_2H_ShortSqueezeFade_DN233",    np.where(_cooldown(reclaim_S & dn233_htf), -1, 0), 3.0, 0.0, 4.0, 1.5, 96),
        ("B1_2H_LiqMomentum_Break",         np.where(sw_wick_L & dn233_htf & (clv < -0.2), -1, np.where(sw_wick_S & up233_htf & (clv > 0.2), 1, 0)), 2.5, 0.0, 3.5, 1.0, 72),
        ("SLFix_NextBar+Cool+3ATR_2H",      np.where(_cooldown(reclaim_L & up233_htf), 1, np.where(_cooldown(reclaim_S & dn233_htf), -1, 0)), 3.0, 0.0, 4.0, 1.5, 96),
    ]

    summaries: list[dict[str, Any]] = []
    tdfs: dict[str, pd.DataFrame] = {}
    print("\n=== HYPOTHESIS MATRIX (2H BTCUSDT, 1m-exact, IS<=2023 | OOS=2024-2026) ===")
    for nm, sa, sl, tp, tr, be, mh in experiments:
        sm, tdf = _eval_variant(nm, g2, a2, m1_tup, ftup, 7200, sa, sl, tp, tr, be, mh, oi_start_ts)
        summaries.append(sm)
        tdfs[nm] = tdf
        print(f"{nm:30s} | n={sm['n']:3d} WR={sm['wr']:4.1f}% SL={sm['sl_pct']:4.1f}% NetR={sm['r']:+6.1f} PF={sm['pf']:4.2f} t={sm['t_hac']:+5.2f} | IS_PF={sm['is_pf']:4.2f} | OOS(n={sm['oos_n']:3d}, R={sm['oos_r']:+5.1f}, PF={sm['oos_pf']:4.2f})")

    # ---- Also run 4H HTF Sweep Analysis ----
    vw4_L = g4["vwz2d_low"].to_numpy(dtype=float) <= -2.0
    vw4_H = g4["vwz2d_high"].to_numpy(dtype=float) >= 2.0
    up4 = (g4["c"] > g4["ema233"]).to_numpy(dtype=bool)
    dn4 = (g4["c"] < g4["ema233"]).to_numpy(dtype=bool)
    zdoi4 = g4["z_doi"].to_numpy(dtype=float)
    zvol4 = g4["z_vol"].to_numpy(dtype=float)
    clv4 = g4["clv"].to_numpy(dtype=float)
    vpin4 = g4["vpin_rank"].to_numpy(dtype=float)

    print("\n=== 4H TIMEFRAME CONFIRMATION ===")
    ex4 = [
        ("4H_H0_Base_2DVWAP_HTF233",      np.where(vw4_L & up4, 1, np.where(vw4_H & dn4, -1, 0)), 2.5, 4.0, 0.0, 1.0, 72),
        ("4H_C1_ShortSqzMom_UP233->L",    np.where(vw4_H & (zdoi4 <= -1.5) & (zvol4 >= 1.5) & up4, 1, 0), 2.5, 0.0, 3.5, 1.0, 72),
        ("4H_C4_ShortSqzFade_DN233->S",   np.where(pd.Series(vw4_H & (zdoi4 <= -1.5)).shift(1).fillna(False).to_numpy() & (g4["c"] < g4["o"]).to_numpy() & dn4, -1, 0), 3.0, 0.0, 4.0, 1.5, 96),
    ]
    for nm, sa, sl, tp, tr, be, mh in ex4:
        sm, tdf = _eval_variant(nm, g4, a4, m1_tup, ftup, 14400, sa, sl, tp, tr, be, mh, oi_start_ts)
        summaries.append(sm)
        tdfs[nm] = tdf
        print(f"{nm:30s} | n={sm['n']:3d} WR={sm['wr']:4.1f}% SL={sm['sl_pct']:4.1f}% NetR={sm['r']:+6.1f} PF={sm['pf']:4.2f} t={sm['t_hac']:+5.2f} | IS_PF={sm['is_pf']:4.2f} | OOS(n={sm['oos_n']:3d}, R={sm['oos_r']:+5.1f}, PF={sm['oos_pf']:4.2f})")

    # ---- Strict SL Forensics on H3_OIPurge+VolPump_Naive ----
    base_df = tdfs.get("H3_OIPurge+VolPump_Naive", pd.DataFrame())
    forensics: dict[str, Any] = {}
    if len(base_df) > 10:
        sl_df = base_df[base_df["reason"] == "SL"]
        win_df = base_df[base_df["r"] > 0]
        # Knife-catch: CLV against direction
        kn_sl = float(((sl_df["side"] == 1) & (sl_df["clv"] < -0.25) | (sl_df["side"] == -1) & (sl_df["clv"] > 0.25)).mean() * 100.0) if len(sl_df) else 0.0
        kn_win = float(((win_df["side"] == 1) & (win_df["clv"] < -0.25) | (win_df["side"] == -1) & (win_df["clv"] > 0.25)).mean() * 100.0) if len(win_df) else 0.0
        fast_sl = float((sl_df["dur_h"] <= 4.0).mean() * 100.0) if len(sl_df) else 0.0
        vpin_hi_sl = float((sl_df["vpin"] >= 0.85).mean() * 100.0) if len(sl_df) else 0.0
        vpin_hi_win = float((win_df["vpin"] >= 0.85).mean() * 100.0) if len(win_df) else 0.0
        forensics = {
            "total_trades": len(base_df),
            "sl_count": len(sl_df),
            "win_count": len(win_df),
            "sl_median_dur_h": round(float(sl_df["dur_h"].median()), 1) if len(sl_df) else 0.0,
            "sl_within_4h_pct": round(fast_sl, 1),
            "sl_knife_catch_clv_pct": round(kn_sl, 1),
            "win_knife_catch_clv_pct": round(kn_win, 1),
            "sl_high_vpin85_pct": round(vpin_hi_sl, 1),
            "win_high_vpin85_pct": round(vpin_hi_win, 1),
            "sl_mean_range_atr": round(float(sl_df["range_atr"].mean()), 2) if len(sl_df) else 0.0,
            "win_mean_range_atr": round(float(win_df["range_atr"].mean()), 2) if len(win_df) else 0.0,
        }
        print("\n=== SL FAILURE-MODE FORENSICS (H3 Baseline) ===")
        for k, v in forensics.items():
            print(f"  {k:26s}: {v}")

    # Render 4-panel chart
    plt.style.use("dark_background")
    fig, axes = plt.subplots(2, 2, figsize=(15, 10), facecolor="#0d1117")
    fig.suptitle("BTCUSDT 5Y OI Z-Score + 2D VWAP ±2σ + High-Volume Pumping Forensics (2021–2026)", fontsize=13.5, fontweight="bold", color="#f0f6fc")

    # 1) Bar comparison of PF (Full vs OOS)
    ax1 = axes[0, 0]
    plot_s = [s for s in summaries if s["n"] >= 8]
    y = np.arange(len(plot_s))
    ax1.barh(y - 0.18, [s["pf"] for s in plot_s], height=0.35, color="#58a6ff", label="Full PF")
    ax1.barh(y + 0.18, [s["oos_pf"] for s in plot_s], height=0.35, color="#3fb950", label="Frozen OOS PF")
    ax1.axvline(1.0, color="#f85149", linestyle="--", linewidth=1.0)
    ax1.set_yticks(y)
    ax1.set_yticklabels([f"{s['name'][:22]} (n={s['n']})" for s in plot_s], fontsize=7.5)
    ax1.set_title("1. Hypothesis Matrix Profit Factor (Full vs Frozen OOS)", fontsize=10)
    ax1.legend(loc="lower right", fontsize=8)

    # 2) Equity curves: Naive Knife-Catch vs Winning OI-Sweep Variants
    ax2 = axes[0, 1]
    for nm, col in [
        ("B2_4DVWAP_Immediate_NoWait", "#f85149"),
        ("H3_OIPurge+VolPump_Naive", "#d29922"),
        ("B3_4DVWAP_Reclaim+3ATR+Trail", "#58a6ff"),
        ("B1_2H_LiqMomentum_Break", "#a371f7"),
        ("4H_C1_ShortSqzMom_UP233->L", "#3fb950"),
    ]:
        df_p = tdfs.get(nm)
        if df_p is not None and len(df_p) > 0:
            ax2.plot(np.arange(len(df_p)), df_p["r"].cumsum(), label=f"{nm[:22]} ({df_p['r'].sum():+.1f}R)", color=col, linewidth=1.8)
    ax2.axhline(0.0, color="#30363d", linewidth=1.0)
    ax2.axvline(0.0, color="#30363d", linewidth=0.8)
    ax2.set_title("2. Cumulative R-Curve: Baseline vs OI Expansion vs OI Purge+VolPump vs SL-Fixed", fontsize=10)
    ax2.set_xlabel("Chronological Trade #")
    ax2.set_ylabel("Cumulative R")
    ax2.legend(loc="upper left", fontsize=7.5)

    # 3) SL Hit Rate % Reduction across variants
    ax3 = axes[1, 0]
    sl_vals = [s["sl_pct"] for s in plot_s]
    cols3 = ["#3fb950" if v <= 42.0 else ("#d29922" if v <= 52.0 else "#f85149") for v in sl_vals]
    ax3.barh(y, sl_vals, color=cols3, height=0.65)
    ax3.set_yticks(y)
    ax3.set_yticklabels([s["name"][:24] for s in plot_s], fontsize=7.5)
    ax3.set_title("3. Stop-Loss Hit Rate % (Lower = Fewer Stopped-Out Sweeps)", fontsize=10)
    ax3.set_xlabel("Trades Closed at SL (%)")

    # 4) Net R Contribution & Feature Separation on SL vs Wins
    ax4 = axes[1, 1]
    if forensics:
        cats = ["Within <=4h\nAftershock", "Knife-Catch\nCLV<-0.25", "Toxic VPIN\n>=0.85"]
        sl_f = [forensics["sl_within_4h_pct"], forensics["sl_knife_catch_clv_pct"], forensics["sl_high_vpin85_pct"]]
        wn_f = [0.0, forensics["win_knife_catch_clv_pct"], forensics["win_high_vpin85_pct"]]
        x = np.arange(len(cats))
        ax4.bar(x - 0.18, sl_f, width=0.35, color="#f85149", label="Stopped-Out Trades (%)")
        ax4.bar(x + 0.18, wn_f, width=0.35, color="#3fb950", label="Winning Trades (%)")
        ax4.set_xticks(x)
        ax4.set_xticklabels(cats, fontsize=8.5)
        ax4.set_title("4. SL Failure Forensics: Why Liquidation Sweeps Get Stopped Out", fontsize=10)
        ax4.legend(loc="upper right", fontsize=8)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(PNG_PATH), dpi=140)
    plt.close(fig)

    out = {"oi_span_start": int(m_df["ts"].iloc[0]), "oi_rows": len(m_df), "experiments": summaries, "sl_forensics": forensics, "chart": str(PNG_PATH)}
    REPORT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    run_oi_sweep_research()
