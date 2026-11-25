#!/usr/bin/env python3
"""Per-asset AEC-Trend optimization over the top-20 Binance<->MT5 universe.

For each asset: run the 15m maker grid (448 scenarios, TP in {2R, 3R, 5R} or pure
EMA-ride), select the best config IN-SAMPLE ONLY with hard constraints:
  - IS trades >= 30 (regime-conditional families need a minimum sample)
  - exit MUST be a TP variant with tp_r >= 2  (user requirement: minimum 2R per trade)
Then report the untouched OOS stats + R-multiple profile. Assets with no qualifying
IS config are reported as such (honest null).

Outputs: research/aec_per_asset.json, research/aec_per_asset_summary.csv,
         research/aec_asset_overrides.json (engine-ready per-asset params).
"""
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
from aec_backtest import DATA, build_grid, simulate_asset, summarize

OUT = "/home/z/my-project/binance_orderflow/data/research"
MIN_IS_N = 30
MIN_OOS_N = 8
SYMBOL_MAP = {  # Binance USDT-M -> this broker's MT5 CFD ticker (verified in MT5 historical store)
    "BTCUSDT": "BTCUSD.pi", "ETHUSDT": "ETHUSD.pi", "SOLUSDT": "SOLUSD.p",
    "XRPUSDT": "XRPUSD.pi", "BNBUSDT": "BNBUSD.p", "DOGEUSDT": "DOGUSD.p",
    "LINKUSDT": "LNKUSD.p", "AVAXUSDT": "AVXUSD.p", "LTCUSDT": "LTCUSD.pi",
    "TRXUSDT": "TRXUSD.p", "DOTUSDT": "DOTUSD.pi", "BCHUSDT": "BCHUSD.p",
    "XLMUSDT": "XLMUSD.p", "UNIUSDT": "UNIUSD.p", "ATOMUSDT": "ATMUSD.p",
    "FILUSDT": "FILUSD.p", "NEARUSDT": "NERUSD.p", "XMRUSDT": "XMRUSD.p",
    "ALGOUSDT": "ALGUSD.p", "VETUSDT": "VETUSD.p",
}


def r_profile(tr: pd.DataFrame) -> dict:
    """R-multiple profile from risk_bps."""
    if len(tr) == 0:
        return {}
    r = (tr["ret_bps"] / tr["risk_bps"].replace(0, np.nan)).dropna()
    wins = r[r > 0]
    return {"avg_R": round(float(r.mean()), 3), "med_R": round(float(r.median()), 3),
            "avg_win_R": round(float(wins.mean()), 3) if len(wins) else None,
            "R_p5": round(float(np.percentile(r, 5)), 3),
            "R_p95": round(float(np.percentile(r, 95)), 3)}


def main():
    dsf = sys.argv[1] if len(sys.argv) > 1 else "research_15m.parquet"
    ds = pd.read_parquet(f"{DATA}/{dsf}")
    split_ts = int(pd.Timestamp("2026-11-08", tz="UTC").value // 10 ** 6)
    grid = build_grid()
    tp_pool = {i for i, g in enumerate(grid) if g["exit"] != "ema50"}   # 2R-min pool
    results, summary, overrides = {}, [], {}
    for sym in sorted(ds["symbol"].unique()):
        d = ds[ds["symbol"] == sym].sort_values("ts").reset_index(drop=True)
        tr = simulate_asset(d, grid, split_ts)
        if len(tr) == 0:
            summary.append({"symbol": sym, "verdict": "no trades"}); continue
        tr.to_parquet(f"{OUT}/pa_trades_{sym}.parquet", index=False)
        rows = []
        for sid, grp in tr.groupby("scen"):
            if sid not in tp_pool:
                continue
            s_is, s_oos = summarize(grp[~grp["oos"]]), summarize(grp[grp["oos"]])
            if s_is.get("n", 0) < MIN_IS_N:
                continue
            rows.append({"scen": int(sid), **grid[sid], "IS": s_is, "OOS": s_oos,
                         "IS_R": r_profile(grp[~grp["oos"]]), "OOS_R": r_profile(grp[grp["oos"]])})
        if not rows:
            summary.append({"symbol": sym, "verdict": "no qualifying config (IS n<%d in 2R pool)" % MIN_IS_N})
            results[sym] = {"verdict": "null"}
            continue
        rows.sort(key=lambda r: (r["IS"]["tstat"]), reverse=True)
        best = rows[0]
        qualifies = best["OOS"].get("n", 0) >= MIN_OOS_N and best["OOS"]["avg_bps"] > 0
        results[sym] = {"best": best, "qualifies": bool(qualifies),
                        "top3": rows[:3]}
        summary.append({"symbol": sym, "verdict": "OK" if qualifies else "OOS-negative",
                        "regime": best["regime"], "entry": best["entry"], "ac": best["ac"],
                        "stop_mult": best["stop_mult"], "tp_r": best["exit"].replace("tp", "").replace("r_ema50", ""),
                        "side": best["side"], "IS_n": best["IS"]["n"], "IS_t": best["IS"]["tstat"],
                        "OOS_n": best["OOS"]["n"], "OOS_avg_bps": best["OOS"]["avg_bps"],
                        "OOS_pf": best["OOS"]["pf"], "OOS_avg_R": best["OOS_R"].get("avg_R"),
                        "OOS_avg_win_R": best["OOS_R"].get("avg_win_R")})
        if qualifies and sym in SYMBOL_MAP:
            overrides[sym] = {
                "mt5_symbol": SYMBOL_MAP.get(sym, sym.replace("USDT", "USD")),
                "regime": best["regime"], "entry": best["entry"], "ac_trigger": best["ac"],
                "stop_mult": float(best["stop_mult"]), "tp_r": float(best["exit"].replace("tp", "").replace("r_ema50", "")),
                "side": best["side"], "source": "aec_per_asset IS-selection, OOS-verified"}
        print(f"{sym:14s} {summary[-1]['verdict']:12s} IS_t={best['IS']['tstat']:+.2f} "
              f"OOS n={best['OOS']['n']:4d} avg={best['OOS']['avg_bps']:+7.1f}bps "
              f"pf={best['OOS']['pf']:.2f} avgR={best['OOS_R'].get('avg_R')}")
    pd.DataFrame(summary).to_csv(f"{OUT}/aec_per_asset_summary.csv", index=False)
    with open(f"{OUT}/aec_per_asset.json", "w") as f:
        json.dump(results, f, indent=1, default=str)
    with open(f"{OUT}/aec_asset_overrides.json", "w") as f:
        json.dump({"universe": SYMBOL_MAP, "asset_overrides": overrides}, f, indent=1)
    ok = sum(1 for s in summary if s.get("verdict") == "OK")
    print(f"\n{ok}/{len(summary)} assets with OOS-verified configs; "
          f"{len(overrides)} overrides written -> aec_asset_overrides.json")


if __name__ == "__main__":
    main()
