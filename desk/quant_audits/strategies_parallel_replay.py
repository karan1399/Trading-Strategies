"""Parallel shared-account simulator across all 94 modular recipes in strategies/s01..s16.

Evaluates the entire strategies/ directory running together on ONE shared account:
  - Prop / Crypto Perpetual fractional-lot modes ($5,000 & $2,501.80) across
    Core-45 (verified), Training-Screened-62 (pre-2024 causal), All-94, and 1-Crypto gate.
  - Blueberry MT5 #5126976 contract-constrained modes ($2,501.80) using live MT5
    SymbolSpecs, volume_min/vol_step rounding via lots_for_risk(), $25 (1% cap) vs $6.00
    desk cap, and live MT5 bid-ask spread stress.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from desk.quant_audits import r048_verify as rv
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk, spec_from_mt5
from exchange_feed.backtesting.fast_kernels import sim_1m_trades_nb
from exchange_feed.backtesting.mt5_shared_account_portfolio_optimizer import simulate_shared_account
from strategies.s11_of_initiative_breakout.strategy import OrderFlowInitiativeBreakoutStrategy


STRATEGIES_DIR = ROOT / "strategies"
DATA_DIR = ROOT / "exchange_feed" / "data"
DEFAULT_OUT = Path(__file__).resolve().with_name("strategies_parallel_results.json")

TS_2024 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
TS_2025 = int(pd.Timestamp("2025-01-23", tz="UTC").timestamp())

ASSET_TO_MT5 = {
    "AAVE": "AVEUSD.p",
    "ADA": "ADAUSD.p",
    "AVAX": "AVXUSD.p",
    "BCH": "BCHUSD.p",
    "BNB": "BNBUSD.p",
    "BTC": "BTCUSD.pi",
    "CL": "USWTI.p",
    "DASH": "DSHUSD.p",
    "ETH": "ETHUSD.pi",
    "GRT": "GRTUSD.p",
    "LINK": "LNKUSD.p",
    "LTC": "LTCUSD.pi",
    "SOL": "SOLUSD.p",
    "UNI": "UNIUSD.p",
    "VET": "VETUSD.p",
    "XAG": "XAGUSD.pi",
    "XAU": "GAUUSD.pi",
    "XLM": "XLMUSD.p",
    "XPD": "XPDUSD.pi",
    "YFI": "YFIUSD.p",
    "ZEC": "ZECUSD.p",
}


def load_all_strategy_recipes() -> list[dict[str, Any]]:
    """Discover and parse all <ASSET>.json recipes inside strategies/s01..s16."""
    items: list[dict[str, Any]] = []
    for folder in sorted(STRATEGIES_DIR.iterdir()):
        if not folder.is_dir() or not folder.name.startswith("s"):
            continue
        for jf in sorted(folder.glob("*.json")):
            if jf.name == "config.json":
                continue
            d = json.loads(jf.read_text(encoding="utf-8"))
            d["_folder"] = folder.name
            d["_asset"] = jf.stem
            items.append(d)
    return items


def _replay_s11_d1_trades(recipe: dict[str, Any], mutual: dict[str, Any]) -> list[dict[str, Any]]:
    """Fresh 1m-exact causal replay for the 3 D1-gated s11 Initiative Breakout recipes."""
    sym = recipe["symbol"]
    g4 = rv.cached(sym, "4H")
    m1_df = pd.read_parquet(DATA_DIR / "cache" / f"{sym}_m1.parquet", columns=["bar_1m", "o", "h", "l", "c"])
    m1 = tuple(np.ascontiguousarray(m1_df[k].to_numpy()) for k in ("bar_1m", "o", "h", "l", "c"))
    bars = rv._df_to_bars(g4)
    arr = rv._prep_arrs(g4)
    p = recipe["optimal_parameters"]
    strat = OrderFlowInitiativeBreakoutStrategy()
    raw = strat.generate_signals(bars, dict(p, d1_ema_trend_gate=True))
    side_arr = np.zeros(len(g4), dtype=np.int8)
    for sig in raw:
        side_arr[sig.index] = sig.side
    res = sim_1m_trades_nb(
        *arr, side_arr, *m1, 14400, p["sl_atr"], p["tp_r"], p["trail_atr"],
        p["be_r"], 0.05, p["step_lock"], p["max_hold_bars"], 1, p["dyn_atr"], True,
        mutual[sym]["sp_bps"], False, 0.0, 0.0, 5, -2.5, -1.5,
        np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64),
    )
    end_ts_a = g4["end_ts"].to_numpy(dtype=np.int64)
    atr_a = g4["atr"].to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []
    for k in range(len(res[1])):
        tin = int(res[1][k])
        tout = int(res[2][k])
        sd = "BUY" if int(res[3][k]) == 1 else "SELL"
        bi = int(np.searchsorted(end_ts_a, tin, side="right")) - 1
        atr_val = float(atr_a[max(0, bi)])
        rows.append({
            "symbol": sym,
            "mt5_symbol": ASSET_TO_MT5[recipe["_asset"]],
            "category": recipe.get("category", "CRYPTO"),
            "sleeve": "s11_of_initiative_breakout",
            "bucket": "BREAKOUT",
            "tf": "4H",
            "side": sd,
            "ts_in": tin,
            "ts_out": tout,
            "exit_available_ts": tout + 60,
            "entry": float(res[5][k]),
            "r": float(res[7][k]),
            "vof_mult": 1.0,
            "mt5_cost_bps": float(mutual[sym]["sp_bps"]),
            "atr": atr_val,
            "sl_atr": float(p["sl_atr"]),
            "sl_dist": float(p["sl_atr"]) * atr_val,
            "weight": float(p.get("weight", 1.0)),
            "folder": recipe["_folder"],
            "asset": recipe["_asset"],
            "entry_id": f"{recipe['_folder']}:{recipe['_asset']}:{tin}",
        })
    return rows


def build_parallel_trade_pools() -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Build causal trade pools for Core-45, Screened-62, and All-94 strategy recipes."""
    recipes = load_all_strategy_recipes()
    mutual = rv.read(DATA_DIR / "mt5_mutual_specs.json")

    prop_cache: dict[str, pd.DataFrame] = {}
    mt5_cache: dict[str, pd.DataFrame] = {}
    bar_cache: dict[tuple[str, str], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    def _get_bars(sym: str, tf: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        key = (sym, tf.upper())
        if key not in bar_cache:
            g = rv.cached(sym, "2H" if key[1] == "2H" else "4H")
            bar_cache[key] = (
                g["ts"].to_numpy(dtype=np.int64),
                g["end_ts"].to_numpy(dtype=np.int64),
                g["atr"].to_numpy(dtype=float),
            )
        return bar_cache[key]

    pools: dict[str, list[dict[str, Any]]] = {"core45": [], "screened62": [], "all94": []}
    recipe_meta: list[dict[str, Any]] = []

    for rec in recipes:
        sym = rec["symbol"]
        ast = rec["_asset"]
        folder = rec["_folder"]
        strat = rec["strategy"]
        tf = rec["timeframe"]
        cfg_s = rec["parameter_string"]
        study = (rec.get("performance_audit") or {}).get("source_study", "?")
        sl_atr = float(rec["optimal_parameters"].get("sl_atr", 2.5))
        weight = float(rec["optimal_parameters"].get("weight", 1.0))

        # Load matching ledger rows cleanly (exact provenance match or fresh s11 D1 replay)
        rows: list[dict[str, Any]] = []
        if folder == "s11_of_initiative_breakout" and "d1_ema_gate=true" in cfg_s:
            rows = _replay_s11_d1_trades(rec, mutual)
            source_used = "fresh_s11_d1_replay"
        else:
            order = ("mt5_portfolio", "prop_26_5k") if study == "mt5_portfolio_stage_a" else ("prop_26_5k", "mt5_portfolio")
            df_match = pd.DataFrame()
            source_used = "none"
            for src in order:
                cache = mt5_cache if src == "mt5_portfolio" else prop_cache
                if sym not in cache:
                    fp = DATA_DIR / src / "trades" / f"{sym}.parquet"
                    cache[sym] = pd.read_parquet(fp) if fp.is_file() else pd.DataFrame()
                sub = rv.exact(cache[sym], strat, tf, cfg_s)
                if not sub.empty:
                    df_match = sub
                    source_used = src
                    break

            if not df_match.empty:
                _, end_a, atr_a = _get_bars(sym, tf)
                for rec_row in rv.decorate(df_match):
                    tin = int(rec_row["ts_in"])
                    bi_raw = rec_row.get("bar_i")
                    if bi_raw is not None and pd.notna(bi_raw) and 0 <= int(bi_raw) < len(atr_a):
                        atr_val = float(atr_a[int(bi_raw)])
                    else:
                        idx = max(0, min(len(atr_a) - 1, int(np.searchsorted(end_a, tin, side="right")) - 1))
                        atr_val = float(atr_a[idx])
                    r_item = dict(rec_row)
                    r_item["sleeve"] = folder
                    r_item["folder"] = folder
                    r_item["asset"] = ast
                    r_item["mt5_symbol"] = ASSET_TO_MT5[ast]
                    r_item["sl_atr"] = sl_atr
                    r_item["atr"] = atr_val
                    r_item["sl_dist"] = sl_atr * atr_val
                    r_item["weight"] = weight
                    r_item["entry_id"] = f"{folder}:{ast}:{tin}:{rec_row.get('side')}"
                    rows.append(r_item)

        is_sum = rv.stats(rows, 0, TS_2024)
        oos_sum = rv.stats(rows, TS_2024, rv.END)
        pass_is = rv.is_selected(is_sum)
        is_core = rec.get("status") == "PROFITABLE_VERIFIED"

        recipe_meta.append({
            "folder": folder,
            "asset": ast,
            "mt5_symbol": ASSET_TO_MT5[ast],
            "category": rec.get("category", "CRYPTO"),
            "tf": tf,
            "status": rec.get("status"),
            "source_used": source_used,
            "total_trades": len(rows),
            "is_n": is_sum["n"],
            "is_r": is_sum["r"],
            "is_pf": is_sum["pf"],
            "pass_pre2024_screen": pass_is,
            "oos_n": oos_sum["n"],
            "oos_r": oos_sum["r"],
            "oos_pf": oos_sum["pf"],
        })

        pools["all94"].extend(rows)
        if is_core:
            pools["core45"].extend(rows)
        if pass_is:
            pools["screened62"].extend(rows)

    return pools, recipe_meta


def fetch_mt5_specs_and_spreads() -> tuple[dict[str, SymbolSpec], dict[str, float], float]:
    """Fetch live Blueberry MT5 SymbolSpecs + live spread_bps for all 21 assets."""
    import MetaTrader5 as mt5

    specs: dict[str, SymbolSpec] = {}
    spread_bps: dict[str, float] = {}
    gau_xau_ratio = 1.0 / 31.1035
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        xau_inf = mt5.symbol_info("XAUUSD.pi")
        gau_inf = mt5.symbol_info("GAUUSD.pi")
        if xau_inf and gau_inf and xau_inf.bid > 0 and gau_inf.bid > 0:
            gau_xau_ratio = float(gau_inf.bid) / float(xau_inf.bid)

        for ms in sorted(set(ASSET_TO_MT5.values())):
            mt5.symbol_select(ms, True)
            sp = spec_from_mt5(ms)
            inf = mt5.symbol_info(ms)
            if sp is not None and inf is not None:
                specs[ms] = sp
                mid = 0.5 * (float(inf.bid) + float(inf.ask))
                if mid > 0:
                    spread_bps[ms] = ((float(inf.ask) - float(inf.bid)) / mid) * 10000.0
    finally:
        mt5.shutdown()
    return specs, spread_bps, gau_xau_ratio


def make_mt5_sizer(
    specs: dict[str, SymbolSpec],
    gau_xau_ratio: float,
    *,
    hard_cap_usd: float | None = None,
    cap_pct_of_equity: float | None = None,
    reject_stats: dict[str, dict[str, int]] | None = None,
):
    """Return a size_risk_usd adapter for simulate_shared_account enforcing real MT5 lot grid."""
    stats_map = reject_stats if reject_stats is not None else {}

    def _sizer(row: dict[str, Any], reserved_risk: float, equity: float) -> tuple[float, float] | None:
        ms = str(row["mt5_symbol"])
        spec = specs.get(ms)
        sl_dist = float(row.get("sl_dist", 0.0) or 0.0)
        ast = str(row.get("asset", ""))
        if spec is None or sl_dist <= 0:
            d = stats_map.setdefault(ms, {"pass": 0, "reject": 0})
            d["reject"] += 1
            return None

        eff_sl = sl_dist * gau_xau_ratio if ast == "XAU" else sl_dist
        if hard_cap_usd is not None:
            cap = float(hard_cap_usd)
        elif cap_pct_of_equity is not None:
            cap = float(equity) * float(cap_pct_of_equity) / 100.0
        else:
            cap = float(reserved_risk)

        lots, _ = lots_for_risk(spec, eff_sl, reserved_risk, cap)
        if lots is None:
            d = stats_map.setdefault(ms, {"pass": 0, "reject": 0})
            d["reject"] += 1
            return None

        d = stats_map.setdefault(ms, {"pass": 0, "reject": 0})
        d["pass"] += 1
        actual = eff_sl * lots * spec.value_per_price_unit()
        return (actual, actual)

    return _sizer


def _apply_live_spread_delta(pool: list[dict[str, Any]], spread_bps: dict[str, float]) -> list[dict[str, Any]]:
    """Return copy of pool with excess MT5 live bid-ask spread subtracted from R."""
    stressed: list[dict[str, Any]] = []
    for row in pool:
        r2 = dict(row)
        ms = str(row["mt5_symbol"])
        saved_bp = float(row.get("mt5_cost_bps", 0.0) or 0.0)
        live_bp = float(spread_bps.get(ms, saved_bp))
        entry_px = float(row.get("entry", 0.0) or 0.0)
        sl_dist = float(row.get("sl_dist", 0.0) or 0.0)
        if live_bp > saved_bp and entry_px > 0 and sl_dist > 0:
            delta_r = ((live_bp - saved_bp) / 10000.0) * entry_px / sl_dist
            r2["r"] = float(row["r"]) - delta_r
        stressed.append(r2)
    return stressed


def run_parallel_audit(out_path: Path = DEFAULT_OUT) -> dict[str, Any]:
    pools, recipe_meta = build_parallel_trade_pools()
    specs, spread_bps, gau_xau_ratio = fetch_mt5_specs_and_spreads()

    stressed_all94 = _apply_live_spread_delta(pools["all94"], spread_bps)
    stressed_scr62 = _apply_live_spread_delta(pools["screened62"], spread_bps)

    def _run_pair(
        key: str,
        label: str,
        pool: list[dict[str, Any]],
        eq0: float,
        rp: float,
        mg: int,
        ma: int,
        cs: int,
        max_crypto: int | None,
        max_heat: float | None,
        sizer_kw: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rej_full: dict[str, dict[str, int]] = {}
        rej_oos: dict[str, dict[str, int]] = {}
        rej_25: dict[str, dict[str, int]] = {}
        sz_f = make_mt5_sizer(specs, gau_xau_ratio, reject_stats=rej_full, **sizer_kw) if sizer_kw else None
        sz_o = make_mt5_sizer(specs, gau_xau_ratio, reject_stats=rej_oos, **sizer_kw) if sizer_kw else None
        sz_25 = make_mt5_sizer(specs, gau_xau_ratio, reject_stats=rej_25, **sizer_kw) if sizer_kw else None

        res_f = simulate_shared_account(
            label, pool, eq0, rp, mg, ma, cs, None, None, True, 4.5, 6.5,
            min_ts=None, mc_runs=20000, size_risk_usd=sz_f,
            max_crypto_total=max_crypto, max_open_risk_pct=max_heat,
        )
        res_o = simulate_shared_account(
            f"{label} [OOS 2024-2026]", pool, eq0, rp, mg, ma, cs, None, None, True, 4.5, 6.5,
            min_ts=TS_2024, mc_runs=20000, size_risk_usd=sz_o,
            max_crypto_total=max_crypto, max_open_risk_pct=max_heat,
        )
        res_25 = simulate_shared_account(
            f"{label} [2025-2026]", pool, eq0, rp, mg, ma, cs, None, None, True, 4.5, 6.5,
            min_ts=TS_2025, mc_runs=20000, size_risk_usd=sz_25,
            max_crypto_total=max_crypto, max_open_risk_pct=max_heat,
        )
        fm, om, r25 = res_f["metrics"], res_o["metrics"], res_25["metrics"]
        fmc = fm.get("mc_20k", {})
        print(
            f"{key:26s} | Full: End=${fm['end_equity']:>10,.2f} CAGR={fm['cagr_pct']:>5.1f}% DD={fm['max_dd_pct']:>5.2f}% PF={fm['pf']:>4.2f} Tr={fm['trades']:>4d} Pass10={fmc.get('pass10_pct',0):>5.1f}%"
            f" || OOS(24-26): End=${om['end_equity']:>9,.2f} CAGR={om['cagr_pct']:>5.1f}% DD={om['max_dd_pct']:>5.2f}% PF={om['pf']:>4.2f} Tr={om['trades']:>4d}",
            flush=True,
        )
        return {
            "label": label,
            "start_equity": eq0,
            "risk_pct": rp,
            "max_global": mg,
            "max_per_asset": ma,
            "max_crypto_total": max_crypto,
            "max_open_risk_pct": max_heat,
            "full": {**res_f, "mt5_lot_stats": rej_full},
            "oos_2024_2026": {**res_o, "mt5_lot_stats": rej_oos},
            "recent_2025_2026": {**res_25, "mt5_lot_stats": rej_25},
        }

    scenarios = {
        "P1_Core45_5K_Prop": _run_pair("P1_Core45_5K_Prop", "45 Verified Core Recipes ($5K Fractional | 6 Slots)", pools["core45"], 5000.0, 0.45, 6, 2, 3, None, None),
        "P2_Screened62_5K_Prop": _run_pair("P2_Screened62_5K_Prop", "62 Pre-2024 Screened Recipes ($5K Fractional | 6 Slots)", pools["screened62"], 5000.0, 0.45, 6, 2, 3, None, None),
        "P3_All94_5K_Prop": _run_pair("P3_All94_5K_Prop", "All 94 s01..s16 Recipes ($5K Fractional | 6 Slots)", pools["all94"], 5000.0, 0.45, 6, 2, 3, None, None),
        "P4_All94_5K_OneCrypto": _run_pair("P4_All94_5K_OneCrypto", "All 94 s01..s16 ($5K Fractional | 1-Crypto Farm Gate)", pools["all94"], 5000.0, 0.45, 2, 1, 1, 1, None),
        "M1_All94_2501_Fractional": _run_pair("M1_All94_2501_Fractional", "All 94 s01..s16 ($2,501.80 Fractional Baseline | 6 Slots)", pools["all94"], 2501.80, 0.45, 6, 2, 3, None, None),
        "M2_All94_2501_MT5_1PctCap": _run_pair("M2_All94_2501_MT5_1PctCap", "All 94 ($2,501.80 Live MT5 Lots | 1%=$25 Cap, 6 Slots)", stressed_all94, 2501.80, 0.80, 6, 2, 3, None, 3.0, {"cap_pct_of_equity": 1.0}),
        "M3_All94_2501_MT5_Desk6": _run_pair("M3_All94_2501_MT5_Desk6", "All 94 ($2,501.80 Live MT5 Lots | $6 Cap, 2 Slots, 1 Crypto)", stressed_all94, 2501.80, 0.80, 2, 1, 1, 1, 1.0, {"hard_cap_usd": 6.0}),
        "M4_Screen62_2501_MT5_Desk6": _run_pair("M4_Screen62_2501_MT5_Desk6", "62 IS-Screened ($2,501.80 Live MT5 Lots | $6 Cap, 2 Slots)", stressed_scr62, 2501.80, 0.80, 2, 1, 1, 1, 1.0, {"hard_cap_usd": 6.0}),
    }

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "recipe_count": len(recipe_meta),
        "screened62_count": sum(1 for r in recipe_meta if r["pass_pre2024_screen"]),
        "core45_count": sum(1 for r in recipe_meta if r["status"] == "PROFITABLE_VERIFIED"),
        "gau_xau_ratio": gau_xau_ratio,
        "mt5_spread_bps": spread_bps,
        "recipes": recipe_meta,
        "scenarios": scenarios,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved strategies parallel results -> {out_path}", flush=True)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulate all 94 s01..s16 strategies running in parallel on one shared account.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    run_parallel_audit(args.out)


if __name__ == "__main__":
    main()
