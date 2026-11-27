"""Offline R047 replay of frozen entry recipes. Run from the repository root."""

import ast
from collections import Counter
from contextlib import redirect_stdout
from hashlib import file_digest
import inspect
import json
from pathlib import Path
import sys
import textwrap

import numpy as np
import pandas as pd

from strategies.archive.unprofitable.bollinger_zscore import BollingerZscoreStrategy
from strategies.archive.unprofitable.ema_pullback import EmaPullbackStrategy
from strategies.archive.unprofitable.vwap_rejection import VwapRejectionStrategy
from strategies.donchian_breakout.strategy import DonchianBreakoutStrategy
from strategies.rsi2_mean_reversion.strategy import Rsi2MeanReversionStrategy
from desk.quant_audits.prop_26_5k_replay import (
    DATA, DAY, END, PROP, SPLIT, START, bootstrap, equity_series, statistics,
)
from footprint_Chart.backtesting import strategy_26_5k_optimizer as optimizer
from footprint_Chart.backtesting.mt5_shared_account_portfolio_optimizer import _entry_order
from footprint_Chart.backtesting.portfolio_engine import _load_funding_arrays


SEED = 47026
MAJORS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
BEFORE_SPLIT = [(START, SPLIT)]
AFTER_SPLIT = [(SPLIT, END), (SPLIT, SPLIT + 366 * DAY),
               (SPLIT + 366 * DAY, SPLIT + 731 * DAY), (SPLIT + 731 * DAY, END)]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_bars(symbol, *_args, **_kwargs):
    gz = [pd.read_parquet(DATA / "cache" / f"{symbol}_{tf}.parquet") for tf in ("2h", "4h")]
    minute = pd.read_parquet(DATA / "cache" / f"{symbol}_m1.parquet",
                             columns=["bar_1m", "o", "h", "l", "c"])
    return (*gz, *(minute[k].to_numpy() for k in ("bar_1m", "o", "h", "l", "c")))


def offline_oi(symbol, **_kwargs):
    path = DATA / f"{symbol}_metrics_5m_full.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def locked_trial_generator(collect, bars, oi):
    # Reuse every original signal builder. Intercept the _push call before scoring,
    # and remove catalog creation and disk writes from this audit-only copy.
    parsed = ast.parse(textwrap.dedent(inspect.getsource(optimizer._optimize_asset_all26)))
    function = parsed.body[0]
    body = function.body
    stop = next(i for i, node in enumerate(body) if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name) and node.target.id == "strat_catalog")
    function.body = body[:stop]
    push = next(node for node in function.body if isinstance(node, ast.FunctionDef)
                and node.name == "_push")
    push.body = [ast.Expr(ast.Call(ast.Name("_collect", ast.Load()),
                                 [ast.Name(arg.arg, ast.Load()) for arg in push.args.args], []))]
    sources = dict(vars(optimizer), _collect=collect, load_asset_tfs_cached=lambda *_a, **_kw: bars,
                   ensure_full_metrics=lambda *_a, **_kw: oi)
    exec(compile(ast.fix_missing_locations(parsed), optimizer.__file__, "exec"), sources)
    return sources[function.name]


def ordered_completed(result, start, end):
    return (result[1] >= start) & (result[2] + 60 < end)


def score(result, spread_stress, start, end):
    take = ordered_completed(result, start, end)
    r = result[7][take]
    stress = spread_stress[take]
    equity = np.r_[1.0, np.cumprod(1 + 0.0045 * r)]
    dd = (1 - equity / np.maximum.accumulate(equity)).max()
    cagr = equity[-1] ** (DAY * 365.25 / (end - start)) - 1
    loss = -r[r < 0].sum()
    return {"n": len(r), "r": round(float(r.sum()), 5), "r_mean": round(float(r.mean()), 5) if len(r) else None,
            "cost_stress_r": round(float((r - stress).sum()), 5),
            "pf": round(float(r[r > 0].sum() / loss), 5) if loss else None,
            "dd_pct": round(float(dd) * 100, 5), "cagr_pct": round(float(cagr) * 100, 5),
            "calmar": round(float(cagr / dd), 5) if dd > 0 else None}


def candidate_trials(config):
    base = config["optimal_parameters"]
    return {
        "locked": (base["sl_atr"], base["trail_atr"], base["be_r"]),
        "be_1": (base["sl_atr"], base["trail_atr"], 1.0),
        "be_15": (base["sl_atr"], base["trail_atr"], 1.5),
        "trail_minus_05": (base["sl_atr"], max(0.0, base["trail_atr"] - 0.5), base["be_r"]),
        "trail_plus_05": (base["sl_atr"], base["trail_atr"] + 0.5, base["be_r"]),
        "sl_minus_05": (base["sl_atr"] - 0.5, base["trail_atr"], base["be_r"]),
        "sl_plus_05": (base["sl_atr"] + 0.5, base["trail_atr"], base["be_r"]),
    }


def paired_block_bounds(base, challenger):
    days = (END - SPLIT) // DAY
    difference = np.zeros(days)
    for result, sign in [(base, -1), (challenger, 1)]:
        take = ordered_completed(result, SPLIT, END)
        np.add.at(difference, (result[2][take] + 60 - SPLIT) // DAY, sign * result[7][take])
    bounds = {}
    for block in (8, 32):
        rng = np.random.default_rng(SEED)
        starts = rng.integers(0, days - block + 1, size=(2000, (days + block - 1) // block))
        indices = (starts[..., None] + np.arange(block)).reshape(2000, -1)[:, :days]
        totals = difference[indices].sum(axis=1)
        bounds[str(block)] = np.quantile(totals, [0.025, 0.975]).round(3).tolist()
    return bounds


def audit_asset(symbol, configs, specs, chart_map):
    bars = load_bars(symbol)
    g2, g4, *minute = bars
    spread = specs["sp_bps"]
    ledger_path = PROP / "trades" / f"{symbol}.parquet"
    ledger = pd.read_parquet(ledger_path)
    ft, fr = _load_funding_arrays(symbol, DATA)
    assert all(a.flags.c_contiguous for a in minute)
    audits, portfolio_tape = {}, []
    def collect(sid, tf, mode, cfg, frame, arrays, seconds, signals, *_unused):
        config = configs.get(sid)
        if config is None:
            return
        if sid == "of_initiative_breakout":
            if cfg != "lb=22,dp=2.0,sl=2.5,tp=8.0,tr=4.0,be1" or tf != "4H":
                return
            close = g4.set_index("end_ts")["c"]
            close = close.loc[close.index % DAY == 0]
            fast = close.ewm(span=50, adjust=False, min_periods=50).mean()
            slow = close.ewm(span=200, adjust=False, min_periods=200).mean()
            direction = np.sign(fast - slow).reindex(
                frame["end_ts"], method="ffill", tolerance=DAY - 1).to_numpy()
            ungated = signals.copy()
            signals = np.where(signals == direction, signals, 0)
        elif cfg != config["parameter_string"] or tf != config["timeframe"]:
            return
        assert sid not in audits, (sid, symbol, cfg)
        base = config["optimal_parameters"]
        base_args = candidate_trials(config)
        runs = {}
        for label, controls in base_args.items():
            sl, trail, be = controls
            existing = next((runs[name] for name, params in base_args.items()
                             if params == controls and name in runs), None)
            if existing is not None:
                runs[label] = existing
                continue
            runs[label] = optimizer.sim_1m_trades_nb(
                *arrays, np.ascontiguousarray(signals, dtype=np.int8), *minute, seconds,
                sl, base["tp_r"], trail, be, 0.05, base["step_lock"], base["max_hold_bars"],
                1, base["dyn_atr"], True, spread, False, 0.0, 0.0, 5, -2.5, -1.5, ft, fr)
        if sid == "of_initiative_breakout":
            runs["d1_disabled"] = optimizer.sim_1m_trades_nb(
                *arrays, np.ascontiguousarray(ungated, dtype=np.int8), *minute, seconds,
                base["sl_atr"], base["tp_r"], base["trail_atr"], base["be_r"], 0.05,
                base["step_lock"], base["max_hold_bars"], 1, base["dyn_atr"], True, spread, False,
                0.0, 0.0, 5, -2.5, -1.5, ft, fr)
            base_args["d1_disabled"] = base_args["locked"]
        locked = runs["locked"]
        previous = ledger[ledger["sleeve"] == sid]
        matches = sid != "of_initiative_breakout" and (
            np.array_equal(locked[1], previous["ts_in"].to_numpy())
            and np.array_equal(locked[2], previous["ts_out"].to_numpy())
            and np.allclose(locked[7], previous["r"].to_numpy(), atol=1e-10, rtol=0))
        output = {}
        for label, result in runs.items():
            stress = .001 * result[5] / (arrays[2][result[0]] * base_args[label][0])
            windows = [score(result, stress, a, b) for a, b in BEFORE_SPLIT + AFTER_SPLIT]
            output[label] = dict(zip(("IS", "validation", "2024", "2025", "2026"), windows))
            if label != "locked":
                output[label]["delta_total_r_central_95_bounds_by_block_days"] = paired_block_bounds(locked, result)
        prior = output["locked"]["IS"]["r_mean"] or 0.0
        prior_n = output["locked"]["IS"]["n"]
        prior *= prior_n / (prior_n + 30)
        for i in range(len(locked[0])):
            k = int(locked[0][i])
            distance = base["sl_atr"] * arrays[2][k]
            h4_index = np.searchsorted(g4["end_ts"], locked[1][i], side="right") - 1
            last_h4 = g4.iloc[max(0, h4_index - 245):h4_index + 1]
            mult = 1.0
            if h4_index >= 0:
                v = last_h4.iloc[-1]
                imbalance = int(v.buy_imb - v.sell_imb) if v.buy_imb + v.sell_imb else int(locked[3][i])
                mult = round(float(optimizer.vol_of_conviction_risk_usd(
                    last_h4["c"].to_numpy(), "H4", int(locked[3][i]), float(v.d_pct),
                    imbalance, float(v.vpin_rank), 1.0, 0.65, 1.25, target_ann_vol=0.55, lookback=240)), 4)
            portfolio_tape.append({
                "symbol": symbol, "sleeve": sid, "tf": tf, "bar_i": k,
                "mt5_symbol": config["mt5_symbol"], "side": "BUY" if locked[3][i] == 1 else "SELL",
                "ts_in": int(locked[1][i]), "ts_out": int(locked[2][i]), "entry": float(locked[5][i]),
                "r": float(locked[7][i]), "stop_dist": float(distance), "spread": spread, "vof_mult": mult,
                "lag": int(locked[1][i] - arrays[1][k]), "is_prior": float(prior),
            })
        audits[sid] = {"baseline_ledger_exact": bool(matches), "recorded_config": config,
                       "median_spread_to_stop": round(float(np.median(
                           spread / 10_000 * locked[5] / (arrays[2][locked[0]] * base["sl_atr"]))), 5),
                       "trials": output}

    with redirect_stdout(sys.stderr):
        locked_trial_generator(collect, bars, offline_oi(symbol))(
            symbol, specs, chart_map, force=True)
    assert set(audits) == set(configs), (symbol, set(configs) - set(audits))
    return audits, portfolio_tape, g4.set_index("end_ts")["c"]


def farm_admit(active, trade, policy):
    """Inputs are already canonicalized and include reservations and pending orders."""
    gov = policy["governor"]
    if len(active) >= gov["max_global_positions"]:
        return "global_slots"
    if sum(a["symbol"] == trade["symbol"] for a in active) >= gov["max_positions_per_symbol"]:
        return "symbol"
    crypto = [a for a in active if a.get("category", "CRYPTO") == "CRYPTO"]
    if trade.get("category", "CRYPTO") != "CRYPTO":
        return None
    if sum(a["side"] == trade["side"] for a in crypto) >= gov["max_crypto_same_side"]:
        return "same_side"
    cluster = gov["symbol_cluster_limits"]["crypto_majors"]
    members = set(cluster["members"])
    if trade["symbol"] in members and sum(a["symbol"] in members for a in crypto) >= cluster["max_positions"]:
        return "major_slots"
    if len(crypto) >= gov["crypto_cluster_max_positions"]:
        return "crypto_slots"
    return None


def farm_replay(tape, marks, specs, policy, priority, cap_lots, stress_bp=0, start=SPLIT, end=END):
    eligible = [dict(t, entry_priority=priority(t)) for t in tape if start <= t["ts_in"] < end]
    cash = peak = 5000.0
    curve = [(start, cash)]
    active, fills = [], []
    rejected = Counter()
    throttled = False
    max_major_same_side = 0
    max_heat = 0.0

    def close(positions):
        nonlocal cash, peak
        for t in sorted(positions, key=lambda t: t["known_exit"]):
            cash += t["pnl"]
            peak = max(cash, peak)
            curve.append((t["known_exit"], cash))

    for trade in sorted(eligible, key=_entry_order):
        ts = trade["ts_in"]
        close([a for a in active if a["known_exit"] <= ts])
        active = [a for a in active if a["known_exit"] > ts]
        if trade["lag"] > 300:
            rejected["stale"] += 1
            continue
        reason = farm_admit(active, trade, policy)
        if reason:
            rejected[reason] += 1
            continue
        drawdown = (1 - cash / peak) * 100
        if drawdown >= 6:
            throttle, throttled = 0.25, True
        elif drawdown >= 4 or (throttled and drawdown >= 2):
            throttle, throttled = 0.50, True
        else:
            throttle, throttled = 1.0, False
        cost_multiple = trade["entry"] / trade["stop_dist"] / 10_000
        reserve = 1 + (trade["spread"] + 15 + stress_bp) * cost_multiple
        risk = cash * 0.0045 * trade["vof_mult"] * throttle
        if cap_lots:
            spec = specs[trade["symbol"]]
            loss_lot = trade["stop_dist"] * spec["tick_value"] / spec["tick_size"]
            lot_step = spec["vol_step"]
            lots = np.floor(min(risk, 6 / reserve) / loss_lot / lot_step + 1e-9) * lot_step
            if lots + 1e-9 < spec["vol_min"]:
                rejected["min_lot"] += 1
                continue
            risk = float(lots * loss_lot)
        allocated = sum(t["reserved_risk"] for t in active) + risk * reserve
        if allocated > cash * policy["governor"]["max_total_open_risk_pct"] / 100.0 + 1e-9:
            rejected["heat"] += 1
            continue
        result = trade["r"] - stress_bp * cost_multiple
        fill = dict(trade, known_exit=trade["ts_out"] + 60, result_r=result, pnl=result * risk,
                    reserved_risk=risk * reserve, stop_risk=risk)
        active.append(fill)
        fills.append(fill)
        max_heat = max(max_heat, allocated / cash * 100)
        major = Counter(a["side"] for a in active if a["symbol"] in MAJORS)
        max_major_same_side = max(max_major_same_side, *major.values(), 0)
    close(active)
    complete_fills = [t for t in fills if t["known_exit"] <= end]
    eq = equity_series(fills, curve, marks, start, end, False)
    mtm = equity_series(fills, curve, marks, start, end, True)
    return statistics(complete_fills, eq) | {
        "entered": len(fills), "rejected": dict(rejected), "marked_dd": statistics(complete_fills, mtm)["dd"],
        "major_same_side_peak": max_major_same_side, "peak_heat_at_entry_pct": round(max_heat, 5),
        "marked_bootstrap": bootstrap(mtm),
    }


def policy(capacity, same_side, major_cap, heat):
    return {"governor": {"max_global_positions": capacity, "crypto_cluster_max_positions": capacity,
                          "max_positions_per_symbol": 1, "max_crypto_same_side": same_side,
                          "symbol_cluster_limits": {"crypto_majors": {
                              "members": sorted(MAJORS), "max_positions": major_cap}},
                          "max_total_open_risk_pct": heat}}


def main():
    configs = {}
    for path in sorted(Path("strategies").glob("*/*.json")):
        if path.stem == "config":
            continue
        config = read(path)
        if "optimal_parameters" in config:
            configs.setdefault(config["symbol"], {})[config["strategy"]] = config
    specs = read(DATA / "mt5_mutual_specs.json")
    all_results, all_tape, marks, hashes = {}, [], {}, {}
    # The reviewed commit's Charts discovery scans legacy loose files only.
    plugins = (BollingerZscoreStrategy, EmaPullbackStrategy, VwapRejectionStrategy,
               DonchianBreakoutStrategy, Rsi2MeanReversionStrategy)
    chart_map = {plugin.id: plugin() for plugin in plugins}
    for symbol, universe in sorted(configs.items()):
        all_results[symbol], tape, marks[symbol] = audit_asset(symbol, universe, specs[symbol], chart_map)
        all_tape.extend(tape)
        for f in [*(DATA / "cache" / f"{symbol}_{tf}.parquet" for tf in ("2h", "4h", "m1")),
                  DATA / f"{symbol}_metrics_5m_full.parquet", PROP / "trades" / f"{symbol}.parquet"]:
            if f.is_file():
                with f.open("rb") as source:
                    hashes[f.as_posix()] = file_digest(source, "sha256").hexdigest()
    permissive = policy(4, 3, 3, 1)
    major_only = policy(2, 1, 1, 1)
    hard = policy(1, 1, 1, 1)
    hard["governor"]["max_global_positions"] = 2
    ranking = lambda t: -round(t["is_prior"] * 1_000_000)
    lexical = lambda _t: 0
    runs = {
        "relaxed_research_only": (permissive, lexical, False, 0),
        "major_and_side_research_only": (major_only, lexical, False, 0),
        "desk_lexical": (hard, lexical, False, 0),
        "desk_lexical_cost_stress": (hard, lexical, False, 10),
        "desk_lexical_lot_cap": (hard, lexical, True, 0),
        "desk_lexical_lot_cap_and_cost_stress": (hard, lexical, True, 10),
        "desk_is_rank": (hard, ranking, False, 0),
        "desk_is_rank_cost_stress": (hard, ranking, False, 10),
        "desk_is_rank_lot_cap": (hard, ranking, True, 0),
    }
    portfolio = {name: farm_replay(all_tape, marks, specs, *args) for name, args in runs.items()}
    print(json.dumps({"scope": "Historical validation, frozen entry signal masks and candidate parameters",
                      "cache_sha256": hashes, "pairs": all_results, "farm": portfolio},
                     indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
