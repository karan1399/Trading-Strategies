"""Sovereign $2,500 Hybrid Multi-Strategy Causal Portfolio Replay & Ablation Engine.

Implements Research-First validation for the approved Sovereign $2,500 Hybrid plan:
  - Fixed Risk Budget: 0.25% risk per trade ($6.25 at $2,501.80, scaling with equity)
    and 1.00% ($25.00, 4 orthogonal cluster slots) maximum open book heat.
  - Compares cleanly under live Blueberry MT5 #5126976 SymbolSpecs & weekday spreads:
      (A) M4 Desk Control (config_m4_v3b_desk.json)
      (B) M4 Pruned @ 0.25% (excl. SP500.p 1H)
      (C) Sovereign Hybrid Unfiltered @ 0.25% (M4 Pruned + IS-screened Crypto/GAU/CL + TradFi H4)
      (D) Sovereign Hybrid + Corr-ERC + S08 Vol-Target @ 0.25%
      (E) Sovereign Hybrid + 7d-Purged WF CatBoost_Huber_ER ML Gate @ 0.25%
      (F) Sovereign Hybrid + 7d-Purged WF MetaEnsemble_ER ML Gate @ 0.25%
  - Controls against overfitting & peeking:
      * Pre-2024 strict IS selection (<= 2023-12-31), frozen OOS (2024-2026) & 2025-2026 stress.
      * Strictly 7-day-purged expanding walk-forward ML predictions (zero final_bundle leakage).
      * 1.0x / 1.5x / 2.0x weekday spread stress, Deflated Sharpe Ratio (DSR),
        Benjamini-Hochberg FDR q-values, Newey-West t_HAC, and 20,000-path Crash MC.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import sys
from typing import Any
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desk.quant_audits import m4_live_parity_replay as m4_rep
from desk.quant_audits import strategies_parallel_replay as par_rep
from desk.quant_audits import r048_verify as rv
from engine import m4_live_engine as m4
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk, spec_from_mt5
from exchange_feed.backtesting.fast_kernels import sim_ohlc_trades_nb
from exchange_feed.backtesting.ml_meta_engine import (
    FINAL_BUNDLE_PATH,
    PURGE_SEC,
    SEQ_LEN,
    _compute_decay_weights,
    _extract_fa_row_and_seq,
    _fit_er_ensemble,
    _precompute_tf_feature_arrays,
    _predict_pytorch_seq_er_model,
    build_btc_macro_reference,
    build_ml_dataset,
    resolve_ml_sleeve_label,
)
from exchange_feed.backtesting.mt5_shared_account_portfolio_optimizer import prop_mc_20k
from exchange_feed.backtesting.multi_asset_optimizer import compute_hac_stats, evaluate_symbol_all_sleeves
from exchange_feed.backtesting.portfolio_engine import _dsr_pct, load_asset_tfs_cached
from exchange_feed.data_pipeline.binance_dumper import DATA_DIR
from exchange_feed.data_pipeline.mt5_universe import load_mt5_contract_specs
from strategies.core.fp_features import build_fp_context
from strategies.s08_vol_target_overlay.overlay import vol_of_conviction_risk_usd, vol_target_risk_usd


TS_2021 = int(pd.Timestamp("2021-01-01", tz="UTC").timestamp())
TS_2024 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
TS_2025 = int(pd.Timestamp("2025-01-23", tz="UTC").timestamp())
DEFAULT_OUT = Path(__file__).resolve().with_name("sovereign_hybrid_results.json")
DASHBOARD_HTML = Path(
    r"C:\Users\SIGMA\.gemini\antigravity\brain\4ed94c81-b1da-4868-8a7c-aec85386790d\sovereign_hybrid_dashboard.html"
)

# Strictly excluded stubbed OI recipes & poisoned/cost-blocked assets per R048 + 0.25% live audit
EXCLUDED_OI_PREFIXES = ("s12_oi_", "s13_oi_", "s14_oi_")
MT5_TO_BINANCE_FP = {
    "ETHUSD.pi": "ETHUSDT",
    "BNBUSD.p": "BNBUSDT",
    "SOLUSD.p": "SOLUSDT",
    "DSHUSD.p": "DASHUSDT",
    "UNIUSD.p": "UNIUSDT",
    "AVEUSD.p": "AAVEUSDT",
    "AVXUSD.p": "AVAXUSDT",
    "YFIUSD.p": "YFIUSDT",
    "VETUSD.p": "VETUSDT",
    "GAUUSD.pi": "XAUUSDT",
    "USWTI.p": "CLUSDT",
}
BINANCE_FP_TO_MT5 = {v: k for k, v in MT5_TO_BINANCE_FP.items()}

TRADFI_SOVEREIGN_TARGETS = [
    ("GAUUSD.pi", "GOLD_GRAM", "s03_htf_ema_rejection", 2.0, 2.5, 0.0, 1.0, 96),
    ("GAUUSD.pi", "GOLD_GRAM", "s09_donchian_cta", 1.5, 3.0, 0.0, 1.0, 96),
    ("US2000.p", "INDEX", "PA03_EMA233_Rejection", 1.5, 3.0, 0.0, 1.0, 96),
]

TRADFI_WITHHELD_TARGETS = [
    ("UKBRENT.p", "ENERGY", "PA03_EMA233_Rejection", 1.2, 4.0, 0.0, 1.0, 96),
    ("USWTI.p", "ENERGY", "PA03_EMA233_Rejection", 1.5, 3.0, 0.0, 1.0, 96),
]

SOVEREIGN_TRADFI_EXTRA = [
    "GAUUSD.pi",
    "GAUCNH.pi",
    "JP225.p",
    "UKBRENT.p",
    "USWTI.p",
    "US2000.p",
    "AU200.p",
    "USDJPY.pi",
    "EURUSD.pi",
]


def _cluster_for_mt5(ms: str, cat: str) -> str:
    m_up = ms.upper()
    c_up = cat.upper()
    if m_up.startswith("GAU") or c_up in ("METAL", "METALS"):
        return "GOLD_GRAM" if m_up.startswith("GAU") else "METAL"
    if c_up in ("ENERGY", "COMMODITY") or any(k in m_up for k in ("WTI", "BRENT")):
        return "ENERGY"
    if c_up in ("CRYPTO", "INDEX", "FOREX"):
        return c_up
    return c_up or "OTHER"


def benjamini_hochberg_q(p_values: list[float]) -> list[float]:
    """Compute Benjamini-Hochberg FDR q-values preserving input order."""
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(max(0.0, min(1.0, float(p))) for p in p_values), key=lambda x: x[1])
    raw_q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        orig_i, p_val = indexed[rank - 1]
        q_val = min(prev, p_val * m / rank)
        raw_q[orig_i] = round(float(q_val), 4)
        prev = q_val
    return raw_q


def fetch_mt5_sovereign_market() -> dict[str, Any]:
    """Fetch live SymbolSpecs, median weekday H1 spreads, and H1/H4 OHLC histories from Blueberry MT5."""
    import MetaTrader5 as mt5

    all_ms = sorted(set(par_rep.ASSET_TO_MT5.values()) | set(SOVEREIGN_TRADFI_EXTRA) | {"SP500.p", "XAUUSD.pi"})
    specs: dict[str, SymbolSpec] = {}
    weekday_sp_px: dict[str, float] = {}
    weekday_sp_bps: dict[str, float] = {}
    live_sp_bps: dict[str, float] = {}
    gau_xau_ratio = 1.0 / 31.1035
    h4_bars: dict[str, pd.DataFrame] = {}

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        xau_i = mt5.symbol_info("XAUUSD.pi")
        gau_i = mt5.symbol_info("GAUUSD.pi")
        if xau_i and gau_i and xau_i.bid > 0 and gau_i.bid > 0:
            gau_xau_ratio = float(gau_i.bid) / float(xau_i.bid)

        for ms in all_ms:
            mt5.symbol_select(ms, True)
            sp = spec_from_mt5(ms)
            inf = mt5.symbol_info(ms)
            if sp is None or inf is None:
                continue
            specs[ms] = sp
            mid = 0.5 * (float(inf.bid) + float(inf.ask))
            if mid > 0:
                live_sp_bps[ms] = round(((float(inf.ask) - float(inf.bid)) / mid) * 10000.0, 2)

            r1_med = mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H1, 0, 200)
            if r1_med is not None and len(r1_med) > 20:
                sp_pts = float(np.median([x["spread"] for x in r1_med[:-1]]))
                med_px = sp_pts * sp.point
                ref_mid = float(np.median([x["close"] for x in r1_med[:-1]]))
                comm_bp = 0.8 if ms.endswith(".pi") else 0.0
                weekday_sp_px[ms] = float(med_px + (comm_bp / 10000.0) * ref_mid)
                weekday_sp_bps[ms] = round((med_px / max(ref_mid, 1e-12)) * 10000.0 + comm_bp, 2)
            else:
                weekday_sp_px[ms] = float(inf.spread) * sp.point
                weekday_sp_bps[ms] = live_sp_bps.get(ms, 15.0)

        # Fetch full multi-year closed H4 bars for TradFi additions (GAU bridged via 0.01 * XAU pre-2025-01-23)
        for ms in ("JP225.p", "UKBRENT.p", "USWTI.p", "US2000.p", "AU200.p", "USDJPY.pi", "EURUSD.pi"):
            r4 = mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H4, 0, 20000)
            if r4 is not None and len(r4) >= 400:
                h4_bars[ms] = m4.M4LiveEngine._rates_to_df(None, r4, 14400)

        gau_r4 = m4.M4LiveEngine._rates_to_df(None, mt5.copy_rates_from_pos("GAUUSD.pi", mt5.TIMEFRAME_H4, 0, 20000), 14400)
        xau_r4 = m4.M4LiveEngine._rates_to_df(None, mt5.copy_rates_from_pos("XAUUSD.pi", mt5.TIMEFRAME_H4, 0, 20000), 14400)
        gau_start = int(gau_r4["ts"].iloc[0])
        pre_xau = xau_r4[xau_r4["ts"] < gau_start].copy()
        for col in ("open", "high", "low", "close"):
            pre_xau[col] = pre_xau[col] * 0.01
        h4_bars["GAUUSD.pi"] = pd.concat([pre_xau, gau_r4], ignore_index=True).sort_values("ts").reset_index(drop=True)
    finally:
        mt5.shutdown()

    return {
        "specs": specs,
        "weekday_sp_px": weekday_sp_px,
        "weekday_sp_bps": weekday_sp_bps,
        "live_sp_bps": live_sp_bps,
        "gau_xau_ratio": gau_xau_ratio,
        "h4_bars": h4_bars,
    }


def _extract_m4_pruned_solo_trades(
    desk_cfg: dict[str, Any],
    sigs: dict[str, m4_rep.SignalSeries],
    tapes: dict[str, m4_rep.PriceTape],
    specs: dict[str, SymbolSpec],
    spreads: dict[str, float],
) -> list[dict[str, Any]]:
    """Replay each enabled non-SP500 M4 pair uncapped to harvest true causal parity trades."""
    out: list[dict[str, Any]] = []
    for p in desk_cfg["pairs"]:
        if not p.get("enabled", True) or p["mt5_symbol"] == "SP500.p":
            continue
        pid = p["pair_id"]
        if pid not in sigs:
            continue
        solo_cfg = {
            "account": desk_cfg["account"],
            "governor": {
                **desk_cfg["governor"],
                "risk_pct": 0.25,
                "max_risk_single_trade_pct": 0.25,
                "max_total_open_risk_pct": 1.0,
                "max_global": 1,
                "max_per_asset": 1,
                "max_crypto_same_side": 1,
                "max_crypto_total": 1,
            },
            "pairs": [{**p, "pair_weight": 1.0, "max_lot_risk_usd": 100.0}],
        }
        res = m4_rep.simulate_m4_live_parity(
            pid, solo_cfg, {pid: sigs[pid]}, tapes, specs, spreads,
            start_equity=2501.80, min_ts=TS_2021, override_max_lot_cap=100.0, mc_runs=0,
        )
        ss = sigs[pid]
        idx_map = {int(ss.end_ts[i]): i for i in range(len(ss.end_ts))}
        ms = p["mt5_symbol"]
        cl = _cluster_for_mt5(ms, p["category"])
        for t in res["trades"]:
            bi = idx_map.get(int(t["ts_in"]), -1)
            sl_dist = abs(float(t["entry"]) - float(t["sl_init"]))
            sp_bps = (float(spreads.get(ms, 0.0)) / max(float(t["entry"]), 1e-9)) * 10000.0
            out.append({
                "source": "M4_PRUNED",
                "symbol": str(p["source_symbol"]),
                "mt5_symbol": ms,
                "category": str(p["category"]),
                "cluster": cl,
                "sleeve": f"m4:{p['sid']}",
                "raw_sid": str(p["sid"]),
                "cfg": str(p.get("cfg", "")),
                "tf": str(p["tf"]),
                "tf_k": 2 if str(p["tf"]).upper() == "2H" else 4,
                "side": str(t["side"]),
                "ts_in": int(t["ts_in"]),
                "ts_out": int(t["ts_out"]),
                "exit_available_ts": int(t["ts_out"]) + (3600 if p["feed_type"] == "mt5_ohlc" else 60),
                "entry": float(t["entry"]),
                "r": float(t["r_realized"]),
                "sl_dist": sl_dist,
                "vof_mult": float(t.get("vof_mult", 1.0)),
                "mt5_cost_bps": round(sp_bps, 2),
                "bar_i": bi,
                "fp_sym": str(p["source_symbol"]) if p["feed_type"] == "binance_footprint" else None,
                "weight": float(p.get("pair_weight", 1.0)),
                "entry_id": f"M4:{pid}:{t['ts_in']}:{t['side']}",
            })
    return out


def _extract_tradfi_sovereign_trades(mkt: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate causal H4 active strategies on feasible MT5 TradFi symbols under strict <= 2023 IS gate."""
    chart_map = m4.discover_chart_strategies()
    h4_bars = mkt["h4_bars"]
    specs = mkt["specs"]
    sp_bps_map = mkt["weekday_sp_bps"]
    out_trades: list[dict[str, Any]] = []
    withheld_trades: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []

    all_targets = [(row, False) for row in TRADFI_SOVEREIGN_TARGETS] + [(row, True) for row in TRADFI_WITHHELD_TARGETS]
    for (ms, cluster, sid, sl_m, tp_r, tr_m, be_r, mh), is_withheld in all_targets:
        df4 = h4_bars.get(ms)
        sp = specs.get(ms)
        if df4 is None or sp is None:
            continue
        tsigs = m4.eval_tradfi_signals(df4, chart_map)
        sig = tsigs.get(sid)
        if sig is None:
            continue
        o_a = df4["open"].to_numpy(float)
        h_a = df4["high"].to_numpy(float)
        l_a = df4["low"].to_numpy(float)
        c_a = sig["c"]
        ts_a = sig["ts"]
        end_a = sig["end_ts"]
        atr_a = sig["atr"]
        side_a = sig["side"]
        rd_a = sig.get("rd", np.zeros(len(c_a), dtype=float))
        sp_bp = float(sp_bps_map.get(ms, 5.0))

        _, ti, to, sdv, rv, _ = sim_ohlc_trades_nb(
            ts_a, o_a, h_a, l_a, c_a, atr_a, side_a, rd_a,
            float(sl_m), float(tp_r), float(tr_m), float(be_r), 0.05, int(mh),
            sp_bp, -1.0, -0.5, 5, 1,
        )
        ti_to_end = {int(ts_a[k]): int(end_a[k]) for k in range(len(ts_a))}
        ti_to_idx = {int(ts_a[k]): k for k in range(len(ts_a))}

        mis = ti < TS_2024
        moos = ti >= TS_2024
        ris, roos = rv[mis], rv[moos]
        pfis = float(ris[ris > 0].sum() / abs(ris[ris <= 0].sum())) if (ris <= 0).any() and abs(ris[ris <= 0].sum()) > 0 else (9.9 if (ris > 0).any() else 0.0)
        pfoos = float(roos[roos > 0].sum() / abs(roos[roos <= 0].sum())) if (roos <= 0).any() and abs(roos[roos <= 0].sum()) > 0 else (9.9 if (roos > 0).any() else 0.0)
        admitted = int(mis.sum()) >= 12 and float(ris.sum()) > 0.0 and pfis >= 1.15
        audit_rows.append({
            "source": "TRADFI_H4",
            "mt5_symbol": ms,
            "cluster": cluster,
            "sid": sid,
            "is_n": int(mis.sum()),
            "is_r": round(float(ris.sum()), 2),
            "is_pf": round(pfis, 2),
            "oos_n": int(moos.sum()),
            "oos_r": round(float(roos.sum()), 2),
            "oos_pf": round(pfoos, 2),
            "admitted_pre2024": admitted,
            "disposition": "WITHHELD_ENERGY_CHOP" if is_withheld else ("PROMOTED_CORE" if admitted else "REJECTED_IS"),
        })
        if not admitted:
            continue

        target_list = withheld_trades if is_withheld else out_trades
        # Precompute S08 vol target multiplier strictly on closed bars 0..bar_i
        for k in range(len(ti)):
            t_in_open = int(ti[k])
            t_out_close = int(to[k])
            if t_in_open < TS_2021:
                continue
            b_idx = ti_to_idx.get(t_in_open, -1)
            sig_idx = max(0, b_idx - 1) if b_idx > 0 else 0
            sig_end = int(end_a[sig_idx])
            entry_px = float(o_a[b_idx]) if b_idx >= 0 else float(c_a[sig_idx])
            atr_i = float(atr_a[sig_idx])
            rd_val = float(rd_a[sig_idx]) if float(rd_a[sig_idx]) > 0 else float(sl_m) * atr_i
            v_mult = float(vol_target_risk_usd(c_a[: sig_idx + 1], "H4", 1.0, 0.75, 1.25, target_ann_vol=0.22, lookback=20))
            target_list.append({
                "source": "TRADFI_H4",
                "symbol": f"MT5_{ms}",
                "mt5_symbol": ms,
                "category": "METALS" if cluster == "GOLD_GRAM" else cluster,
                "cluster": cluster,
                "sleeve": f"tradfi:{sid}",
                "raw_sid": sid,
                "cfg": f"sl={sl_m},tp={tp_r}",
                "tf": "4H",
                "tf_k": 4,
                "side": "BUY" if int(sdv[k]) == 1 else "SELL",
                "ts_in": sig_end,
                "ts_out": t_out_close,
                "exit_available_ts": t_out_close + 14400,
                "entry": entry_px,
                "r": float(rv[k]),
                "sl_dist": rd_val,
                "vof_mult": v_mult,
                "mt5_cost_bps": sp_bp,
                "bar_i": sig_idx,
                "fp_sym": None,
                "weight": min(1.25, max(0.75, pfis / 1.4)),
                "entry_id": f"TF:{ms}:{sid}:{sig_end}:{int(sdv[k])}",
            })
    return out_trades, withheld_trades, audit_rows


def _build_purged_wf_ml_predictions(trades: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    """Compute strictly 7d-purged expanding walk-forward (CatBoost, MetaEnsemble) E[R] predictions."""
    warnings.filterwarnings("ignore", message="X does not have valid feature names")
    specs_fp = load_mt5_contract_specs(DATA_DIR)
    fp_syms_needed = sorted({t["fp_sym"] for t in trades if t.get("fp_sym")})
    train_syms = ["BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT", "BCHUSDT", "ZECUSDT"]
    avail_train = [s for s in train_syms if (DATA_DIR / f"{s}_agg_footprint.parquet").is_file()]
    per_symbol = {s: evaluate_symbol_all_sleeves(s, DATA_DIR, "mt5", specs_fp) for s in avail_train}

    x_df, x_seq, meta_df, feat_names = build_ml_dataset(per_symbol)
    x_np = x_df.to_numpy(dtype=np.float64)
    r_raw = meta_df["r"].to_numpy(dtype=np.float64)
    y_er = np.clip(r_raw, -1.5, 4.0)
    ts_out = meta_df["ts_out"].to_numpy(dtype=np.int64)
    slv_to_id = {s: idx for idx, s in enumerate(sorted(meta_df["sleeve"].unique()))}

    # Precompute lightweight causal 4H/2H bar arrays from disk caches for every requested footprint asset
    btc_ref = build_btc_macro_reference(per_symbol["BTCUSDT"]["g4"]) if "BTCUSDT" in per_symbol else None
    fa_map: dict[tuple[str, int], dict[str, np.ndarray]] = {}
    for s in fp_syms_needed:
        if s in per_symbol:
            g4 = per_symbol[s]["g4"]
        else:
            g4_p = DATA_DIR / "cache" / f"{s}_4h.parquet"
            g4 = pd.read_parquet(g4_p) if g4_p.is_file() else None
        if g4 is not None and not g4.empty:
            fa_map[(s, 4)] = _precompute_tf_feature_arrays(g4, build_fp_context(g4), btc_ref)
        g2_p = DATA_DIR / "cache" / f"{s}_2h.parquet"
        if g2_p.is_file():
            g2 = pd.read_parquet(g2_p)
            if not g2.empty:
                fa_map[(s, 2)] = _precompute_tf_feature_arrays(g2, build_fp_context(g2), btc_ref)

    preds_by_id: dict[str, tuple[float, float]] = {}
    fold_bounds = [
        (2023, int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()), TS_2024),
        (2024, TS_2024, int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())),
        (2025, int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()), int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())),
        (2026, int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())),
    ]

    for yr, f_start, f_end in fold_bounds:
        tr_mask = ts_out < (f_start - PURGE_SEC)
        if int(tr_mask.sum()) < 150:
            continue
        sw = _compute_decay_weights(f_start, ts_out[tr_mask], half_life_years=1.5, decay_floor=0.20)
        fitted = _fit_er_ensemble(x_np[tr_mask], x_seq[tr_mask], y_er[tr_mask], sample_weight=sw, seed=2026 + yr)

        fold_trades = [t for t in trades if t.get("fp_sym") and f_start <= int(t["ts_in"]) < f_end]
        if not fold_trades:
            continue
        x_tabs, x_seqs, f_eids = [], [], []
        for t in fold_trades:
            sym = str(t["fp_sym"])
            tf_k = int(t.get("tf_k", 4))
            fa = fa_map.get((sym, tf_k)) or fa_map.get((sym, 4))
            if fa is None:
                continue
            bi = int(t.get("bar_i", -1))
            if bi < SEQ_LEN or bi >= len(fa["c"]):
                bi = int(np.searchsorted(fa["end_ts"], int(t["ts_in"]), side="right")) - 1
            side_f = 1.0 if t["side"] == "BUY" else -1.0
            is_met = 1.0 if t["mt5_symbol"].startswith(("GAU", "XAU", "XAG")) else 0.0
            slv_lbl = resolve_ml_sleeve_label(str(t["raw_sid"]), tf_k=tf_k, cfg_str=str(t.get("cfg", "")), slv_to_id=slv_to_id)
            ext = _extract_fa_row_and_seq(fa, bi, side_f, slv_lbl, tf_k, float(t["mt5_cost_bps"]), is_met, slv_to_id)
            if ext is None:
                continue
            row, seq_m = ext
            x_tabs.append([float(row.get(k, 0.0)) for k in feat_names])
            x_seqs.append(seq_m)
            f_eids.append(str(t["entry_id"]))
        if not x_tabs:
            continue
        xt_a = np.asarray(x_tabs, dtype=np.float64)
        xs_a = np.asarray(x_seqs, dtype=np.float32)
        cb_p = fitted["CatBoost_Huber_ER"].predict(xt_a)
        ens_p = (
            0.10 * fitted["HuberLinear_ER"].predict(xt_a)
            + 0.25 * fitted["LightGBM_Huber_ER"].predict(xt_a)
            + 0.25 * fitted["XGBoost_Huber_ER"].predict(xt_a)
            + 0.30 * cb_p
            + 0.10 * _predict_pytorch_seq_er_model(fitted["pt_model_live"], fitted["pt_imp"], fitted["pt_scl"], xt_a, xs_a)
        )
        for idx_p, eid in enumerate(f_eids):
            preds_by_id[eid] = (float(cb_p[idx_p]), float(ens_p[idx_p]))

    return preds_by_id


def simulate_sovereign_portfolio(
    name: str,
    trades_pool: list[dict[str, Any]],
    specs: dict[str, SymbolSpec],
    sp_px_map: dict[str, float],
    sp_bps_map: dict[str, float],
    gau_xau_ratio: float,
    *,
    start_equity: float = 2501.80,
    risk_pct: float = 0.25,
    max_open_risk_pct: float = 1.00,
    max_global: int = 4,
    max_per_asset: int = 1,
    cluster_caps: dict[str, int] | None = None,
    use_s08: bool = False,
    use_corr_erc: bool = False,
    ml_mode: str = "none",
    ml_min_er: float = 0.0,
    ml_preds: dict[str, tuple[float, float]] | None = None,
    spread_mult: float = 1.0,
    min_ts: int | None = None,
    max_ts: int | None = None,
    mc_runs: int = 20000,
    k_trials_dsr: int = 94,
) -> dict[str, Any]:
    """Chronological point-in-time portfolio simulator enforcing exact 0.25% MT5 lot & cluster bounds."""
    tl = [
        t for t in trades_pool
        if (min_ts is None or int(t["ts_in"]) >= min_ts) and (max_ts is None or int(t["ts_in"]) < max_ts)
    ]
    tl.sort(key=lambda t: (int(t["ts_in"]), -float(t.get("weight", 1.0)), str(t["entry_id"])))
    if not tl:
        return {"name": name, "metrics": {"trades": 0, "end_equity": start_equity, "cagr_pct": 0.0, "max_dd_pct": 0.0, "calmar": 0.0, "pf": 0.0}, "curve": [], "by_cluster": [], "by_sleeve": []}

    cl_caps = cluster_caps or {"CRYPTO": 1, "GOLD_GRAM": 1, "ENERGY": 1, "INDEX": 1, "FOREX": 1}
    eq = float(start_equity)
    pk = float(start_equity)
    dd_vals: list[float] = [0.0]
    max_dd_pct = 0.0
    throttled = False
    active: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    daily_r_map: dict[int, float] = {}
    daily_cl_r: dict[str, dict[int, float]] = {}
    curve: list[tuple[int, float]] = [(int(tl[0]["ts_in"]), round(eq, 2))]
    rej: dict[str, int] = {}

    for t in tl:
        tin = int(t["ts_in"])
        tout = int(t["exit_available_ts"])
        ms = str(t["mt5_symbol"])
        cl = str(t["cluster"])
        side = str(t["side"])

        closing = sorted([a for a in active if int(a["ts_out"]) <= tin], key=lambda a: int(a["ts_out"]))
        for a in closing:
            eq += a["pnl_usd"]
            pk = max(pk, eq)
            dd = ((pk - eq) / pk * 100.0) if pk > 0 else 0.0
            max_dd_pct = max(max_dd_pct, dd)
            dd_vals.append(dd)
            curve.append((int(a["ts_out"]), round(eq, 2)))
        active = [a for a in active if int(a["ts_out"]) > tin]

        if len(active) >= max_global:
            rej["MAX_GLOBAL"] = rej.get("MAX_GLOBAL", 0) + 1
            continue
        if sum(1 for a in active if a["mt5_symbol"] == ms) >= max_per_asset:
            rej["MAX_ASSET"] = rej.get("MAX_ASSET", 0) + 1
            continue
        if cl in cl_caps and sum(1 for a in active if a["cluster"] == cl) >= int(cl_caps[cl]):
            rej["CLUSTER_CAP"] = rej.get("CLUSTER_CAP", 0) + 1
            continue
        if cl == "CRYPTO" and sum(1 for a in active if a["cluster"] == "CRYPTO" and a["side"] == side) >= 1:
            rej["CRYPTO_SIDE"] = rej.get("CRYPTO_SIDE", 0) + 1
            continue

        spec = specs.get(ms)
        sl_dist = float(t.get("sl_dist", 0.0) or 0.0)
        if spec is None or sl_dist <= 0:
            rej["BAD_SPEC"] = rej.get("BAD_SPEC", 0) + 1
            continue
        eff_sl = sl_dist * gau_xau_ratio if t["symbol"] == "XAUUSDT" else sl_dist

        # Enforced <= 20% spread-to-SL filter under active spread_mult stress
        entry_px = float(t.get("entry", 0.0) or 0.0)
        sp_px_eff = float(sp_px_map.get(ms, 0.0)) * float(spread_mult)
        if sp_px_eff <= 0 and entry_px > 0:
            sp_px_eff = (float(sp_bps_map.get(ms, 10.0)) / 10000.0) * entry_px * float(spread_mult)
        if sp_px_eff > 0.20 * eff_sl:
            rej["SPREAD_GT_20PCT"] = rej.get("SPREAD_GT_20PCT", 0) + 1
            continue

        # Causal ML gate strictly on Footprint-domain assets (TradFi keeps ml_mult = 1.0)
        ml_mult = 1.0
        if ml_mode != "none" and t.get("fp_sym") and ml_preds is not None:
            pred = ml_preds.get(str(t["entry_id"]))
            if pred is not None:
                er_val = pred[0] if ml_mode == "catboost" else pred[1]
                if er_val < float(ml_min_er):
                    rej["ML_VETO"] = rej.get("ML_VETO", 0) + 1
                    continue
                ml_mult = float(np.clip(1.0 + er_val * 1.5, 0.50, 1.35))

        cur_dd = ((pk - eq) / pk * 100.0) if pk > 0 else 0.0
        if cur_dd >= 6.5:
            th_mult = 0.25
            throttled = True
        elif cur_dd >= 4.5 or (throttled and cur_dd >= 2.25):
            th_mult = 0.50
            throttled = True
        else:
            th_mult = 1.0
            throttled = False

        s8_mult = float(t.get("vof_mult", 1.0)) if use_s08 else 1.0

        # Empirical trailing 60-day cross-cluster correlation ERC weight
        corr_mult = 1.0
        if use_corr_erc and active:
            cur_day = int(tin // 86400)
            hist_days = list(range(cur_day - 60, cur_day))
            cl_vec_new = np.array([daily_cl_r.get(cl, {}).get(d, 0.0) for d in hist_days], dtype=float)
            rho_sum = 0.0
            for a in active:
                c_a = a["cluster"]
                if c_a == cl:
                    rho_sum += 0.55
                    continue
                v_open = np.array([daily_cl_r.get(c_a, {}).get(d, 0.0) for d in hist_days], dtype=float)
                if np.std(cl_vec_new) > 1e-9 and np.std(v_open) > 1e-9:
                    r_corr = float(np.corrcoef(cl_vec_new, v_open)[0, 1])
                    if np.isfinite(r_corr):
                        rho_sum += max(0.0, r_corr)
            corr_mult = float(np.clip(1.0 / (1.0 + rho_sum), 0.50, 1.00))

        base_cap_usd = eq * (float(risk_pct) / 100.0)
        planned_usd = base_cap_usd * th_mult * s8_mult * corr_mult * ml_mult
        max_single_cap = base_cap_usd * 1.25 if (use_s08 or ml_mode != "none") else base_cap_usd
        lots, _ = lots_for_risk(spec, eff_sl, planned_usd, max_single_cap)
        if lots is None:
            rej["RISK_REJECT"] = rej.get("RISK_REJECT", 0) + 1
            continue

        actual_risk = eff_sl * float(lots) * spec.value_per_price_unit()
        committed_heat = sum(a["risk_usd"] for a in active)
        if committed_heat + actual_risk > eq * (float(max_open_risk_pct) / 100.0) + 1e-9:
            rej["HEAT_CAP"] = rej.get("HEAT_CAP", 0) + 1
            continue

        # Apply exact extra cost stress delta relative to 1.0x saved basis
        saved_bp = float(t.get("mt5_cost_bps", 10.0) or 10.0)
        live_bp = float(sp_bps_map.get(ms, saved_bp)) * float(spread_mult)
        eff_r_trade = float(t["r"])
        if live_bp > saved_bp and entry_px > 0 and sl_dist > 0:
            eff_r_trade -= ((live_bp - saved_bp) / 10000.0) * entry_px / sl_dist

        pnl_usd = actual_risk * eff_r_trade
        acct_r = pnl_usd / max(1e-9, eq * (float(risk_pct) / 100.0))
        active.append({"ts_out": tout, "mt5_symbol": ms, "cluster": cl, "side": side, "pnl_usd": pnl_usd, "risk_usd": actual_risk})
        executed.append({**t, "pnl_usd": round(pnl_usd, 2), "r_eff": round(eff_r_trade, 3), "acct_r": round(acct_r, 3), "risk_usd": round(actual_risk, 2)})

        dk = int(tout // 86400)
        daily_r_map[dk] = daily_r_map.get(dk, 0.0) + acct_r
        daily_cl_r.setdefault(cl, {})[dk] = daily_cl_r.get(cl, {}).get(dk, 0.0) + acct_r

    for a in sorted(active, key=lambda x: int(x["ts_out"])):
        eq += a["pnl_usd"]
        pk = max(pk, eq)
        dd = ((pk - eq) / pk * 100.0) if pk > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)
        dd_vals.append(dd)
        curve.append((int(a["ts_out"]), round(eq, 2)))

    if not executed:
        return {"name": name, "metrics": {"trades": 0, "end_equity": round(eq, 2), "cagr_pct": 0.0, "max_dd_pct": 0.0, "calmar": 0.0, "pf": 0.0}, "curve": curve, "rejects": rej, "by_cluster": [], "by_sleeve": []}

    r_acct_arr = np.array([t["acct_r"] for t in executed], dtype=float)
    pnl_arr = np.array([t["pnl_usd"] for t in executed], dtype=float)
    gains = float(pnl_arr[pnl_arr > 0].sum())
    losses = float(abs(pnl_arr[pnl_arr <= 0].sum()))
    pf = (gains / losses) if losses > 0 else 9.99
    wr = float((pnl_arr > 0).mean() * 100.0)
    span_s = max(86400.0, float(curve[-1][0] - curve[0][0]))
    yrs = max(0.25, span_s / (365.25 * 86400.0))
    cagr = (((eq / start_equity) ** (1.0 / yrs) - 1.0) * 100.0) if eq > 0 else -100.0
    calmar = (cagr / max_dd_pct) if max_dd_pct > 0 else 0.0
    ulcer = float(math.sqrt(np.mean(np.square(dd_vals)))) if dd_vals else 0.0
    hac = compute_hac_stats(r_acct_arr)
    dsr_pct = _dsr_pct(r_acct_arr, max(2, k_trials_dsr))

    d_min, d_max = min(daily_r_map), max(daily_r_map)
    d_vals = np.array([daily_r_map.get(d, 0.0) for d in range(d_min, d_max + 1)], dtype=float)
    sharpe = float(d_vals.mean() / (d_vals.std() + 1e-9) * math.sqrt(365.0)) if len(d_vals) > 10 else 0.0
    down_d = d_vals[d_vals < 0]
    sortino = float(d_vals.mean() / (down_d.std() + 1e-9) * math.sqrt(365.0)) if len(down_d) > 3 else sharpe
    mc_20k = prop_mc_20k(d_vals, float(risk_pct), n_mc=mc_runs) if mc_runs > 0 else {}

    by_cl_d: dict[str, dict[str, Any]] = {}
    by_slv_d: dict[str, dict[str, Any]] = {}
    slv_rs: dict[str, list[float]] = {}
    for t in executed:
        c_row = by_cl_d.setdefault(t["cluster"], {"cluster": t["cluster"], "trades": 0, "wins": 0, "pnl_usd": 0.0, "sum_r": 0.0})
        c_row["trades"] += 1
        c_row["wins"] += 1 if t["pnl_usd"] > 0 else 0
        c_row["pnl_usd"] += t["pnl_usd"]
        c_row["sum_r"] += t["acct_r"]

        s_key = f"{t['mt5_symbol']}:{t['sleeve']}"
        s_row = by_slv_d.setdefault(s_key, {"sleeve": s_key, "cluster": t["cluster"], "trades": 0, "wins": 0, "pnl_usd": 0.0, "sum_r": 0.0})
        s_row["trades"] += 1
        s_row["wins"] += 1 if t["pnl_usd"] > 0 else 0
        s_row["pnl_usd"] += t["pnl_usd"]
        s_row["sum_r"] += t["acct_r"]
        slv_rs.setdefault(s_key, []).append(t["acct_r"])

    slv_list = sorted(by_slv_d.values(), key=lambda x: x["pnl_usd"], reverse=True)
    pvals = [compute_hac_stats(np.array(slv_rs[x["sleeve"]], dtype=float))["p_hac"] for x in slv_list]
    qvals = benjamini_hochberg_q(pvals)
    for row, qv in zip(slv_list, qvals):
        row["pnl_usd"] = round(row["pnl_usd"], 2)
        row["sum_r"] = round(row["sum_r"], 2)
        row["wr"] = round(100.0 * row["wins"] / row["trades"], 1)
        row["bh_fdr_q"] = qv
    for row in by_cl_d.values():
        row["pnl_usd"] = round(row["pnl_usd"], 2)
        row["sum_r"] = round(row["sum_r"], 2)
        row["wr"] = round(100.0 * row["wins"] / row["trades"], 1)

    return {
        "name": name,
        "metrics": {
            "start_equity": round(start_equity, 2),
            "end_equity": round(eq, 2),
            "net_pnl_usd": round(eq - start_equity, 2),
            "ret_pct": round((eq / start_equity - 1.0) * 100.0, 2),
            "cagr_pct": round(cagr, 2),
            "trades": len(executed),
            "wr": round(wr, 1),
            "pf": round(pf, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "calmar": round(calmar, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "ulcer": round(ulcer, 2),
            "t_hac": hac["t_hac"],
            "p_hac": hac["p_hac"],
            "dsr_pct": dsr_pct,
            "net_r_acct": round(float(r_acct_arr.sum()), 2),
            "mc_20k": mc_20k,
        },
        "curve": curve,
        "rejects": rej,
        "by_cluster": sorted(by_cl_d.values(), key=lambda x: x["pnl_usd"], reverse=True),
        "by_sleeve": slv_list,
    }


def _render_comparison_dashboard(results: dict[str, Any], out_html: Path) -> None:
    """Render clean standalone interactive HTML dashboard comparing M4 vs Parallel-94 vs Hybrid."""
    oos = results["oos_2024_2026"]
    series = []
    palette = ["#94a3b8", "#38bdf8", "#a78bfa", "#34d399", "#fbbf24", "#f472b6"]
    for idx, (k, obj) in enumerate(oos.items()):
        pts = [{"t": datetime.fromtimestamp(c[0], tz=timezone.utc).strftime("%Y-%m-%d"), "y": c[1]} for c in obj.get("curve", [])]
        series.append({"name": obj["name"], "color": palette[idx % len(palette)], "pts": pts, "metrics": obj["metrics"]})

    rows_html = []
    for k, obj in oos.items():
        m = obj["metrics"]
        mc = m.get("mc_20k") or {}
        rows_html.append(
            f"<tr><td>{html.escape(obj['name'])}</td><td>${m.get('end_equity',0):,.2f}</td>"
            f"<td>{m.get('cagr_pct',0):+.2f}%</td><td>{m.get('max_dd_pct',0):.2f}%</td>"
            f"<td>{m.get('pf',0):.2f}</td><td>{m.get('calmar',0):.2f}</td>"
            f"<td>{m.get('t_hac',0):.2f}</td><td>{m.get('dsr_pct',0):.1f}%</td>"
            f"<td>{mc.get('crash_dd95',0):.1f}%</td><td>{m.get('trades',0)}</td></tr>"
        )

    cfg_r = results.get("config") or {}
    r_pct = float(cfg_r.get("risk_pct", 0.25))
    h_pct = float(cfg_r.get("max_book_heat_pct", 1.0))
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Sovereign $2,500 Hybrid — {r_pct:.2f}% Risk / {h_pct:.2f}% Heat</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:24px}}table{{border-collapse:collapse;width:100%;margin-top:16px;font-size:13px}}th,td{{border:1px solid #334155;padding:8px 10px;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#1e293b}}canvas{{width:100%;height:380px;background:#1e293b;border-radius:8px;margin-top:12px}}</style>
</head><body>
<h2>Sovereign $2,500 Multi-Strategy Portfolio — Untouched OOS (2024–2026) @ {r_pct:.2f}% Risk / {h_pct:.2f}% Heat</h2>
<canvas id="cv" width="1200" height="380"></canvas>
<table><thead><tr><th>Scenario</th><th>End Eq</th><th>CAGR</th><th>Max DD</th><th>PF</th><th>Calmar</th><th>t_HAC</th><th>DSR</th><th>Crash DD95</th><th>Trades</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<script>
const S = {json.dumps(series)};
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
let minY=1e9, maxY=0, minT=Infinity, maxT=0;
for (const s of S) for (const p of s.pts) {{ const t=Date.parse(p.t); minY=Math.min(minY,p.y); maxY=Math.max(maxY,p.y); minT=Math.min(minT,t); maxT=Math.max(maxT,t); }}
const pad=48, W=cv.width-2*pad, H=cv.height-2*pad;
ctx.strokeStyle='#334155'; ctx.strokeRect(pad,pad,W,H);
S.forEach((s,idx)=>{{ if(!s.pts.length)return; ctx.strokeStyle=s.color; ctx.lineWidth=2; ctx.beginPath();
s.pts.forEach((p,i)=>{{ const x=pad+(Date.parse(p.t)-minT)/Math.max(1,maxT-minT)*W, y=pad+(maxY-p.y)/Math.max(1,maxY-minY)*H; i?ctx.lineTo(x,y):ctx.moveTo(x,y); }});
ctx.stroke(); ctx.fillStyle=s.color; ctx.fillText(s.name, pad+8, pad+16+idx*16); }});
</script></body></html>"""
    out_html.write_text(doc, encoding="utf-8")


def run_sovereign_hybrid_audit(
    *,
    start_equity: float = 2501.80,
    risk_pct: float = 0.25,
    max_book_heat_pct: float = 1.00,
    spread_mults: tuple[float, ...] = (1.0, 1.5, 2.0),
    mc_runs: int = 20000,
    out_path: Path = DEFAULT_OUT,
) -> dict[str, Any]:
    desk_cfg = json.loads((ROOT / "engine" / "config_m4_v3b_desk.json").read_text(encoding="utf-8"))
    mkt = fetch_mt5_sovereign_market()
    specs: dict[str, SymbolSpec] = mkt["specs"]
    sp_px = mkt["weekday_sp_px"]
    sp_bps = mkt["weekday_sp_bps"]
    gau_xau_ratio = float(mkt["gau_xau_ratio"])

    # 1. Exact M4 parity baselines (A: Desk Control, B: M4 Pruned @ 0.25%)
    sigs, tapes, m4_specs, m4_sp = m4_rep.load_live_market_inputs(desk_cfg, extend_xau_pre2025=True)
    for k, v in m4_specs.items():
        specs.setdefault(k, v)
    pruned_cfg = json.loads(json.dumps(desk_cfg))
    pruned_cfg["governor"].update({
        "risk_pct": risk_pct,
        "max_risk_single_trade_pct": risk_pct,
        "max_total_open_risk_pct": max_book_heat_pct,
        "max_global": 4,
    })
    for p in pruned_cfg["pairs"]:
        p["max_lot_risk_usd"] = round(start_equity * risk_pct / 100.0, 2)
        if p["mt5_symbol"] == "SP500.p":
            p["enabled"] = False

    # 2. Construct Unified Hybrid Trade Pool from strictly causal sources (pre-2024 screened + M4 pruned + TradFi H4)
    m4_solo = _extract_m4_pruned_solo_trades(desk_cfg, sigs, tapes, specs, m4_sp)
    tf_trades, tf_withheld, tf_audit = _extract_tradfi_sovereign_trades(mkt)

    pools_par, recipe_meta = par_rep.build_parallel_trade_pools()
    s08_g4_cache: dict[str, pd.DataFrame] = {}
    hyb_alt_trades: list[dict[str, Any]] = []
    seen_m4_keys = {(t["mt5_symbol"], t["side"], int(t["ts_in"]) // 14400) for t in m4_solo}

    for row in pools_par["screened62"]:
        folder = str(row["folder"])
        if folder.startswith(EXCLUDED_OI_PREFIXES):
            continue
        ms = str(row["mt5_symbol"])
        if ms in ("BTCUSD.pi", "ZECUSD.p", "XAGUSD.pi", "XPDUSD.pi", "LNKUSD.p", "BCHUSD.p", "ADAUSD.p", "LTCUSD.pi", "TRXUSD.p", "XLMUSD.p"):
            continue
        if (ms, row["side"], int(row["ts_in"]) // 14400) in seen_m4_keys:
            continue
        sym_bin = str(row["symbol"])
        if sym_bin not in s08_g4_cache and (DATA_DIR / "cache" / f"{sym_bin}_4h.parquet").is_file():
            s08_g4_cache[sym_bin] = pd.read_parquet(DATA_DIR / "cache" / f"{sym_bin}_4h.parquet")
        g4 = s08_g4_cache.get(sym_bin)
        bi = int(row.get("bar_i", -1) or -1)
        v_mult = 1.0
        if g4 is not None and len(g4) > 250:
            if bi < 0 or bi >= len(g4):
                bi = int(np.searchsorted(g4["end_ts"].to_numpy(np.int64), int(row["ts_in"]), side="right")) - 1
            if bi >= 245:
                v_mult = float(
                    vol_of_conviction_risk_usd(
                        g4["c"].to_numpy(float)[max(0, bi - 245) : bi + 1], "H4",
                        1 if row["side"] == "BUY" else -1, float(g4["d_pct"].iloc[bi]),
                        int(g4["buy_imb"].iloc[bi] - g4["sell_imb"].iloc[bi]), 0.5,
                        1.0, 0.70, 1.25, target_ann_vol=0.55, lookback=240,
                    )
                )
        hyb_alt_trades.append({
            **row,
            "source": "STRAT_SCREEN62",
            "cluster": _cluster_for_mt5(ms, str(row.get("category", "CRYPTO"))),
            "raw_sid": str(row.get("folder", row["sleeve"])),
            "tf_k": 2 if str(row.get("tf", "4H")).upper() == "2H" else 4,
            "fp_sym": sym_bin if sym_bin != "CLUSDT" else None,
            "bar_i": bi,
            "vof_mult": v_mult,
        })

    core_pool = m4_solo + tf_trades
    energy_probe_pool = core_pool + tf_withheld
    unified_pool = core_pool + hyb_alt_trades
    print(f"[HybridPool] core={len(core_pool)} with_energy={len(energy_probe_pool)} wide62={len(unified_pool)} (M4_Pruned={len(m4_solo)}, CoreTF={len(tf_trades)}, WithheldEnergy={len(tf_withheld)}, Screen62_MT5={len(hyb_alt_trades)})", flush=True)

    # 3. Build strict 7-day-purged expanding walk-forward ML predictions
    ml_preds = _build_purged_wf_ml_predictions(unified_pool)
    print(f"[ML-WF] Scored {len(ml_preds)} strictly purged footprint trade events", flush=True)

    def _run_window(w_min: int, w_label: str) -> dict[str, Any]:
        w_out: dict[str, Any] = {}
        res_a = m4_rep.simulate_m4_live_parity(f"A) M4 Desk Control ($6 | {w_label})", desk_cfg, sigs, tapes, specs, m4_sp, start_equity=start_equity, min_ts=w_min, mc_runs=mc_runs)
        res_b = m4_rep.simulate_m4_live_parity(f"B) M4 Pruned @ {risk_pct:.2f}% ({w_label})", pruned_cfg, sigs, tapes, specs, m4_sp, start_equity=start_equity, min_ts=w_min, mc_runs=mc_runs)
        w_out["A_M4_Desk"] = res_a
        w_out["B_M4_Pruned"] = res_b

        s_caps = {"CRYPTO": 2, "GOLD_GRAM": 1, "ENERGY": 1, "INDEX": 1, "FOREX": 1}
        variants = [
            ("C_Hybrid_Wide62", f"C) Wide Screen62 Hybrid @ {risk_pct:.2f}%", unified_pool, False, False, "none", 0.0, s_caps),
            ("D_Sovereign_Core", f"D) Sovereign 4-Cluster Core + Corr-ERC + S08 @ {risk_pct:.2f}%", core_pool, True, True, "none", 0.0, s_caps),
            ("E_Sovereign_CatBoost", f"E) Sovereign Core + Purged CatBoost E[R]>=0.00", core_pool, True, True, "catboost", 0.0, s_caps),
            ("F_Sovereign_Ensemble", f"F) Sovereign Core + Purged Ensemble E[R]>=-0.02", core_pool, True, True, "ensemble", -0.02, s_caps),
            ("G_Withheld_Energy", f"G) Withheld Probe (+Energy PA03)", energy_probe_pool, True, True, "none", 0.0, s_caps),
        ]
        for key, lbl, p_use, u8, uerc, mlm, mlthr, clc in variants:
            w_out[key] = simulate_sovereign_portfolio(
                lbl, p_use, specs, sp_px, sp_bps, gau_xau_ratio,
                start_equity=start_equity, risk_pct=risk_pct, max_open_risk_pct=max_book_heat_pct,
                max_global=4, cluster_caps=clc, use_s08=u8, use_corr_erc=uerc,
                ml_mode=mlm, ml_min_er=mlthr, ml_preds=ml_preds,
                spread_mult=1.0, min_ts=w_min, mc_runs=mc_runs,
            )
        return w_out

    oos_2024 = _run_window(TS_2024, "OOS 2024-26")
    recent_2025 = _run_window(TS_2025, "2025-26 Plateau")
    full_2021 = _run_window(TS_2021, "Full 2021-26")

    # 4. Spread stress sensitivity (1.0x, 1.5x, 2.0x) with identical cluster_caps
    stress_grid: dict[str, dict[str, Any]] = {}
    s_caps_stress = {"CRYPTO": 2, "GOLD_GRAM": 1, "ENERGY": 1, "INDEX": 1, "FOREX": 1}
    for mult in spread_mults:
        stress_grid[f"{mult:.1f}x"] = {
            "D_Sovereign_Core": simulate_sovereign_portfolio(
                f"D @ {mult:.1f}x Spread", core_pool, specs, sp_px, sp_bps, gau_xau_ratio,
                start_equity=start_equity, risk_pct=risk_pct, max_open_risk_pct=max_book_heat_pct,
                max_global=4, cluster_caps=s_caps_stress, use_s08=True, use_corr_erc=True,
                spread_mult=float(mult), min_ts=TS_2024, mc_runs=0,
            )["metrics"],
            "E_Sovereign_CatBoost": simulate_sovereign_portfolio(
                f"E @ {mult:.1f}x Spread", core_pool, specs, sp_px, sp_bps, gau_xau_ratio,
                start_equity=start_equity, risk_pct=risk_pct, max_open_risk_pct=max_book_heat_pct,
                max_global=4, cluster_caps=s_caps_stress, use_s08=True, use_corr_erc=True,
                ml_mode="catboost", ml_min_er=0.0, ml_preds=ml_preds,
                spread_mult=float(mult), min_ts=TS_2024, mc_runs=0,
            )["metrics"],
        }

    results = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "config": {"start_equity": start_equity, "risk_pct": risk_pct, "max_book_heat_pct": max_book_heat_pct, "mc_runs": mc_runs},
        "gau_xau_ratio": gau_xau_ratio,
        "weekday_spread_bps": sp_bps,
        "live_quote_spread_bps": mkt["live_sp_bps"],
        "tradfi_h4_screen": tf_audit,
        "oos_2024_2026": oos_2024,
        "recent_2025_2026": recent_2025,
        "full_2021_2026": full_2021,
        "spread_stress_oos": stress_grid,
    }
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    _render_comparison_dashboard(results, DASHBOARD_HTML)

    print("\n=== SOVEREIGN $2,500 HYBRID — UNTOUCHED OOS (2024–2026) @ 0.25% ===", flush=True)
    for k, obj in oos_2024.items():
        m = obj["metrics"]
        mc = m.get("mc_20k") or {}
        print(
            f"{k:24s} | End=${m['end_equity']:>8,.2f} CAGR={m['cagr_pct']:+6.2f}% DD={m['max_dd_pct']:5.2f}% "
            f"PF={m['pf']:4.2f} Cal={m['calmar']:5.2f} Sh={m['sharpe']:4.2f} tHAC={m.get('t_hac',0):5.2f} "
            f"CrashDD95={mc.get('crash_dd95',0):4.1f}% Tr={m['trades']:4d}",
            flush=True,
        )
    print(f"\nSaved JSON -> {out_path}\nSaved Dashboard -> {DASHBOARD_HTML}", flush=True)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Sovereign $2,500 Hybrid Causal Replay & 20K MC Audit")
    ap.add_argument("--equity", type=float, default=2501.80)
    ap.add_argument("--risk-pct", type=float, default=0.25)
    ap.add_argument("--max-book-heat-pct", type=float, default=1.0)
    ap.add_argument("--spread-mult", type=float, nargs="+", default=[1.0, 1.5, 2.0])
    ap.add_argument("--mc-runs", type=int, default=20000)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    run_sovereign_hybrid_audit(
        start_equity=args.equity, risk_pct=args.risk_pct, max_book_heat_pct=args.max_book_heat_pct,
        spread_mults=tuple(args.spread_mult), mc_runs=args.mc_runs, out_path=args.out,
    )


if __name__ == "__main__":
    main()
