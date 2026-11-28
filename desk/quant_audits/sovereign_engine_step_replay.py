"""Replay SovereignCoreEngine.step_once on local native CFD candles using a simulated broker."""

from collections import Counter
import argparse
from copy import deepcopy
from hashlib import file_digest
import json
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import patch

import numpy as np
import pandas as pd

from desk.quant_audits.m4_live_parity_replay import _crypto_signal_key, compute_crypto_vof_series
from engine import sovereign_core_engine as sov
from engine import unified_alpha_pilot as pilot
from engine.sovereign_challengers import challenger_signals
from engine.unified_alpha_pilot import SymbolSpec
from strategies.s08_vol_target_overlay.overlay import vol_target_risk_usd


ROOT = sov.ROOT
DATA = ROOT / "historical_data" / "mt5_parquet"
CACHE = ROOT / "exchange_feed" / "data" / "cache"
SYMBOLS = {"BTCUSD.pi": "BTC", "ETHUSD.pi": "ETH", "USDJPY.pi": "USDJPY",
           "GAUUSD.pi": "GAUUSD", "US2000.p": "US2000"}
EQUITY = 2501.80
DONE = 10009
REJECTED = 10006


def utc(value):
    return int(pd.Timestamp(value, tz="UTC").timestamp())


def iso(ts):
    return pd.Timestamp(ts, unit="s", tz="UTC").isoformat()


def sha(path):
    with path.open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def control_probes(cfg):
    pair = cfg["pairs"][0]
    magic = pair["magic"]
    mapping = {p["magic"]: p for p in cfg["pairs"]}
    gate = sov.core.m4_portfolio_gate
    accepted_string, _ = gate(pair, 1, [], cfg, mapping, {str(magic): 1000}, 1001, EQUITY, 0., 2.)
    accepted_int, _ = gate(pair, 1, [], cfg, mapping, {magic: 1000}, 1001, EQUITY, 0., 2.)

    engine = sov.SovereignCoreEngine.__new__(sov.SovereignCoreEngine)
    engine.cfg, engine.pairs, engine.pair_by_magic = cfg, [pair], mapping
    engine.live, engine.dry = True, False
    engine._offline_replay = True
    engine.state = Obj(state={"bars": {}, "positions": {}, "cooldown": {}}, save=lambda: None)
    engine.journal = MemoryJournal()
    engine._gather_signals = lambda: ({}, {})
    engine._manage_open_positions = lambda *_: None
    pos = Obj(magic=magic, type=0, volume=.01, symbol=pair["mt5_symbol"], price_open=150., ticket=1)
    attempts = []

    def reject_close(*args):
        attempts.append(args)
        return Obj(retcode=10027)

    engine.bridge = Obj(connect=lambda: True, positions=lambda: [pos], total_sl_risk_usd=lambda _: 0.,
                        close_position=reject_close)
    terminal = Obj(terminal_info=lambda: Obj(trade_allowed=False),
                   account_info=lambda: Obj(login=cfg["account"]["expected_login"], equity=EQUITY))
    with patch.object(sov.core, "mt5", terminal):
        engine.step_once(utc("2026-11-20 20:30"))
        friday_red = len(attempts)
        engine.step_once(utc("2026-11-21"))
        saturday_retry = len(attempts) - friday_red

    def unavailable():
        raise RuntimeError("unavailable signal feed")

    engine._gather_signals = unavailable
    terminal_green = Obj(terminal_info=lambda: Obj(trade_allowed=True), account_info=terminal.account_info)
    with patch.object(sov.core, "mt5", terminal_green):
        engine.step_once(utc("2026-11-20 20:30"))
        failed_feed_close = len(attempts) - friday_red - saturday_retry
    with patch.object(pilot, "mt5", Obj(positions_get=lambda: None, orders_get=lambda: None)):
        missing_positions = pilot.MT5Bridge.positions(None)
        missing_committed = pilot.MT5Bridge.total_sl_risk_usd(None, [])
    quantity, _ = sov.core.lots_for_risk(SymbolSpec("TEST", tick_value=.01), 620., 5., 6.25)
    return dict(cooldown_admitted_string_key=accepted_string, cooldown_admitted_integer_key=accepted_int,
                red_interlock_friday_close_attempts=friday_red, saturday_close_retry_attempts=saturday_retry,
                friday_close_attempts_on_feed_failure=failed_feed_close, missing_positions_as=missing_positions,
                missing_pending_risk_as=missing_committed, accepted_gross_at_5usd_target=round(620. * quantity, 2),
                same_gross_times_1_2=round(620. * quantity * 1.2, 2))


def native(symbol, tf):
    path = DATA / tf / f"{symbol}.parquet"
    df = pd.read_parquet(path).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    # The archive includes the last forming candle from copy_rates_from_pos.
    return df.iloc[:-1].reset_index(drop=True)


def local_fp(symbol, tf):
    return pd.read_parquet(CACHE / f"{symbol}_{tf}.parquet")


def prepare(cfg):
    chart = sov.core.discover_chart_strategies()
    crypto = {}
    for symbol in sorted({p["source_symbol"] for p in cfg["pairs"] if p["feed_type"] == "binance_footprint"}):
        g2, g4 = local_fp(symbol, "2h"), local_fp(symbol, "4h")
        crypto[symbol] = (g2, g4, sov.core.eval_crypto_signals(g2, g4, chart))
    tradfi = {}
    for symbol in ("USDJPY.pi", "GAUUSD.pi", "US2000.p"):
        raw = native(symbol, "4H").rename(columns={"timestamp": "time"})
        frame = sov.core.M4LiveEngine._rates_to_df(None, raw, 14400, int(raw["time"].iloc[-1]) + 14400)
        tradfi[symbol] = frame, sov.core.eval_tradfi_signals(frame, chart)

    payloads, parity = {}, {}
    for pair in cfg["pairs"]:
        crypto_feed = pair["feed_type"] == "binance_footprint"
        if crypto_feed:
            g2, g4, values = crypto[pair["source_symbol"]]
            if "signal_params" in pair:
                series = challenger_signals(g2 if pair["tf"] == "2H" else g4, pair)
                vof = np.ones(len(series["c"]))
            else:
                key = _crypto_signal_key(pair)
                series = values[key]
                vof = compute_crypto_vof_series(g2, g4, pair, series["side"])
        else:
            frame, values = tradfi[pair["mt5_symbol"]]
            key = pair["sid"]
            series = values[key]
            vof = np.ones(len(series["c"]))
            if cfg["governor"].get("use_s08_vol_target") or pair.get("use_s08_vol_target"):
                for idx in np.flatnonzero(series["side"]):
                    vof[idx] = vol_target_risk_usd(series["c"][:idx + 1], "H4", 1., .75, 1.25,
                                                  target_ann_vol=.22, lookback=20)
        payloads[pair["pair_id"]] = series, vof
        indices = np.flatnonzero(series["side"])[:-1]
        if len(indices) > 10:
            indices = indices[np.linspace(0, len(indices) - 1, 10, dtype=int)]
        failures = []
        for idx in indices:
            end = int(series["end_ts"][idx])
            if crypto_feed:
                g2_p = g2[g2["end_ts"] <= end]
                g4_p = g4[g4["end_ts"] <= end]
                if "signal_params" in pair:
                    local = challenger_signals(g2_p if pair["tf"] == "2H" else g4_p, pair)
                    expected_vof = 1.
                else:
                    local = sov.core.eval_crypto_signals(g2_p, g4_p, chart)[key]
                    expected_vof = float(local["vof"].get(int(local["side"][-1]), 1.0))
            else:
                # Match live MT5's 1500-bar request including its forming bar.
                local = sov.core.eval_tradfi_signals(frame[frame["end_ts"] <= end].tail(1499), chart)[key]
                expected_vof = float(vof[idx])
            risk_dist_same = ("rd" not in local or
                              np.isclose(local["rd"][-1], series["rd"][idx], atol=1e-8, rtol=1e-6))
            if (local["side"][-1] != series["side"][idx]
                    or not np.isclose(local["atr"][-1], series["atr"][idx], rtol=1e-6)
                    or not np.isclose(vof[idx], expected_vof, atol=1e-6)
                    or not risk_dist_same):
                failures.append(iso(end))
        parity[pair["pair_id"]] = {"tested": len(indices), "mismatches": failures}
    return payloads, parity, tradfi, chart


def load_execution_tapes(symbols=SYMBOLS):
    opens, closes, metadata = {}, {}, {}
    for symbol in symbols:
        rows = []
        for frame, duration in (("1H", 3600), ("15M", 900), ("5M", 300), ("1M", 60)):
            df = native(symbol, frame)
            since = int(df["timestamp"].iloc[0])
            rows = [r for r in rows if r[0] + r[1] <= since]
            rows.extend((int(r.timestamp), duration, float(r.open), float(r.high),
                         float(r.low), float(r.close), float(r.spread)) for r in df.itertuples(index=False))
        metadata[symbol] = {"first": iso(rows[0][0]), "end": iso(rows[-1][0] + rows[-1][1]),
                            "bars_by_seconds": dict(Counter(r[1] for r in rows))}
        for r in rows:
            opens.setdefault(r[0], {})[symbol] = r
            closes.setdefault(r[0] + r[1], {})[symbol] = r
    return opens, closes, metadata


class MemoryJournal:
    def __init__(self):
        self.count = Counter()
        self.by_pair = Counter()
        self.examples = {}

    def log(self, event, **fields):
        note = fields.get("note", "")
        reason = note.split(" ")[0] if event == "GATE_BLOCK" else ""
        key = f"{event}:{reason}" if reason else event
        self.count[key] += 1
        self.by_pair[(fields.get("sleeve", ""), key)] += 1
        self.examples.setdefault(key, fields)


class HistoricalBroker:
    TRADE_RETCODE_DONE = DONE

    def __init__(self, cfg, observations, extra_bps, cap_usd):
        self.cfg, self.observations = cfg, observations
        self.extra_bps, self.cap_usd = extra_bps, cap_usd
        self.now, self.balance, self.ticket = 0, EQUITY, 1
        self.open, self.quotes, self.last_spread = {}, {}, {}
        self.trades, self.exposure = [], []
        self.max_entry_stress_usd = 0.
        self.friday_delays, self.time_only_rejects = [], Counter()

    def connect(self):
        return True

    def ensure_visible(self, symbol):
        return True

    def symbol_info(self, symbol):
        spec = self.spec(symbol)
        return Obj(trade_tick_size=spec.tick_size, trade_stops_level=0, trade_freeze_level=0,
                   digits=spec.digits, point=spec.point)

    def terminal_info(self):
        return Obj(trade_allowed=True)

    def spec(self, symbol):
        item = self.observations[symbol]
        contract = float(item["contract_size"])
        if symbol == "USDJPY.pi":
            quote = self.quotes.get(symbol)
            contract /= quote.bid if quote else 150.
        return SymbolSpec(symbol, digits=int(item["digits"]), point=float(item["point"]),
                          tick_size=float(item["point"]), tick_value=contract * float(item["point"]),
                          vol_min=float(item["volume_min"]), vol_step=float(item["volume_step"]))

    def symbol_info_tick(self, symbol):
        return self.quotes.get(symbol)

    def tick(self, symbol):
        tick = self.quotes.get(symbol)
        if not self.can_fill(tick):
            return None, None
        return tick.ask, tick.bid

    def can_fill(self, tick):
        return tick is not None and 0 <= self.now - tick.time <= 3

    def positions(self):
        return list(self.open.values())

    def total_sl_risk_usd(self, positions):
        return sum(abs(p.sl - p.price_open) * p.volume * self.spec(p.symbol).value_per_price_unit()
                   for p in positions)

    def account_info(self):
        unrealized = 0.
        for p in self.positions():
            q = self.quotes[p.symbol]
            mark = q.bid if p.side == 1 else q.ask
            unrealized += (mark - p.price_open) * p.side * p.volume * self.spec(p.symbol).value_per_price_unit()
        return Obj(equity=self.balance + unrealized, login=self.cfg["account"]["expected_login"])

    def send_bracket(self, spec, side, lots, price, sl, tp, magic, comment, deviation):
        quote = self.quotes.get(spec.name)
        if not self.can_fill(quote):
            return Obj(retcode=REJECTED)
        actual_risk = abs(price - sl) * lots * spec.value_per_price_unit()
        reserve_bps = float(self.cfg["governor"].get("risk_cost_reserve_bps", 10.0))
        adverse = 1.20 * actual_risk + price * lots * spec.value_per_price_unit() * reserve_bps / 10000.
        if self.cap_usd is not None and adverse > self.cap_usd + 1e-9:
            self.time_only_rejects["stress_cap"] += 1
            return Obj(retcode=REJECTED)
        self.max_entry_stress_usd = max(self.max_entry_stress_usd, adverse)
        p = Obj(ticket=self.ticket, symbol=spec.name, side=side, type=0 if side == 1 else 1,
                time=self.now, magic=magic, volume=lots, price_open=price, price_current=price,
                sl=sl, tp=tp, risk=actual_risk, last_close_due=None)
        self.open[p.ticket] = p
        self.ticket += 1
        return Obj(retcode=DONE, order=p.ticket, price=price)

    def modify_sl(self, ticket, sl, tp):
        p = self.open[ticket]
        if not self.can_fill(self.quotes[p.symbol]):
            return Obj(retcode=REJECTED)
        p.sl, p.tp = sl, tp
        return Obj(retcode=DONE)

    def finish(self, p, exit_px, reason):
        self.open.pop(p.ticket)
        value = p.volume * self.spec(p.symbol).value_per_price_unit()
        fee = self.extra_bps * (p.price_open + exit_px) / 20_000 * value
        pnl = (exit_px - p.price_open) * p.side * value - fee
        self.balance += pnl
        self.trades.append(dict(symbol=p.symbol, magic=p.magic, side=p.side, entry_ts=p.time, exit_ts=self.now,
                                pnl=pnl, reason=reason, r=pnl / p.risk, risk=p.risk))

    def close_position(self, p, deviation):
        tick = self.quotes.get(p.symbol)
        friday = sov.non_crypto_schedule_state(self.now, self.cfg) == "FRIDAY_CLOSE"
        if friday and p.last_close_due is None:
            p.last_close_due = self.now
        if not self.can_fill(tick):
            self.time_only_rejects["close_no_fresh_quote"] += 1
            return Obj(retcode=REJECTED)
        if p.last_close_due is not None:
            self.friday_delays.append(self.now - p.last_close_due)
        self.finish(p, tick.bid if p.side == 1 else tick.ask, "friday" if friday else "time")
        return Obj(retcode=DONE)

    def market_event(self, ts, opens, closes):
        self.now = ts
        for symbol, row in closes.items():
            opened, _, price, high, low, close, observed_spread = row
            quote = self.quotes.get(symbol)
            if quote is not None and quote.time == opened:
                width = quote.ask - quote.bid
                for p in list(self.open.values()):
                    if p.symbol != symbol or p.time > opened + 3:
                        continue
                    # Bid OHLC and a causal constant spread proxy for the short-side ask.
                    stop_hit = low <= p.sl if p.side == 1 else high + width >= p.sl
                    tp_hit = high >= p.tp if p.side == 1 else low + width <= p.tp
                    if stop_hit:
                        self.finish(p, p.sl, "stop_priority" if tp_hit else "stop")
                    elif tp_hit:
                        self.finish(p, p.tp, "target")
                self.quotes[symbol] = Obj(time=opened, bid=close, ask=close + width)
            self.last_spread[symbol] = observed_spread * self.observations[symbol]["point"]
        for symbol, row in opens.items():
            spread = self.last_spread.get(symbol)
            if spread is None or spread <= 0:
                continue
            bid = row[2]
            tick = Obj(time=ts, bid=bid, ask=bid + spread)
            self.quotes[symbol] = tick
            for p in list(self.open.values()):
                if p.symbol != symbol:
                    continue
                px = tick.bid if p.side == 1 else tick.ask
                p.price_current = px
                if (px - p.sl) * p.side <= 0:
                    self.finish(p, px, "gap_stop")
                elif (px - p.tp) * p.side >= 0:
                    self.finish(p, p.tp, "target")


def replay(cfg, payloads, opens, closes, start, end, observations, *, extra_bps=0., stress_cap=None,
           contract_overrides=None, include_curve=False):
    observations = deepcopy(observations)
    for symbol, contract in (contract_overrides or {}).items():
        observations[symbol]["contract_size"] = contract
    broker = HistoricalBroker(cfg, observations, extra_bps, stress_cap)
    engine = sov.SovereignCoreEngine.__new__(sov.SovereignCoreEngine)
    engine.cfg = cfg
    engine.pairs = [p for p in cfg["pairs"] if p.get("enabled", True)]
    engine.pair_by_magic = {int(p["magic"]): p for p in engine.pairs}
    engine.dry, engine.live = False, True
    engine._offline_replay = True
    engine.bridge = broker
    engine.journal = MemoryJournal()
    engine.state = Obj(state={"bars": {}, "positions": {}, "cooldown": {}}, save=lambda: None)

    def gather():
        buf, atrs = {}, {}
        for p in engine.pairs:
            pid = p["pair_id"]
            series, vof = payloads[pid]
            lag = int(cfg["governor"].get("sync_margin_sec", 3)) if p["feed_type"] == "binance_footprint" else 0
            lookup = broker.now - lag
            if p["feed_type"] == "mt5_ohlc":
                tick = broker.quotes.get(p["mt5_symbol"])
                if tick is None:
                    continue
                lookup = min(lookup, tick.time)
            i = int(np.searchsorted(series["end_ts"], lookup, side="right")) - 1
            if i < 0:
                continue
            atr = float(series["atr"][i])
            out = dict(ts=int(series["ts"][i]), end_ts=int(series["end_ts"][i]), side=int(series["side"][i]),
                       atr=atr, c=float(series["c"][i]), vof_mult=float(vof[i]))
            if "rd" in series:
                out["sl_dist_override"] = float(series["rd"][i])
            buf[pid] = out
            atrs.setdefault(p["mt5_symbol"], {})[p["tf_sec"]] = atr
        return buf, atrs

    engine._gather_signals = gather
    events = {start, end, *(t for t in opens if start - 3600 <= t < end),
              *(t for t in closes if start - 3600 <= t <= end)}
    for p in engine.pairs:
        if p["feed_type"] == "binance_footprint":
            ends = payloads[p["pair_id"]][0]["end_ts"]
            events.update(int(t) + 3 for t in ends if start <= int(t) + 3 < end)
    for d in pd.date_range(pd.to_datetime(start, unit="s", utc=True).floor("D"),
                           pd.to_datetime(end, unit="s", utc=True), freq="W-FRI"):
        events.add(int(d.timestamp()) + 73800)
    marks = [EQUITY]
    times = [start] if include_curve else None
    peaks = Counter()
    with patch.object(sov, "spec_from_mt5", broker.spec), patch.object(sov.core, "mt5", broker), \
            patch.object(sov.core, "time", Obj(time=lambda: broker.now)):
        for ts in sorted(t for t in events if t <= end):
            broker.market_event(ts, opens.get(ts, {}), closes.get(ts, {}))
            if ts < start:
                continue
            if ts < end:
                engine.step_once(now_ts=ts)
            marks.append(broker.account_info().equity)
            if times is not None:
                times.append(ts)
            peaks["global_open"] = max(peaks["global_open"], len(broker.open))
            peaks["crypto_open"] = max(peaks["crypto_open"], sum(
                sov.is_crypto_pair(engine.pair_by_magic[p.magic], cfg) for p in broker.open.values()))
    pnl = np.array([t["pnl"] for t in broker.trades])
    gain, loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    equity = np.array(marks)
    peak = np.maximum.accumulate(equity)
    drawdowns = 100 * (1 - equity / peak)
    dd = float(np.max(drawdowns))
    end_mark = float(marks[-1])
    cagr = 100 * ((end_mark / EQUITY) ** (365.25 * 86400 / (end - start)) - 1)
    trades_by_symbol = {symbol: {"trades": len(ts), "net": round(sum(t["pnl"] for t in ts), 2)}
                         for symbol in observations if (ts := [t for t in broker.trades if t["symbol"] == symbol])}
    daily = pd.Series(0., index=pd.date_range(pd.Timestamp(iso(start)).normalize(),
                                             pd.Timestamp(iso(end)).normalize()))
    for trade in broker.trades:
        daily.at[pd.Timestamp(iso(trade["exit_ts"])).normalize()] += trade["pnl"]
    rng = np.random.default_rng(20261128)
    block_starts = rng.integers(0, len(daily) - 7, size=(10_000, (len(daily) + 7) // 8))
    block_idx = (block_starts[:, :, None] + np.arange(8)).reshape(10_000, -1)[:, :len(daily)]
    resampled = daily.to_numpy()[block_idx].sum(axis=1)
    result = dict(start=iso(start), end=iso(end), start_equity=EQUITY, end_equity=round(end_mark, 2),
                ending_balance=round(broker.balance, 2), net_pct=round(100 * (end_mark / EQUITY - 1), 3),
                trades=len(pnl), entries=broker.ticket - 1, still_open=len(broker.open),
                win_pct=round(float((pnl > 0).mean() * 100), 2) if len(pnl) else None,
                pf=round(float(gain / loss), 3) if loss else None,
                max_marked_dd_pct=round(dd, 3), annualized_return_pct=round(cagr, 3),
                calmar=round(cagr / dd, 3) if dd else None,
                exit_reasons=dict(Counter(t["reason"] for t in broker.trades)), by_symbol=trades_by_symbol,
                journal=dict(engine.journal.count), journal_examples=engine.journal.examples,
                by_pair={pid: {j: c for (pid0, j), c in engine.journal.by_pair.items() if pid0 == pid}
                         for pid in payloads},
                friday_delays_sec=broker.friday_delays, broker_rejects=dict(broker.time_only_rejects),
                exposure_peaks=dict(peaks), max_entry_gross_risk=round(max((t["risk"] for t in broker.trades), default=0), 2),
                max_entry_stress_usd=round(broker.max_entry_stress_usd, 6),
                monthly_realized_pnl={t.strftime("%Y-%m"): float(v) for t, v in daily.resample("MS").sum().round(2).items()},
                eight_day_block_realized_pnl_percentiles=np.quantile(resampled, [.025, .975]).round(2).tolist(),
                trade_log=broker.trades)
    if times is not None:
        result["curve"] = {"ts": times, "equity": marks}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=sov.DEFAULT_CONFIG_PATH)
    args = parser.parse_args()
    cfg_path = args.config.resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    payloads, parity, _, _ = prepare(cfg)
    opens, closes, coverage = load_execution_tapes()
    sources = {ms: ROOT / "data" / "telemetry" / "assets" / f"{name}.json" for ms, name in SYMBOLS.items()}
    observations = {ms: {f: terms[f] for f in ("symbol", "digits", "point", "contract_size", "volume_min", "volume_step")}
                    for ms, path in sources.items() if (terms := json.loads(path.read_text(encoding="utf-8"))["mt5"])}
    end = utc("2026-11-20 23:45")
    start = utc("2026-05-01")
    arms = {
        "as_configured": replay(cfg, payloads, opens, closes, start, end, observations),
        "plus_10bp": replay(cfg, payloads, opens, closes, start, end, observations, extra_bps=10.),
        "plus_20bp": replay(cfg, payloads, opens, closes, start, end, observations, extra_bps=20.),
        "stressed_6usd_screen": replay(cfg, payloads, opens, closes, start, end, observations,
                                       extra_bps=10., stress_cap=6.),
        "alternate_gold_contract_10": replay(cfg, payloads, opens, closes, start, end, observations,
                                             contract_overrides={"GAUUSD.pi": 10.}),
        "finer_bars_oct_nov": replay(cfg, payloads, opens, closes, utc("2026-10-02"), end, observations),
    }
    off = deepcopy(cfg)
    off["session_policy"]["friday_close_non_crypto"] = False
    arms["without_weekend_rule"] = replay(off, payloads, opens, closes, start, end, observations)
    paths = [cfg_path, Path(sov.__file__), Path(sov.core.__file__)]
    paths.extend(DATA / tf / f"{ms}.parquet" for tf in ("1M", "5M", "15M", "1H", "4H") for ms in SYMBOLS)
    paths.extend(CACHE / f"{s}_{tf}.parquet" for s in ("BTCUSDT", "ETHUSDT") for tf in ("2h", "4h"))
    print(json.dumps(dict(scenarios=arms, coverage=coverage, prefix_checks=parity, control_probes=control_probes(cfg),
                          contract_proxies=observations,
                          input_sha256={str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p): sha(p) for p in paths},
                          spread="previous closed native spread; observed CFD candle open as bid",
                          fees="spread proxy, plus specified notional surcharge only; no swaps, latency or venue fills"),
                     indent=2, allow_nan=False, default=lambda x: x.item()))


if __name__ == "__main__":
    main()
