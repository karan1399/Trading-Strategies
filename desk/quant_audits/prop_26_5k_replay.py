"""Offline replay for the 61350ea6e review. Run from the repository root.

    python -m desk.quant_audits.prop_26_5k_replay > audit-output.json

Uses the saved winner ledgers. Rejected strategy-level signals are unavailable.
Daily marks use Binance H4 closes as a price proxy for MT5. No venue client is used.
"""

from collections import Counter
from hashlib import file_digest
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from footprint_Chart.backtesting.mt5_shared_account_portfolio_optimizer import (
    simulate_shared_account,
)


DATA = Path("footprint_Chart/data")
PROP = DATA / "prop_26_5k"
DAY = 86_400
SPLIT = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
START = int(pd.Timestamp("2021-01-01", tz="UTC").timestamp())
END = int(pd.Timestamp("2026-11-27", tz="UTC").timestamp())
MAJORS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def replay(pool, start, end, risk_pct, max_global, max_same_side, *,
           cash=5_000.0, published_order=False, max_heat=None, max_crypto=None,
           stress_bps=0.0, lot_specs=None, max_trade_usd=None):
    """Apply entry-time ranking and release a minute's exits on its close."""
    candidates = [t for t in pool if start <= t["ts_in"] < end]
    if published_order:
        candidates.sort(key=lambda t: (t["ts_in"], t["ts_out"]))
    else:
        candidates.sort(key=lambda t: (t["ts_in"], t["sleeve"], t["symbol"]))
    equity = peak = cash
    throttled = False
    active, fills, curve = [], [], [(start, cash)]
    rejects = Counter()

    def realize(positions):
        nonlocal equity, peak
        for position in sorted(positions, key=lambda item: item["known_exit"]):
            equity += position["pnl"]
            peak = max(peak, equity)
            curve.append((position["known_exit"], equity))

    for t in candidates:
        ts = t["ts_in"]
        realize([a for a in active if a["known_exit"] <= ts])
        active = [a for a in active if a["known_exit"] > ts]
        if len(active) >= max_global:
            rejects["slots"] += 1
            continue
        if any(a["symbol"] == t["symbol"] for a in active):
            rejects["symbol"] += 1
            continue
        if t["category"] == "CRYPTO":
            crypto = [a for a in active if a["category"] == "CRYPTO"]
            if max_crypto is not None and len(crypto) >= max_crypto:
                rejects["crypto_slots"] += 1
                continue
            if sum(a["side"] == t["side"] for a in crypto) >= max_same_side:
                rejects["same_side"] += 1
                continue
        dd = 100 * (1 - equity / peak)
        if dd >= 6:
            throttle, throttled = 0.25, True
        elif dd >= 4 or (throttled and dd >= 2):
            throttle, throttled = 0.50, True
        else:
            throttle, throttled = 1.0, False
        stop_risk = equity * risk_pct / 100 * t["vof_mult"] * throttle
        if max_heat is not None:
            # Keep a reserve for modeled friction as well as stop distance.
            allocated = sum(a["reserved_risk"] for a in active)
            reserve = 1 + ((t["spread"] + 15 + stress_bps) / 10_000) * t["entry"] / t["stop_dist"]
            stop_risk = min(stop_risk, equity * max_heat / 100 / reserve)
        else:
            allocated = 0.0
            reserve = 1.0
        if max_trade_usd is not None:
            stop_risk = min(stop_risk, max_trade_usd / reserve)
        if lot_specs is not None:
            spec = lot_specs[t["symbol"]]
            value_per_unit = spec["tick_value"] / spec["tick_size"]
            loss_per_lot = t["stop_dist"] * value_per_unit
            step = spec["vol_step"]
            lots = math.floor(stop_risk / loss_per_lot / step + 1e-9) * step
            if lots + 1e-9 < spec["vol_min"]:
                rejects["min_lot"] += 1
                continue
            stop_risk = lots * loss_per_lot
        if max_heat is not None and allocated + stop_risk * reserve > equity * max_heat / 100 + 1e-9:
            rejects["heat"] += 1
            continue
        result_r = t["r"] - stress_bps / 10_000 * t["entry"] / t["stop_dist"]
        fill = dict(t, result_r=result_r, stop_risk=stop_risk,
                    reserved_risk=stop_risk * reserve, pnl=result_r * stop_risk,
                    known_exit=t["ts_out"] + (0 if published_order else 60))
        active.append(fill)
        fills.append(fill)

    realize(active)
    return fills, curve, rejects


def equity_series(fills, curve, marks, start, end, use_marks):
    deadlines = np.arange(start + DAY, end + 1, DAY)
    stamps, balance = np.asarray(curve).T
    values = balance[np.searchsorted(stamps, deadlines, side="right") - 1].copy()
    if use_marks:
        for t in fills:
            idx = np.flatnonzero((deadlines > t["ts_in"]) & (deadlines < t["known_exit"]))
            if len(idx):
                closes = marks[t["symbol"]].reindex(deadlines[idx], method="ffill").to_numpy()
                assert np.isfinite(closes).all()
                side = 1 if t["side"] == "BUY" else -1
                values[idx] += (closes - t["entry"]) * side * t["stop_risk"] / t["stop_dist"]
    return np.r_[balance[0], values]


def statistics(fills, equity):
    pnl = np.array([t["pnl"] for t in fills])
    pnl = np.r_[pnl, 0.0]
    dd = (1 - equity / np.maximum.accumulate(equity)).max() * 100
    cagr = ((equity[-1] / equity[0]) ** (365.25 / (len(equity) - 1)) - 1) * 100
    return {
        "n": len(fills), "end": round(equity[-1], 2), "cagr": round(cagr, 3),
        "dd": round(dd, 3), "calmar": round(cagr / dd, 3) if dd else None,
        "pf_usd": round(pnl[pnl > 0].sum() / -pnl[pnl < 0].sum(), 3) if (pnl < 0).any() else None,
        "raw_r": round(sum(t["result_r"] for t in fills), 3),
        "avg_raw_r": round(sum(t["result_r"] for t in fills) / len(fills), 4) if fills else None,
        "worst_day": round(100 * np.min(equity[1:] / equity[:-1] - 1), 3),
    }


def bootstrap(equity):
    """Compound realized or proxy-marked equity returns in 8-calendar-day blocks."""
    returns = equity[1:] / equity[:-1]
    rng = np.random.default_rng(2026)
    starts = rng.integers(0, len(returns) - 7, size=(20_000, 46))
    indices = (starts[..., None] + np.arange(8)).reshape(20_000, -1)[:, :365]
    paths = np.cumprod(returns[indices], axis=1)
    peaks = np.maximum(1, np.maximum.accumulate(paths, axis=1))
    breach = (paths <= 0.90) | (returns[indices] <= 0.95)
    target = paths >= 1.10
    first_breach = np.where(breach.any(axis=1), breach.argmax(axis=1), 366)
    first_target = np.where(target.any(axis=1), target.argmax(axis=1), 366)
    return {
        "trailing10": round(100 * ((paths / peaks).min(axis=1) <= 0.90).mean(), 3),
        "static10_or_daily5": round(100 * (first_breach < 366).mean(), 3),
        "target_before_breach": round(100 * (first_target < first_breach).mean(), 3),
    }


def correlations(fills_by_account, equities):
    matrix = {}
    for period in (60, 14_400):
        returns = {}
        for account, fills in fills_by_account.items():
            frame = pd.DataFrame(fills)
            returns[account] = frame.groupby(frame["ts_in"] // period)["result_r"].sum()
        table = pd.DataFrame(returns)
        corr = table.corr(min_periods=3).round(4).astype(object)
        matrix[f"trade_{period}_corr"] = corr.where(corr.notna(), None).to_dict()
        present = table.notna().astype(int)
        matrix[f"trade_{period}_overlap"] = (present.T @ present).to_dict()
    for label, series in equities.items():
        table = pd.DataFrame(series)
        matrix[label] = table.pct_change().corr().round(4).to_dict()
    return matrix


def stack(fills_by_account, start, end):
    events = []
    for account, fills in fills_by_account.items():
        for t in fills:
            if t["symbol"] in MAJORS:
                key = (account, t["symbol"], t["side"])
                events.extend(((t["ts_in"], 1, key), (min(end, t["known_exit"]), -1, key)))
    states = Counter()
    durations = Counter()
    peaks = Counter()
    previous = start
    for ts, delta, key in sorted(events + [(end, 0, ("", "", ""))]):
        on = [k for k, n in states.items() if n > 0]
        counts = {"any_major": len({a for a, _, _ in on})}
        for symbol in sorted(MAJORS) + ["all_majors"]:
            for side in ("BUY", "SELL"):
                counts[f"{symbol}:{side}"] = len({
                    a for a, s, d in on if d == side and (symbol == "all_majors" or s == symbol)
                })
        for name, n in counts.items():
            if ts > previous:
                peaks[name] = max(peaks[name], n)
                for threshold in range(1, n + 1):
                    durations[(name, threshold)] += ts - previous
        states[key] += delta
        previous = ts
    return {name: {"peak_accounts": peak, "hours_by_min_accounts": {
        n: round(durations[(name, n)] / 3600, 2) for n in range(1, peak + 1)
    }} for name, peak in peaks.items()}


def main():
    cfgs = {p.stem: read_json(p) for p in sorted((PROP / "configs").glob("*.json"))}
    lot_spec_path = DATA / "mt5_mutual_specs.json"
    lot_specs = read_json(lot_spec_path)
    marks, ledger, hashes, cache_hashes, checks = {}, {}, {}, {}, {}
    for symbol in cfgs:
        path = PROP / "trades" / f"{symbol}.parquet"
        trades = pd.read_parquet(path)
        hashes[symbol] = digest(path)
        minute_path = DATA / "cache" / f"{symbol}_m1.parquet"
        minute = pd.read_parquet(minute_path, columns=["bar_1m", "o", "c"])
        cache_hashes[f"{symbol}_1m"] = digest(minute_path)
        minute = minute.set_index("bar_1m")
        fill_prices = minute["o"].reindex(trades["ts_in"]).to_numpy()
        time_prices = minute["c"].reindex(trades["ts_out"]).to_numpy()
        assert np.isfinite(fill_prices).all() and np.isfinite(time_prices).all()
        zero_prints = int((trades["entry"] == 0).sum())
        max_print_error = float(np.abs(trades["entry"] - fill_prices).max())
        assert max_print_error <= 0.00500001
        trades["entry"] = fill_prices
        for tf, group in trades.groupby("tf"):
            cache = DATA / "cache" / f"{symbol}_{tf.lower()}.parquet"
            bars = pd.read_parquet(cache, columns=["end_ts", "atr", "c", "cvd_z24"])
            cache_hashes[f"{symbol}_{tf}"] = digest(cache)
            if tf == "4H":
                marks[symbol] = bars.set_index("end_ts")["c"]
            indexed = bars.iloc[group["bar_i"]]
            trades.loc[group.index, "signal_end"] = indexed["end_ts"].to_numpy()
            trades.loc[group.index, "signal_atr"] = indexed["atr"].to_numpy()
            trades.loc[group.index, "signal_cvd"] = indexed["cvd_z24"].to_numpy()
        lookup = {r["sid"]: r["sl"] for r in cfgs[symbol]["strategy_scorecard"]}
        tp = trades["reason"] == "TP"
        sl = trades["reason"] == "SL"
        timed = trades["reason"] == "TIME"
        sign = trades["side"].map({"BUY": 1, "SELL": -1})
        trades["stop_dist"] = trades["signal_atr"] * trades["sleeve"].map(lookup)
        trades["spread"] = cfgs[symbol]["live_sp_bps"]
        daily = marks[symbol].loc[marks[symbol].index % DAY == 0]
        fast = daily.ewm(span=50, adjust=False, min_periods=50).mean()
        slow = daily.ewm(span=200, adjust=False, min_periods=200).mean()
        daily_side = np.sign(fast - slow).reindex(
            trades["signal_end"], method="ffill", tolerance=DAY - 1).to_numpy()
        trades["daily_aligned"] = daily_side == sign.to_numpy()
        targets = {r["sid"]: r["tp"] for r in cfgs[symbol]["strategy_scorecard"]}
        reconstructed_exit = trades["entry"] + sign * (trades["r"] + trades["fee_r"]) * trades["stop_dist"]
        trades["exit"] = np.select(
            [tp, sl, timed],
            [trades["entry"] + sign * trades["sleeve"].map(targets) * trades["stop_dist"],
             trades["entry"] - sign * trades["stop_dist"], time_prices],
            default=reconstructed_exit,
        )
        lag = trades["ts_in"] - trades["signal_end"]
        implied = (trades["exit"] - trades["entry"]) * sign / trades["stop_dist"] - trades["fee_r"]
        checks[symbol] = {"n": len(trades), "split_straddles": int(
            ((trades["ts_in"] < SPLIT) & (trades["ts_out"] + 60 >= SPLIT)).sum()),
            "lag_not_60s": int((lag != 60).sum()), "max_lag_sec": int(lag.max()),
            "lag_over_5m": int((lag > 300).sum()),
            "rounded_zero_entries": zero_prints, "max_entry_rounding": max_print_error,
            "r_rebuild_max_err": float((implied - trades["r"]).abs().max())}
        ledger[symbol] = trades

    accounts, pools, report, retired = {}, {}, {}, {}
    fills_oos, equities_oos = {}, {"daily_realized_corr": {}, "daily_mark_corr": {}}
    master = read_json(PROP / "prop_26_5k_master.json")
    for path in sorted((PROP / "accounts").glob("Acc_*.json")):
        account = read_json(path)
        key, sid, gov = account["acc_id"], account["sid"], account["governor"]
        accounts[key] = account
        pool = []
        for asset in account["assets"]:
            frame = ledger[asset["symbol"]]
            pool.extend(frame[frame["sleeve"] == sid].to_dict("records"))
        pools[key] = pool
        args = gov["risk_pct"], gov["max_global"], gov["max_crypto_same_side"]
        report[key] = {}
        for name, start, min_ts in (("full", START, None), ("oos", SPLIT, SPLIT)):
            reference = simulate_shared_account(key, pool, 5_000, *args[:2], 1, args[2],
                                                min_ts=min_ts, dd_throttle_1=4, dd_throttle_2=6,
                                                mc_runs=20_000)
            expected = account[f"{name}_metrics"]
            report[key][f"{name}_saved_mismatches"] = {
                k: [expected[k], reference["metrics"].get(k)] for k in expected
                if expected[k] != reference["metrics"].get(k)
            }
            fills, curve, _ = replay(pool, start, END, *args)
            realized = equity_series(fills, curve, marks, start, END, False)
            marked = equity_series(fills, curve, marks, start, END, True)
            closed = [t for t in fills if t["known_exit"] <= END]
            report[key][name] = statistics(fills, realized) | {
                "right_censored": len(fills) - len(closed),
                "proxy_mark_dd": statistics(fills, marked)["dd"],
                "mc_realized": bootstrap(realized),
                "mc_marked": bootstrap(marked),
            }
            if name == "oos":
                fills_oos[key] = fills
                equities_oos["daily_realized_corr"][key] = realized
                equities_oos["daily_mark_corr"][key] = marked
                old, _, _ = replay(pool, start, END, *args, published_order=True)
                fill_keys = lambda rows: {(t["symbol"], t["ts_in"]) for t in rows}
                report[key]["oos_admission_changes"] = len(fill_keys(old) ^ fill_keys(fills))
        report[key]["asset_r"] = {
            sym: round(sum(t["r"] for t in fills_oos[key] if t["symbol"] == sym), 3)
            for sym in sorted({t["symbol"] for t in fills_oos[key]})
        }

    combined = {}
    for group in ("A", "all"):
        ids = [k for k, a in accounts.items() if group == "all" or a["status"].startswith("TIER_A")]
        pool = [t for k in ids for t in pools[k]]
        combined[group] = {}
        for label, period in (("full", START), ("oos", SPLIT)):
            configs = {
                "shared_relaxed_slots": (0.45, 6, 4, {}),
                "shared_one_crypto": (0.45, 2, 1, {"max_crypto": 1, "max_heat": 1}),
                "shared_one_crypto_cost_stress": (
                    0.45, 2, 1, {"max_crypto": 1, "max_heat": 1, "stress_bps": 10}),
                "shared_one_crypto_mt5_lot_proxy": (
                    0.45, 2, 1, {"max_crypto": 1, "max_heat": 1,
                                  "lot_specs": lot_specs, "max_trade_usd": 6.0}),
            }
            by_account = []
            marked_by_account = []
            all_fills = []
            for k in ids:
                g = accounts[k]["governor"]
                fills, curve, _ = replay(pools[k], period, END, g["risk_pct"], g["max_global"],
                                         g["max_crypto_same_side"], cash=5000 / len(ids))
                all_fills.extend(fills)
                by_account.append(equity_series(fills, curve, marks, period, END, False))
                marked_by_account.append(equity_series(fills, curve, marks, period, END, True))
            split_equity = np.sum(by_account, axis=0)
            split_marked = np.sum(marked_by_account, axis=0)
            combined[group][label] = {
                "separate_equal_capital": statistics(all_fills, split_equity)
                | {"mc_realized_pooled": bootstrap(split_equity),
                   "mc_marked": bootstrap(split_marked),
                   "proxy_mark_dd": statistics(all_fills, split_marked)["dd"]}
            }
            for name, (rp, mg, cs, kwargs) in configs.items():
                fills, curve, rejects = replay(pool, period, END, rp, mg, cs, **kwargs)
                realized = equity_series(fills, curve, marks, period, END, False)
                marked = equity_series(fills, curve, marks, period, END, True)
                combined[group][label][name] = statistics(fills, realized) | {
                    "proxy_mark_dd": statistics(fills, marked)["dd"],
                    "mc_realized": bootstrap(realized), "mc_marked": bootstrap(marked),
                    "rejects": dict(rejects),
                }

    for entry in master["accounts"]:
        if entry["status"] != "PURGED_UNPROFITABLE":
            continue
        sid, key = entry["sid"], entry["acc_id"]
        pool, selected = [], []
        for symbol, frame in ledger.items():
            if cfgs[symbol]["live_sp_bps"] > 50:
                continue
            score = next((s for s in cfgs[symbol]["strategy_scorecard"] if s["sid"] == sid), None)
            if score is None or not score["fits_5k_1pct"]:
                continue
            group = frame[frame["sleeve"] == sid]
            train = group[(group["ts_in"] < SPLIT) & (group["ts_out"] + 60 < SPLIT)]["r"]
            pf = train[train > 0].sum() / max(1e-12, -train[train < 0].sum())
            late = train[group.loc[train.index, "ts_in"] >= pd.Timestamp("2022-01-01", tz="UTC").timestamp()]
            recent = train[group.loc[train.index, "ts_in"] >= pd.Timestamp("2023-01-01", tz="UTC").timestamp()]
            if len(train) >= 5 and train.mean() >= 0.18 and pf >= 1.25 and late.sum() > 0 and recent.sum() >= -2.5:
                selected.append(symbol)
                pool.extend(group.to_dict("records"))
        fills, curve, _ = replay(pool, SPLIT, END, 0.45, 4, 2)
        retired[key] = {"sid": sid, "selected": selected} | statistics(
            fills, equity_series(fills, curve, marks, SPLIT, END, False))
        if key == "Acc_09":
            for label, kwargs, tape in (
                ("frozen_cost_stress", {"stress_bps": 10}, pool),
                ("d1_ema50_200", {}, [t for t in pool if t["daily_aligned"]]),
            ):
                fills, curve, _ = replay(tape, SPLIT, END, 0.45, 4, 2, **kwargs)
                retired[key][label] = statistics(
                    fills, equity_series(fills, curve, marks, SPLIT, END, False))

    probes = {}
    for key, name, keep, risk in [
        ("Acc_13", "spread35", lambda t: t["spread"] <= 35, None),
        ("Acc_13", "risk30", lambda t: True, 0.30),
        ("Acc_13", "d1_ema50_200", lambda t: t["daily_aligned"], None),
        ("Acc_05", "cvd_025", lambda t: (1 if t["side"] == "BUY" else -1) * t["signal_cvd"] >= 0.25, None),
        ("Acc_10", "weekday_08_20", lambda t: ((t["signal_end"] // DAY + 3) % 7 < 5
                                               and 8 <= t["signal_end"] % DAY // 3600 < 20), None),
    ]:
        g = accounts[key]["governor"]
        pool = [t for t in pools[key] if keep(t)]
        row = {}
        for label, start in (("full", START), ("oos", SPLIT)):
            fills, curve, _ = replay(pool, start, END, risk or g["risk_pct"], g["max_global"],
                                     g["max_crypto_same_side"])
            eq = equity_series(fills, curve, marks, start, END, False)
            row[label] = statistics(fills, eq) | {"mc_realized": bootstrap(eq)}
        probes[f"{key}_{name}"] = row

    print(json.dumps({
        "ledger_sha256": hashes, "bar_cache_sha256": cache_hashes, "ledger_checks": checks,
        "mt5_lot_spec_sha256": digest(lot_spec_path),
        "accounts": report, "correlations": correlations(fills_oos, equities_oos),
        "tier_a_stack": stack({k: f for k, f in fills_oos.items()
                               if accounts[k]["status"].startswith("TIER_A")}, SPLIT, END),
        "portfolio": combined, "retired": retired, "probes": probes,
    }, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
