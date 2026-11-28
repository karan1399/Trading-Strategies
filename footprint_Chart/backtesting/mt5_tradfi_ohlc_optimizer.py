"""MT5 TradFi/Metals/Indices/FX Multi-Year Per-Asset Optimizer (2021-2026, True Spreads & Swaps).

Pulls real multi-year H1 & H4 candles directly from Blueberry Markets MT5 (`2020-2026`),
evaluates 15 Pure OHLC + Fib-Prime Price-Action & Charts strategies individually on every asset
at LIVE MT5 bid-ask spreads + slippage + overnight swap strictly under Causal IS (<= 2023-12-31),
and saves optimized per-asset configs + H1-executed trade ledgers into `mt5_portfolio/` so
Stage 2 can optimize Crypto + Metals + Indices + FX together on ONE shared MT5 account!
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from Charts.core.strategy_base import discover_strategies as discover_chart_strategies
from footprint_Chart.backtesting.fast_kernels import sim_1m_trades_nb
from footprint_Chart.backtesting.multi_asset_optimizer import SPLIT_2024_TS
from footprint_Chart.backtesting.multi_asset_prop_optimizer import _score_is_oos, _sigs_to_arr
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from strategies.core.indicators import ema_arr
import scripts.delta_pa_trend_optimizer as dpa

MT5_DIR = DATA_DIR / "mt5_portfolio"
CFG_DIR = MT5_DIR / "configs"
TRADES_DIR = MT5_DIR / "trades"

TRADFI_ASSETS = [
    {"symbol": "MT5_XAUUSD", "mt5": "XAUUSD.pi", "cat": "METALS", "sw_l": -1.5, "sw_s": 0.3},
    {"symbol": "MT5_XAGUSD", "mt5": "XAGUSD.pi", "cat": "METALS", "sw_l": -1.5, "sw_s": 0.2},
    {"symbol": "MT5_COPPER", "mt5": "COPPER.p", "cat": "METALS", "sw_l": -2.0, "sw_s": 0.1},
    {"symbol": "MT5_USWTI", "mt5": "USWTI.p", "cat": "ENERGY", "sw_l": 0.5, "sw_s": -2.0},
    {"symbol": "MT5_NAS100", "mt5": "NAS100.p", "cat": "INDEX", "sw_l": -1.5, "sw_s": 0.2},
    {"symbol": "MT5_SP500", "mt5": "SP500.p", "cat": "INDEX", "sw_l": -1.5, "sw_s": 0.2},
    {"symbol": "MT5_EURUSD", "mt5": "EURUSD.pi", "cat": "FOREX", "sw_l": -0.8, "sw_s": 0.2},
    {"symbol": "MT5_USDJPY", "mt5": "USDJPY.pi", "cat": "FOREX", "sw_l": 1.0, "sw_s": -1.5},
]


def _rates_to_df(rates: np.ndarray, tf_sec: int) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df["timestamp"] = df["time"].astype(np.int64)
    df["ts"] = df["timestamp"]
    df["end_ts"] = df["ts"] + tf_sec
    vol_col = "real_volume" if ("real_volume" in df.columns and df["real_volume"].sum() > 0) else "tick_volume"
    df["volume"] = df[vol_col].astype(float)
    for k in ("open", "high", "low", "close"):
        df[k] = df[k].astype(float)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


def run_tradfi_mt5() -> None:
    if not mt5.initialize():
        raise RuntimeError("Failed to initialize MetaTrader5 terminal")

    CFG_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    chart_map = discover_chart_strategies()
    empty_f_ts = np.empty(0, dtype=np.int64)
    empty_f_rt = np.empty(0, dtype=np.float64)

    try:
        for spec in TRADFI_ASSETS:
            sym, ms, cat = spec["symbol"], spec["mt5"], spec["cat"]
            inf = mt5.symbol_info(ms)
            if inf is None or inf.bid <= 0:
                print(f"[SKIP] {ms} unavailable on MT5", flush=True)
                continue
            mid = 0.5 * (inf.bid + inf.ask)
            live_sp = round(((inf.ask - inf.bid) / mid) * 10000.0, 2)
            # Raw .pi commission buffer (~0.7bp RT on FX/Metals)
            eff_sp = round(live_sp + (0.8 if ms.endswith(".pi") else 0.0), 2)

            r1 = mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H1, 0, 50000)
            r4 = mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H4, 0, 20000)
            df1 = _rates_to_df(r1, 3600)
            df4 = _rates_to_df(r4, 14400)

            # Align window from max(2020-08-01, df1.start)
            t_min = max(int(df1["timestamp"].iloc[0]), int(pd.Timestamp("2020-08-01", tz="UTC").timestamp()))
            df1 = df1[df1["timestamp"] >= t_min].reset_index(drop=True)
            df4 = df4[df4["timestamp"] >= t_min].reset_index(drop=True)
            if len(df4) < 800 or len(df1) < 2000:
                print(f"[SKIP] {ms} short history H4={len(df4)}", flush=True)
                continue

            print(f"\n>>> [Stage 1B MT5 TradFi] Optimizing {sym} ({ms} | {cat} | Sp={live_sp:.2f}bp | H4={len(df4)} bars)...", flush=True)

            e1_ts = np.ascontiguousarray(df1["ts"].to_numpy(np.int64))
            e1_o = np.ascontiguousarray(df1["open"].to_numpy(np.float64))
            e1_h = np.ascontiguousarray(df1["high"].to_numpy(np.float64))
            e1_l = np.ascontiguousarray(df1["low"].to_numpy(np.float64))
            e1_c = np.ascontiguousarray(df1["close"].to_numpy(np.float64))

            trials: dict[str, list[dict[str, Any]]] = {}
            split_ts = SPLIT_2024_TS if int(df4["ts"].iloc[0]) < SPLIT_2024_TS - 365 * 86400 else int(df4["ts"].iloc[len(df4) // 2])

            for tf_l, df_u, tf_s, mh in (("4H", df4, 14400, 96), ("1H", df1, 3600, 144)):
                ts_a = np.ascontiguousarray(df_u["ts"].to_numpy(np.int64))
                end_a = np.ascontiguousarray(df_u["end_ts"].to_numpy(np.int64))
                c_a = np.ascontiguousarray(df_u["close"].to_numpy(np.float64))
                atr_a = np.ascontiguousarray(dpa._atr_wilder(df_u["high"].to_numpy(float), df_u["low"].to_numpy(float), c_a, 14))
                e89_a = np.ascontiguousarray(ema_arr(c_a, 89))
                e233_a = np.ascontiguousarray(ema_arr(c_a, 233))
                up233, dn233 = c_a > e233_a, c_a < e233_a

                # Build PA01..PA10 signals
                sigs_dict = dpa._build_pa_signals(df_u)
                # Also add Charts OHLC plugins
                cdf = pd.DataFrame({
                    "timestamp": df_u["timestamp"],
                    "datetime": pd.to_datetime(df_u["timestamp"], unit="s", utc=True),
                    "open": df_u["open"], "high": df_u["high"], "low": df_u["low"],
                    "close": df_u["close"], "volume": df_u["volume"],
                })
                c_grid = {
                    "bollinger_zscore": ("bb_zscore", [{"window": 20, "z_thresh": 2.0}]),
                    "donchian_breakout": ("donchian_cta", [{"lookback": 22}]),
                    "ema_pullback": ("ema_pullback", [{"fast_ema": 13, "slow_ema": 89, "trend_ema": 233, "rsi_buy_max": 52.0, "rsi_sell_min": 48.0}]),
                    "rsi2_mean_reversion": ("rsi2_meanrev", [{"oversold": 15.0, "overbought": 85.0}]),
                    "vwap_rejection": ("vwap_reclaim", [{"min_dist_atr": 0.3}]),
                }
                for c_sid, (cid, plist) in c_grid.items():
                    plug = chart_map[cid]
                    for prm in plist:
                        sa_raw = _sigs_to_arr(len(df_u), plug.generate_signals(cdf, prm))
                        sa_tr = np.where(((sa_raw == 1) & up233) | ((sa_raw == -1) & dn233), sa_raw, 0).astype(np.int8)
                        sigs_dict.setdefault(c_sid, []).append((f"ema233", sa_tr))

                for sid, variants in sigs_dict.items():
                    for var_s, sa in variants:
                        for sl_m, tr_m, be_m in ((2.0, 3.0, 1.25), (2.5, 3.5, 1.25), (3.0, 4.0, 1.5)):
                            res = sim_1m_trades_nb(
                                ts_a, end_a, atr_a, e89_a, e233_a, c_a,
                                np.ascontiguousarray(sa, dtype=np.int8),
                                e1_ts, e1_o, e1_h, e1_l, e1_c,
                                int(tf_s), float(sl_m), 0.0, float(tr_m), float(be_m), 0.05, False, int(mh), 0, True,
                                True, float(eff_sp), False, 0.0, 0.0, 2, float(spec["sw_l"]), float(spec["sw_s"]),
                                empty_f_ts, empty_f_rt,
                            )
                            sc = _score_is_oos(res[1], res[7], split_ts, min_is=10,
                                               exit_available_ts=res[2] + 3600)
                            trials.setdefault(sid, []).append({
                                "sid": sid, "tf": tf_l, "cfg": f"{var_s},sl={sl_m},tr={tr_m},be={be_m}",
                                "sl": sl_m, "tr": tr_m, "be": be_m, "res": res, **sc,
                            })

            # Causal IS plateau selection & clone pruning
            strat_cat: list[dict[str, Any]] = []
            qual_trs: dict[str, list[dict[str, Any]]] = {}
            for sid, tlist in sorted(trials.items()):
                ra = np.array([t["raw_score"] for t in tlist], dtype=float)
                for ix, t in enumerate(tlist):
                    om = (float(ra.sum() - ra[ix]) / (len(ra) - 1)) if len(ra) > 1 else ra[ix]
                    t["plateau"] = 0.65 * t["raw_score"] + 0.35 * max(-1.0, om)
                best = max(tlist, key=lambda x: x["plateau"])
                ok = (best["nis"] >= 10 and best["ispf"] >= 1.22 and best["isr"] > 4.0 and best["ist"] >= 0.85 and best["hmin"] > -2.5)
                bkt = "MEANREV" if any(x in sid for x in ("RSI2", "rsi2", "bollinger", "vwap", "Rejection")) else ("PULLBACK" if any(x in sid for x in ("EMA", "pullback", "Reclaim")) else "BREAKOUT")
                strat_cat.append({
                    "sid": sid, "bucket": bkt, "tf": best["tf"], "mode": "Pure OHLC", "cfg": best["cfg"],
                    "sl": best["sl"], "tr": best["tr"], "be": best["be"],
                    "status": "QUALIFIED" if ok else "NEGLECTED",
                    "nis": best["nis"], "isr": best["isr"], "ispf": best["ispf"], "ist": best["ist"], "hmin": best["hmin"],
                    "noos": best["noos"], "oosr": best["oosr"], "oospf": best["oospf"],
                    "n_all": best["n_all"], "r_all": best["r_all"], "pf_all": best["pf_all"],
                })
                if ok:
                    res = best["res"]
                    rows = []
                    for k in range(len(res[1])):
                        rows.append({
                            "symbol": sym, "mt5_symbol": ms, "category": cat, "sleeve": sid,
                            "bucket": bkt, "tf": best["tf"], "mode": "Pure OHLC", "cfg": best["cfg"],
                            "side": "BUY" if int(res[3][k]) == 1 else "SELL",
                            "ts_in": int(res[1][k]), "ts_out": int(res[2][k]),
                            "exit_available_ts": int(res[2][k]) + 3600, "r": float(res[7][k]),
                            "vof_mult": 1.0, "mt5_cost_bps": eff_sp,
                        })
                    qual_trs[sid] = rows

            # Clone prune strictly on IS (|corr| <= 0.72)
            qlist = sorted([r for r in strat_cat if r["status"] == "QUALIFIED"], key=lambda x: x["ist"] * x["ispf"], reverse=True)
            is_dm: dict[str, dict[int, float]] = {}
            days = set()
            for r in qlist:
                dm: dict[int, float] = {}
                for t in qual_trs[r["sid"]]:
                    if t["ts_in"] < split_ts:
                        dk = int(t["ts_out"] // 86400)
                        dm[dk] = dm.get(dk, 0.0) + float(t["r"])
                        days.add(dk)
                is_dm[r["sid"]] = dm
            picked: list[str] = []
            if len(days) >= 8 and len(qlist) >= 2:
                mat = pd.DataFrame({r["sid"]: [is_dm[r["sid"]].get(d, 0.0) for d in sorted(days)] for r in qlist}).fillna(0.0)
                cm = mat.corr()
                for r in qlist:
                    s = r["sid"]
                    if any(abs(float(cm.loc[s, p])) > 0.72 for p in picked):
                        r["status"] = "CLONE_PRUNED"
                        continue
                    picked.append(s)
            else:
                picked = [r["sid"] for r in qlist]

            kept = [t for s in picked for t in qual_trs[s]]
            if kept:
                pd.DataFrame(kept).to_parquet(TRADES_DIR / f"{sym}.parquet", index=False)
            (CFG_DIR / f"{sym}.json").write_text(json.dumps({
                "symbol": sym, "mt5_symbol": ms, "category": cat,
                "span_start": str(df4["timestamp"].iloc[0]), "live_sp_bps": live_sp, "mt5_cost_bps": eff_sp,
                "qualified_kept": len(picked), "selected_sids": picked, "kept_trades_count": len(kept),
                "strategy_scorecard": strat_cat,
            }, indent=2), encoding="utf-8")

            isr = sum(r["isr"] for r in qlist if r["sid"] in picked)
            oosr = sum(r["oosr"] for r in qlist if r["sid"] in picked)
            print(f"    -> {sym:12s} ({ms:10s}) | Kept={len(picked):2d}/{len(strat_cat)} | Trades={len(kept):4d} | SumIS_R={isr:+6.1f}R | SumOOS_R={oosr:+6.1f}R", flush=True)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    run_tradfi_mt5()
