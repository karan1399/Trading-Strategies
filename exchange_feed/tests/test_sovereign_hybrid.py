"""Causality, 47-feature ML parity, 0.25% MT5 whole-lot, and Corr-ERC heat bound tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from engine import m4_live_engine as m4
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk
from exchange_feed.backtesting.ml_meta_engine import (
    FINAL_BUNDLE_PATH,
    _extract_fa_row_and_seq,
    _load_bundle_cached,
    _precompute_tf_feature_arrays,
    build_btc_macro_reference,
    build_live_ml_feature_vector,
    resolve_ml_sleeve_label,
    score_live_signal,
)
from exchange_feed.backtesting.portfolio_engine import load_asset_tfs_cached
from exchange_feed.data_pipeline.binance_dumper import DATA_DIR
from strategies.core.fp_features import build_fp_context


def test_tradfi_and_crypto_signal_prefix_causality():
    """Truncating bars at t must yield identical signals & ATR at every bar <= t up to n - 1."""
    g2_eth, g4_eth, *_ = load_asset_tfs_cached("ETHUSDT", DATA_DIR, adaptive_tick=True)
    chart_map = m4.discover_chart_strategies()

    cut = 24
    g2_s = g2_eth.tail(400).reset_index(drop=True)
    g4_s = g4_eth.tail(350).reset_index(drop=True)
    full_c = m4.eval_crypto_signals(g2_s, g4_s, chart_map)
    pref_c = m4.eval_crypto_signals(g2_s.iloc[:-cut].reset_index(drop=True), g4_s.iloc[:-cut].reset_index(drop=True), chart_map)
    for k, d_full in full_c.items():
        if str(k[1]).upper() == "2H":
            continue
        assert np.array_equal(d_full["side"][:-cut], pref_c[k]["side"]), f"Crypto lookahead in {k}"
        assert np.allclose(d_full["atr"][:-cut], pref_c[k]["atr"], equal_nan=True), f"ATR skew in {k}"

    # TradFi OHLC synthetic dataframe including new causal s03_htf, s04_vc, s09_donchian
    n = 340
    idx_a = np.arange(n, dtype=float)
    base_c = 100.0 + 0.08 * idx_a + 1.8 * np.sin(idx_a * 0.17)
    df_u = m4.pd.DataFrame({
        "timestamp": 1700000000 + np.arange(n, dtype=np.int64) * 14400,
        "ts": 1700000000 + np.arange(n, dtype=np.int64) * 14400,
        "end_ts": 1700000000 + (np.arange(n, dtype=np.int64) + 1) * 14400,
        "open": base_c - 0.15,
        "high": base_c + 0.65,
        "low": base_c - 0.65,
        "close": base_c,
        "volume": np.full(n, 1000.0),
    })
    tf_full = m4.eval_tradfi_signals(df_u, chart_map)
    tf_pref = m4.eval_tradfi_signals(df_u.iloc[:-15].reset_index(drop=True), chart_map)
    for sid in ("s03_htf_ema_rejection", "s04_vc_squeeze_breakout", "s09_donchian_cta", "PA03_EMA233_Rejection"):
        assert sid in tf_full
        assert np.array_equal(tf_full[sid]["side"][:-15], tf_pref[sid]["side"]), f"TradFi lookahead in {sid}"


def test_47_feature_live_ml_parity_exact_match():
    """Live build_live_ml_feature_vector must match offline _extract_fa_row_and_seq within 1e-6 on all 47 columns."""
    assert FINAL_BUNDLE_PATH.is_file()
    bundle = _load_bundle_cached(FINAL_BUNDLE_PATH)
    assert bundle is not None and len(bundle["feat_names"]) == 47
    slv_to_id = bundle["slv_to_id"]

    _, g4_btc, *_ = load_asset_tfs_cached("BTCUSDT", DATA_DIR, adaptive_tick=True)
    _, g4_eth, *_ = load_asset_tfs_cached("ETHUSDT", DATA_DIR, adaptive_tick=True)

    btc_ref = build_btc_macro_reference(g4_btc)
    fa_off = _precompute_tf_feature_arrays(g4_eth, build_fp_context(g4_eth), btc_ref)
    slv_lbl = resolve_ml_sleeve_label("rsi2_of_reversion", tf_k=4, cfg_str="rsi=15.0", slv_to_id=slv_to_id)

    for test_idx in (len(g4_eth) - 25, len(g4_eth) - 1):
        off_pair = _extract_fa_row_and_seq(fa_off, test_idx, 1.0, slv_lbl, 4, 14.5, 0.0, slv_to_id)
        live_pair = build_live_ml_feature_vector(
            g4_eth, bar_idx=test_idx, side=1.0, sleeve="rsi2_of_reversion",
            tf_k=4, sp_bps=14.5, is_metal=0.0, btc_g4_df=g4_btc,
            bundle_path=FINAL_BUNDLE_PATH, cfg_str="rsi=15.0",
        )
        assert off_pair is not None and live_pair is not None
        off_row, off_seq = off_pair
        live_row, live_seq = live_pair
        assert list(off_row.keys()) == bundle["feat_names"]
        max_tab_diff = max(abs(off_row[k] - live_row[k]) for k in bundle["feat_names"])
        max_seq_diff = float(np.max(np.abs(live_seq - off_seq)))
        assert max_tab_diff < 1e-6, f"47-feature mismatch {max_tab_diff}"
        assert max_seq_diff < 1e-6, f"(16,8) tensor mismatch {max_seq_diff}"

    # Verify standalone CatBoost & Ensemble live scoring returns valid outputs
    sc_ens = score_live_signal(live_row, live_seq, bundle_path=FINAL_BUNDLE_PATH)
    sc_cb = score_live_signal(live_row, live_seq, bundle_path=FINAL_BUNDLE_PATH, model_name="CatBoost_Huber_ER")
    assert sc_ens["ok"] and sc_cb["ok"]
    assert sc_cb["expected_r"] == pytest.approx(sc_ens["per_model"]["CatBoost_Huber_ER"], abs=1e-4)


def test_025pct_mt5_whole_lot_rejections_and_cluster_heat_bounds():
    """Enforce $6.25 (0.25% of $2,501.80) cap without stop compression and 1.00% cluster heat cap."""
    equity = 2501.80
    cap_025 = equity * 0.25 / 100.0  # $6.2545
    btc = SymbolSpec("BTCUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    xau = SymbolSpec("XAUUSD.pi", digits=2, point=0.01, tick_value=1.0, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    eth = SymbolSpec("ETHUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    bnb = SymbolSpec("BNBUSD.p", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    gau = SymbolSpec("GAUUSD.pi", digits=4, point=0.0001, tick_value=0.01, tick_size=0.0001, vol_min=0.01, vol_step=0.01)

    assert lots_for_risk(btc, 1784.0, cap_025, cap_025)[0] is None
    assert lots_for_risk(xau, 90.02, cap_025, cap_025)[0] is None
    assert lots_for_risk(eth, 65.0, cap_025, cap_025)[0] == 0.09
    assert lots_for_risk(bnb, 18.0, cap_025, cap_025)[0] == 0.34
    assert lots_for_risk(gau, 2.88, cap_025, cap_025)[0] == 0.02

    # Cluster cap & Corr-ERC & 1.00% ($25.02) aggregate book heat verification
    p_eth = {"pair_id": "ETH", "magic": 44001, "mt5_symbol": "ETHUSD.pi", "category": "CRYPTO", "cluster": "CRYPTO", "bucket": "MEANREV", "tf_sec": 14400, "pair_weight": 1.0}
    p_bnb = {"pair_id": "BNB", "magic": 44002, "mt5_symbol": "BNBUSD.p", "category": "CRYPTO", "cluster": "CRYPTO", "bucket": "BREAKOUT", "tf_sec": 14400, "pair_weight": 1.0}
    p_gau = {"pair_id": "GAU", "magic": 44003, "mt5_symbol": "GAUUSD.pi", "category": "METALS", "cluster": "GOLD_GRAM", "bucket": "MEANREV", "tf_sec": 14400, "pair_weight": 1.0}
    p_jpy = {"pair_id": "JPY", "magic": 44004, "mt5_symbol": "USDJPY.pi", "category": "FOREX", "cluster": "FOREX", "bucket": "PULLBACK", "tf_sec": 14400, "pair_weight": 1.0}
    cfg = {
        "account": {"equity_floor_usd": 1800.0},
        "governor": {
            "risk_pct": 0.25,
            "max_global": 4,
            "max_per_asset": 1,
            "max_crypto_same_side": 1,
            "cluster_caps": {"CRYPTO": 2, "GOLD_GRAM": 1, "ENERGY": 1, "INDEX": 1, "FOREX": 1},
            "max_total_open_risk_pct": 1.00,
        },
    }
    pm = {p["magic"]: p for p in (p_eth, p_bnb, p_gau, p_jpy)}
    pos_eth = [SimpleNamespace(magic=44001, symbol="ETHUSD.pi", type=0)]

    # Corr-ERC shrinks correlated same-cluster risk multiplier below 1.0 while keeping orthogonal clusters at 1.0
    assert m4.compute_corr_erc_mult(p_bnb, pos_eth, pm) < 1.0
    assert m4.compute_corr_erc_mult(p_gau, pos_eth, pm) == 1.0

    # Orthogonal cluster passes within 1.00% heat, while 5th slot or >$25.02 committed risk blocks
    ok_g, _ = m4.m4_portfolio_gate(p_gau, 1, pos_eth, cfg, pm, {}, 1000, equity, 6.20, 6.20)
    assert ok_g
    ok_heat, rsn_heat = m4.m4_portfolio_gate(p_jpy, -1, pos_eth, cfg, pm, {}, 1000, equity, 20.00, 6.20)
    assert not ok_heat and "open risk" in rsn_heat


def test_config_sovereign_2500_parity_and_invariants():
    """Verify Sovereign $2,500 JSON config structure, unique magics, 0.25%/1.00% caps, and 1:1 signal dispatch."""
    import json

    cfg_path = Path(__file__).resolve().parents[2] / "engine" / "config_sovereign_2500.json"
    assert cfg_path.is_file()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    gov = cfg["governor"]
    pairs = cfg["pairs"]

    assert len(pairs) == 18
    assert len({int(p["magic"]) for p in pairs}) == 18
    assert abs(float(gov["risk_pct"]) - 0.25) < 1e-9
    assert abs(float(gov["max_risk_single_trade_pct"]) - 0.25) < 1e-9
    assert abs(float(gov["max_total_open_risk_pct"]) - 1.0) < 1e-9
    assert int(gov["max_global"]) == 4 and int(gov["max_per_asset"]) == 1
    assert gov["cluster_caps"] == {"CRYPTO": 2, "GOLD_GRAM": 1, "ENERGY": 1, "INDEX": 1, "FOREX": 1}
    assert all(p["mt5_symbol"] not in ("SP500.p", "UKBRENT.p", "USWTI.p") for p in pairs)
    assert all(not str(p["sid"]).startswith(("s12_", "s13_", "s14_")) for p in pairs)
    assert all(float(p["max_lot_risk_usd"]) <= 6.25 + 1e-9 for p in pairs)

    chart_map = m4.discover_chart_strategies()
    _, g4_btc, *_ = load_asset_tfs_cached("BTCUSDT", DATA_DIR, adaptive_tick=True)
    g2_btc, *_ = load_asset_tfs_cached("BTCUSDT", DATA_DIR, adaptive_tick=True)
    csigs = m4.eval_crypto_signals(g2_btc.tail(400).reset_index(drop=True), g4_btc.tail(350).reset_index(drop=True), chart_map)

    n = 300
    idx_a = np.arange(n, dtype=float)
    base_c = 100.0 + 0.05 * idx_a + 1.5 * np.sin(idx_a * 0.15)
    df_u = m4.pd.DataFrame({
        "timestamp": 1700000000 + np.arange(n, dtype=np.int64) * 14400,
        "ts": 1700000000 + np.arange(n, dtype=np.int64) * 14400,
        "end_ts": 1700000000 + (np.arange(n, dtype=np.int64) + 1) * 14400,
        "open": base_c - 0.1,
        "high": base_c + 0.5,
        "low": base_c - 0.5,
        "close": base_c,
        "volume": np.full(n, 1000.0),
    })
    tsigs = m4.eval_tradfi_signals(df_u, chart_map)

    for p in pairs:
        if p["feed_type"] == "binance_footprint":
            key = (p["sid"], p["tf"])
            if p["sid"] == "rsi2_of_reversion":
                tag = "rsi=20.0" if "rsi=20" in p["cfg"] else "rsi=15.0"
                key = (f"rsi2_of_reversion:{tag}", p["tf"])
            assert key in csigs, f"Missing crypto builder {key} for {p['pair_id']}"
        else:
            assert p["sid"] in tsigs, f"Missing TradFi builder {p['sid']} for {p['pair_id']}"


def test_sovereign_schedule_crypto_24_7_and_friday_close_non_crypto():
    """Verify exact UTC boundaries for Friday cutoff, Friday flatten, weekend blackout, and Sunday reopen."""
    from datetime import datetime, timezone
    from engine import sovereign_core_engine as sov

    cfg = {"session_policy": {
        "crypto_24_7": True,
        "crypto_categories": ["CRYPTO"],
        "friday_close_non_crypto": True,
        "friday_entry_cutoff_utc": "19:00",
        "friday_close_utc": "20:30",
        "sunday_reopen_utc": "22:05",
    }}
    ts = lambda s: int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())

    assert sov.non_crypto_schedule_state(ts("2026-11-27 18:59"), cfg) == "ACTIVE"
    assert sov.non_crypto_schedule_state(ts("2026-11-27 19:00"), cfg) == "FRIDAY_CUTOFF"
    assert sov.non_crypto_schedule_state(ts("2026-11-27 20:29"), cfg) == "FRIDAY_CUTOFF"
    assert sov.non_crypto_schedule_state(ts("2026-11-27 20:30"), cfg) == "FRIDAY_CLOSE"
    assert sov.non_crypto_schedule_state(ts("2026-11-28 14:00"), cfg) == "WEEKEND_BLACKOUT"
    assert sov.non_crypto_schedule_state(ts("2026-11-29 22:04"), cfg) == "WEEKEND_BLACKOUT"
    assert sov.non_crypto_schedule_state(ts("2026-11-29 22:05"), cfg) == "ACTIVE"
    assert sov.non_crypto_schedule_state(ts("2026-11-30 03:00"), cfg) == "ACTIVE"

    assert sov.is_crypto_pair({"category": "CRYPTO"}, cfg) is True
    assert sov.is_crypto_pair({"category": "FOREX"}, cfg) is False
    assert sov.is_crypto_pair({"category": "METALS"}, cfg) is False
    assert sov.is_crypto_pair({"category": "INDEX"}, cfg) is False


def test_sovereign_engine_step_once_closes_non_crypto_on_friday_keeps_crypto_24_7(monkeypatch, tmp_path):
    """Friday >=20:30 UTC closes open TradFi/FX positions while Crypto keeps running & trading 24/7."""
    from datetime import datetime, timezone
    from engine import m4_live_engine as m4_mod
    from engine import sovereign_core_engine as sov

    cfg_path = tmp_path / "sov.json"
    cfg = {
        "account": {"expected_login": 5126976, "equity_floor_usd": 1800.0},
        "session_policy": {
            "crypto_24_7": True,
            "crypto_categories": ["CRYPTO"],
            "friday_close_non_crypto": True,
            "friday_entry_cutoff_utc": "19:00",
            "friday_close_utc": "20:30",
            "sunday_reopen_utc": "22:05",
        },
        "governor": {
            "risk_pct": 0.25, "max_risk_single_trade_pct": 0.25, "max_total_open_risk_pct": 1.0,
            "max_global": 4, "max_per_asset": 1, "max_crypto_total": 2, "max_crypto_same_side": 1,
            "cluster_caps": {"CRYPTO": 2, "GOLD_GRAM": 1, "FOREX": 1},
            "bucket_caps": {"BREAKOUT": 2, "PULLBACK": 2, "MEANREV": 3},
            "dd_throttle": [4.5, 6.5], "dd_resume_ratio": 0.5,
            "safety_tp_r_when_trail_only": 8.0, "cooldown_bars": 1,
            "spread_atr_max": 0.45, "max_spread_sl_ratio": 0.20, "deviation": 30,
        },
        "symbol_specs_fallback": {
            "ETHUSD.pi": {"digits": 2, "tick_value": 0.01, "tick_size": 0.01, "vol_min": 0.01, "vol_step": 0.01},
            "GAUUSD.pi": {"digits": 4, "tick_value": 0.01, "tick_size": 0.0001, "vol_min": 0.01, "vol_step": 0.01},
            "USDJPY.pi": {"digits": 3, "tick_value": 0.65, "tick_size": 0.001, "vol_min": 0.01, "vol_step": 0.01},
        },
        "pairs": [
            {
                "pair_id": "ETHUSD.pi:vwap_rejection:4H", "magic": 44015,
                "source_symbol": "ETHUSDT", "mt5_symbol": "ETHUSD.pi",
                "category": "CRYPTO", "cluster": "CRYPTO", "feed_type": "binance_footprint",
                "sid": "vwap_rejection", "tf": "4H", "tf_sec": 14400, "bucket": "MEANREV",
                "sl_atr": 2.0, "tp_r": 4.0, "trail_atr": 0.0, "be_r": 1.0, "be_pad_r": 0.05,
                "step_lock": False, "dyn_atr": False, "max_hold_bars": 96,
                "pair_weight": 1.0, "max_lot_risk_usd": 6.25, "enabled": True,
            },
            {
                "pair_id": "GAUUSD.pi:s03_htf_ema_rejection:4H", "magic": 44016,
                "source_symbol": "MT5_GAUUSD", "mt5_symbol": "GAUUSD.pi",
                "category": "METALS", "cluster": "GOLD_GRAM", "feed_type": "mt5_ohlc",
                "sid": "s03_htf_ema_rejection", "tf": "4H", "tf_sec": 14400, "bucket": "PULLBACK",
                "sl_atr": 2.0, "tp_r": 2.5, "trail_atr": 0.0, "be_r": 1.0, "be_pad_r": 0.05,
                "step_lock": False, "dyn_atr": False, "max_hold_bars": 96,
                "pair_weight": 1.0, "max_lot_risk_usd": 6.25, "enabled": True,
            },
            {
                "pair_id": "USDJPY.pi:PA05_Supertrend_EMA233:4H", "magic": 44001,
                "source_symbol": "MT5_USDJPY", "mt5_symbol": "USDJPY.pi",
                "category": "FOREX", "cluster": "FOREX", "feed_type": "mt5_ohlc",
                "sid": "PA05_Supertrend_EMA233", "tf": "4H", "tf_sec": 14400, "bucket": "PULLBACK",
                "sl_atr": 2.5, "tp_r": 3.0, "trail_atr": 0.0, "be_r": 1.25, "be_pad_r": 0.05,
                "step_lock": False, "dyn_atr": False, "max_hold_bars": 96,
                "pair_weight": 1.4, "max_lot_risk_usd": 6.25, "enabled": True,
            },
        ],
    }
    cfg_path.write_text(__import__("json").dumps(cfg), encoding="utf-8")

    fri_close_ts = int(datetime(2026, 11, 27, 20, 35, tzinfo=timezone.utc).timestamp())
    sat_ts = int(datetime(2026, 11, 28, 14, 5, tzinfo=timezone.utc).timestamp())

    open_pos = [
        SimpleNamespace(ticket=101, magic=44016, symbol="GAUUSD.pi", type=0, volume=0.02, price_open=85.0, price_current=86.0, sl=83.0, tp=90.0, time=fri_close_ts - 7200),
        SimpleNamespace(ticket=102, magic=44015, symbol="ETHUSD.pi", type=0, volume=0.05, price_open=3000.0, price_current=3020.0, sl=2940.0, tp=3240.0, time=fri_close_ts - 7200),
    ]
    closed_tickets: list[int] = []
    sent_orders: list[dict] = []

    class FakeMT5:
        TIMEFRAME_H1 = 1
        TIMEFRAME_H4 = 4
        TRADE_RETCODE_DONE = 10009
        @staticmethod
        def terminal_info():
            return SimpleNamespace(trade_allowed=True)
        @staticmethod
        def account_info():
            return SimpleNamespace(login=5126976, equity=2501.80)
        @staticmethod
        def symbol_info_tick(sym):
            px = {"ETHUSD.pi": (3020.5, 3020.0), "GAUUSD.pi": (86.05, 86.0), "USDJPY.pi": (152.01, 152.0)}[sym]
            return SimpleNamespace(ask=px[0], bid=px[1], time=sat_ts)
        @staticmethod
        def symbol_info(sym):
            return None

    monkeypatch.setattr(m4_mod, "mt5", FakeMT5)
    monkeypatch.setattr(sov, "spec_from_mt5", lambda _s: None)

    eng = sov.SovereignCoreEngine(cfg_path, tmp_path / "st.json", tmp_path / "j.csv", dry=False, live=True)

    class FakeBridge:
        def connect(self): return True
        def shutdown(self): pass
        def ensure_visible(self, _s): pass
        def positions(self): return list(open_pos)
        def total_sl_risk_usd(self, _pos): return 3.0
        def tick(self, sym):
            t = FakeMT5.symbol_info_tick(sym)
            return t.ask, t.bid
        def close_position(self, pos, _dev):
            closed_tickets.append(int(pos.ticket))
            open_pos[:] = [x for x in open_pos if int(x.ticket) != int(pos.ticket)]
            return SimpleNamespace(retcode=10009)
        def send_bracket(self, spec, side, lots, price, sl, tp, magic, comment, dev):
            sent_orders.append({"symbol": spec.name, "magic": magic, "side": side, "lots": lots})
            return SimpleNamespace(retcode=10009, order=900 + len(sent_orders), price=price)

    eng.bridge = FakeBridge()
    # Step 1: Friday 20:35 UTC -> GAUUSD.pi closed, ETHUSD.pi kept open, USDJPY.pi new entry blocked
    pl_fri = {
        "USDJPY.pi:PA05_Supertrend_EMA233:4H": {"ts": fri_close_ts - 14400, "end_ts": fri_close_ts - 60, "side": 1, "atr": 0.30, "c": 152.0, "vof_mult": 1.0, "sl_dist_override": None},
    }
    monkeypatch.setattr(eng, "_gather_signals", lambda: (pl_fri, {"ETHUSD.pi": {14400: 30.0}, "GAUUSD.pi": {14400: 1.0}}))
    sm_fri = eng.step_once(now_ts=fri_close_ts)
    assert closed_tickets == [101], f"Expected GAUUSD.pi (101) closed on Friday, got {closed_tickets}"
    assert [int(p.ticket) for p in open_pos] == [102], "ETHUSD.pi (102) must stay open 24/7"
    assert sent_orders == [], "USDJPY.pi must NOT enter during FRIDAY_CLOSE"
    assert sm_fri["non_crypto_schedule"] == "FRIDAY_CLOSE"
    assert sm_fri["crypto_schedule"] == "24/7_ACTIVE"

    # Step 2: Saturday 14:05 UTC -> remove open ETH pos to allow fresh ETH entry; verify ETH opens while GAU stays blocked
    open_pos.clear()
    pl_sat = {
        "ETHUSD.pi:vwap_rejection:4H": {"ts": sat_ts - 14400, "end_ts": sat_ts - 300, "side": 1, "atr": 30.0, "c": 3020.0, "vof_mult": 1.0, "sl_dist_override": None},
        "GAUUSD.pi:s03_htf_ema_rejection:4H": {"ts": sat_ts - 14400, "end_ts": sat_ts - 300, "side": 1, "atr": 1.0, "c": 86.0, "vof_mult": 1.0, "sl_dist_override": None},
    }
    monkeypatch.setattr(eng, "_gather_signals", lambda: (pl_sat, {"ETHUSD.pi": {14400: 30.0}, "GAUUSD.pi": {14400: 1.0}}))
    sm_sat = eng.step_once(now_ts=sat_ts)
    assert [o["symbol"] for o in sent_orders] == ["ETHUSD.pi"], f"Crypto must trade 24/7 on Saturday, got {sent_orders}"
    assert sm_sat["non_crypto_schedule"] == "WEEKEND_BLACKOUT"


