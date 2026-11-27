"""Verification tests for footprint_Chart."""

from __future__ import annotations

from fastapi.testclient import TestClient

from footprint_Chart.app import app
from footprint_Chart.backtesting.backtest import run_footprint_backtest
from footprint_Chart.backtesting.multi_asset_optimizer import compute_hac_stats
from footprint_Chart.backtesting.portfolio_engine import _sim_sleeve_1m_exact
from footprint_Chart.data_pipeline.binance_dumper import compute_adaptive_tick_bucket
from footprint_Chart.data_pipeline.mt5_universe import CORE_12_ASSETS
from footprint_Chart.engines.coinglass_parity import compute_cg_signals, load_coinglass_parity
from footprint_Chart.engines.footprint_engine import build_footprint_bars, list_datasets
from footprint_Chart.engines.inhouse_engine import load_inhouse_footprint
from footprint_Chart.data_pipeline.metrics_store import list_materialized_parquets, materialize_bar_parquets, sync_binance_metrics
from footprint_Chart.engines.strategy_base import discover_strategies


def test_btcusdt_parquet_and_zero_auction_detection():
    datasets = list_datasets()
    assert "BTCUSDT" in datasets
    res = build_footprint_bars("BTCUSDT", timeframe="15m", unit="trades", row_size=20.0, limit=40)
    assert len(res["bars"]) >= 1
    bar = res["bars"][0]
    assert "trades" in bar and bar["trades"] > 0
    assert "zero_top" in bar and "zero_bot" in bar
    assert "levels" in bar and len(bar["levels"]) >= 1


def test_strategy_discovery_and_backtest():
    strats = discover_strategies()
    assert {
        "zero_auction_reversal",
        "imbalance_stack",
        "delta_divergence",
        "cg_signals_strategy",
        "of_initiative_breakout",
        "vwap_of_reclaim",
        "ema_zero_orderflow",
        "rsi2_of_reversion",
        "ema233_imb_rejection",
    }.issubset(strats.keys())
    res = build_footprint_bars("BTCUSDT", timeframe="1h", unit="coin", row_size=40.0, limit=160)
    bars = res["bars"]
    for s in strats.values():
        sigs = s.generate_signals(bars, s.default_params())
        bt_net = run_footprint_backtest(bars, sigs, risk_usd=10.0, fee_bps=10.0, slippage_bps=2.0)
        bt_gross = run_footprint_backtest(bars, sigs, risk_usd=10.0, fee_bps=0.0, slippage_bps=0.0)
        assert "metrics" in bt_net
        assert "gross_r" in bt_net["metrics"] and "total_fees_r" in bt_net["metrics"]
        if bt_net["metrics"]["total_trades"] > 0:
            assert bt_net["metrics"]["total_r"] < bt_gross["metrics"]["total_r"]
            assert bt_net["metrics"]["total_fees_usd"] > 0

    # Verify causal alignment on cg_signals_strategy (no negative-delay lookahead vs compute_cg_signals)
    cg_sigs = strats["cg_signals_strategy"].generate_signals(bars, strats["cg_signals_strategy"].default_params())
    for sig in cg_sigs:
        assert f"causal@{sig.index}" in sig.reason


def test_coinglass_live_parity_and_bar_types():
    client = TestClient(app)
    resp = client.post(
        "/api/footprint",
        json={
            "symbol": "BTCUSDT",
            "source": "coinglass",
            "timeframe": "15m",
            "bar_type": "time",
            "unit": "coin",
            "row_size": 20.0,
            "limit": 30,
        },
    )
    assert resp.status_code == 200
    time_res = resp.json()
    assert time_res["source"] == "coinglass_live" or time_res["source"].startswith("inhouse_fallback")
    assert len(time_res["bars"]) >= 10
    b = time_res["bars"][-2]
    assert b["volume"] > 0 and b["trades"] > 0 and b["avg_size"] > 0
    assert "oi_change" in b and "max_delta" in b and "min_delta" in b

    vol_res = load_coinglass_parity("BTCUSDT", timeframe="15m", bar_type="vol", unit="coin", row_size=20.0, limit=20)
    assert vol_res["bar_type"] == "vol" and vol_res["bar_threshold"] > 0 and len(vol_res["bars"]) >= 1

    rng_res = load_coinglass_parity("BTCUSDT", timeframe="15m", bar_type="range", unit="coin", row_size=20.0, limit=20)
    assert rng_res["bar_type"] == "range" and rng_res["bar_threshold"] > 0 and len(rng_res["bars"]) >= 1


def test_inhouse_all_bar_types_and_signals():
    sync_binance_metrics("BTCUSDT", days=2)
    counts = materialize_bar_parquets("BTCUSDT")
    assert counts["metrics_5m"] >= 100
    assert counts["bars_15m"] >= 100
    assert any(k.startswith("bars_vol_") and v >= 20 for k, v in counts.items())
    assert any(k.startswith("bars_range_") and v >= 20 for k, v in counts.items())
    parqs = list_materialized_parquets("BTCUSDT")
    assert "BTCUSDT_metrics_5m.parquet" in parqs
    assert "BTCUSDT_bars_15m.parquet" in parqs

    for bt in ("time", "vol", "range"):
        res = load_inhouse_footprint("BTCUSDT", timeframe="15m", bar_type=bt, unit="coin", limit=30)
        assert res["source"] == "inhouse_binance"
        assert res["bar_type"] == bt
        assert len(res["bars"]) >= 5
        b = res["bars"][-2]
        assert "oi_change" in b and "funding_rate" in b and "top_ls" in b
        assert b["oi_change"] is not None
        assert "all_signals" in res
        bundle = res["all_signals"]
        assert bundle["vol_thr"] > 0 and bundle["range_thr"] > 0
        assert set(bundle["counts"].keys()) == {"all", "time", "vol", "range"}
        assert bundle["counts"]["all"] >= 1


def test_fastapi_endpoints():
    client = TestClient(app)
    m = client.get("/api/meta")
    assert m.status_code == 200
    assert "BTCUSDT" in m.json()["datasets"]
    assert "inhouse" in m.json()["sources"] and "coinglass" in m.json()["sources"]
    lv = client.get("/api/live?symbol=BTCUSDT")
    assert lv.status_code == 200
    for src in ("inhouse", "parquet"):
        fp = client.post(
            "/api/footprint",
            json={
                "symbol": "BTCUSDT",
                "source": src,
                "timeframe": "15m",
                "bar_type": "time",
                "unit": "coin",
                "strategy_id": "cg_signals_strategy",
            },
        )
        assert fp.status_code == 200
        payload = fp.json()
        assert len(payload["bars"]) >= 1
        assert "cg_signals" in payload

    pf_opt = client.post(
        "/api/portfolio-backtest",
        json={
            "symbol": "BTCUSDT", "start_equity": 5000.0, "risk_pct": 1.25, "max_concurrent": 3,
            "mc_runs": 20000, "entry_delay_mins": 1, "realistic_costs": True, "use_real_funding": True, "max_same_side": 2,
        },
    )
    assert pf_opt.status_code == 200
    body_opt = pf_opt.json()
    assert body_opt["profile"] == "optimal_8s"
    assert len(body_opt["sleeves"]) == 8
    assert all(s["trades"] >= 30 and s["sum_r"] > 0 for s in body_opt["sleeves"])
    pm_opt = body_opt["metrics"]
    assert pm_opt["end_equity"] >= 70000.0
    assert pm_opt["calmar_ratio"] >= 2.5
    assert pm_opt["sharpe_ratio"] >= 1.7
    assert pm_opt["mc_20k"]["runs"] == 20000
    assert len(pm_opt["annual_oos"]) >= 5 and all(a["sum_r"] > 0 for a in pm_opt["annual_oos"])
    assert pm_opt["wf_splits"]["is_21_23_r"] > 0 and pm_opt["wf_splits"]["oos_24_26_r"] > 0

    pf = client.post(
        "/api/portfolio-backtest",
        json={
            "symbol": "BTCUSDT", "start_equity": 5000.0, "risk_pct": 1.25, "max_concurrent": 3,
            "mc_runs": 20000, "profile": "institutional_4h_6y",
            "entry_delay_mins": 1, "realistic_costs": True, "use_real_funding": True, "max_same_side": 2,
        },
    )
    assert pf.status_code == 200
    body = pf.json()
    assert body["profile"] == "institutional_4h_6y"
    assert body["execution_mode"] == "1m_exact_realistic_dyn_atr"
    assert body["reality_fixes"]["entry_delay_mins"] == 1
    assert body["reality_fixes"]["realistic_costs"] is True
    assert body["reality_fixes"]["pruned_void_sleeves"] is True
    pm = body["metrics"]
    assert pm["start_equity"] == 5000.0
    assert pm["end_equity"] >= 50000.0
    assert pm["calmar_ratio"] >= 2.0
    assert pm["profit_factor"] >= 1.65
    assert pm["mc_20k"]["runs"] == 20000
    assert pm["mc_20k"]["crash_dd_p95"] > 0
    assert len(body["sleeves"]) == 6
    assert all(s["trades"] >= 30 and s["sum_r"] > 0 for s in body["sleeves"])
    assert len(pm["annual_oos"]) >= 5
    assert all(a["sum_r"] > 0 for a in pm["annual_oos"])
    assert pm["wf_splits"]["is_21_23_r"] > 0 and pm["wf_splits"]["oos_24_26_r"] > 0

    pf_fp = client.post(
        "/api/portfolio-backtest",
        json={
            "symbol": "BTCUSDT", "start_equity": 5000.0, "risk_pct": 1.25, "max_concurrent": 3,
            "mc_runs": 20000, "profile": "s01_s11_fp",
            "entry_delay_mins": 1, "realistic_costs": True, "use_real_funding": True, "max_same_side": 2,
        },
    )
    assert pf_fp.status_code == 200
    body_fp = pf_fp.json()
    assert body_fp["profile"] == "s01_s11_fp"
    pm_fp = body_fp["metrics"]
    assert len(body_fp["sleeves"]) == 7
    assert pm_fp["end_equity"] >= 14000.0
    assert pm_fp["calmar_ratio"] >= 2.0
    assert pm_fp["profit_factor"] >= 1.50
    assert pm_fp["mc_20k"]["runs"] == 20000
    assert sum(1 for s in body_fp["sleeves"] if s["sum_r"] > 0) >= 6


def test_scale_invariant_tick_and_mt5_cost_model():
    assert compute_adaptive_tick_bucket(90000.0) >= 5.0
    assert 0.1 <= compute_adaptive_tick_bucket(3500.0) <= 0.5
    assert 0.001 <= compute_adaptive_tick_bucket(35.0) <= 0.01
    assert 0.0001 <= compute_adaptive_tick_bucket(1.5) <= 0.001
    assert len(CORE_12_ASSETS) == 12
    assert any(a["binance"] == "XAUUSDT" and a["cat"] == "METAL" for a in CORE_12_ASSETS)
    assert any(a["binance"] == "XAGUSDT" and a["cat"] == "METAL" for a in CORE_12_ASSETS)

    import numpy as np, pandas as pd
    n_b = 260
    ts = np.arange(1700000000, 1700000000 + n_b * 14400, 14400, dtype=np.int64)
    c = np.full(n_b, 100.0)
    df = pd.DataFrame({
        "ts": ts, "end_ts": ts + 14400, "atr": np.full(n_b, 1.0),
        "c": c, "ema89": c, "ema233": c,
    })
    m1_ts = np.arange(ts[0], ts[-1] + 14400, 60, dtype=np.int64)
    m1_o = np.full(len(m1_ts), 100.0)
    m1_h = np.full(len(m1_ts), 104.0)
    m1_l = np.full(len(m1_ts), 99.5)
    m1_c = np.full(len(m1_ts), 101.0)
    ml = np.zeros(n_b, dtype=bool)
    ms = np.zeros(n_b, dtype=bool)
    ml[240] = True
    t_bn = _sim_sleeve_1m_exact(df, ml, ms, 14400, "test", m1_ts, m1_o, m1_h, m1_l, m1_c, sl_atr=2.0, tp_r=1.5, trail_atr=0.0, realistic_costs=True)
    t_mt5_wide = _sim_sleeve_1m_exact(
        df, ml, ms, 14400, "test", m1_ts, m1_o, m1_h, m1_l, m1_c,
        sl_atr=2.0, tp_r=1.5, trail_atr=0.0,
        mt5_cost={"symbol": "TESTUSDT", "sp_bps": 60.0, "sw_l_bps": -5.0, "sw_s_bps": -1.0, "roll3d": 5},
    )
    assert len(t_bn) == 1 and len(t_mt5_wide) == 1
    assert t_mt5_wide[0]["fee_r"] > t_bn[0]["fee_r"]
    assert t_mt5_wide[0]["symbol"] == "TESTUSDT"
    hac = compute_hac_stats(np.array([1.0, -0.5, 1.5, 0.8, 1.2, -0.4, 2.0, 0.9, 1.1]))
    assert hac["t_hac"] > 1.5 and hac["p_hac"] < 0.25

    client = TestClient(app)
    m = client.get("/api/meta")
    assert m.status_code == 200
    assert "mt5_specs" in m.json() and "XAUUSDT" in m.json()["mt5_specs"]


def test_numba_indicators_and_ml_dataset_causality():
    import numpy as np
    from footprint_Chart.backtesting.ml_meta_engine import PURGE_SEC, build_ml_dataset
    from footprint_Chart.backtesting.multi_asset_optimizer import _fast_s04_masks, _fast_s07_masks, evaluate_symbol_all_sleeves
    from footprint_Chart.data_pipeline.mt5_universe import load_mt5_contract_specs
    from strategies.core.fp_features import build_fp_context
    from strategies.core.indicators import adx_arr, ema_arr, rsi_wilder_arr, wilder_atr
    from strategies.core.registry import load_signal_fn

    n = 120
    c = np.linspace(100.0, 120.0, n)
    h = c + 1.0
    l = c - 1.0
    for arr in (adx_arr(h, l, c, 14), ema_arr(c, 20), rsi_wilder_arr(c, 2), wilder_atr(h, l, c, 14)):
        assert isinstance(arr, np.ndarray) and len(arr) == n and np.all(np.isfinite(arr))

    specs = load_mt5_contract_specs()
    res_xau = evaluate_symbol_all_sleeves("XAUUSDT", cost_mode="mt5", specs_map=specs)
    g4 = res_xau["g4"]
    fp4 = build_fp_context(g4)
    ml7, ms7 = _fast_s07_masks(g4, fp4, {"fp_mode": True})
    ml4, ms4 = _fast_s04_masks(g4, fp4, {"fp_mode": True})
    fn7 = load_signal_fn("s07_daily_pivot_breakout")
    fn4 = load_signal_fn("s04_vc_squeeze_breakout")
    o, hh, ll, cc = fp4["o"], fp4["h"], fp4["l"], fp4["c"]
    tm = (g4["ts"].to_numpy() // 60).astype(np.int64)
    atr = fp4["atr20"]
    for idx in range(235, min(len(g4) - 3, 600), 7):
        s7 = fn7(o, hh, ll, cc, tm, atr, idx, {"fp_mode": True}, {"fp": fp4})
        assert (1 if ml7[idx] else (-1 if ms7[idx] else 0)) == (s7["side"] if s7 else 0)
        s4 = fn4(o, hh, ll, cc, tm, atr, idx, {"fp_mode": True}, {"fp": fp4})
        assert (1 if ml4[idx] else (-1 if ms4[idx] else 0)) == (s4["side"] if s4 else 0)

    assert PURGE_SEC == 7 * 86400
    x_df, x_seq, meta_df, feats = build_ml_dataset({"XAUUSDT": res_xau})
    assert len(x_df) == len(meta_df) and len(x_df) > 20
    assert x_seq.shape == (len(x_df), 16, 8)
    assert np.all(np.isfinite(x_seq))
    assert "signed_btc_dist233" in feats and "sp_to_atr_bps" in feats


def test_final_ml_bundle_and_pilot_gate():
    import numpy as np
    from fastapi.testclient import TestClient
    from engine.unified_alpha_pilot import SymbolSpec, _ml_score_pilot_signal, load_resolved_config
    from footprint_Chart.app import app
    from footprint_Chart.backtesting.ml_meta_engine import FINAL_BUNDLE_PATH, score_live_signal

    assert FINAL_BUNDLE_PATH.is_file()
    res = score_live_signal({"sleeve": "S09 Donchian22 + OF (4h)", "side": -1.0, "sp_bps": 17.7, "adx14": 25.0})
    assert res["ok"] is True
    assert 0.50 <= res["risk_mult"] <= 1.35
    assert res["tier"] in {"HIGH_CONVICTION", "APPROVED", "VETO_REGIME"}

    cfg = load_resolved_config()
    assert cfg["sleeves"]["ig_s7_daily_pivot"].get("params")
    assert cfg["sleeves"]["ig_s9_donchian_cta"].get("params")
    n = 260
    c = np.linspace(1800.0, 1700.0, n)
    closed = {
        "open": c + 2.0,
        "high": c + 5.0,
        "low": c - 5.0,
        "close": c,
        "time": np.arange(1790000000, 1790000000 + n * 14400, 14400, dtype=np.int64),
    }
    spec = SymbolSpec(name="ETHUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01)
    p_score = _ml_score_pilot_signal("ETHUSD.pi", "ig_s1_williams_pullback", "H4", closed, spec, -1, 1700.0, 3.0, cfg)
    assert p_score is not None and p_score["ok"] is True

    client = TestClient(app)
    r = client.get("/api/final-live-portfolio")
    assert r.status_code == 200
    body = r.json()
    assert "universe_tight_spread" in body and "ml_walkforward_oos" in body


def test_per_asset_strict_is_oos_optimizer():
    import numpy as np
    from footprint_Chart.backtesting.multi_asset_optimizer import evaluate_symbol_all_sleeves, optimize_per_asset_strict_is_oos
    from footprint_Chart.data_pipeline.mt5_universe import load_mt5_contract_specs

    specs = load_mt5_contract_specs()
    per_sym = {
        "BTCUSDT": evaluate_symbol_all_sleeves("BTCUSDT", cost_mode="binance", specs_map=specs),
        "ETHUSDT": evaluate_symbol_all_sleeves("ETHUSDT", cost_mode="binance", specs_map=specs),
    }
    res = optimize_per_asset_strict_is_oos(per_sym, cost_mode="binance", specs_map=specs, mc_runs=1000)
    assert len(res["per_asset_recipes"]) == 2
    for rec in res["per_asset_recipes"]:
        assert rec["total_is_r"] > 50.0 and rec["total_oos_r"] > 50.0
    m = res["parallel_all_oos"]["metrics"]
    assert m["profit_factor"] > 1.3 and m["oos_r"] > 100.0
    assert res["parallel_tight_full"]["metrics"]["calmar_ratio"] >= 2.0

    # Verify Numba OHLC kernel & Track B per-asset optimizer on GAUUSD.pi
    from engine.mt5_per_asset_optimizer import _evaluate_one_mt5_symbol
    from footprint_Chart.backtesting.fast_kernels import sim_ohlc_trades_nb
    ts_a = np.arange(1700000000, 1700000000 + 300 * 14400, 14400, dtype=np.int64)
    c_a = np.linspace(100.0, 120.0, 300)
    sd_a = np.zeros(300, dtype=np.int8)
    sd_a[220] = 1
    obi, _, _, _, r_a, _ = sim_ohlc_trades_nb(
        ts_a, c_a, c_a + 1.0, c_a - 0.5, c_a, np.full(300, 1.0), sd_a, np.zeros(300),
        1.5, 2.5, 0.0, 1.0, 0.05, 48, 2.0, -1.0, -0.5, 5, 6,
    )
    assert len(obi) == 1 and float(r_a[0]) > 1.5
    gau_res = _evaluate_one_mt5_symbol("GAUUSD.pi", {"sp_bps": 1.0, "sp_px": 0.1, "vol_min": 0.01, "val_per_pt": 1.0, "sw_l_bps": -1.1, "sw_s_bps": 0.3, "roll3d": 3})
    assert gau_res is not None and len(gau_res["winners"]) >= 2
