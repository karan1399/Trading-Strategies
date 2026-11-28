"""Chronological Sovereign strategy coverage and native-engine portfolio comparison."""

from collections import defaultdict
from copy import deepcopy
import json
import math

import numpy as np

from desk.quant_audits import sovereign_engine_step_replay as native
from desk.quant_audits.strategies_parallel_replay import build_parallel_trade_pools


TRAIN = (native.utc("2026-05-01"), native.utc("2026-08-01"))
CHECK = (TRAIN[1], native.utc("2026-10-02"))
FINAL = (CHECK[1], native.utc("2026-11-20 23:45"))


def ensure_risk_fields(pair):
    return dict(pair, max_lot_risk_usd=min(6.0, float(pair["max_lot_risk_usd"])))


def with_pairs(cfg, pairs, **governor):
    result = deepcopy(cfg)
    result["pairs"] = [ensure_risk_fields(pair) for pair in pairs]
    result["governor"].update(governor)
    return result


def challenger_pairs():
    specifications = (
        ("s02_donchian_breakout", "BTC", "BREAKOUT", {"lookback": 22}),
        ("s02_donchian_breakout", "ETH", "BREAKOUT", {"lookback": 22, "trend_filter": "adx_er"}),
        ("s05_ema_zero_orderflow", "ETH", "PULLBACK",
         {"min_delta_pct": 5., "enable_ema_touch": 1, "enable_zero_trend": 0}),
        ("s07_daily_pivot_breakout", "ZEC", "BREAKOUT", {"swing_k": 2}),
        ("s09_donchian_trend_cta", "BTC", "BREAKOUT", {"channel": 16}),
        ("s10_ema_pullback", "YFI", "PULLBACK", {"min_delta_pct": 5.}),
        ("s10_ema_pullback", "VET", "PULLBACK", {"min_delta_pct": 5.}),
        ("s11_of_initiative_breakout", "BTC", "BREAKOUT", None),
        ("s11_of_initiative_breakout", "ETH", "BREAKOUT", None),
        ("s15_rsi2_mean_reversion", "BCH", "MEANREV", {"oversold": 20., "overbought": 80.}),
        ("s16_zero_auction_reversal", "ETH", "MEANREV", {"require_delta_confirm": 0, "min_extreme_val": 1.}),
    )
    pairs = []
    for magic, (folder, base, bucket, parameters) in enumerate(specifications, 44101):
        path = native.ROOT / "strategies" / folder / f"{base}.json"
        recipe = json.loads(path.read_text(encoding="utf-8"))
        risk = {k: v for k, v in recipe["optimal_parameters"].items()
                if k in ("sl_atr", "tp_r", "trail_atr", "be_r", "step_lock", "dyn_atr", "max_hold_bars")}
        tf = recipe["timeframe"].upper().replace("H4", "4H").replace("H2", "2H")
        pairs.append(dict(risk, pair_id=f"{recipe['mt5_symbol']}:{folder}:{tf}", magic=magic,
                          source_symbol=recipe["symbol"], mt5_symbol=recipe["mt5_symbol"], category="CRYPTO",
                          cluster="CRYPTO", feed_type="binance_footprint", sid=folder, tf=tf, tf_sec=int(tf[0]) * 3600,
                          bucket=bucket, cfg=recipe["parameter_string"], signal_source=f"strategies/{folder}/{path.name}",
                          signal_params=parameters if parameters is not None else recipe["optimal_parameters"],
                          base_asset=base,
                          be_pad_r=.05, use_s08_vol_target=False, pair_weight=1., max_lot_risk_usd=6., enabled=True))
    return pairs


def portfolio_paths(cfg, pairs):
    return {
        "safety_corrected_18": with_pairs(cfg, cfg["pairs"]),
        "desk_slots_18": with_pairs(cfg, cfg["pairs"], max_global=2, max_crypto_total=1),
        "ranked_12_desk_slots": with_pairs(cfg, pairs[:12], max_global=2, max_crypto_total=1),
        "ranked_8_desk_slots": with_pairs(cfg, pairs[:8], max_global=2, max_crypto_total=1),
        "ranked_12_short_hold": with_pairs(cfg, [dict(
            p, be_r=1.0, trail_atr=min(2.5, p["trail_atr"]), max_hold_bars=min(36, p["max_hold_bars"])
        ) for p in pairs[:12]], max_global=2, max_crypto_total=1),
        "ranked_12_equal_weight": with_pairs(cfg, [dict(p, pair_weight=1.0) for p in pairs[:12]],
                                            max_global=2, max_crypto_total=1),
    }


def compact(result):
    return {k: result[k] for k in ("start", "end", "trades", "end_equity", "net_pct", "pf",
                                    "max_marked_dd_pct", "by_symbol", "exposure_peaks",
                                    "eight_day_block_realized_pnl_percentiles")}


def stress_return(row, bps):
    # This is additive R from an already-produced research ledger. It is used
    # to nominate candidates for native replay, and carries no portfolio weight.
    stop = float(row["sl_dist"])
    price = float(row.get("entry", 0.0))
    if not all(math.isfinite(v) and v > 0.0 for v in (stop, price)):
        return None
    return float(row["r"]) - price * bps / (stop * 10_000)


def settled_stats(rows, lower, cutoff, bps=20.):
    values = [r for t in rows
              if lower <= int(t["ts_in"]) < cutoff and int(t["exit_available_ts"]) < cutoff
              if (r := stress_return(t, bps)) is not None and math.isfinite(r)]
    a = np.asarray(values)
    gains, losses = a[a > 0.].sum(), -a[a < 0.].sum()
    return {"n": len(values), "net_r": round(float(a.sum()), 5),
            "pf": round(float(gains / losses), 5) if losses > 0 else None}


def research_coverage():
    pools, recipes = build_parallel_trade_pools()
    keyed = defaultdict(list)
    for t in pools["all94"]:
        label = t.get("folder") or t["sleeve"]
        asset = t.get("asset") or t["symbol"].removesuffix("USDT")
        keyed[label, asset].append(t)
    folds = {
        "fit": (native.utc("2021-01-01"), native.utc("2024-01-01")),
        "diagnostic_2024": (native.utc("2024-01-01"), native.utc("2025-01-01")),
        "diagnostic_2025": (native.utc("2025-01-01"), native.utc("2026-01-01")),
        "diagnostic_2026": (native.utc("2026-01-01"), FINAL[1]),
    }
    for recipe in recipes:
        rows = keyed[recipe["folder"], recipe["asset"]]
        stats = {fold: settled_stats(rows, *bounds) for fold, bounds in folds.items()}
        fit = stats["fit"]
        recipe["settled_20bp_stress"] = stats
        recipe["pre2024_candidate"] = fit["n"] >= 15 and fit["net_r"] > 0 and (fit["pf"] or 0) >= 1.15
    recipes.sort(key=lambda r: (not r["pre2024_candidate"],
                                -r["settled_20bp_stress"]["fit"]["net_r"] /
                                math.sqrt(max(1, r["settled_20bp_stress"]["fit"]["n"])),
                                r["folder"], r["asset"]))
    return {"recipes": recipes, "ledger_rows": len(pools["all94"]),
            "overlay": "S08 is a risk overlay and has no standalone asset recipe."}


def main():
    cfg = json.loads(native.sov.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    # R050's chronological selection is frozen at its original sizing policy.
    cfg["_meta"]["base_risk_pct"] = .25
    cfg["governor"].update(risk_pct=.25, max_risk_single_trade_pct=.25)
    extra_pairs = challenger_pairs()
    feeds, prefix, _, _ = native.prepare(with_pairs(cfg, cfg["pairs"] + extra_pairs))
    markets = {**native.SYMBOLS, **{p["mt5_symbol"]: p["base_asset"] for p in extra_pairs}}
    opens, closes, _ = native.load_execution_tapes(markets)
    observations = {symbol: json.loads((native.ROOT / "data" / "telemetry" / "assets" / f"{alias}.json")
                                       .read_text(encoding="utf-8"))["mt5"]
                    for symbol, alias in markets.items()}

    def run(profile, window, bps=20.):
        return compact(native.replay(profile, feeds, opens, closes, *window, observations, extra_bps=bps))

    ranked = []
    for pair in cfg["pairs"]:
        trial = run(with_pairs(cfg, [pair]), TRAIN)
        trades = trial["trades"]
        value = (trial["end_equity"] - native.EQUITY) / math.sqrt(max(1, trades)) if trades >= 3 else -1_000
        ranked.append((value, pair["magic"], pair, trial))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    paths = portfolio_paths(cfg, [row[2] for row in ranked])
    challengers = {p["pair_id"]: {"train": run(with_pairs(cfg, [p]), TRAIN),
                                   "chronological_check": run(with_pairs(cfg, [p]), CHECK)} for p in extra_pairs}
    admitted = [p for p in extra_pairs if not prefix[p["pair_id"]]["mismatches"]
                and all(r["net_pct"] > 0 and r["trades"] >= 3 for r in challengers[p["pair_id"]].values())]
    paths["all_challengers_diagnostic"] = with_pairs(cfg, cfg["pairs"] + extra_pairs, max_global=2, max_crypto_total=1)
    if admitted:
        paths["admitted_challengers"] = with_pairs(cfg, [*cfg["pairs"], *admitted], max_global=2, max_crypto_total=1)
    fits = {label: dict(train=run(profile, TRAIN), chronological_check=run(profile, CHECK))
            for label, profile in paths.items()}
    # Pick only among profiles that are profitable with sufficient trades on
    # both chronological pre-final windows. The final comparison is read once.
    eligible = [label for label, r in fits.items() if label != "all_challengers_diagnostic"
                if all(r[w]["trades"] >= 8 and r[w]["net_pct"] > 0 for w in ("train", "chronological_check"))]
    chosen = max(eligible, key=lambda label: (
        min(fits[label][w]["net_pct"] / max(.25, fits[label][w]["max_marked_dd_pct"])
            for w in ("train", "chronological_check")), label), default="desk_slots_18")
    outputs = {label: {**stats, "final_diagnostic": run(paths[label], FINAL),
                       "final_no_extra_bps": run(paths[label], FINAL, bps=0.)}
               for label, stats in fits.items()}
    output = {"selection": chosen, "eligibility_for_selection": eligible,
              "may_august_route_ranking": {row[2]["pair_id"]: row[3] for row in ranked},
              "portfolios": outputs, "frozen_shortlist": [{"pair_id": p["pair_id"], "magic": p["magic"]}
                                                         for p in paths[chosen]["pairs"]],
              "selected_profile": paths[chosen], "new_routes": challengers,
              "challenger_prefix_checks": {p["pair_id"]: prefix[p["pair_id"]] for p in extra_pairs},
              "admitted_challengers": [p["pair_id"] for p in admitted],
              "strategy_coverage": research_coverage(),
              "scope": "Archived data previously viewed. Native fills simulated. No connected venue access."}
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
