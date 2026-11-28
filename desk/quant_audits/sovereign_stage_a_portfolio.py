"""Broker-lot portfolio screen of every freshly refit Stage A recipe."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from desk.quant_audits.sovereign_engine_step_replay import EQUITY, local_fp, utc
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk
from exchange_feed.backtesting.mt5_shared_account_portfolio_optimizer import simulate_shared_account


FIT = utc("2024-01-01")
CHECK = utc("2025-01-01")
FINAL = utc("2026-01-01")
STOP = utc("2026-11-21")


def instrument_map():
    spec_path = Path("exchange_feed/data/mt5_mutual_specs.json")
    specs = json.loads(spec_path.read_text(encoding="utf-8"))
    usable = {}
    for symbol, meta in specs.items():
        if not meta.get("mt5_available"):
            continue
        fields = (meta.get(k) for k in ("tick_size", "tick_value", "vol_min", "vol_step"))
        tick_size, tick_value, minimum, step = (float(v or 0.) for v in fields)
        if min(tick_size, tick_value, minimum, step) > 0.:
            usable[symbol] = SymbolSpec(meta["mt5"], tick_size=tick_size, tick_value=tick_value,
                                        vol_min=minimum, vol_step=step)
    return usable


def sized_risk(row, requested, equity):
    cap = min(equity * .0025, 6.)
    lots, _ = lots_for_risk(row["instrument"], row["stress_dist"], requested, cap)
    if lots is None:
        return None
    unit = lots * row["instrument"].value_per_price_unit()
    return unit * row["sl_dist"], unit * row["stress_dist"]


def build_bank(refit_dir, surcharge):
    specs = instrument_map()
    bank = []
    evidence = Counter()
    for path in sorted((refit_dir / "configs").glob("*.json")):
        cfg = json.loads(path.read_text(encoding="utf-8"))
        assert cfg["fit_asof_ts"] == FIT, path
        scores = cfg["strategy_scorecard"]
        evidence["fitted_cells"] += len(scores)
        if path.stem not in specs:
            evidence["cells_missing_spec"] += len(scores)
            continue
        frame = pd.read_parquet(refit_dir / "trades" / f"{path.stem}.parquet")
        timeframes = {}
        for tf in frame["tf"].unique():
            bars = local_fp(path.stem, tf.lower())
            timeframes[tf] = (bars["end_ts"].to_numpy(np.int64), bars["atr"].to_numpy(float),
                              bars["c"].to_numpy(float))
        for s in scores:
            rows = frame[frame["sleeve"] == s["sid"]].sort_values("ts_in")
            if rows.empty:
                continue
            end, atr, price = timeframes[s["tf"]]
            tape = []
            for row in rows.to_dict("records"):
                i = int(np.searchsorted(end, row["ts_in"], side="right")) - 1
                if i < 0 or not (0 <= row["ts_in"] - end[i] <= 300):
                    continue
                dist = atr[i] * float(s["sl"])
                if not all(math.isfinite(v) and v > 0 for v in (dist, price[i])):
                    continue
                extra_r = surcharge * price[i] / (10_000 * dist)
                row.update(sl_dist=dist, stress_dist=1.2 * dist + price[i] * .001, cost_surcharge_r=extra_r,
                           instrument=specs[path.stem], r=row["r"] - extra_r)
                tape.append(row)
            training = np.array([row["r"] for row in tape if row["exit_available_ts"] < FIT
                                 and sized_risk(row, EQUITY*.0025, EQUITY) is not None])
            wins, losses = training[training > 0.].sum(), -training[training < 0.].sum()
            pf = float(wins / losses) if losses else 0.
            if len(training) < 15 or training.sum() <= 0. or pf < 1.15:
                continue
            evidence["stress_and_is_qualified"] += 1
            edge = float(training.sum()) / math.sqrt(len(training))
            bank.append(dict(symbol=path.stem, sid=s["sid"], tf=s["tf"], cfg=s["cfg"],
                             score=edge, fit_n=len(training), fit_net_r=float(training.sum()), fit_pf=pf,
                             archived_family=bool(s["retired"]), tape=tape))
    bank.sort(key=lambda x: (-x["score"], x["sid"], x["symbol"]))
    return bank, dict(evidence)


def basket(bank, top, symbol_ceiling, family_ceiling, active_only):
    chosen, symbols, families = [], Counter(), Counter()
    for p in bank:
        if active_only and p["archived_family"]:
            continue
        if symbols[p["symbol"]] >= symbol_ceiling or families[p["sid"]] >= family_ceiling:
            continue
        symbols[p["symbol"]] += 1
        families[p["sid"]] += 1
        chosen.append(p)
        if len(chosen) == top:
            break
    return chosen


def compare(banks):
    arms = (
        ("active_8", 8, 1, 2, True), ("active_16", 16, 1, 3, True),
        ("active_24", 24, 2, 4, True), ("all_8", 8, 1, 2, False),
        ("all_16", 16, 1, 3, False), ("all_24", 24, 2, 4, False),
    )
    windows = {"fit": (utc("2021-01-01"), FIT), "2024_check": (FIT, CHECK),
               "2025_check": (CHECK, FINAL), "2026_diagnostic": (FINAL, STOP)}
    result = {}
    ranked = banks[20.0]
    for label, top, max_symbol, max_family, active in arms:
        selection = basket(ranked, top, max_symbol, max_family, active)
        results = {}
        for bp, bank in banks.items():
            # Keep the exact same route identities and fit priority for all charges.
            indexed = {(p["symbol"], p["sid"]): p for p in bank}
            pool = [dict(t, entry_priority=i) for i, fit in enumerate(selection)
                    for t in indexed[fit["symbol"], fit["sid"]]["tape"]]
            parts = {}
            for window, (lower, upper) in windows.items():
                portfolio = simulate_shared_account(
                    label, pool, EQUITY, .25, 2, 1, 1, {"BREAKOUT": 1, "PULLBACK": 1, "MEANREV": 1},
                    min_ts=lower, max_ts=upper, size_risk_usd=sized_risk, max_open_risk_pct=1.0,
                    max_crypto_total=1)
                parts[window] = portfolio["metrics"]
            results[str(bp)] = parts
        result[label] = {"routes": [{k: v for k, v in p.items() if k != "tape"} for p in selection],
                         "replays": results}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("refit_dir", type=Path)
    args = parser.parse_args()
    # Build ranking once with +20 bp. Reconstruct the zero-surcharge tape
    # from the same candidates so friction cannot change route eligibility.
    bank, counts = build_bank(args.refit_dir, 20.)
    unstressed = [dict(p, tape=[dict(t, r=t["r"] + t["cost_surcharge_r"]) for t in p["tape"]]) for p in bank]
    report = {"coverage": counts, "portfolios": compare({20.: bank, 0.: unstressed}),
              "qualified_routes": [{k: v for k, v in p.items() if k != "tape"} for p in bank],
              "cost_scope": "Snapshot lot economics on cached strategy prices; archive windows previously viewed."}
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
