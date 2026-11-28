"""Offline R048 provenance, candidate expansion, and V3B frozen-ledger scenarios."""

from collections import Counter
from hashlib import file_digest
import json
from pathlib import Path

import numpy as np
import pandas as pd

from engine.m4_live_engine import eval_crypto_signals, eval_tradfi_signals
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk
from exchange_feed.backtesting.btc_all25_optimizer_v2 import _df_to_bars, _prep_arrs
from exchange_feed.backtesting.fast_kernels import sim_1m_trades_nb
from exchange_feed.backtesting.mt5_shared_account_portfolio_optimizer import (
    _exit_available_at, simulate_shared_account,
)
from exchange_feed.backtesting.strategy_26_5k_optimizer import discover_chart_strategies
from strategies.s11_of_initiative_breakout.strategy import (
    OrderFlowInitiativeBreakoutStrategy, completed_d1_directions,
)


DATA = Path("exchange_feed/data")
PROP = DATA / "prop_26_5k"
MT5 = DATA / "mt5_portfolio"
START = 1609459200
SPLIT = 1704067200
LATE = 1735689600
END = 1795737600  # 2026-11-27 UTC, exclusive
SEED_EQUITY = 2501.80


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ledger(directory, symbol):
    path = directory / "trades" / f"{symbol}.parquet"
    return pd.read_parquet(path) if path.is_file() else pd.DataFrame()


def exact(frame, sid, timeframe, recipe):
    if frame.empty:
        return frame
    tf = timeframe[1:] + timeframe[:1] if timeframe.startswith("H") else timeframe
    return frame[(frame.sleeve == sid) & (frame.tf == tf) & (frame.cfg == recipe)]


def decorate(frame):
    items = frame.to_dict("records")
    for t in items:
        t["exit_available_ts"] = _exit_available_at(t)
    return items


def stats(rows, start, end):
    returns = np.array([t["r"] for t in rows if start <= t["ts_in"] < end and _exit_available_at(t) < end])
    loss = -returns[returns < 0].sum()
    return {"n": len(returns), "r": round(float(returns.sum()), 5),
            "pf": round(float(returns[returns > 0].sum() / loss), 5) if loss > 0 else None}


def is_selected(summary):
    return summary["n"] >= 20 and summary["r"] > 0 and (
        summary["pf"] is None or summary["pf"] >= 1.15)


def ownership_audit():
    files = []
    stores = {}
    tape_by_pair = {}
    for p in sorted(Path("strategies").glob("s[0-9][0-9]_*/*.json")):
        c = read(p)
        if "optimal_parameters" not in c:
            continue
        sid, symbol = c["strategy"], c["symbol"]
        source = c["performance_audit"].get("source_study", "")
        directories = (MT5, PROP) if source.startswith("mt5_portfolio") else (PROP, MT5)
        located = []
        for directory in directories:
            key = directory, symbol
            if key not in stores:
                stores[key] = ledger(directory, symbol)
            found = exact(stores[key], sid, c["timeframe"], c["parameter_string"])
            if not found.empty:
                located.append((directory, found))
        tape = decorate(located[0][1]) if located else []
        train, validation = stats(tape, START, SPLIT), stats(tape, SPLIT, END)
        files.append({"path": p.as_posix(), "sid": sid, "symbol": symbol, "tag": c["status"],
                      "source_study": source, "sources_exact": [d.name for d, _ in located],
                      "declared_r": c["performance_audit"].get("out_of_sample_net_r"),
                      "declared_pf": c["performance_audit"].get("out_of_sample_pf"),
                      "train": train, "validation": validation, "passes_train": is_selected(train),
                      "broker_spread_bps": c["performance_audit"].get("broker_spread_bps")})
        tape_by_pair[(sid, symbol)] = tape
    cohorts = {}
    for group, subset in [("all", files),
                          ("added", [c for c in files if c["tag"] == "STAGE_A_CANDIDATE"]),
                          ("original", [c for c in files if c["tag"] != "STAGE_A_CANDIDATE"])]:
        cohorts[group] = {}
        for name, admitted in [
            ("exact", [c for c in subset if c["sources_exact"]]),
            ("training_only", [c for c in subset if c["passes_train"]]),
            ("validation_screened", [c for c in subset if c["validation"]["r"] > 0]),
            ("validation_pf115", [c for c in subset if (c["validation"]["pf"] or 0) >= 1.15]),
        ]:
            rows = [t for c in admitted for t in tape_by_pair[c["sid"], c["symbol"]]]
            cohorts[group][name] = {"pairs": len(admitted), "training": stats(rows, START, SPLIT),
                                    "validation": stats(rows, SPLIT, END), "2025_26": stats(rows, LATE, END)}
    # An unscreened pre-existing recipe universe shows what the exported files leave out.
    universe = {}
    sids = {x["sid"] for x in files}
    for directory in (PROP, MT5):
        for path in (directory / "trades").glob("*.parquet"):
            df = stores.get((directory, path.stem))
            if df is None:
                df = ledger(directory, path.stem)
            for sid, frame in df[df.sleeve.isin(sids)].groupby("sleeve"):
                if (sid, path.stem) not in universe:
                    universe[sid, path.stem] = decorate(frame)
    eligible = [(key, rows) for key, rows in universe.items() if is_selected(stats(rows, START, SPLIT))]
    omitted = [(key, rows) for key, rows in eligible if key not in tape_by_pair]
    cohorts["available_legacy_winners"] = {"pairs": len(universe), "pass_train": len(eligible),
        "validation_profitable": sum(stats(rows, SPLIT, END)["r"] > 0 for _, rows in eligible),
        "omitted_train_passers": len(omitted),
        "omitted_neg_validation": sum(stats(rows, SPLIT, END)["r"] < 0 for _, rows in omitted)}
    donchian = [c for c in files if c["sid"] == "donchian_breakout"]
    baskets = {}
    for name, screened in [
        ("original_eth_sol", [c for c in donchian if c["tag"] != "STAGE_A_CANDIDATE"]),
        ("expanded", donchian),
        ("train_pass", [c for c in donchian if c["passes_train"]]),
        ("train_pass_spread35", [c for c in donchian if c["passes_train"] and c["broker_spread_bps"] <= 35]),
    ]:
        tape = [t for c in screened for t in tape_by_pair[c["sid"], c["symbol"]]]
        run = simulate_shared_account(name, tape, 5000, .45, 4, 1, 2,
                                      min_ts=SPLIT, max_ts=END, use_vof=True,
                                      dd_throttle_1=4.0, dd_throttle_2=6.0)
        baskets[name] = {"symbols": [c["symbol"] for c in screened], "raw": stats(tape, SPLIT, END),
                         "portfolio": run["metrics"], "by_asset": run.get("by_asset", [])}
    return {"files": files, "cohorts": cohorts, "donchian": baskets,
            "research_only_oi_files": sum(x["sid"].startswith("oi_") for x in files)}


def cached(symbol, timeframe):
    return pd.read_parquet(DATA / "cache" / f"{symbol}_{timeframe.lower()}.parquet")


def native(symbol, timeframe):
    path = Path("historical_parquet") / timeframe / f"{symbol}.parquet"
    if not path.is_file():
        return None
    df = pd.read_parquet(path).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["ts"] = df["timestamp"]
    df["end_ts"] = df["ts"] + (14400 if timeframe == "4H" else 3600)
    df["volume"] = df["tick_volume"]
    return df[df["end_ts"] < END].reset_index(drop=True)


def contract_proxy(pair, mid, unified, mutual):
    name = pair["mt5_symbol"]
    if pair["category"] == "CRYPTO":
        snap = mutual[pair["source_symbol"]]
        digits = unified.get(name, {}).get("digits", 2)
        return SymbolSpec(name, digits, 10.0 ** -digits, snap["tick_value"], snap["tick_size"],
                          snap["vol_min"], snap["vol_step"]), "saved_mutual_snapshot"
    target = {"SP500.p": "SP500", "USDJPY.pi": "USDJPY", "GAUUSD.pi": "GAUUSD",
              "GAUCNH.pi": "GAUCNH", "USWTI.p": "USWTI"}.get(name)
    path = Path("data/telemetry/assets") / f"{target}.json"
    if not path.is_file():
        return None, "missing_contract_snapshot"
    terms = read(path)["mt5"]
    if terms["symbol"] != name:
        return None, "different_broker_symbol"
    # Conversion in account USD. GAUCNH needs an explicit CNH conversion snapshot.
    if name == "GAUCNH.pi":
        return None, "missing_CNH_loss_tick_conversion"
    point = float(terms["point"])
    value_per_unit = float(terms["contract_size"]) / (mid if name == "USDJPY.pi" else 1.0)
    return SymbolSpec(name, terms["digits"], point, point * value_per_unit, point,
                      terms["volume_min"], terms["volume_step"]), "telemetry_contract_conversion_proxy"


def sizing(pair, atr, price, unified, mutual):
    spec, provenance = contract_proxy(pair, price, unified, mutual)
    if spec is None:
        return {"input": provenance}
    nominal = SEED_EQUITY * 0.008 * pair["pair_weight"]
    distance = atr * pair["sl_atr"]
    out = {"input": provenance, "stop_dist": distance,
           "min_lot_risk": spec.vol_min * distance * spec.value_per_price_unit()}
    for key, cap in [("pair_cap", pair["max_lot_risk_usd"]), ("desk_cap", 6.0)]:
        qty, reason = lots_for_risk(spec, distance, nominal, cap)
        actual = qty * distance * spec.value_per_price_unit() if qty is not None else None
        assert actual is None or actual <= cap + 1e-9
        out[key] = {"cap": cap, "lots": qty, "gross_stop_usd": actual, "reason": reason}
    return out


def shadow_native_replacement(symbol, chart, unified, mutual):
    frame = native(symbol, "4H")
    if frame is None or len(frame) < 300:
        return {"status": "missing_history"}
    payload = eval_tradfi_signals(frame, chart)["PA04_Donchian_Trend"]
    atr, times, ends = (np.ascontiguousarray(payload[k]) for k in ("atr", "ts", "end_ts"))
    side = np.ascontiguousarray(payload["side"], dtype=np.int8)
    o, h, l, c = (np.ascontiguousarray(frame[k], dtype=np.float64) for k in ("open", "high", "low", "close"))
    empty = np.array([], dtype=np.int64)
    empty_float = np.array([], dtype=np.float64)
    train_spread = frame[(frame["end_ts"] < SPLIT) & (frame["spread"] > 0)]
    point = 10 ** -int(unified.get(symbol, {}).get("digits", 2))
    spread_bps = (max(1.0, train_spread["spread"].median() * point / train_spread["close"].median() * 10_000)
                  if not train_spread.empty else 10.0)
    result = sim_1m_trades_nb(times, ends, atr, c, c, c, side, times, o, h, l, c,
                            14400, 2.5, 8.0, 3.5, 1.25, .05, False, 96, 0, True,
                            True, spread_bps, False, 0., 0., 5, 0., 0., empty, empty_float)
    tape = []
    category = "INDEX" if symbol.startswith("SP") else "FOREX" if symbol.startswith("USD") else "CFD"
    for ti, to, result_r, sd, reason in zip(result[1], result[2], result[7], result[3], result[4]):
        if reason == 0 and ti + 96 * 14400 > times[-1]:
            continue
        tape.append(dict(symbol=f"SHADOW_{symbol}", mt5_symbol=symbol, sleeve="PA04_native_shadow",
                         bucket="BREAKOUT", category=category, side="BUY" if sd == 1 else "SELL",
                         ts_in=int(ti), ts_out=int(to), exit_available_ts=int(to)+14400, r=float(result_r)))
    # This local history can be too short for a pre-2024 decision.
    training = stats(tape, START, SPLIT)
    terms = dict(source_symbol=f"SHADOW_{symbol}", mt5_symbol=symbol, category=category,
                 sl_atr=2.5, pair_weight=1.0, max_lot_risk_usd=20.0)
    return {"first_bar": int(times[0]), "last_bar": int(times[-1]), "training": training,
            "eligible_on_train": is_selected(training), "validation": stats(tape, SPLIT, END),
            "2025_26": stats(tape, LATE, END), "spread_proxy_bp": spread_bps,
            "last_bar_lot": sizing(terms, atr[-1], c[-1], unified, mutual), "tape": tape}


def v3b_audit():
    cfg = read("engine/config_m4_v3b.json")
    gov = cfg["governor"]
    mutual = read(DATA / "mt5_mutual_specs.json")
    unified = read("engine/config.json")["symbol_specs_fallback"]
    chart = discover_chart_strategies()
    pairs = [p for p in cfg["pairs"] if p["enabled"]]
    weights, tape, attribution, lot_checks = {}, [], {}, {}
    g4s, venues, entry_frames, pair_by_magic = {}, {}, {}, {}
    for p in pairs:
        source, sid = p["source_symbol"], p["sid"]
        rows = decorate(exact(ledger(MT5, source), sid, p["tf"], p["cfg"]))
        assert rows, p["pair_id"]
        weights[source, sid] = p["pair_weight"]
        pair_by_magic[p["magic"]] = p
        for t in rows:
            t["entry_priority"] = 0
            t["magic"] = p["magic"]
        tape.extend(rows)
        attribution[p["pair_id"]] = {"magic": p["magic"], "training": stats(rows, START, SPLIT),
                                      "validation": stats(rows, SPLIT, END),
                                      "late": stats(rows, LATE, END)}
        if p["category"] == "CRYPTO":
            if source not in g4s:
                g4s[source] = cached(source, "4H")
            frame = cached(source, p["tf"])
            last = frame[frame["end_ts"] < END].iloc[-1]
            last_atr, last_price = float(last["atr"]), float(last["c"])
            entry_frames[p["magic"]] = (frame["end_ts"].to_numpy(), frame["atr"].to_numpy(),
                                         frame["c"].to_numpy())
        else:
            key = p["mt5_symbol"], p["tf"]
            if key not in venues:
                venues[key] = native(*key)
            frame = venues[key]
            payload = eval_tradfi_signals(frame, chart)[sid]
            entry_frames[p["magic"]] = tuple(payload[k] for k in ("end_ts", "atr", "c"))
            last_atr, last_price = float(payload["atr"][-1]), float(payload["c"][-1])
        lot_checks[p["pair_id"]] = sizing(p, last_atr, last_price, unified, mutual)
        lot_checks[p["pair_id"]]["weight_at_most_1"] = sizing(
            dict(p, pair_weight=min(p["pair_weight"], 1.0)), last_atr, last_price, unified, mutual)
    eth = g4s["ETHUSDT"]
    d1 = np.asarray(completed_d1_directions(_df_to_bars(eth)))
    vol = eth["atr"] / eth["c"]
    compressed = (vol <= vol.rolling(60).median()).to_numpy()
    closes = eth["end_ts"].to_numpy()
    eth_filter = {"d1": [], "compression": []}
    for t in tape:
        if t["symbol"] != "ETHUSDT":
            for arm in eth_filter.values():
                arm.append(t)
            continue
        i = int(np.searchsorted(closes, t["ts_in"], side="right")) - 1
        if i < 0:
            continue
        if d1[i] == (1 if t["side"] == "BUY" else -1):
            eth_filter["d1"].append(t)
        if compressed[i]:
            eth_filter["compression"].append(t)
    replacements = {s: shadow_native_replacement(s, chart, unified, mutual)
                    for s in ("SP500.p", "USDJPY.pi", "GAUUSD.pi", "GAUCNH.pi", "USWTI.p")}
    no_eth = [t for t in tape if t["symbol"] != "ETHUSDT"]
    replacements_kept = [r for r in replacements.values() if r.get("eligible_on_train")]
    add_native = [t for r in replacements_kept for t in r["tape"]]
    legacy = [dict(t, exit_available_ts=t["ts_out"] + (14400 if t["tf"] == "4H" else 3600))
              if t["symbol"].startswith("MT5_") else t for t in tape]
    one_weight = {k: min(v, 1.0) for k, v in weights.items()}
    live_priority = [dict(t, entry_priority=(-round(1_000_000 * weights[t["symbol"], t["sleeve"]])
                                            * 100_000 + t["magic"])) for t in tape]
    scenarios = {
        "requested_v3b_control": (legacy, weights),
        "h1_execution_close": (tape, weights),
        "h1_plus_m4_priority": (live_priority, weights),
        "cap_weight_1": (tape, one_weight),
        "drop_eth_diagnostic": (no_eth, weights),
        "eth_d1_shadow": (eth_filter["d1"], weights),
        "eth_compression_shadow": (eth_filter["compression"], weights),
        "vol_target_overlay_off": ([dict(t, vof_mult=1.0) for t in tape], weights),
    }
    if add_native:
        scenarios["remove_eth_add_is_eligible_native"] = (no_eth + add_native, weights)
    windows = {"train": (START, SPLIT), "validation_2024": (SPLIT, LATE),
               "historical_2025_26": (LATE, END), "full": (START, None), "validation": (SPLIT, None)}
    experiments = {}
    for name, (pool, w) in scenarios.items():
        experiments[name] = {}
        for window, (start, end) in windows.items():
            m = simulate_shared_account(name, pool, SEED_EQUITY, gov["risk_pct"], gov["max_global"],
                                        gov["max_per_asset"], gov["max_crypto_same_side"], gov["bucket_caps"],
                                        w, True, *gov["dd_throttle"], min_ts=start, max_ts=end if window != "full" else None)
            experiments[name][window] = {"metrics": m["metrics"], "by_asset": m.get("by_asset", [])}
    # Restrict contract-size stress to entries with a local native/bar match. Missing
    # execution data is excluded from both its control and capped arm.
    sized_tape = []
    for t in tape:
        p = pair_by_magic[t["magic"]]
        ends, atr, close = entry_frames[t["magic"]]
        index = int(np.searchsorted(ends, t["ts_in"], side="right")) - 1
        if index < 0 or t["ts_in"] - ends[index] > 900 or not np.isfinite(atr[index]) or atr[index] <= 0:
            continue
        spec, _ = contract_proxy(p, close[index], unified, mutual)
        if spec is None:
            continue
        sized_tape.append(dict(t, size_spec=spec, sl_dist=float(atr[index] * p["sl_atr"]),
                               decision_close=float(close[index])))
    execution_proxy = {}
    for name, w, cap, canonical, extra_bps in (
        ("covered_fractional", weights, None, False, 0),
        ("covered_pair_caps", weights, "pair", False, 0),
        ("covered_weight1_pair_caps", one_weight, "pair", False, 0),
        ("covered_desk6", weights, "desk", False, 0),
        ("covered_desk6_and_slots", weights, "desk", True, 0),
        ("covered_desk6_and_slots_plus10bp", weights, "desk", True, 10),
        ("covered_desk6_and_slots_plus20bp", weights, "desk", True, 20),
    ):
        counts = Counter()

        def cap_size(row, target, equity):
            pair = pair_by_magic[row["magic"]]
            limit = pair["max_lot_risk_usd"] if cap == "pair" else min(6.0, equity * 0.01)
            qty, _ = lots_for_risk(row["size_spec"], row["sl_dist"], target, limit)
            if qty is None:
                counts["lot_reject"] += 1
                return None
            risk = row["sl_dist"] * qty * row["size_spec"].value_per_price_unit()
            assert risk <= limit + 1e-9
            counts["lot_pass"] += 1
            return risk

        maximum = (2, 1, 1) if canonical else (
            gov["max_global"], gov["max_per_asset"], gov["max_crypto_same_side"])
        arm = (sized_tape if extra_bps == 0 else [
            dict(t, r=t["r"] - extra_bps * t["decision_close"] / (10_000 * t["sl_dist"]))
            for t in sized_tape
        ])
        m = simulate_shared_account(name, arm, SEED_EQUITY, gov["risk_pct"], *maximum, gov["bucket_caps"],
                                    w, True, *gov["dd_throttle"], min_ts=LATE, max_ts=END,
                                    size_risk_usd=cap_size if cap is not None else None,
                                    max_crypto_total=1 if canonical else None,
                                    max_open_risk_pct=1.0 if canonical else None)
        execution_proxy[name] = {"metrics": m["metrics"], "sizing_checks": dict(counts),
                                 "additional_round_trip_bps": extra_bps}
    for r in replacements.values():
        r.pop("tape", None)
    return {"pair_count": len(pairs), "candidate_rows": len(tape), "pair_attribution": attribution,
            "lot_proxy": lot_checks, "replacement_shadow": replacements, "scenarios": experiments,
            "historical_size_proxy": {"cover_2025_26": stats(sized_tape, LATE, END),
                                      "full_2025_26": stats(tape, LATE, END), "arms": execution_proxy}}


def prefix_audit():
    chart = discover_chart_strategies()
    g2, g4 = cached("BTCUSDT", "2H"), cached("BTCUSDT", "4H")
    key = ("s07_daily_pivot_breakout", "4H")
    full = eval_crypto_signals(g2, g4, chart)[key]["side"]
    probes = np.flatnonzero(full)
    # Include the exact R046 signal and sample nonzero outcomes through the data span.
    indices = sorted({*map(int, probes[::max(1, len(probes) // 12)]), int(probes[-1])})
    results = []
    for i in indices:
        end = g4["end_ts"].iloc[i]
        cut = g4.iloc[:i+1]
        prefix = eval_crypto_signals(g2[g2["end_ts"] <= end], cut, chart)[key]["side"]
        assert np.array_equal(full[:i+1], prefix)
        results.append((int(end), int(prefix[-1])))
    bars = _df_to_bars(g4)
    d1 = completed_d1_directions(bars)
    close = g4.loc[g4["end_ts"] % 86400 == 0].set_index("end_ts")["c"]
    fast = close.ewm(span=50, adjust=False, min_periods=50).mean()
    slow = close.ewm(span=200, adjust=False, min_periods=200).mean()
    expected = np.sign(fast - slow).reindex(g4["end_ts"], method="ffill", tolerance=86399).fillna(0)
    assert np.array_equal(d1, expected.to_numpy())
    for i in indices:
        assert completed_d1_directions(bars[:i+1]) == d1[:i+1]
    return {"s07_nonzero_total": len(probes), "s07_prefix_cases": results,
            "d1_r047_mask_matches": len(d1), "r046_event_available": any(t == 1795233600 for t, _ in results)}


def initiative_parity_replay():
    mutual = read(DATA / "mt5_mutual_specs.json")
    strategy = OrderFlowInitiativeBreakoutStrategy()
    results = {}
    for base in ("BTC", "ETH", "SOL"):
        conf = read(Path("strategies/s11_of_initiative_breakout") / f"{base}.json")
        symbol = conf["symbol"]
        g4 = cached(symbol, "4H")
        frame = pd.read_parquet(DATA / "cache" / f"{symbol}_m1.parquet",
                                columns=["bar_1m", "o", "h", "l", "c"])
        m1 = tuple(np.ascontiguousarray(frame[k].to_numpy()) for k in ("bar_1m", "o", "h", "l", "c"))
        bars = _df_to_bars(g4)
        arr = _prep_arrs(g4)
        p = conf["optimal_parameters"]
        results[symbol] = {}
        for d1 in (True, False):
            raw = strategy.generate_signals(bars, dict(p, d1_ema_trend_gate=d1))
            side = np.zeros(len(g4), dtype=np.int8)
            for sig in raw:
                side[sig.index] = sig.side
            result = sim_1m_trades_nb(
                *arr, side, *m1, 14400, p["sl_atr"], p["tp_r"], p["trail_atr"],
                p["be_r"], .05, p["step_lock"], p["max_hold_bars"], 1, p["dyn_atr"], True,
                mutual[symbol]["sp_bps"], False, 0., 0., 5, -2.5, -1.5,
                np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64))
            tape = [dict(symbol=symbol, ts_in=int(i), ts_out=int(o), r=float(r))
                    for i, o, r in zip(result[1], result[2], result[7])]
            results[symbol]["d1" if d1 else "no_d1"] = stats(tape, SPLIT, END)
    return results


def main():
    report = {"horizon": ownership_audit(), "m4": v3b_audit(), "prefix": prefix_audit()}
    report["initiative_plugin_replay"] = initiative_parity_replay()
    files = [*PROP.glob("trades/*.parquet"), *MT5.glob("trades/*.parquet"),
             *(DATA / "cache" / f"{s}_{tf}.parquet"
               for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT") for tf in ("2h", "4h", "m1")),
             *(Path("historical_parquet") / tf / f"{s}.parquet" for tf in ("1H", "4H")
               for s in ("SP500.p", "USDJPY.pi", "GAUUSD.pi", "GAUCNH.pi", "USWTI.p"))]
    hashes = {}
    for path in sorted(files):
        if path.is_file():
            with path.open("rb") as f:
                hashes[path.as_posix()] = file_digest(f, "sha256").hexdigest()
    report["local_input_sha256"] = hashes
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
