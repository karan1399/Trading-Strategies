"""Regression, Zero-Lookahead Causality, Schedule, and Parity Tests for HolyGrailEngine."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import holy_grail_engine as hg
from engine import unified_alpha_pilot as pilot


def _synth_4h_df(n: int = 320, start_ts: int = 1700000000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = start_ts + np.arange(n, dtype=np.int64) * 14400
    drift = np.linspace(100.0, 145.0, n) + np.cumsum(rng.normal(0.0, 0.4, n))
    drift = np.maximum(10.0, drift)
    spread = np.abs(rng.normal(1.2, 0.3, n))
    bv = rng.uniform(500.0, 1200.0, n)
    sv = rng.uniform(400.0, 1000.0, n)
    return pd.DataFrame({
        "ts": ts,
        "end_ts": ts + 14400,
        "o": drift,
        "h": drift + spread,
        "l": np.maximum(1.0, drift - spread),
        "c": drift + rng.normal(0.0, 0.25, n),
        "bv": bv,
        "sv": sv,
        "v": bv + sv,
        "nt": rng.integers(80, 300, n),
        "buy_imb": rng.integers(0, 3, n),
        "sell_imb": rng.integers(0, 3, n),
        "oi_chg_6b": rng.normal(0.5, 2.5, n),
        "fund_bp": rng.normal(0.4, 0.5, n),
    })


def _synth_1m_df(n: int = 1900, start_ts: int = 1700000000, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = start_ts + np.arange(n, dtype=np.int64) * 60
    c = 100.0 + np.cumsum(rng.normal(0.005, 0.08, n))
    c = np.maximum(5.0, c)
    v = rng.uniform(20.0, 90.0, n)
    return pd.DataFrame({
        "bar_1m": ts,
        "open": c,
        "high": c + 0.15,
        "low": np.maximum(1.0, c - 0.15),
        "close": c + rng.normal(0.0, 0.03, n),
        "volume": v,
        "taker_buy_vol": v * rng.uniform(0.35, 0.72, n),
        "count": rng.integers(5, 45, n),
    })


def test_zero_lookahead_4h_physics_prefix_truncation() -> None:
    df_full = _synth_4h_df(320)
    btc_full = _synth_4h_df(320, seed=99)
    cut = 280
    f_full = hg.compute_4h_physics_features(df_full, btc_full).iloc[:cut]
    f_pref = hg.compute_4h_physics_features(df_full.iloc[:cut].copy(), btc_full.iloc[:cut].copy())

    for col in ("vr4", "hurst_4h", "bb_w_rk", "was_sq", "brk_up20", "brk_dn20", "sc_long", "sc_short", "btc_bull"):
        a = f_full[col].to_numpy()
        b = f_pref[col].to_numpy()
        if a.dtype == bool or b.dtype == bool:
            assert np.array_equal(a, b), f"Lookahead drift in bool feature {col}"
        else:
            np.testing.assert_allclose(a, b, rtol=1e-10, atol=1e-10, equal_nan=True)


def test_zero_lookahead_volclock_prefix_truncation() -> None:
    k1 = _synth_1m_df(1950)
    cut = 1850
    vc_full = hg.build_causal_volclock_bars(k1, None, "TESTUSDT", is_comm=True)
    vc_pref = hg.build_causal_volclock_bars(k1.iloc[:cut].copy(), None, "TESTUSDT", is_comm=True)
    cut_ts = int(k1["bar_1m"].iloc[cut - 1]) + 60
    sub_full = vc_full[vc_full["end_ts"] <= cut_ts].reset_index(drop=True)
    sub_pref = vc_pref[vc_pref["end_ts"] <= cut_ts].reset_index(drop=True)
    assert len(sub_full) >= 3 and len(sub_pref) == len(sub_full)
    # Every closed bar constructed strictly inside prefix must match open/high/low/close/d_pct exactly
    for col in ("ts", "end_ts", "o", "h", "l", "c", "d_pct", "vah", "val"):
        np.testing.assert_allclose(sub_full[col].to_numpy(float), sub_pref[col].to_numpy(float), rtol=1e-10, atol=1e-10)


def test_commodity_friday_gap_splits_volclock_bar() -> None:
    k1 = _synth_1m_df(1800)
    # Inject a 48h weekend gap after bar 1650 (beyond 720m warmup)
    k1.loc[1650:, "bar_1m"] += 48 * 3600
    vc = hg.build_causal_volclock_bars(k1, None, "CLUSDT", is_comm=True)
    assert not vc.empty
    # No Volume-Clock bar should span > 240 minutes or across the 48h gap
    assert int((vc["end_ts"] - vc["ts"]).max()) <= 240 * 60 + 60


def test_macro_blackout_and_fomc_15m_causality() -> None:
    fomc_ts = hg.et_to_utc_ts("2026-09-16", 14, 0)
    k1 = _synth_1m_df(1600, start_ts=fomc_ts - 1500 * 60)
    idx0 = int(np.searchsorted(k1["bar_1m"].to_numpy(np.int64), fomc_ts))
    # Construct bullish aligned 15m FOMC impulse
    k1.loc[idx0 : idx0 + 14, "open"] = 100.0
    k1.loc[idx0 : idx0 + 14, "close"] = 102.0
    k1.loc[idx0 : idx0 + 14, "volume"] = 1000.0
    k1.loc[idx0 : idx0 + 14, "taker_buy_vol"] = 700.0  # dp15 = +40% >= 4%

    # Must return None BEFORE t+15m closes
    assert hg.eval_fomc_15m_aligned(k1, fomc_ts, "BTCUSDT", fomc_ts + 600) is None
    sig = hg.eval_fomc_15m_aligned(k1, fomc_ts, "BTCUSDT", fomc_ts + 900)
    assert sig is not None and sig["sleeve"] == "B_FOMC_15m_ALN" and sig["side"] == 1


def test_portfolio_gate_sleeve_non_blocking_and_friday_rule() -> None:
    cfg = json.loads(hg.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    gov = cfg["governor"]
    ev_ts = hg.et_to_utc_ts("2026-09-16", 14, 0)
    fri_open = int(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc).timestamp())
    fri_close = int(datetime(2026, 11, 27, 20, 30, tzinfo=timezone.utc).timestamp())

    # Macro blackout suppresses A & C, allows B
    assert hg.holy_grail_portfolio_gate({"sym": "BTCUSDT", "family": "A", "side": 1}, [], gov, [ev_ts], ev_ts - 1200) == (False, "MACRO_PRE_EVENT_BLACKOUT")
    assert hg.holy_grail_portfolio_gate({"sym": "BTCUSDT", "family": "C", "side": 1}, [], gov, [ev_ts], ev_ts + 300) == (False, "MACRO_PRE_EVENT_BLACKOUT")
    assert hg.holy_grail_portfolio_gate({"sym": "BTCUSDT", "family": "B", "side": 1}, [], gov, [ev_ts], ev_ts + 900)[0] is True

    # Friday 20:30 UTC blocks commodity, allows 24/7 crypto
    assert hg.holy_grail_portfolio_gate({"sym": "CLUSDT", "family": "A", "side": 1, "is_comm": True}, [], gov, [], fri_close) == (False, "FRIDAY_COMMODITY_BLACKOUT")
    assert hg.holy_grail_portfolio_gate({"sym": "BTCUSDT", "family": "A", "side": 1, "is_comm": False}, [], gov, [], fri_close)[0] is True

    # Non-blocking per-sleeve gate: 1 A + 1 C allowed on same asset, 2nd A rejected
    active = [{"sym": "SOLUSDT", "family": "A", "side": 1}]
    assert hg.holy_grail_portfolio_gate({"sym": "SOLUSDT", "family": "C", "side": 1, "is_comm": False}, active, gov, [], fri_open)[0] is True
    assert hg.holy_grail_portfolio_gate({"sym": "SOLUSDT", "family": "A", "side": 1, "is_comm": False}, active, gov, [], fri_open) == (False, "MAX_PER_ASSET_SLEEVE")


def test_t4_convex_trail_short_ratchet_and_dd_hysteresis() -> None:
    # Long: entry=100, orig_sd=5, atr=2; BE triggers at +1.5R (107.5) -> SL becomes 100.25
    sl1 = hg.compute_t4_trail_sl(100.0, 95.0, 108.0, 2.0, 5.0, side=1)
    assert math.isclose(sl1, 100.25, rel_tol=1e-6)
    # Price runs to 120 -> 5.0 * 2.0 = 10 trail below 120 -> 110.0
    sl2 = hg.compute_t4_trail_sl(100.0, sl1, 120.0, 2.0, 5.0, side=1)
    assert math.isclose(sl2, 110.0, rel_tol=1e-6)
    # Short climax ratchet: entry=100, orig_sd=5, close=96 (+0.8R), climax_warn=True -> min(98.25, 96+1.6=97.6) = 97.6
    sl_sh = hg.compute_t4_trail_sl(100.0, 105.0, 96.0, 2.0, 5.0, side=-1, cur_close=96.0, climax_warn=True)
    assert math.isclose(sl_sh, 97.6, rel_tol=1e-6)

    # DD Hysteresis: 10% -> 0.50x, 15% -> 0.25x, 6.4% stays 0.50x, <5% restores 1.0x
    assert hg.holy_grail_dd_mult(2250.0, 2500.0, False) == (0.50, True)
    assert hg.holy_grail_dd_mult(2125.0, 2500.0, True) == (0.25, True)
    assert hg.holy_grail_dd_mult(2340.0, 2500.0, True) == (0.50, True)
    assert hg.holy_grail_dd_mult(2400.0, 2500.0, True) == (1.0, False)


def test_venue_sizing_non_compression_and_engine_step(tmp_path: Path) -> None:
    # Strict MT5 non-compression when min_lot risks more than max_risk
    sp = pilot.SymbolSpec("BTCUSD.pi", 2, 0.01, 0.01, 0.01, 0.01, 0.01)
    lots_bad, _ = hg.venue_risk_size(3000.0, 18.75, 25.0, spec=sp, venue="mt5")
    assert lots_bad is None
    qty_perp, _ = hg.venue_risk_size(3000.0, 18.75, 25.0, venue="perp")
    assert qty_perp == round(18.75 / 3000.0, 6)

    # Step dry engine cleanly in isolated temp state/journal paths (offline mock without hitting MT5/REST)
    st_p = tmp_path / "hg_state.json"
    jr_p = tmp_path / "hg_journal.csv"
    eng = hg.HolyGrailEngine(hg.DEFAULT_CONFIG_PATH, st_p, jr_p, dry=True, live=False)
    eng._offline_replay = True
    flatten_calls = []
    eng._flatten_non_crypto_for_friday = lambda pos: flatten_calls.append(len(pos))
    eng._hg_sleeve_signals = lambda ts: ([
        {"sym": "BNBUSDT", "sleeve": "A_4H_L", "family": "A", "side": 1, "ts_sig": ts, "tf": "4H", "ref_px": 600.0, "is_comm": False},
        {"sym": "CLUSDT", "sleeve": "C1_VC_PURE_L", "family": "C", "side": 1, "ts_sig": ts, "tf": "VC", "ref_px": 70.0, "is_comm": True},
    ], {})
    fri_open = int(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc).timestamp())
    fri_close = int(datetime(2026, 11, 27, 20, 35, tzinfo=timezone.utc).timestamp())
    eng.step_once(now_ts=fri_open)
    eng.step_once(now_ts=fri_close)
    assert st_p.is_file() and jr_p.is_file()
    jr_txt = jr_p.read_text(encoding="utf-8")
    assert "BNBUSDT" in jr_txt and "A_4H_L" in jr_txt and "FRIDAY_COMMODITY_BLACKOUT" in jr_txt


def test_fetch_binance_klines_excludes_forming_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        ok = True
        def raise_for_status(self): pass
        def json(self):
            # Bar 1 closed at 1000_000, Bar 2 still forming at 1005_000 (> cutoff 1002_000)
            return [
                [940000, "100", "105", "99", "104", "50", 999999, "5200", 25, "32", "3300", "0"],
                [1000000, "104", "106", "103", "105", "20", 1059999, "2100", 10, "12", "1260", "0"],
            ]
    monkeypatch.setattr(hg.requests, "get", lambda *a, **kw: _Resp())
    df = hg.fetch_binance_klines("ETHUSDT", "1m", limit=2, now_ms=1002000)
    assert len(df) == 1
    assert int(df["ts_open"].iloc[0]) == 940 and float(df["bv"].iloc[0]) == 32.0 and float(df["sv"].iloc[0]) == 18.0


def test_live_mt5_punch_gate_and_trailing_sl_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    eng = hg.HolyGrailEngine(hg.DEFAULT_CONFIG_PATH, tmp_path / "s.json", tmp_path / "j.csv", dry=False, live=True)

    sent_orders = []
    mod_sls = []
    class _FakeMT5:
        TRADE_RETCODE_DONE = 10009
        def terminal_info(self): return SimpleNamespace(trade_allowed=True)
        def account_info(self): return SimpleNamespace(login=5126976, equity=2501.80)
        def symbol_info(self, sym): return SimpleNamespace(digits=2, point=0.01, trade_tick_value=0.01, trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, trade_stops_level=10)
        def symbol_info_tick(self, sym): return SimpleNamespace(ask=1646.0, bid=1644.0)
        def orders_get(self): return []

    monkeypatch.setattr(hg.core, "MT5_OK", True)
    monkeypatch.setattr(hg.core, "mt5", _FakeMT5())
    eng.bridge.connect = lambda: True
    eng.bridge.ensure_visible = lambda sym: None
    eng.bridge.positions = lambda: []
    eng.bridge.total_sl_risk_usd = lambda pos=None: 0.0
    def _send_bracket(spec, side, lots, price, sl, tp, magic, comment, deviation):
        sent_orders.append((spec.name, side, lots, price, sl, tp, magic))
        return SimpleNamespace(retcode=10009)
    eng.bridge.send_bracket = _send_bracket
    eng.bridge.modify_sl = lambda ticket, sl, tp: mod_sls.append((ticket, sl, tp)) or SimpleNamespace(retcode=10009)

    cand = {"sym": "ETHUSDT", "sleeve": "A_4H_L", "family": "A", "side": 1, "ts_sig": 1795860000, "tf": "4H", "ref_px": 1645.0, "atr": 40.0, "sl_dist": 100.0, "short_ratchet": False, "climax_warn": False, "is_comm": False}
    ok, msg = eng._punch_hg_signal(cand, 2501.80, 0.0, 1.0, 1795860060)
    assert ok is True and len(sent_orders) == 1
    assert sent_orders[0][0] == "ETHUSD.pi" and sent_orders[0][2] == 0.25  # floor(25.02 / 100.0, 0.01) = 0.25

    # Verify T4_Convex trailing update fires when position runs > +1.5R BE trigger
    fake_pos = SimpleNamespace(ticket=9911, magic=46002, symbol="ETHUSD.pi", type=0, price_open=1646.0, sl=1546.0, tp=2846.0, time=1795860060)
    monkeypatch.setattr(hg.core.mt5, "symbol_info_tick", lambda sym: SimpleNamespace(ask=1806.0, bid=1805.0))
    eng._manage_hg_open_positions([fake_pos], {}, 1795863660)
    assert len(mod_sls) == 1 and mod_sls[0][1] > 1646.0  # Moved into BE+pad profit lock!


def test_hgv4_pods_trsi_tradfi_and_delta_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify HGv4 Pod 2 (KL_MR), Pod 3 (TradFi Friday flatten), non-blocking pod_caps & Pod 4 Delta gate."""
    from types import SimpleNamespace

    # 1. Non-blocking pod caps & Friday blackout on TRADFI
    cfg = json.loads(hg.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    gov = cfg["governor"]
    fri_open = int(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc).timestamp())
    fri_close = int(datetime(2026, 11, 27, 20, 35, tzinfo=timezone.utc).timestamp())
    core_longs = [{"sym": f"SYM{i}", "family": "A", "pod": "CORE", "side": 1} for i in range(6)]
    ok_tf, r_tf = hg.holy_grail_portfolio_gate(
        {"sym": "US2000.p", "mt5_symbol": "US2000.p", "family": "TF", "pod": "TRADFI", "side": 1, "is_comm": True},
        core_longs, gov, [], fri_open,
    )
    ok_tf_fri, r_tf_fri = hg.holy_grail_portfolio_gate(
        {"sym": "US2000.p", "mt5_symbol": "US2000.p", "family": "TF", "pod": "TRADFI", "side": 1, "is_comm": True},
        core_longs, gov, [], fri_close,
    )
    assert ok_tf is True and r_tf == "PASS"
    assert ok_tf_fri is False and r_tf_fri == "FRIDAY_COMMODITY_BLACKOUT"

    # 2. Pod 2 S17 Triple-RSI causal entry + Daily RSI(5)>50 exit without tight SL trailing
    n = 240
    ts0 = 1700000000
    rng = np.random.default_rng(12)
    c_vals = np.linspace(100.0, 150.0, n) + rng.normal(0.0, 0.25, n)
    c_vals[-10:-4] = [151.0, 150.8, 150.2, 149.8, 149.3, 148.9]
    c_vals[-4:] = [147.8, 146.2, 144.4, 142.1]
    o_vals = np.roll(c_vals, 1); o_vals[0] = c_vals[0]
    daily_df = pd.DataFrame({
        "ts": np.arange(ts0, ts0 + 86400 * n, 86400),
        "end_ts": np.arange(ts0 + 86400, ts0 + 86400 * (n + 1), 86400),
        "o": o_vals,
        "h": np.maximum(o_vals, c_vals) + 0.6,
        "l": np.minimum(o_vals, c_vals) - 0.6,
        "c": c_vals,
        "v": np.full(n, 1000.0),
    })
    sig_kl, exit_kl = hg.eval_kl_triple_rsi_latest(daily_df, "BTCUSDT", "D_TRSI_KL", "KL_MR", "D", is_comm=False)
    assert sig_kl is not None and sig_kl["kl_nosl"] is True and sig_kl["tf"] == "1D" and exit_kl is False

    # Bounce sharply -> Daily RSI(5) > 50 triggers exit signal
    daily_bounce = daily_df.copy()
    daily_bounce.iloc[-1, daily_bounce.columns.get_loc("c")] = 195.0
    _, exit_bounce = hg.eval_kl_triple_rsi_latest(daily_bounce, "BTCUSDT", "D_TRSI_KL", "KL_MR", "D", is_comm=False)
    assert exit_bounce is True

    # 3. KL open position closes on Daily RSI>50 exit instead of tightening SL
    eng = hg.HolyGrailEngine(hg.DEFAULT_CONFIG_PATH, tmp_path / "s2.json", tmp_path / "j2.csv", dry=False, live=True)
    closed_tickets = []
    mod_sls = []
    class _FakeMT5KL:
        def symbol_info(self, sym): return SimpleNamespace(digits=2, point=0.01, trade_stops_level=10)
        def symbol_info_tick(self, sym): return SimpleNamespace(ask=196.0, bid=195.0)
    monkeypatch.setattr(hg.core, "mt5", _FakeMT5KL())
    eng.bridge.close_position = lambda pos, dev: closed_tickets.append(pos.ticket) or SimpleNamespace(retcode=10009)
    eng.bridge.modify_sl = lambda tid, sl, tp: mod_sls.append(tid) or SimpleNamespace(retcode=10009)
    eng.state.set("positions", {
        "8811": {"sleeve": "D_TRSI_KL", "family": "D", "pod": "KL_MR", "magic": 46001, "mt5_symbol": "BTCUSD.pi",
                 "source_symbol": "BTCUSDT", "side": 1, "entry": 161.0, "orig_sd": 24.0, "best_px": 195.0,
                 "open_ts": 1795860000, "kl_nosl": True, "max_hold_sec": 12 * 86400}
    })
    kl_pos = SimpleNamespace(ticket=8811, magic=46001, symbol="BTCUSD.pi", type=0, price_open=161.0, sl=137.0, tp=305.0, time=1795860000)
    eng._manage_hg_open_positions([kl_pos], {}, 1795863600, daily_exit_map={"BTCUSDT": True})
    assert closed_tickets == [8811] and mod_sls == []

    # 4. Pod 4 S19 Delta gate stays SHADOW_WAIT_FUNDING when Delta equity < min_delta_equity_usd ($250)
    eng2 = hg.HolyGrailEngine(hg.DEFAULT_CONFIG_PATH, tmp_path / "s3.json", tmp_path / "j3.csv", dry=True, live=False)
    eng2._offline_replay = True
    monkeypatch.setattr(hg.delta_direct, "request_delta", lambda m, p, q="", b="": {"success": True, "result": [{"asset_symbol": "USD", "balance": "3.37", "available_balance": "3.22"}]})
    pod4_sm = eng2._step_pod4_cs_delta(1795860000, dd_mult=1.0, feat_4h_map={})
    assert pod4_sm["mode"] in {"SHADOW_WAIT_FUNDING", "SHADOW_WARMUP"} and pod4_sm["delta_avail_usd"] == pytest.approx(3.22)


def test_mt5_server_timezone_offset_alignment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify MT5 broker UTC+2 server bars normalize cleanly to UTC without 2h drop/lag."""
    from types import SimpleNamespace

    cur_utc = 1795874405  # 14:00:05 UTC (just 5s after 4H close)
    # Broker server time is UTC+2 (+7200s): 16:00:05 server; just-closed bar opened 12:00 server (10:00 UTC)
    class _FakeMT5TZ:
        def symbol_select(self, sym, vis): return True
        def symbol_info_tick(self, sym): return SimpleNamespace(time=cur_utc + 7199, ask=100.0, bid=99.9)
        def copy_rates_from_pos(self, sym, tf, start, cnt):
            return [
                {"time": cur_utc - 5 - 14400 + 7200, "open": 99.0, "high": 100.0, "low": 98.5, "close": 99.5, "tick_volume": 100},
                {"time": cur_utc - 5 - 14400 + 7200 + 14400 - 14400, "open": 99.5, "high": 101.0, "low": 99.0, "close": 100.5, "tick_volume": 120},
            ]

    # Build clean distinct bar times: t-2 = 08:00 srv (06:00 UTC), t-1 (closed) = 12:00 srv (10:00 UTC)
    bar_closed_srv = (cur_utc - 5) - 14400 + 7200
    bar_prev_srv = bar_closed_srv - 14400
    monkeypatch.setattr(_FakeMT5TZ, "copy_rates_from_pos", lambda self, s, t, st, c: [
        {"time": bar_prev_srv, "open": 99.0, "high": 100.0, "low": 98.5, "close": 99.5, "tick_volume": 100},
        {"time": bar_closed_srv, "open": 99.5, "high": 101.0, "low": 99.0, "close": 100.5, "tick_volume": 120},
    ])
    monkeypatch.setattr(hg.core, "MT5_OK", True)
    monkeypatch.setattr(hg.core, "mt5", _FakeMT5TZ())

    df = hg.sync_mt5_bars("US2000.p", 16388, 14400, "4h", live_dir=tmp_path, bars_count=2, now_ts=cur_utc)
    assert len(df) == 2
    assert int(df["end_ts"].iloc[-1]) == cur_utc - 5
    assert (cur_utc - int(df["end_ts"].iloc[-1])) == 5  # Immediately triggers within 30s loop, zero 2h lag!


def test_ws_collector_out_dir_splice(tmp_path: Path) -> None:
    ws = hg.core.BinanceAggTradeCollector(["ETHUSDT"], out_dir=tmp_path / "ws")
    assert hasattr(ws, "out_dir")
    ws_p = ws.out_dir / "ETHUSDT_ws_1m_fp.parquet"
    df_ws = pd.DataFrame({
        "bar_1m": [1700000000, 1700000060],
        "price_bucket": [1600.0, 1605.0],
        "buy_trades": [10, 12],
        "sell_trades": [5, 8],
        "buy_qty": [20.0, 25.0],
        "sell_qty": [10.0, 15.0],
        "open": [1600.0, 1605.0],
        "high": [1602.0, 1606.0],
        "low": [1599.0, 1604.0],
        "close": [1601.0, 1605.5],
    })
    df_ws.to_parquet(ws_p, index=False)
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "bar_1m": [1699999940], "open": [1598.0], "high": [1600.0], "low": [1597.0], "close": [1599.0],
        "volume": [10.0], "taker_buy_vol": [5.0], "count": [4],
    }).to_parquet(live_dir / "ETHUSDT_1m_live.parquet", index=False)
    _, k1, _ = hg.sync_live_symbol_feeds("ETHUSDT", live_dir=live_dir, now_ts=1700000200, enable_rest=False, ws_collector=ws)
    assert len(k1) == 3 and int(k1["bar_1m"].iloc[-1]) == 1700000060 and float(k1["volume"].iloc[-1]) == 40.0
