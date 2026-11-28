"""Refit all local Delta contracts and freeze an IS-selected cross-product shadow book."""

from collections import Counter
from hashlib import file_digest
import json
from pathlib import Path

import numpy as np
import pandas as pd

from Delta_Exchange_Data.delta_pa_live_trader import compute_safe_contracts
from footprint_Chart.backtesting.mt5_shared_account_portfolio_optimizer import simulate_shared_account
from scripts import delta_pa_trend_optimizer as model


DATA = Path("Delta_Exchange_Data")
END = int(pd.Timestamp("2026-11-27", tz="UTC").timestamp())
SEPT = int(pd.Timestamp("2026-09-01", tz="UTC").timestamp())
START = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
BASE_BP = 25.0


def arrays(frame, keys, dtype):
    return tuple(np.ascontiguousarray(frame[key], dtype=dtype) for key in keys)


def read_frame(symbol, tf):
    tf_seconds = {"1H": 3600, "4H": 14400}[tf]
    path = DATA / f"ohlc_{tf.lower()}" / f"{symbol}.parquet"
    df = pd.read_parquet(path).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df[df["timestamp"] + tf_seconds < END]


def rebuild_recipes(config, product, extra_cost):
    symbol = config["symbol"]
    scratch = {}
    e1 = read_frame(symbol, "1H")
    exec_ts, = arrays(e1, ("timestamp",), np.int64)
    exec_prices = arrays(e1, ("open", "high", "low", "close"), np.float64)
    size = float(product.get("contract_value") or 0.0)
    im = float(product.get("initial_margin") or 5.0)
    pool = []
    for recipe in config["recipe"]:
        tf, sid = recipe["tf"], recipe["sid"]
        seconds = 3600 if tf == "1H" else 14400
        if tf not in scratch:
            df = e1 if tf == "1H" else read_frame(symbol, tf)
            ts, = arrays(df, ("timestamp",), np.int64)
            hi, lo, close = arrays(df, ("high", "low", "close"), np.float64)
            scratch[tf] = ts, model._atr_wilder(hi, lo, close, 14), close, model._build_pa_signals(df)
        ts, atr, close, sig = scratch[tf]
        variant, settings = recipe["cfg"].split(",sl=", maxsplit=1)
        pars = dict(x.split("=") for x in ("sl=" + settings).split(","))
        sl, tp, trail, be = (float(pars[key]) for key in ("sl", "tp", "tr", "be"))
        side = dict(sig[sid])[variant]
        ti, to, returns = model._sim_delta_nb(
            ts, ts + seconds, atr, close, side, exec_ts, *exec_prices,
            sl, tp, trail, be, 144 if tf == "1H" else 96, seconds, BASE_BP + extra_cost)
        for t_in, t_out, r in zip(ti, to, returns):
            index = int(np.searchsorted(ts + seconds, t_in, side="right")) - 1
            price = float(exec_prices[0][np.searchsorted(exec_ts, t_in)])
            pool.append(dict(symbol=symbol, mt5_symbol=symbol, category="CRYPTO", bucket="PA",
                             sleeve=sid, tf=tf, side="BUY" if side[index] == 1 else "SELL",
                             ts_in=int(t_in), ts_out=int(t_out), exit_available_ts=int(t_out) + 3600,
                             r=float(r), entry_price=price, stop_dist=sl * atr[index], cv=size, im=im))
    return pool


def replay_book(pool, start, end, entry_rank, use_contracts, extra_cost):
    counts = Counter()

    def size(trade, target, equity):
        risk_pct = min(.5, target * 100 / equity)
        sl, price, cv = (trade[k] for k in ("stop_dist", "entry_price", "cv"))
        leverage = max(1, min(10, int(np.floor(0.40 / max(sl / price, 1e-4)))))
        qty, _ = compute_safe_contracts(equity, equity, price, sl, cv,
                                        max(trade["im"], 100 / leverage),
                                        risk_pct=risk_pct, stress_bps=BASE_BP + extra_cost)
        if qty <= 0:
            counts["contract_reject"] += 1
            return None
        counts["contract_pass"] += 1
        assert qty * cv * (sl + price * (BASE_BP + extra_cost) / 10_000) <= min(
            .50, target, equity * .005
        ) + 1e-9
        return qty * cv * sl

    sequenced = [dict(t, entry_priority=entry_rank.get(t["symbol"], 0)) for t in pool]
    result = simulate_shared_account("Delta shadow", sequenced, 5000, .5, 1, 1, 1,
                                     use_vof=False, dd_throttle_1=4.0, dd_throttle_2=6.0,
                                     min_ts=start, max_ts=end, mc_runs=20_000,
                                     size_risk_usd=size if use_contracts else None)
    return {"metrics": result["metrics"], "sizing": dict(counts), "contributions": result.get("by_asset", [])}


def main():
    products = {r["symbol"]: r for r in json.loads((DATA / "delta_products_catalog.json").read_text("utf-8"))}
    prior = json.loads((DATA / "delta_pa_optimization_report.json").read_text("utf-8"))
    coverage = {}
    results = {}
    for path in sorted((DATA / "ohlc_4h").glob("*.parquet")):
        symbol = path.stem
        if symbol not in products:
            continue
        product = products[symbol]
        result = model.optimize_delta_symbol(dict(product, fixed_research_cost_bps=BASE_BP), run_mc=False)
        if result is not None:
            results[symbol] = result
            for tf in ("1h", "4h"):
                source = DATA / f"ohlc_{tf}" / f"{symbol}.parquet"
                with source.open("rb") as stream:
                    coverage[source.as_posix()] = file_digest(stream, "sha256").hexdigest()
    eligible = [r for r in results.values() if r.get("train", {}).get("n", 0) >= 20
                and r["train"]["pf"] >= 1.20 and r["train"]["net_r"] > 0]
    ranked = sorted(eligible, key=lambda r: (-r["train"]["calmar"], r["symbol"]))
    selected = {r["symbol"]: r for r in ranked[:12]}
    rank = {symbol: i for i, symbol in enumerate(selected)}
    base = [t for s, r in selected.items() for t in rebuild_recipes(r, products[s], 0.0)]
    stress = [t for s, r in selected.items() for t in rebuild_recipes(r, products[s], 10.0)]
    periods = {"train": (START, model.DELTA_FIT_TS), "jun_aug": (model.DELTA_FIT_TS, SEPT),
               "sep_nov": (SEPT, END), "validation": (model.DELTA_FIT_TS, END)}
    portfolio = {}
    for name, pool, ranking, lot, cost in (
        ("lexical_fractional", base, {}, False, 0.0),
        ("IS_rank_fractional", base, rank, False, 0.0),
        ("IS_rank_fractional_plus10", stress, rank, False, 10.0),
        ("IS_rank_hard_contract_cap", base, rank, True, 0.0),
        ("IS_rank_hard_contract_cap_plus10", stress, rank, True, 10.0),
    ):
        portfolio[name] = {w: replay_book(pool, a, b, ranking, lot, cost) for w, (a, b) in periods.items()}
    output = {"fixed_round_trip_bps": BASE_BP, "fit_asof_ts": model.DELTA_FIT_TS,
              "outer_holdout_start_ts": model.DELTA_OUTER_TS, "contracts_evaluated": len(results),
              "legacy_verdicts": dict(Counter(p["verdict"] for p in prior["assets"])),
              "refit_verdicts": dict(Counter(p["verdict"] for p in results.values())),
              "IS_eligible_pool": len(eligible), "locked_book": selected,
              "portfolio": portfolio, "per_contract": results, "local_input_sha256": coverage}
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
