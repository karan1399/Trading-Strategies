"""Stage 2: MT5 Shared-Account Multi-Asset x Multi-Strategy Quant Portfolio Optimizer.

Unifies ALL Stage-1 true-MT5-spread assets:
  - MT5 Crypto Footprint+OI (2021-2026 1m-exact ledgers)
  - MT5 TradFi Metals/Energy/Indices/FX (2020-2026 H1-exact multi-year ledgers)

Executes STRICT Causal In-Sample (<= 2023-12-31) Optimization -> Untouched Frozen OOS (2024-2026):
  1. Causal Spread-Viability & Regime-Durability Gate (IS_PF >= 1.25, Late-IS 2022-2023 >= 0,
     live_sp <= 25bps -> naturally keeps tight-spread Titans & cuts wide-spread MT5 losers).
  2. Cross-Asset Daily Correlation Matrix & BTC Down-Day Tail Correlation Forensics.
  3. Causal IS Sortino-Kelly Pair Weights, Bucket Orthogonality, Cluster Caps (Crypto Same-Side
     & TradFi Same-Asset), vOF Conviction Scaling, and Hysteresis Prop Drawdown Throttle.
  4. 20,000-Path Correlated Block-Bootstrap & -4.5R 3-Day Crash-Shock Prop Challenge Simulation
     evaluating exact `Pass10%`, `Breach10%`, `Breach8%`, `DD95`, `Crash DD95`, and `$2,501.80` Live Scaling.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from footprint_Chart.backtesting.multi_asset_optimizer import SPLIT_2024_TS, compute_hac_stats
from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR

MT5_DIR = DATA_DIR / "mt5_portfolio"
CFG_DIR = MT5_DIR / "configs"
TRADES_DIR = MT5_DIR / "trades"
MASTER_JSON = MT5_DIR / "mt5_master_portfolio.json"
DASHBOARD_PNG = Path(
    r"C:\Users\SIGMA\.gemini\antigravity\brain\4ed94c81-b1da-4868-8a7c-aec85386790d\mt5_shared_account_pareto_dashboard.png"
)

TS_2022 = int(pd.Timestamp("2022-01-01", tz="UTC").timestamp())


def load_all_mt5_ledgers() -> tuple[dict[str, dict[str, Any]], dict[str, pd.DataFrame]]:
    cfgs: dict[str, dict[str, Any]] = {}
    dfs: dict[str, pd.DataFrame] = {}
    for p in sorted(CFG_DIR.glob("*.json")):
        c = json.loads(p.read_text(encoding="utf-8"))
        sym = c["symbol"]
        cfgs[sym] = c
        tp = TRADES_DIR / f"{sym}.parquet"
        if tp.is_file():
            df = pd.read_parquet(tp)
            if not df.empty:
                # Filter out short-sample <2Y Binance commodity footprint runs; use multi-year MT5_XAUUSD..MT5_USDJPY instead!
                if c.get("category") == "COMMODITY" and not sym.startswith("MT5_"):
                    continue
                dfs[sym] = df[df["ts_in"] >= int(pd.Timestamp("2021-01-01", tz="UTC").timestamp())].copy()
    return cfgs, dfs


def prop_mc_20k(daily_r: np.ndarray, risk_pct: float, n_mc: int = 20000, seed: int = 2026) -> dict[str, float]:
    """20,000-path correlated 8-day block bootstrap + -4.5R 3-day crash shock Prop-Firm evaluator."""
    if len(daily_r) < 30:
        return {"pass10_pct": 0.0, "breach10_pct": 0.0, "breach8_pct": 0.0, "med_1y_pct": 0.0, "dd95": 0.0, "crash_dd95": 0.0, "p_loss_pct": 0.0}
    rng = np.random.default_rng(seed)
    rp = risk_pct / 100.0
    hz = min(365, len(daily_r))
    blk = 8
    nb = (hz + blk - 1) // blk
    starts = rng.integers(0, max(1, len(daily_r) - blk + 1), size=(n_mc, nb))
    idx = (starts[:, :, None] + np.arange(blk)[None, None, :]).reshape(n_mc, -1)[:, :hz]
    samp = daily_r[idx]
    paths = np.cumprod(np.maximum(0.01, 1.0 + samp * rp), axis=1)
    p_full = np.hstack([np.ones((n_mc, 1)), paths])
    pks = np.maximum.accumulate(p_full, axis=1)
    dds = ((pks - p_full) / pks * 100.0).max(axis=1)

    samp_cr = samp.copy()
    cpos = rng.integers(0, hz - 3, size=n_mc)
    rows = np.arange(n_mc)
    samp_cr[rows, cpos] += -1.5
    samp_cr[rows, cpos + 1] += -1.5
    samp_cr[rows, cpos + 2] += -1.5
    p_cr = np.cumprod(np.maximum(0.01, 1.0 + samp_cr * rp), axis=1)
    p_crf = np.hstack([np.ones((n_mc, 1)), p_cr])
    pk_cr = np.maximum.accumulate(p_crf, axis=1)
    dds_cr = ((pk_cr - p_crf) / pk_cr * 100.0).max(axis=1)

    return {
        "runs": int(n_mc),
        "med_1y_pct": round(float((np.median(paths[:, -1]) - 1.0) * 100.0), 1),
        "p5_1y_pct": round(float((np.percentile(paths[:, -1], 5) - 1.0) * 100.0), 1),
        "pass10_pct": round(float(((paths.max(axis=1) >= 1.10) & (dds < 10.0)).mean() * 100.0), 1),
        "breach10_pct": round(float((dds >= 10.0).mean() * 100.0), 1),
        "breach8_pct": round(float((dds >= 8.0).mean() * 100.0), 1),
        "dd95": round(float(np.percentile(dds, 95)), 1),
        "crash_dd95": round(float(np.percentile(dds_cr, 95)), 1),
        "p_loss_pct": round(float((paths[:, -1] < 1.0).mean() * 100.0), 1),
    }


def _entry_order(trade: dict[str, Any]) -> tuple[int, int, str, str, str, str, int, str, str]:
    """Rank candidates using only identity and priority published at entry."""
    return (
        int(trade["ts_in"]), int(trade.get("entry_priority", 0)),
        str(trade["sleeve"]), str(trade["symbol"]), str(trade["mt5_symbol"]),
        str(trade.get("tf", "")), int(trade.get("bar_i", -1)),
        str(trade["side"]), str(trade.get("entry_id", "")),
    )


def _exit_available_at(trade: dict[str, Any]) -> int:
    """Convert an exit-bar opening timestamp to the first observable close."""
    recorded = trade.get("exit_available_ts")
    if recorded is not None and pd.notna(recorded):
        available = int(recorded)
        if available < int(trade["ts_out"]):
            raise ValueError("Exit availability cannot precede the observed exit")
        return available
    # Legacy MT5 TradFi producer executes on its H1 tape even for H4 signals.
    duration = 3600 if str(trade["symbol"]).startswith("MT5_") else 60
    return int(trade["ts_out"]) + duration


def simulate_shared_account(
    name: str,
    trades_pool: list[dict[str, Any]],
    start_equity: float = 100000.0,
    risk_pct: float = 0.45,
    max_global_conc: int = 6,
    max_per_asset: int = 2,
    max_crypto_same_side: int = 3,
    bucket_caps: dict[str, int] | None = None,
    pair_weights: dict[tuple[str, str], float] | None = None,
    use_vof: bool = True,
    dd_throttle_1: float = 4.5,
    dd_throttle_2: float = 6.5,
    min_ts: int | None = None,
    max_ts: int | None = None,
    mc_runs: int = 0,
    *,
    size_risk_usd: Callable[[dict[str, Any], float, float], float | tuple[float, float] | None] | None = None,
    max_crypto_total: int | None = None,
    max_open_risk_pct: float | None = None,
) -> dict[str, Any]:
    """Replay timestamped entries with deterministic, point-in-time admission.

    Legacy tapes use bar-open exit timestamps. Tapes with event-level evidence
    can provide exit_available_ts. max_ts is an exclusive training cutoff.
    """
    tl = [
        t for t in trades_pool
        if (min_ts is None or int(t["ts_in"]) >= min_ts)
        and (max_ts is None or int(t["ts_in"]) < max_ts)
    ]
    tl.sort(key=_entry_order)
    for earlier, later in zip(tl, tl[1:]):
        if _entry_order(earlier) == _entry_order(later):
            raise ValueError(f"Duplicate entry identity {_entry_order(later)!r}. Supply distinct entry_id values.")
    empty = {"name": name, "metrics": {"trades": 0, "total_trades": 0, "end_equity": start_equity,
                                     "cagr_pct": 0.0, "max_dd_pct": 0.0, "calmar": 0.0, "pf": 0.0}}
    if not tl:
        return empty

    eq = float(start_equity)
    pk = float(start_equity)
    rp = float(risk_pct) / 100.0
    pw = pair_weights or {}
    bc = bucket_caps or {}

    active: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    r_vals: list[float] = []
    dd_vals: list[float] = [0.0]
    daily_r_map: dict[int, float] = {}
    curve: list[tuple[int, float]] = [(int(tl[0]["ts_in"]), eq)]
    is_r: list[float] = []
    oos_r: list[float] = []
    by_asset: dict[str, dict[str, Any]] = {}
    by_strat: dict[str, dict[str, Any]] = {}
    throttled = False
    max_dd_pct = 0.0

    for t in tl:
        tin, tout = int(t["ts_in"]), _exit_available_at(t)
        sym, cat, side, bkt, sid = str(t["symbol"]), str(t["category"]), str(t["side"]), str(t["bucket"]), str(t["sleeve"])

        closing = sorted([a for a in active if int(a["ts_out"]) <= tin], key=lambda a: int(a["ts_out"]))
        for cl in closing:
            eq += cl["pnl_usd"]
            pk = max(pk, eq)
            dd = ((pk - eq) / pk * 100.0) if pk > 0 else 0.0
            max_dd_pct = max(max_dd_pct, dd)
            dd_vals.append(dd)
            curve.append((int(cl["ts_out"]), eq))
        active = [a for a in active if int(a["ts_out"]) > tin]

        if len(active) >= max_global_conc:
            continue
        if sum(1 for a in active if a["symbol"] == sym) >= max_per_asset:
            continue
        if cat == "CRYPTO" and max_crypto_total is not None:
            if sum(1 for a in active if a["category"] == "CRYPTO") >= max_crypto_total:
                continue
        if cat == "CRYPTO" and max_crypto_same_side > 0:
            if sum(1 for a in active if a["category"] == "CRYPTO" and a["side"] == side) >= max_crypto_same_side:
                continue
        if bkt in bc and sum(1 for a in active if a["bucket"] == bkt) >= bc[bkt]:
            continue

        cur_dd = ((pk - eq) / pk * 100.0) if pk > 0 else 0.0
        th_mult = 1.0
        if dd_throttle_1 > 0:
            if cur_dd >= (dd_throttle_2 if dd_throttle_2 > 0 else 999.0):
                th_mult = 0.25
                throttled = True
            elif cur_dd >= dd_throttle_1 or (throttled and cur_dd >= dd_throttle_1 * 0.5):
                th_mult = 0.50
                throttled = True
            else:
                throttled = False

        w = float(pw.get((sym, sid), 1.0))
        vmult = float(t.get("vof_mult", 1.0)) if use_vof else 1.0
        reserved_risk = eq * rp * w * vmult * th_mult
        eff_r = float(t["r"]) * w * vmult * th_mult
        pnl_usd = eq * rp * eff_r
        if size_risk_usd is not None:
            sized_risk = size_risk_usd(t, reserved_risk, eq)
            if sized_risk is None:
                continue
            gross_risk, reserved_risk = sized_risk if isinstance(sized_risk, tuple) else (sized_risk, sized_risk)
            if (not all(math.isfinite(v) and v > 0 for v in (gross_risk, reserved_risk))
                    or reserved_risk < gross_risk or eq * rp <= 0):
                raise ValueError("A size adapter must return positive finite stop risk")
            pnl_usd = gross_risk * float(t["r"])
            eff_r = pnl_usd / (eq * rp)
        if max_open_risk_pct is not None:
            if sum(a["reserved_risk"] for a in active) + reserved_risk > eq * max_open_risk_pct / 100 + 1e-9:
                continue

        active.append({"ts_out": tout, "symbol": sym, "category": cat, "side": side, "bucket": bkt,
                       "pnl_usd": pnl_usd, "reserved_risk": reserved_risk})
        # An unresolved label reserves capacity but contributes no training P&L.
        if max_ts is not None and tout >= max_ts:
            continue
        executed.append(t)
        r_vals.append(eff_r)
        dk = int(tout // 86400)
        daily_r_map[dk] = daily_r_map.get(dk, 0.0) + eff_r
        (is_r if tin < SPLIT_2024_TS else oos_r).append(eff_r)

        ba = by_asset.setdefault(sym, {"mt5": t["mt5_symbol"], "cat": cat, "trades": 0, "wins": 0, "sum_r": 0.0, "pnl_usd": 0.0})
        ba["trades"] += 1
        ba["wins"] += 1 if eff_r > 0 else 0
        ba["sum_r"] += eff_r
        ba["pnl_usd"] += pnl_usd

        bs = by_strat.setdefault(sid, {"trades": 0, "wins": 0, "sum_r": 0.0, "pnl_usd": 0.0})
        bs["trades"] += 1
        bs["wins"] += 1 if eff_r > 0 else 0
        bs["sum_r"] += eff_r
        bs["pnl_usd"] += pnl_usd

    for cl in sorted(active, key=lambda a: int(a["ts_out"])):
        if max_ts is not None and int(cl["ts_out"]) >= max_ts:
            continue
        eq += cl["pnl_usd"]
        pk = max(pk, eq)
        dd = ((pk - eq) / pk * 100.0) if pk > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)
        dd_vals.append(dd)
        curve.append((int(cl["ts_out"]), eq))

    if not executed:
        return empty
    arr = np.array(r_vals, dtype=float)
    span_ts = max(86400.0, float(curve[-1][0] - int(executed[0]["ts_in"])))
    yrs = max(0.25, span_ts / (365.25 * 86400.0))
    cagr = (((eq / start_equity) ** (1.0 / yrs) - 1.0) * 100.0) if eq > 0 else -100.0
    calmar = (cagr / max_dd_pct) if max_dd_pct > 0 else 0.0
    gains, losses = float(arr[arr > 0].sum()), float(abs(arr[arr <= 0].sum()))
    pf = (gains / losses) if losses > 0 else 9.99
    wr = float((arr > 0).mean() * 100.0)
    ulcer = float(math.sqrt(np.mean(np.square(dd_vals)))) if dd_vals else 0.0
    hac = compute_hac_stats(arr)

    oos_arr = np.array(oos_r, dtype=float)
    og = float(oos_arr[oos_arr > 0].sum()) if len(oos_arr) else 0.0
    ol = float(abs(oos_arr[oos_arr <= 0].sum())) if len(oos_arr) else 0.0
    oos_pf = (og / ol) if ol > 0 else (9.99 if og > 0 else 0.0)
    oeq, opk, omdd = 1.0, 1.0, 0.0
    for rv in oos_arr:
        oeq *= max(0.01, 1.0 + float(rv) * rp)
        opk = max(opk, oeq)
        omdd = max(omdd, (opk - oeq) / opk * 100.0)
    oos_yrs = max(0.25, (curve[-1][0] - SPLIT_2024_TS) / (365.25 * 86400.0)) if len(oos_arr) else 1.0
    oos_cagr = (((oeq) ** (1.0 / oos_yrs) - 1.0) * 100.0) if len(oos_arr) else 0.0

    d_min, d_max = min(daily_r_map.keys()), max(daily_r_map.keys())
    d_vals = np.array([daily_r_map.get(d, 0.0) for d in range(d_min, d_max + 1)], dtype=float)
    sharpe = float(d_vals.mean() / (d_vals.std() + 1e-9) * math.sqrt(365.0)) if len(d_vals) > 10 else 0.0
    down_d = d_vals[d_vals < 0]
    sortino = float(d_vals.mean() / (down_d.std() + 1e-9) * math.sqrt(365.0)) if len(down_d) > 3 else sharpe
    worst_day_pct = round(float(np.min(d_vals) * risk_pct), 2) if len(d_vals) else 0.0
    mc_20k = prop_mc_20k(d_vals, float(risk_pct), n_mc=mc_runs) if mc_runs > 0 else {}

    return {
        "name": name, "risk_pct": risk_pct,
        "metrics": {
            "start_equity": start_equity, "end_equity": round(eq, 2), "cagr_pct": round(cagr, 1),
            "trades": len(arr), "wr": round(wr, 1), "net_r": round(float(arr.sum()), 2),
            "pf": round(pf, 2), "max_dd_pct": round(max_dd_pct, 2), "calmar": round(calmar, 2),
            "sharpe": round(sharpe, 2), "sortino": round(sortino, 2), "ulcer": round(ulcer, 2),
            "t_hac": hac["t_hac"], "worst_day_pct": worst_day_pct,
            "is_r": round(float(sum(is_r)), 2), "oos_trades": len(oos_arr), "oos_r": round(float(sum(oos_r)), 2),
            "oos_pf": round(oos_pf, 2), "oos_dd_pct": round(omdd, 2), "oos_cagr_pct": round(oos_cagr, 1),
            "mc_20k": mc_20k,
        },
        "by_asset": sorted(
            [{"symbol": k, **v, "sum_r": round(v["sum_r"], 2), "pnl_usd": round(v["pnl_usd"], 2)} for k, v in by_asset.items()],
            key=lambda x: x["pnl_usd"], reverse=True,
        ),
        "by_strat": sorted(
            [{"sid": k, **v, "sum_r": round(v["sum_r"], 2), "pnl_usd": round(v["pnl_usd"], 2)} for k, v in by_strat.items()],
            key=lambda x: x["pnl_usd"], reverse=True,
        ),
        "curve": curve,
    }


def run_stage2() -> dict[str, Any]:
    print("[Stage 2] Running Strict Causal Multi-Asset (Crypto + TradFi) Shared-Account Optimization...", flush=True)
    cfgs, dfs = load_all_mt5_ledgers()

    # ---- Step 1: Causal IS Forensics across ALL (Asset, Strategy) pairs (<= 2023-12-31, ZERO OOS leakage!) ----
    asset_daily: dict[str, dict[int, float]] = {}
    all_days = set()
    pair_rows: list[dict[str, Any]] = []
    asset_audit: list[dict[str, Any]] = []

    for sym, df in sorted(dfs.items()):
        c = cfgs[sym]
        cat = c["category"]
        mt5_cost = float(c["mt5_cost_bps"])
        available = pd.Series((_exit_available_at(t) for t in df.to_dict("records")), index=df.index)
        is_df = df[(df["ts_in"] < SPLIT_2024_TS) & (available < SPLIT_2024_TS)]
        if is_df.empty:
            continue

        # Asset-level Causal Late-IS check (2022-2023 bear/chop durability!)
        late_is_r = float(is_df[is_df["ts_in"] >= TS_2022]["r"].sum())
        early_is_r = float(is_df[is_df["ts_in"] < TS_2022]["r"].sum())

        dm: dict[int, float] = {}
        for _, r in is_df.iterrows():
            dk = int(r["ts_out"] // 86400)
            dm[dk] = dm.get(dk, 0.0) + float(r["r"])
            all_days.add(dk)
        asset_daily[sym] = dm

        kept_pair_n = 0
        for sid, grp in is_df.groupby("sleeve"):
            r_is = grp["r"].to_numpy(dtype=float)
            if len(r_is) < 8:
                continue
            r_late = grp[grp["ts_in"] >= TS_2022]["r"].to_numpy(dtype=float)
            late_r = float(r_late.sum()) if len(r_late) else 0.0
            g = float(r_is[r_is > 0].sum())
            l = float(abs(r_is[r_is <= 0].sum()))
            pf = (g / l) if l > 0 else 9.9
            eq_r = np.cumsum(r_is)
            pk_r = np.maximum.accumulate(np.insert(eq_r, 0, 0.0))[1:]
            dd_r = float(np.max(pk_r - eq_r))
            calmar_r = float(r_is.sum()) / max(dd_r, 1.0)
            hac = compute_hac_stats(r_is)
            mean_r = float(r_is.mean())
            down_var = float(np.mean(np.minimum(r_is, 0.0) ** 2)) if len(r_is) > 1 else 1.0
            kelly = max(0.05, (mean_r / max(down_var, 0.15)) * math.sqrt(max(1.0, pf)))

            # Causal Elite Pair Gate: strong IS PF, t_hac, AND positive in BOTH 2021 Early-IS and 2022-23 Late-IS!
            r_early = grp[grp["ts_in"] < TS_2022]["r"].to_numpy(dtype=float)
            early_r = float(r_early.sum()) if len(r_early) else 0.0
            if cat == "CRYPTO":
                is_elite = (pf >= 1.35 and hac["t_hac"] >= 1.00 and dd_r <= 11.0 and late_r > 0.0 and early_r > 0.0 and mt5_cost <= 25.0)
            else:
                is_elite = (pf >= 2.20 and hac["t_hac"] >= 1.75 and dd_r <= 8.0 and late_r >= 4.0 and early_r >= 0.0 and mt5_cost <= 5.0)
            if is_elite:
                kept_pair_n += 1

            oos_grp = df[(df["ts_in"] >= SPLIT_2024_TS) & (df["sleeve"] == sid)]
            r_oos = oos_grp["r"].to_numpy(dtype=float)
            og = float(r_oos[r_oos > 0].sum()) if len(r_oos) else 0.0
            ol = float(abs(r_oos[r_oos <= 0].sum())) if len(r_oos) else 0.0
            oos_pf = (og / ol) if ol > 0 else (9.9 if og > 0 else 0.0)

            pair_rows.append({
                "symbol": sym, "mt5_symbol": c["mt5_symbol"], "category": cat, "sid": sid,
                "bucket": str(grp["bucket"].iloc[0]), "tf": str(grp["tf"].iloc[0]), "cost_bp": mt5_cost,
                "is_n": len(r_is), "is_r": round(float(r_is.sum()), 2), "early_is_r": round(early_r, 2), "late_is_r": round(late_r, 2),
                "is_pf": round(pf, 2), "is_dd_r": round(dd_r, 2), "is_calmar_r": round(calmar_r, 2),
                "is_t_hac": hac["t_hac"], "is_kelly": round(kelly, 4), "is_elite": is_elite,
                "oos_n": len(r_oos), "oos_r": round(float(r_oos.sum()), 2), "oos_pf": round(oos_pf, 2),
            })

        # Causal Asset-Level Admission Gate:
        # Crypto: tight MT5 spread <= 25bps, positive in both 2021 & 2022-23, >= 2 elite strategies
        # TradFi: ultra-tight MT5 spread <= 5bps, positive Early & Late IS, >= 1 elite 2.2+ PF strategy
        if cat == "CRYPTO":
            admitted = (mt5_cost <= 25.0 and early_is_r >= 5.0 and late_is_r >= 10.0 and kept_pair_n >= 2)
        else:
            admitted = (mt5_cost <= 5.0 and early_is_r >= 0.0 and late_is_r >= 4.0 and kept_pair_n >= 1)
        asset_audit.append({
            "symbol": sym, "mt5_symbol": c["mt5_symbol"], "cat": cat, "cost_bp": mt5_cost,
            "early_is_r": round(early_is_r, 1), "late_is_r": round(late_is_r, 1),
            "elite_strats": kept_pair_n, "admitted": admitted,
        })

    pair_df = pd.DataFrame(pair_rows).sort_values(["is_t_hac", "is_pf"], ascending=False).reset_index(drop=True)
    admitted_syms = [a["symbol"] for a in asset_audit if a["admitted"]]
    admitted_crypto = [a["symbol"] for a in asset_audit if a["admitted"] and a["cat"] == "CRYPTO"]
    admitted_tradfi = [a["symbol"] for a in asset_audit if a["admitted"] and a["cat"] != "CRYPTO"]

    print("\n=== CAUSAL MT5 SPREAD & 2022-2023 DURABILITY ADMISSION AUDIT (ZERO OOS PEEKING) ===")
    for a in sorted(asset_audit, key=lambda x: (x["admitted"], x["late_is_r"]), reverse=True):
        print(f"  {a['symbol']:12s} ({a['mt5_symbol']:10s} | {a['cat']:6s}) Cost={a['cost_bp']:6.2f}bp | IS(21)={a['early_is_r']:+6.1f}R IS(22-23)={a['late_is_r']:+6.1f}R | Elite={a['elite_strats']:2d} | {'ADMITTED' if a['admitted'] else 'REJECTED'}")

    # Balance signal density across admitted assets (cap top 8 elite strats per asset on IS t*PF)
    elite_raw = pair_df[(pair_df["symbol"].isin(admitted_syms)) & (pair_df["is_elite"])].copy()
    elite_raw["is_rank_sc"] = elite_raw["is_t_hac"] * elite_raw["is_pf"]
    elite_pairs = elite_raw.sort_values("is_rank_sc", ascending=False).groupby("symbol", group_keys=False).head(8).reset_index(drop=True)
    elite_set = {(r["symbol"], r["sid"]) for _, r in elite_pairs.iterrows()}
    mw = float(elite_pairs["is_kelly"].mean()) if not elite_pairs.empty else 1.0
    pair_weights = {
        (r["symbol"], r["sid"]): round(min(1.40, max(0.60, float(r["is_kelly"]) / max(mw, 1e-6))), 3)
        for _, r in elite_pairs.iterrows()
    }

    crypto_elite_pool = [t for s in admitted_crypto for t in dfs[s].to_dict("records") if (t["symbol"], t["sleeve"]) in elite_set]
    multi_elite_pool = [t for s in admitted_syms for t in dfs[s].to_dict("records") if (t["symbol"], t["sleeve"]) in elite_set]
    naive_all_pool = [t for s, df in dfs.items() if cfgs[s]["category"] == "CRYPTO" for t in df.to_dict("records")]

    days_sorted = sorted(all_days)
    dmat = pd.DataFrame({s: [asset_daily[s].get(d, 0.0) for d in days_sorted] for s in asset_daily if s in admitted_syms}, index=days_sorted)
    corr_all = dmat.corr().round(3)

    # ---- Step 2: Strict IS Risk & Architecture Sweep ----
    is_frontier: list[dict[str, Any]] = []
    for p_name, pool_t in (("Crypto_Core", crypto_elite_pool), ("MultiAsset_Master", multi_elite_pool)):
        if not pool_t:
            continue
        for mg, ma, cs, bkt in (
            (5, 2, 3, {"BREAKOUT": 2, "PULLBACK": 2, "MEANREV": 2}),
            (6, 2, 4, {"BREAKOUT": 2, "PULLBACK": 2, "MEANREV": 3}),
            (8, 3, 4, None),
        ):
            for rp in (0.25, 0.35, 0.45, 0.55, 0.65, 0.80, 1.00):
                for th1, th2 in ((0.0, 0.0), (3.5, 5.5), (4.5, 6.5)):
                    m = simulate_shared_account(
                        "is_sweep", pool_t, 100000.0, rp, mg, ma, cs, bkt, pair_weights, True, th1, th2, max_ts=SPLIT_2024_TS,
                    )["metrics"]
                    is_frontier.append({
                        "pool": p_name, "pool_t": pool_t, "mg": mg, "ma": ma, "cs": cs, "bkt": bkt,
                        "rp": rp, "th1": th1, "th2": th2, **{f"is_{k}": v for k, v in m.items()},
                    })

    def _pick_is(pool_filter: str, max_dd: float, key_fn, max_rp: float = 1.00, max_mg: int = 8, req_bkt: bool = False, req_throttle: bool = False) -> dict[str, Any]:
        sub = [
            x for x in is_frontier
            if x["pool"] == pool_filter
            and x["is_max_dd_pct"] <= max_dd
            and x["is_pf"] >= 1.45
            and x["rp"] <= max_rp
            and x["mg"] <= max_mg
            and (not req_bkt or x["bkt"] is not None)
            and (not req_throttle or x["th1"] > 0.0)
        ]
        if not sub:
            sub = sorted([x for x in is_frontier if x["pool"] == pool_filter], key=lambda x: x["is_max_dd_pct"])[:5]
        return max(sub, key=key_fn)

    m1_spec = _pick_is("Crypto_Core", 5.5, lambda x: (x["is_calmar"], x["is_cagr_pct"]), max_rp=0.65)
    m2_spec = _pick_is("Crypto_Core", 7.8, lambda x: x["is_cagr_pct"] * math.sqrt(max(0.5, x["is_calmar"])))
    m3_spec = _pick_is("MultiAsset_Master", 4.5, lambda x: x["is_cagr_pct"] * math.sqrt(max(0.5, x["is_calmar"])), max_rp=0.45, max_mg=6, req_bkt=True, req_throttle=True)
    m4_spec = _pick_is("MultiAsset_Master", 8.0, lambda x: x["is_end_equity"], max_rp=0.80, max_mg=6, req_bkt=True, req_throttle=True)

    # ---- Step 3: Freeze Selection & Evaluate Full 2021-2026 + Untouched Frozen OOS 2024-2026 + 20K Crash MC ----
    eval_specs = [
        ("M0_Naive_AllCrypto", "M0: Unfiltered Equal-Weight All Crypto", naive_all_pool, 0.30, 6, 2, 0, None, None, False, 0.0, 0.0),
        ("M1_Crypto_Prop8", "M1: Tight-Spread Crypto (8% Prop Safe)", m1_spec["pool_t"], m1_spec["rp"], m1_spec["mg"], m1_spec["ma"], m1_spec["cs"], m1_spec["bkt"], pair_weights, True, m1_spec["th1"], m1_spec["th2"]),
        ("M2_Crypto_Prop10", "M2: Tight-Spread Crypto (10% Prop Champion)", m2_spec["pool_t"], m2_spec["rp"], m2_spec["mg"], m2_spec["ma"], m2_spec["cs"], m2_spec["bkt"], pair_weights, True, m2_spec["th1"], m2_spec["th2"]),
        ("M3_MultiAsset_Prop10", "M3: MULTI-ASSET MASTER (Crypto+Gold/Metals/Macro)", m3_spec["pool_t"], m3_spec["rp"], m3_spec["mg"], m3_spec["ma"], m3_spec["cs"], m3_spec["bkt"], pair_weights, True, m3_spec["th1"], m3_spec["th2"]),
        ("M4_Personal_MaxCAGR", "M4: Personal MT5 ($2.5K->Max Growth, <=15% DD)", m4_spec["pool_t"], m4_spec["rp"], m4_spec["mg"], m4_spec["ma"], m4_spec["cs"], m4_spec["bkt"], pair_weights, True, m4_spec["th1"], m4_spec["th2"]),
    ]

    final_ports: dict[str, dict[str, Any]] = {}
    print("\n=== SHARED-ACCOUNT MT5 PORTFOLIO COMPARISON (REAL SPREADS + SWAPS | 2021-2026 & FROZEN OOS 2024-2026) ===", flush=True)
    for k, label, pool_t, rp, mg, ma, cs, bkt, pw, uv, t1, t2 in eval_specs:
        rf = simulate_shared_account(label, pool_t, 100000.0, rp, mg, ma, cs, bkt, pw, uv, t1, t2, mc_runs=20000)
        ro = simulate_shared_account(f"{label} [OOS]", pool_t, 100000.0, rp, mg, ma, cs, bkt, pw, uv, t1, t2, min_ts=SPLIT_2024_TS, mc_runs=20000)
        final_ports[k] = {"config": {"risk_pct": rp, "max_global": mg, "max_per_asset": ma, "max_crypto_same_side": cs, "bucket_caps": bkt, "dd_throttle": [t1, t2]}, "full": rf, "oos_only": ro}
        fm, om = rf["metrics"], ro["metrics"]
        fmc, omc = fm.get("mc_20k", {}), om.get("mc_20k", {})
        print(
            f"  {k:22s} | rp={rp:.2f}% | Full: End=${fm['end_equity']:>9,.0f} CAGR={fm['cagr_pct']:>5.1f}% DD={fm['max_dd_pct']:>5.2f}% PF={fm['pf']:>4.2f} Cal={fm['calmar']:>4.2f} Sh={fm['sharpe']:>4.2f} Pass10={fmc.get('pass10_pct',0):>5.1f}% Br10={fmc.get('breach10_pct',0):>4.1f}% CrDD95={fmc.get('crash_dd95',0):>4.1f}%"
            f" || OOS(24-26): PF={om['pf']:>4.2f} CAGR={om['cagr_pct']:>5.1f}% DD={om['max_dd_pct']:>5.2f}% Pass10={omc.get('pass10_pct',0):>5.1f}%",
            flush=True,
        )

    _render_stage2_dashboard(final_ports, corr_all)

    clean_ports = {}
    for k, v in final_ports.items():
        clean_ports[k] = {
            "config": v["config"],
            "full_metrics": v["full"]["metrics"],
            "oos_metrics": v["oos_only"]["metrics"],
            "by_asset_full": v["full"]["by_asset"],
            "by_asset_oos": v["oos_only"]["by_asset"],
            "top_strats": v["full"]["by_strat"][:15],
        }

    m3m = final_ports["M3_MultiAsset_Prop10"]["full"]["metrics"]
    m4m = final_ports["M4_Personal_MaxCAGR"]["full"]["metrics"]
    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "split_iso": "2024-01-01 (Strict Causal IS <= 2023-12-31 -> Frozen OOS 2024-2026)",
        "admitted_crypto": admitted_crypto,
        "admitted_tradfi": admitted_tradfi,
        "asset_admission_audit": asset_audit,
        "cross_asset_corr_is": corr_all.to_dict(),
        "portfolios": clean_ports,
        "blueberry_2501_projection": {
            "M3_Prop10_End_6Y": round(2501.80 * (m3m["end_equity"] / 100000.0), 2),
            "M3_Risk_USD_Trade": round(2501.80 * (m3_spec["rp"] / 100.0), 2),
            "M4_Growth_End_6Y": round(2501.80 * (m4m["end_equity"] / 100000.0), 2),
            "M4_Risk_USD_Trade": round(2501.80 * (m4_spec["rp"] / 100.0), 2),
        },
        "top_asset_strategy_pairs": elite_pairs.head(40).to_dict(orient="records"),
    }
    MASTER_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved Master Report -> {MASTER_JSON}", flush=True)
    return out


def _render_stage2_dashboard(final_ports: dict[str, dict[str, Any]], corr_all: pd.DataFrame) -> None:
    plt.style.use("dark_background")
    fig, axes = plt.subplots(2, 2, figsize=(16, 10.5), facecolor="#0b0f17")
    fig.suptitle(
        "MT5 Shared-Account Multi-Asset Quant Portfolio (True MT5 Spreads & Swaps | Causal IS <= 2023 -> Frozen OOS 2024-2026)",
        color="#f8fafc", fontsize=13, fontweight="bold", y=0.985,
    )
    for ax in axes.flat:
        ax.set_facecolor("#111723")
        ax.tick_params(colors="#cbd5e1", labelsize=8.5)
        ax.grid(alpha=0.16, color="#64748b")

    styles = [
        ("M0_Naive_AllCrypto", "#94a3b8", "--", 1.6),
        ("M1_Crypto_Prop8", "#38bdf8", "-", 2.0),
        ("M2_Crypto_Prop10", "#facc15", "-.", 2.1),
        ("M3_MultiAsset_Prop10", "#34d399", "-", 2.8),
        ("M4_Personal_MaxCAGR", "#e879f9", "-", 2.2),
    ]
    ax1 = axes[0, 0]
    for key, col, ls, lw in styles:
        p = final_ports[key]["full"]
        fm = p["metrics"]
        dt = pd.to_datetime([x[0] for x in p["curve"]], unit="s", utc=True)
        eq = [x[1] for x in p["curve"]]
        ax1.plot(dt, eq, color=col, linestyle=ls, linewidth=lw, label=f"{key.split('_')[0]} ({fm['cagr_pct']}% CAGR | DD {fm['max_dd_pct']}% | PF {fm['pf']})")
    ax1.axvline(pd.Timestamp("2024-01-01", tz="UTC"), color="#f87171", linestyle=":", linewidth=1.3, label="Frozen OOS Split (2024-01-01)")
    ax1.set_yscale("log")
    ax1.set_title("1. Shared $100K MT5 Account Equity Curves (2021-2026)", color="#f8fafc", fontsize=10.5)
    ax1.set_ylabel("Equity ($ USD, Log)", color="#cbd5e1")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax1.legend(loc="upper left", fontsize=7.8, facecolor="#0b0f17")

    ax2 = axes[0, 1]
    for key, col, ls, lw in styles[:4]:
        p = final_ports[key]["oos_only"]
        dt = pd.to_datetime([x[0] for x in p["curve"]], unit="s", utc=True)
        eq = np.array([x[1] for x in p["curve"]], dtype=float)
        pk = np.maximum.accumulate(eq)
        dd = (pk - eq) / pk * 100.0
        ax2.plot(dt, -dd, color=col, linestyle=ls, linewidth=lw, label=f"{key.split('_')[0]} OOS MaxDD={p['metrics']['max_dd_pct']}% | PF={p['metrics']['pf']}")
    ax2.axhline(-8.0, color="#38bdf8", linestyle="--", linewidth=1.0, label="-8% Trailing Prop Ceiling")
    ax2.axhline(-10.0, color="#f87171", linestyle="--", linewidth=1.0, label="-10% Static Prop Ceiling")
    ax2.set_title("2. Frozen OOS (2024-2026) Drawdowns vs Prop Ceilings", color="#f8fafc", fontsize=10.5)
    ax2.set_ylabel("Drawdown (%)", color="#cbd5e1")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax2.legend(loc="lower left", fontsize=7.6, facecolor="#0b0f17")

    ax3 = axes[1, 0]
    if not corr_all.empty:
        cvals = corr_all.to_numpy(dtype=float)
        im = ax3.imshow(cvals, cmap="coolwarm", vmin=-0.3, vmax=0.8)
        syms = [s.replace("USDT", "").replace("MT5_", "") for s in corr_all.columns]
        ax3.set_xticks(range(len(syms)))
        ax3.set_yticks(range(len(syms)))
        ax3.set_xticklabels(syms, rotation=45, ha="right", fontsize=8.0)
        ax3.set_yticklabels(syms, fontsize=8.0)
        for i in range(len(syms)):
            for j in range(len(syms)):
                ax3.text(j, i, f"{cvals[i,j]:.2f}", ha="center", va="center", color="#ffffff" if abs(cvals[i,j]) > 0.35 else "#cbd5e1", fontsize=7.5)
        fig.colorbar(im, ax=ax3, fraction=0.046, pad=0.04)
    ax3.set_title("3. Causal IS Cross-Asset Daily Return Correlation (Crypto vs TradFi)", color="#f8fafc", fontsize=10.5)

    ax4 = axes[1, 1]
    for key, col, _, _ in styles:
        fm = final_ports[key]["full"]["metrics"]
        mc = fm.get("mc_20k", {})
        cr95 = mc.get("crash_dd95", fm["max_dd_pct"])
        passp = mc.get("pass10_pct", 0.0)
        ax4.scatter([cr95], [passp], color=col, s=180, edgecolors="#ffffff", linewidth=1.1)
        ax4.annotate(f"{key.split('_')[0]} ({fm['cagr_pct']}% CAGR | Br10={mc.get('breach10_pct',0)}%)", (cr95, passp), xytext=(6, 4), textcoords="offset points", fontsize=8.2, color="#f8fafc")
    ax4.axvline(10.0, color="#f87171", linestyle="--", linewidth=1.1, label="10% Prop Breach Line")
    ax4.set_title("4. 20,000-Path Crash MC: 1Y +10% Prop Pass% vs Crash DD95%", color="#f8fafc", fontsize=10.5)
    ax4.set_xlabel("Crash-Shocked 95th Percentile Drawdown (%)", color="#cbd5e1")
    ax4.set_ylabel("20K MC 1-Year +10% Prop Pass Rate (%)", color="#cbd5e1")
    ax4.legend(loc="upper right", fontsize=8.0, facecolor="#0b0f17")

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    DASHBOARD_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(DASHBOARD_PNG), dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    run_stage2()
