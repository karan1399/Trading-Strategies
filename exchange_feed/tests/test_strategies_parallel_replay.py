"""Tests for parallel shared-account simulation across all 94 strategies/s01..s16 recipes."""
from __future__ import annotations

import pytest

from desk.quant_audits.strategies_parallel_replay import (
    ASSET_TO_MT5,
    build_parallel_trade_pools,
    load_all_strategy_recipes,
    make_mt5_sizer,
)
from engine.unified_alpha_pilot import SymbolSpec
from exchange_feed.backtesting.mt5_shared_account_portfolio_optimizer import simulate_shared_account


def test_all_94_recipes_discovered_and_mapped_without_gaps():
    recipes = load_all_strategy_recipes()
    assert len(recipes) == 94
    assert all(r["_asset"] in ASSET_TO_MT5 for r in recipes)


def test_parallel_pools_load_all_94_recipes_causally():
    pools, meta = build_parallel_trade_pools()
    assert len(meta) == 94
    assert all(m["total_trades"] > 0 for m in meta)
    assert len(pools["core45"]) > 1000
    assert len(pools["screened62"]) > len(pools["core45"])
    assert len(pools["all94"]) > len(pools["screened62"])


def test_mt5_sizer_enforces_hard_cap_and_live_spread_stress():
    specs = {
        "BTCUSD.pi": SymbolSpec("BTCUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01),
        "ETHUSD.pi": SymbolSpec("ETHUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01),
        "GAUUSD.pi": SymbolSpec("GAUUSD.pi", digits=4, point=0.0001, tick_value=0.01, tick_size=0.0001, vol_min=0.01, vol_step=0.01),
    }
    rej: dict[str, dict[str, int]] = {}
    sizer = make_mt5_sizer(specs, 1.0 / 31.1035, hard_cap_usd=6.0, reject_stats=rej)

    btc_row = {"mt5_symbol": "BTCUSD.pi", "asset": "BTC", "sl_dist": 2500.0}
    eth_row = {"mt5_symbol": "ETHUSD.pi", "asset": "ETH", "sl_dist": 120.0}
    xau_row = {"mt5_symbol": "GAUUSD.pi", "asset": "XAU", "sl_dist": 60.0}

    assert sizer(btc_row, 20.0, 2501.80) is None
    assert sizer(eth_row, 20.0, 2501.80) == pytest.approx((6.0, 6.0))
    gau_sized = sizer(xau_row, 20.0, 2501.80)
    assert gau_sized is not None and gau_sized[0] <= 6.0
    assert rej["BTCUSD.pi"]["reject"] == 1
    assert rej["ETHUSD.pi"]["pass"] == 1
    assert rej["GAUUSD.pi"]["pass"] == 1
