import numpy as np
import pandas as pd
import pytest

from Delta_Exchange_Data.delta_pa_live_trader import compute_safe_contracts
from engine.m4_live_engine import BinanceAggTradeCollector
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk
from footprint_Chart.backtesting.fast_kernels import sim_1m_trades_nb
from footprint_Chart.backtesting.multi_asset_prop_optimizer import _score_is_oos
from footprint_Chart.backtesting.mt5_shared_account_portfolio_optimizer import simulate_shared_account
from footprint_Chart.backtesting.oi_vwap_sweep_research import attach_causal_oi_vwap_features
from footprint_Chart.backtesting.prop_execution import ExecutionTape, replay_lot_risk
from footprint_Chart.backtesting.strategy_26_5k_optimizer import _build_wf_trade_pool
from strategies.s11_of_initiative_breakout.strategy import (
    OrderFlowInitiativeBreakoutStrategy,
    completed_d1_directions,
)
from scripts.delta_pa_trend_optimizer import _score_arr, _sim_combo, _sim_delta_nb


def test_is_score_ignores_unsettled_and_outer_labels():
    entries = np.array([1, 2, 3, 4, 9, 10, 15, 20])
    exits = np.array([2, 3, 4, 5, 10, 12, 20, 21])
    values = np.array([1.0, -0.5, 1.5, 0.25, 900.0, 2.0, 800.0, 700.0])
    a = _score_is_oos(entries, values, 10, 4, exit_available_ts=exits, evaluation_end_ts=20)
    changed = values.copy()
    changed[[4, 6, 7]] = -10_000
    b = _score_is_oos(entries, changed, 10, 4, exit_available_ts=exits, evaluation_end_ts=20)
    assert a == b
    assert (a["nis"], a["noos"], a["isr"], a["oosr"]) == (4, 1, 2.25, 2.0)


def test_stage_a_replays_closed_tail_and_gapped_stop_without_fabricating_time_exit():
    ts = np.arange(240, dtype=np.int64) * 3600
    end = ts + 3600
    atr = np.ones(240)
    close = np.full(240, 100.0)
    side = np.zeros(240, dtype=np.int8)
    side[235] = 1
    minutes = np.arange(end[235], end[235] + 4 * 60, 60)
    prices = np.array([100.0, 100.0, 96.0, 96.0])
    no_funding = np.array([], dtype=float)

    def replay(bars, minute_count):
        tape = prices[:minute_count]
        return sim_1m_trades_nb(ts[:bars], end[:bars], atr[:bars], close[:bars], close[:bars],
                                close[:bars], side[:bars], minutes[:minute_count], tape, tape, tape, tape,
                                3600, 2.0, 2.0, 0.0, 0.0, 0.0, False, 1, 1, False, False,
                                0.0, False, 0.0, 0.0, 0, 0.0, 0.0, no_funding.astype(np.int64), no_funding,
                                closed_bar_replay=True)

    assert len(replay(236, 2)[0]) == 0
    prefix, full = replay(236, 3), replay(240, 4)
    for actual, expected in zip(prefix, full):
        np.testing.assert_array_equal(actual, expected)
    assert prefix[2].tolist() == [minutes[2]]
    assert prefix[6].tolist() == [96.0]
    assert prefix[7].tolist() == [-2.0]


def test_walkforward_requires_refitted_recipe_and_purges_training_cutoff():
    first = int(pd.Timestamp("2022-07-01", tz="UTC").timestamp())

    def refit(sid, lower, split, end):
        if split != first:
            return []
        rows = [dict(ts_in=split - 1000 + i, ts_out=split - 100 + i, r=r,
                     symbol="A") for i, r in enumerate([1.0, 1.0, -0.5, 0.75])]
        unresolved = dict(ts_in=split - 20, ts_out=split, r=-100, symbol="A")
        later = dict(ts_in=split + 60, ts_out=end - 90, r=2.0, symbol="A")
        return [dict(symbol="A", sp_bps=4.0, train_start_ts=lower, fit_asof_ts=split,
                     cfg=sid, trades=rows + [unresolved, later])]

    pool, _, report = _build_wf_trade_pool("S", refit)
    assert [x["r"] for x in pool] == [2.0]
    assert report[0]["configs"] == {"A": "S"}

    def stale(sid, lower, split, end):
        return [dict(x, fit_asof_ts=split + 60) for x in refit(sid, lower, split, end)]

    with pytest.raises(ValueError, match="fold-local"):
        _build_wf_trade_pool("S", stale)


def test_quote_gate_uses_available_at_and_stressed_minimum_lot():
    q = dict(available_at=100, spread_bps=2.0, commission_bps=.5,
             min_stop_distance=.01, tick_value_loss=1.0, tick_size=1.0, vol_min=.01, vol_max=1.0, vol_step=.01)
    expensive = dict(q, available_at=160, tick_value_loss=100.0)
    tape = ExecutionTape([expensive, q], [], max_quote_age=120, cashflow_coverage=(0, 500))
    assert tape.quote_at(99) is None
    assert tape.admissible(159, 100, 10)
    assert not tape.admissible(160, 100, 10)
    assert tape.quote_at(281) is None
    assert not tape.admissible(159, 100, .02)
    terms = tape.sizing_terms(159, 100, 10)
    assert replay_lot_risk(terms, .10, 5000.) is None
    assert replay_lot_risk(terms, .22, 5000.) == pytest.approx((.2, .2036))


def test_stressed_lot_reservation_drives_heat_but_only_gross_risk_drives_pnl():
    base = dict(ts_in=1, ts_out=10, exit_available_ts=11, category="CRYPTO", side="BUY", bucket="test",
                sleeve="s", r=1.0, gross_per_lot=400.0, stressed_per_lot=500.0, hard_risk_cap_usd=6.0,
                vol_min=.01, vol_max=1.0, vol_step=.01)
    rows = [dict(base, symbol=symbol, mt5_symbol=symbol) for symbol in ("A", "B")]
    result = simulate_shared_account("strict", rows, 900., risk_pct=1.0, max_open_risk_pct=1.0,
                                     size_risk_usd=replay_lot_risk)
    assert result["metrics"]["trades"] == 1
    assert result["metrics"]["end_equity"] == 904.0


def test_costs_use_both_quotes_and_signed_realized_rollover():
    q = dict(available_at=100, spread_bps=2.0, commission_bps=.5,
             min_stop_distance=.01, tick_value_loss=1.0, tick_size=1.0, vol_min=.01, vol_max=1.0, vol_step=.01)
    tape = ExecutionTape([q, dict(q, available_at=200, spread_bps=6.0)],
                         [dict(cashflow_ts=150, buy_bps=3.0, sell_bps=-3.0)],
                         cashflow_coverage=(0, 500))
    result = [np.array([v]) for v in (0, 100, 140, 1, 1, 100., 90., -1., 0., 0)]
    tape.apply_costs(result, np.array([10.]))
    assert result[8][0] == pytest.approx(((1.5 + 3) * 100 + 3.5 * 90) / 100_000)
    assert result[7][0] == pytest.approx(-1 - result[8][0])
    uncovered = ExecutionTape([q], [], cashflow_coverage=(0, 199))
    with pytest.raises(ValueError, match="rollover coverage"):
        uncovered.apply_costs(result, np.array([10.]))


def test_d1_midnight_warmup_gap_and_prefix():
    day, h4 = 86400, 14400
    closes = [dict(time=i * day - h4, end_ts=i * day, close=100. + i)
              for i in range(1, 201)]
    late_friday = dict(time=200 * day, end_ts=200 * day + h4, close=1000.)
    weekend_gap = dict(time=203 * day, end_ts=203 * day + h4, close=1000.)
    rows = closes + [late_friday, weekend_gap]
    got = completed_d1_directions(rows)
    assert got[-4:] == [0, 1, 1, 0]
    for cut in (198, 199, 200, 201):
        assert completed_d1_directions(rows[:cut]) == got[:cut]
    assert completed_d1_directions(rows, max_age_seconds=4 * day)[-1] == 1
    assert completed_d1_directions(rows[:-1] + [dict(weekend_gap, end_ts=None)])[-1] == 0


def test_initiative_short_input_uses_same_ema_as_full_history():
    prices = [100 + (i % 17) + i / 10 for i in range(270)]
    bars = [dict(time=i * 3600, end_ts=(i + 1) * 3600, open=p-1, high=p, low=p-2,
                 close=p, delta_pct=10.0, buy_imbalances=3, sell_imbalances=1)
            for i, p in enumerate(prices)]
    fn = OrderFlowInitiativeBreakoutStrategy().generate_signals
    assert fn(bars, {})
    for cut in (55, 100, 259):
        assert fn(bars[:cut], {}) == [signal for signal in fn(bars, {}) if signal.index < cut]


def test_m4_lot_rounding_respects_cap_even_above_minimum_lot():
    spec = SymbolSpec("test", digits=2, point=.01, tick_value=.01,
                      tick_size=.01, vol_min=.01, vol_step=.01)
    lots, _ = lots_for_risk(spec, 100., 35., 20.)
    assert lots == .2
    assert lots_for_risk(spec, 2500., 5.0, 20.)[0] is None


def test_ws_minute_ohlc_uses_trade_order_and_force_never_flushes_open_bar(tmp_path, monkeypatch):
    from engine import m4_live_engine as m4

    clock = [120.]
    monkeypatch.setattr(m4.time, "time", lambda: clock[0])
    sink = BinanceAggTradeCollector(["BTCUSDT"], tmp_path)
    for px, event, ts in [(115., 3, 61003), (100., 1, 61001), (120., 2, 61002),
                          (115., 3, 61003), (125., 4, 120001)]:
        sink._on_trade("BTCUSDT", px, 1.0, event, event, ts, False)
    sink.flush_closed()
    assert not list(tmp_path.glob("*.parquet"))
    with monkeypatch.context() as io_failure:
        def fail_write(*_args, **_kwargs):
            raise OSError("temporary disk error")

        io_failure.setattr(m4.pq, "write_table", fail_write)
        with pytest.raises(OSError, match="temporary disk error"):
            sink.flush_closed(force=True)
    sink.flush_closed(force=True)
    path = next(tmp_path.glob("*.parquet"))
    closed = pd.read_parquet(path)
    assert closed.bar_1m.unique().tolist() == [60]
    assert closed.iloc[0][["open", "high", "low", "close"]].tolist() == [100, 120, 100, 115]
    assert closed["buy_qty"].sum() == 3
    sink._on_trade("BTCUSDT", 999, 1.0, 5, 5, 61004, False)
    assert sink.late_count == 1


def test_delta_training_and_concurrency_wait_for_execution_bar_close():
    rows = [("B", 0, 0, -1.0), ("A", 0, 3600, 1.0), ("C", 3600, 3600, 900.0)]
    result = _sim_combo(rows, max_conc=1, max_ts=10_800)
    assert result["n"] == 1 and result["net_r"] == 1.0
    assert _sim_combo(rows, max_conc=1, max_ts=7200)["n"] == 0
    score = _score_arr(np.array([0, 3600, 7200]), np.array([0, 3600, 7200]),
                       np.array([1.0, 99.0, 2.0]), 7200)
    assert (score["nis"], score["isr"], score["noos"], score["oosr"]) == (1, 1.0, 1, 2.0)


def test_delta_stops_fill_through_gaps_and_tail_labels_stay_unsettled():
    times = np.arange(240, dtype=np.int64) * 3600
    prices = np.full(240, 100.)
    side = np.zeros(240, dtype=np.int8)
    side[235] = 1
    atr = np.ones(240)

    def replay(px):
        return _sim_delta_nb(times, times + 3600, atr, prices, side, times,
                             px, np.maximum(px, 100.), np.minimum(px, 100.), px,
                             1., 2., 0., 0., 12, 3600, 0.)

    assert len(replay(prices)[0]) == 0
    gap = prices.copy()
    gap[237] = 95.
    assert replay(gap)[2].tolist() == [-5.0]


def test_delta_never_rounds_up_to_one_over_budget_or_margin_cap():
    assert compute_safe_contracts(10., 10., 10., .20, 1., 5.)[0] == 0
    assert compute_safe_contracts(5000., 5000., 100., 1., 1., 5.)[0] == 0
    qty, _ = compute_safe_contracts(5000., 5000., 10., .20, 1., 5.)
    assert qty == 2
    assert qty * (0.20 + 10 * .0025) <= .50 and qty * 10 * .05 <= 1.50


def test_strict_oi_rejects_unpublished_and_backfilled_older_samples():
    bars = pd.DataFrame([dict(ts=100, end_ts=1000, h=10., l=9., c=9.5, v=20.),
                         dict(ts=1000, end_ts=1200, h=10., l=9., c=9.5, v=20.)])
    metrics = pd.DataFrame([
        dict(ts=50, available_at=50, oi_coin=120.),
        dict(ts=800, available_at=1100, oi_coin=60.),
        dict(ts=900, available_at=950, oi_coin=100.),
        dict(ts=1000, available_at=1050, oi_coin=10.),
    ]).assign(oi_usd=1., top_ls=1., global_ls=1., taker_ls=1.)
    aligned = attach_causal_oi_vwap_features(bars, metrics, 2, require_available_at=True)
    assert aligned["oi_coin"].tolist() == [100., 10.]
    assert aligned["max_drop_bar_pct"].iloc[0] == pytest.approx(-100 / 6)
    with pytest.raises(ValueError, match="availability"):
        attach_causal_oi_vwap_features(bars, metrics.drop(columns="available_at"), 2,
                                      require_available_at=True)


def test_delta_api_error_never_looks_like_an_empty_position_book(monkeypatch):
    from Delta_Exchange_Data import delta_pa_live_trader as delta

    class BrokenReadback:
        def get_public(self, *_):
            return {"result": []}

        def request(self, *_args, **_kwargs):
            return {"success": False, "result": None}

    monkeypatch.setattr(delta, "check_wallet_and_ip", lambda *_: (True, 2500., 100.))
    monkeypatch.setattr(delta, "manage_trailing_stops", lambda *_: pytest.fail("unsafe management"))
    delta.run_cycle(BrokenReadback(), {}, [], {}, {}, live_mode=False)
