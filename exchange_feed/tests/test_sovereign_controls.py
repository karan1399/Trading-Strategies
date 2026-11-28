import json
from types import SimpleNamespace as Obj

import numpy as np
import pandas as pd

from desk.quant_audits.sovereign_engine_step_replay import MemoryJournal, utc
from engine import sovereign_core_engine as sov
from engine.sovereign_challengers import challenger_signals


def harness():
    cfg = json.loads(sov.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    engine = sov.SovereignCoreEngine.__new__(sov.SovereignCoreEngine)
    engine.cfg = cfg
    engine.pairs = cfg["pairs"]
    engine.pair_by_magic = {p["magic"]: p for p in cfg["pairs"]}
    engine.live, engine.dry = True, False
    engine.journal = MemoryJournal()
    engine.state = Obj(state={"bars": {}, "positions": {}, "cooldown": {}}, save=lambda: None)
    return engine


def terminal(engine, *, allowed=True, positions=lambda: [], orders=lambda: []):
    return Obj(terminal_info=lambda: Obj(trade_allowed=allowed),
               account_info=lambda: Obj(login=engine.cfg["account"]["expected_login"], equity=2501.80),
               positions_get=positions, orders_get=orders)


def test_red_interlock_blocks_protective_operations_and_persists_record(monkeypatch, tmp_path):
    engine = harness()
    engine.bridge = Obj(connect=lambda: True)
    monkeypatch.setattr(sov, "INTERLOCK_DIR", tmp_path)
    monkeypatch.setattr(sov.core, "mt5", terminal(engine, allowed=False))
    result = engine.step_once(utc("2026-11-20 20:30"))
    assert not result["trade_allowed"]
    assert engine.state.state["interlock_rearm_required"]
    assert engine.state.state["weekend_flatten_due"]
    events = list(tmp_path.glob("interlock_*.json"))
    assert len(events) == 1
    assert json.loads(events[0].read_text())["value"] is False


def test_failed_position_snapshot_halts_without_reaching_signals(monkeypatch):
    engine = harness()
    engine.bridge = Obj(connect=lambda: True)
    monkeypatch.setattr(sov.core, "mt5", terminal(engine, positions=lambda: None))
    result = engine.step_once(utc("2026-11-19 13:30"))
    assert result["committed_risk"] is None
    assert result["signals_evaluated"] == 0


def test_interlock_falling_during_cycle_blocks_close_at_bridge(monkeypatch, tmp_path):
    engine = harness()
    monkeypatch.setattr(sov, "INTERLOCK_DIR", tmp_path)
    monkeypatch.setattr(sov.core, "mt5", terminal(engine, allowed=False))
    monkeypatch.setattr(sov.core.MT5Bridge, "close_position", lambda *_: (_ for _ in ()).throw(
        AssertionError("blocked close reached the venue bridge")))
    guarded = sov.SovereignProtectionBridge(engine.cfg, engine._allow_protective_mutation)
    assert guarded.close_position(Obj(ticket=10), 30) is None
    assert engine.state.state["interlock_rearm_required"]
    assert list(tmp_path.glob("interlock_*.json"))


def test_missed_friday_flatten_retries_at_sunday_reopen_even_if_feed_fails(monkeypatch):
    engine = harness()
    pair = engine.pairs[0]
    pos = Obj(ticket=5, magic=pair["magic"], symbol=pair["mt5_symbol"], type=0, volume=.01, price_open=1.)
    positions = [pos]
    mutations = []

    def close_position(*_):
        mutations.append(utc_time[0])
        if len(mutations) == 2:
            positions.clear()
        return Obj(retcode=10009 if not positions else 10006)

    def unavailable():
        raise RuntimeError("feed down")

    utc_time = [utc("2026-11-20 20:30")]
    engine._gather_signals = unavailable
    engine._manage_open_positions = lambda *_: None
    engine.bridge = Obj(connect=lambda: True, close_position=close_position, total_sl_risk_usd=lambda _: 0.)
    monkeypatch.setattr(sov.core, "mt5", terminal(engine, positions=lambda: tuple(positions)))
    for clock in ("2026-11-20 20:30", "2026-11-21 00:00", "2026-11-22 22:05"):
        utc_time[0] = utc(clock)
        engine.step_once(utc_time[0])
    assert mutations == [utc("2026-11-20 20:30"), utc("2026-11-22 22:05")]
    assert not engine.state.state["weekend_flatten_due"]
    assert engine.state.state["cooldown"][str(pair["magic"])] == mutations[-1]
    allowed, reason = sov.core.m4_portfolio_gate(pair, 1, [], engine.cfg, engine.pair_by_magic,
                                                engine.state.state["cooldown"], mutations[-1] + 1, 2501.80, 0., 1.)
    assert (allowed, reason) == (False, "cooldown active")


def test_missing_orders_do_not_reach_exposure_accounting(monkeypatch):
    engine = harness()
    engine.bridge = Obj(connect=lambda: True)
    engine._gather_signals = lambda: ({}, {})
    engine._manage_open_positions = lambda *_: None
    monkeypatch.setattr(sov.core, "mt5", terminal(engine, orders=lambda: None))
    result = engine.step_once(utc("2026-11-19 13:30"))
    assert result["committed_risk"] is None


def test_unprotected_or_unvalueable_exposure_blocks_proposals(monkeypatch):
    engine = harness()
    item = Obj(type=0, price_open=100., price_current=105., sl=97., volume=.01, symbol="EXAMPLE")
    broker = Obj(order_calc_profit=lambda order_type, sym, vol, mark, stop: (stop-mark)*vol)
    monkeypatch.setattr(sov.core, "mt5", broker)
    assert engine._committed_live_risk([item], []) > 0.
    broker.order_calc_profit = lambda *_: None
    assert engine._committed_live_risk([item], []) is None
    item.sl = 0.
    with_unexpected_call = lambda *_: (_ for _ in ()).throw(AssertionError("an unprotected stop was valued"))
    broker.order_calc_profit = with_unexpected_call
    assert engine._committed_live_risk([item], []) is None


def test_s09_challenger_breakout_uses_only_prior_channel_bars():
    n = 245
    ts = np.arange(n) * 14400
    close = np.arange(n) + 100.
    frame = pd.DataFrame(dict(ts=ts, end_ts=ts + 14400, h=close + .25, l=close - .25, c=close,
                              atr=1.0, ema233=close - 20., clv=.5, d_pct=2., buy_imb=4, sell_imb=1))
    pair = {"sid": "s09_donchian_trend_cta", "signal_params": {"channel": 16}}
    full = challenger_signals(frame, pair)
    assert full["side"][-1] == 1
    np.testing.assert_array_equal(full["side"][:-1], challenger_signals(frame.iloc[:-1], pair)["side"])
    frame.loc[n - 1, "c"] = frame.loc[n - 2, "h"]
    assert challenger_signals(frame, pair)["side"][-1] == 0
