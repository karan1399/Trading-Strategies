"""Verification tests for the Charts parquet viewer & strategy lab."""

from __future__ import annotations

from fastapi.testclient import TestClient

from Charts.app import app
from Charts.core.backtest import run_backtest
from Charts.core.data_loader import list_symbols, list_timeframes, load_ohlcv
from Charts.core.indicators import compute_indicators
from Charts.core.strategy_base import Signal, discover_strategies


def test_parquet_discovery_and_load():
    tfs = list_timeframes()
    assert "15M" in tfs
    syms = list_symbols("15M")
    assert len(syms) > 10
    df = load_ohlcv(syms[0], "15M", limit=300)
    assert len(df) == 300
    assert list(df.columns) == ["timestamp", "datetime", "open", "high", "low", "close", "volume", "spread"]
    assert df["timestamp"].is_monotonic_increasing


def test_strategy_discovery_and_signal_generation():
    strategies = discover_strategies()
    assert {"ema_pullback", "rsi2_meanrev", "donchian_cta", "bb_zscore", "vwap_reclaim"}.issubset(strategies.keys())
    syms = list_symbols("15M")
    sym = "EURUSD.pi" if "EURUSD.pi" in syms else syms[0]
    df = load_ohlcv(sym, "15M", limit=1500)
    for strat in strategies.values():
        sigs = strat.generate_signals(df, strat.default_params())
        assert isinstance(sigs, list)
        for s in sigs:
            assert s.side in (1, -1)
            assert 0 <= s.index < len(df)


def test_backtest_causal_and_metrics():
    df = load_ohlcv(list_symbols("15M")[0], "15M", limit=300)
    sigs = [
        Signal(index=50, timestamp=int(df["timestamp"].iloc[50]), side=1, price=float(df["close"].iloc[50])),
        Signal(index=150, timestamp=int(df["timestamp"].iloc[150]), side=-1, price=float(df["close"].iloc[150])),
    ]
    res = run_backtest(df, sigs, risk_usd=10.0)
    assert res["metrics"]["total_trades"] >= 1
    assert len(res["equity_curve"]) == len(res["trades"])


def test_indicators_and_fastapi_endpoints():
    client = TestClient(app)
    meta_res = client.get("/api/meta")
    assert meta_res.status_code == 200
    meta = meta_res.json()
    assert "15M" in meta["timeframes"]
    assert len(meta["strategies"]) >= 5

    sym = "EURUSD.pi" if "EURUSD.pi" in meta["symbols"] else meta["symbols"][0]
    chart_res = client.post(
        "/api/chart",
        json={
            "timeframe": meta["default_timeframe"],
            "symbol": sym,
            "limit": 600,
            "indicators": ["ema_13", "ema_89", "rsi_14"],
            "strategy_id": "donchian_cta",
            "strategy_params": {},
            "risk_usd": 10.0,
        },
    )
    assert chart_res.status_code == 200
    body = chart_res.json()
    assert body["bars"] == 600
    assert len(body["candles"]) == 600
    assert len(body["indicators"]) >= 3
    assert "metrics" in body

    df = load_ohlcv(sym, "15M", limit=100)
    inds = compute_indicators(df, ["ema_13", "bb_20", "vwap_day", "rsi_14"])
    assert len(inds) == 6
