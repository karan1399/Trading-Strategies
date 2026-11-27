"""Regression tests for full D01-D16 endpoint parity and live continuation."""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import telemetry_16p_engine as eng  # noqa: E402


def test_audit_symbol_16p_extracts_all_d01_d16_fields(tmp_path):
    assets_dir = tmp_path / "assets"
    fp_dir = tmp_path / "footprint" / "ETH"
    assets_dir.mkdir(parents=True)
    fp_dir.mkdir(parents=True)

    now_ms = time.time() * 1000.0
    sample = {
        "symbol": "ETH",
        "feed_type": "coinglass_perp",
        "updated_at": "2026-11-25T18:30:00Z",
        "ticker": {"price": 1680.0, "dir": "down", "source": "live"},
        "market_structure": {
            "price": {"last": 1680.0},
            "auction": {"session": {"poc": 1665.0}, "day": {"val": 1640.0, "poc": 1660.0, "vah": 1685.0}, "week": {"poc": 1670.0}, "month": {"poc": 1710.0}},
            "vwap": {"vwap": 1650.0, "bands": {"sd1": 15.0}},
            "emas": {"m15": {"e89": 1675.0, "e233": 1690.0}, "h1": {"e89": 1685.0, "e233": 1720.0}, "h4": {"e13": 1670.0, "e89": 1750.0, "e233": 1820.0}, "d1": {"e13": 1740.0, "e89": 1900.0, "e233": 2100.0}},
            "rsi": {"m15": 54.0, "h1": 49.0, "h4": 44.0, "d1": 39.0},
            "levels": {"pdh": 1721.14, "pdl": 1663.17},
        },
        "narrative_engine": {"regime": "RANGE_BALANCE", "activeModel": "M1", "direction": "SHORT", "confidence": 68, "grade": "A", "probabilities": {"evR": 2.1}},
        "athena_oracle": {"score": 41, "verdict": "Bearish", "factors": [{"id": "apollo", "score": 30}]},
        "microstructure_l2_l3": {"hl": {"mark": 1680.0, "oracle": 1680.5, "markVsOraclePct": -0.03, "vol24hUsd": 1e9}},
        "quant_positioning": {"lsRatios": {"15m": {"global": {"ratio": 2.2}, "topAccounts": {"ratio": 3.1}}, "1h": {"global": {"ratio": 2.1}}, "4h": {"global": {"ratio": 1.9}}}, "oiQuadrant": "SHORT_BUILDUP", "basis": {"pct": -0.04}},
        "options_deribit": {
            "dvol": {"value": 68.5},
            "riskReversal25d": {"value": -4.73},
            "gammaFlipPrice": 1734.2,
            "gex": {"regime": "NEGATIVE_GAMMA_EXPANSION", "netGexUsdPer1Pct": -2987600.0},
            "maxPainStrike": 2100,
        },
        "liquidations": {
            "clusters": {"3d": {"stats": {"totals": {"longs": 1000000.0, "shorts": 3000000.0}, "keyLevels": {"above": [{"price": 1736.22}], "below": [{"price": 1601.19}]}}}},
            "stopmap": {"m1_coin_24h": {"overview": {"largestLevel": 42248049.7}}, "m1_coin_3d": {"keyLevels": {"above": [{"price": 1729.60}, {"price": 1735.93}], "below": [{"price": 1607.16}]}}},
            "live_prints_tape": {"events": [{"t": now_ms - 60_000, "usd": 12500.0}, {"t": now_ms - 400_000, "usd": 99999.0}], "stats24h": {"usd": 500000.0}},
        },
        "book_walls": {
            "futures": {"m15": {"imbalance": -0.35, "bidDepth": 1e6, "askDepth": 2e6, "walls": [{"price": 1700.0}]}},
            "spot": {"m15": {"imbalance": -0.5, "walls": [{"price": 1695.0}]}},
            "live_binance": {"imbalance": -0.22},
        },
        "cvd": {"perp": {"m15": {"kpis": {"perpBuy": 500, "perpSell": 900, "latestDiv": {"type": "bear"}}}, "h1": {"kpis": {"perpBuy": 200, "perpSell": 600}}}, "spot": {"m15": {"kpis": {"spotBuy": 100, "spotSell": 300}}}},
        "footprint": {"history_7d": {"cum_delta_7d": -420.5}, "history_1h": {"cum_delta_1h": -55.0}},
        "large_orders": {"resting_and_executed": {"24h": [{"id": 1}]}, "large_taker_orders_100k": {"supported": True, "groups": [{"id": 9}], "activeExchanges": ["Binance"]}},
        "funding_and_oi": {"funding": [{"name": "Binance", "rate8h": 0.005}], "funding_aggregate": {"fundingPct8h": 0.0077, "apr_pct": 8.43}, "open_interest": [{}], "oi_aggregate": {"oiUsd": 3.9e9, "oiDelta1hPct": 0.97, "oiDelta4hPct": -0.63, "oiDelta24hPct": -1.55}},
    }
    (assets_dir / "ETH.json").write_text(json.dumps(sample), encoding="utf-8")
    (fp_dir / "signals_7d.json").write_text(json.dumps({"signals": [{"t": "abs"}, {"t": "trap"}]}), encoding="utf-8")

    out, err = eng.audit_symbol_16p("ETH", assets_dir=str(assets_dir), footprint_dir=str(tmp_path / "footprint"))
    assert err is None and out is not None
    assert out["7_options_deribit"]["risk_reversal_25d"] == -4.73
    assert out["7_options_deribit"]["gamma_flip_price"] == 1734.2
    assert out["7_options_deribit"]["gex_regime"] == "NEGATIVE_GAMMA_EXPANSION"
    assert out["8_liquidations"]["stopmap_3d_above"] == [1729.60, 1735.93]
    assert out["8_liquidations"]["lsr_3d"] == 3.0
    assert out["8_liquidations"]["liq_5m_pace_usd"] == 12500.0
    assert out["9_book_walls"]["live_binance_imbalance"] == -0.22
    assert out["12_large_orders"]["lto_active_exchanges"] == ["Binance"]
    assert out["16_funding_and_oi"]["annualized_apr_pct"] == 8.43
    assert out["16_funding_and_oi"]["oi_delta_4h_pct"] == -0.63
    assert out["2_market_structure"]["levels"]["pdh"] == 1721.14


def test_live_continue_recovers_stale_disk_asset(tmp_path, monkeypatch):
    assets_dir = tmp_path / "assets"
    fp_dir = tmp_path / "footprint"
    assets_dir.mkdir(parents=True)
    fp_dir.mkdir(parents=True)

    fpath = assets_dir / "ETH.json"
    fpath.write_text(json.dumps({"symbol": "ETH", "feed_type": "coinglass_perp", "liquidations": {}, "funding_and_oi": {}}), encoding="utf-8")
    old = time.time() - 1200
    os.utime(fpath, (old, old))

    def fake_get(url: str, timeout_s: float = 2.0):
        if "/api/cg/stopmap" in url:
            return {"ok": True, "model": {"keyLevels": {"above": [{"price": 1729.60}], "below": [{"price": 1607.16}]}, "overview": {"largestLevel": 42000000.0}}}
        if "/api/cg/oi" in url:
            return {"ok": True, "venues": [], "aggregate": {"oiUsd": 3.9e9, "oiDelta4hPct": -0.55}}
        if "/api/cg/funding" in url:
            return {"ok": True, "venues": [], "aggregate": {"fundingPct8h": 0.01}}
        return None

    monkeypatch.setattr(eng, "_http_get_json", fake_get)
    out, err = eng.audit_symbol_16p("ETH", assets_dir=str(assets_dir), footprint_dir=str(fp_dir), max_stale_sec=600, live_continue=True)
    assert err is None and out is not None
    assert out["_meta"]["is_fresh"] is False
    assert out["_meta"]["effective_fresh"] is True
    assert out["_meta"]["continuation"] == "DISK_STALE+LIVE_HTTP"
    assert out["8_liquidations"]["stopmap_3d_above"] == [1729.60]
    assert out["16_funding_and_oi"]["oi_delta_4h_pct"] == -0.55
    assert out["16_funding_and_oi"]["annualized_apr_pct"] == 10.95


def test_stopmap_walls_parity_and_swept_vs_active():
    from scripts.telemetry_analytics import compute_stopmap_walls

    candles = [{"ts": 1000 * i, "o": 1700, "h": 1710, "l": 1690, "c": 1700} for i in range(10)]
    y_axis = [1680.0 + i * 2.0 for i in range(20)]  # rows 0..19, bin=2.0
    # Active short wall at row 16 (1713.0) across cols 5..9; swept long wall at row 3 (1687.0) cols 1..4
    cols_data = []
    for ci in range(10):
        cells = []
        if ci >= 5:
            cells.append([16, 4_000_000.0])
        if 1 <= ci <= 4:
            cells.append([3, 3_000_000.0])
        cols_data.append({"ts": candles[ci]["ts"], "cells": cells})
    res = compute_stopmap_walls({
        "yAxis": y_axis,
        "priceStep": 2.0,
        "lastPrice": 1700.0,
        "candles": candles,
        "columns": cols_data,
    })
    assert res["total_walls_found"] == 2
    assert res["active_count"] == 1
    assert res["swept_count"] == 1
    assert res["max_short"]["active"] is True
    assert res["max_short"]["price"] == 1713.0
    assert res["max_long"]["active"] is False
    assert res["max_long"]["price"] == 1687.0


def test_diagonal_imbalances_exact_3x_ratio():
    from scripts.telemetry_analytics import compute_diagonal_imbalances

    # Level prices: i=0..3 => 100.5, 101.5, 102.5, 103.5
    # Buyer imbalance at i=2: b[2]=600 vs s[1]=100 -> 6.0x; i=3: b[3]=450 vs s[2]=100 -> 4.5x (2-stack!)
    # Seller imbalance at i=0: s[0]=350 vs b[1]=50 -> 7.0x
    fp = {"lo": 100.0, "tick": 1.0, "b": [10.0, 50.0, 600.0, 450.0], "s": [350.0, 100.0, 100.0, 10.0]}
    out = compute_diagonal_imbalances(fp)
    assert out["buy_3x_count"] >= 2
    assert out["sell_3x_count"] >= 1
    assert any(z["count"] == 2 and z["lo"] == 102.5 and z["hi"] == 103.5 for z in out["buy_stack_zones"])


def test_bounded_ttl_cache_eviction_and_trim():
    from scripts.telemetry_analytics import BoundedTTLCache

    c = BoundedTTLCache(max_entries=3, max_bytes=2500)
    c.set("k1", {"x": "a" * 300})
    c.set("k2", {"x": "b" * 300})
    c.set("k3", {"x": "c" * 300})
    c.set("k4", {"x": "d" * 300})
    st = c.stats()
    assert st["entries"] <= 3
    assert c.get("k1", 60) is None
    assert c.get("k4", 60) is not None
    c.trim_to(0.0)
    assert c.stats()["entries"] == 0


def test_stopmap_and_bookwalls_compaction_drops_grid_bloat():
    from scripts.telemetry_analytics import build_compact_bookwalls, summarize_stopmap_payload

    rolling_sm = {
        "ok": True,
        "source": "liq-snapshot-rolling:archive",
        "model": {
            "symbol": "SUI",
            "lastPrice": 0.65,
            "clusters": {"above": [{"price": 0.7}], "below": [{"price": 0.6}]},
            "totals": {"longs": 100.0, "shorts": 200.0},
            "grid": [[0.0] * 100 for _ in range(50)],  # raw 2D grid must be dropped
            "cumulative": [{"price": i} for i in range(400)],
        },
    }
    sm_out = summarize_stopmap_payload(rolling_sm)
    assert "grid" not in sm_out
    assert sm_out["grid_meta"] == {"rows": 50}
    assert len(sm_out["cumulative"]) <= 80

    huge_bw = {
        "ok": True,
        "model": {
            "last": 3500.0,
            "grid": 1.0,
            "imbalance": -0.2,
            "walls": [{"side": "a", "price": 3510, "usd": 1e6}],
            "events": [],
            "ladder": [{"price": 3400.0 + i, "qty": 1.0, "usd": 3500.0, "side": "a"} for i in range(400)],
            "strip": [{"t": i, "mid": 3500.0, "cells": [[i, 10]]} for i in range(50)],
        },
    }
    bw_out = build_compact_bookwalls(huge_bw, strip_tail=2, max_ladder=120)
    assert bw_out["ladder_total"] == 400
    assert len(bw_out["ladder"]) == 120
    assert len(bw_out["strip_recent"]) == 2


def test_16p_engine_reads_walls_multi_bw_and_diag_fp(tmp_path):
    assets_dir = tmp_path / "assets"
    fp_dir = tmp_path / "footprint" / "ETH"
    assets_dir.mkdir(parents=True)
    fp_dir.mkdir(parents=True)

    sample = {
        "symbol": "ETH",
        "feed_type": "coinglass_perp",
        "updated_at": "2026-11-26T00:40:00Z",
        "ticker": {"price": 1682.0, "dir": "up", "source": "live"},
        "market_structure": {"price": {"last": 1682.0}},
        "liquidations": {
            "stopmap": {
                "m1_coin_24h": {"overview": {"largestLevel": 42000000.0}},
                "m1_coin_3d": {"keyLevels": {"above": [], "below": []}, "walls": {"active_short_prices": [1713.76, 1730.65], "active_long_prices": [], "max_short": {"price": 1730.65, "mx_usd": 42248049.0}, "max_long": None}},
                "m2_coin_3d": {"kind": "rolling_snapshot"},
                "m3_coin_3d": {"kind": "rolling_snapshot"},
            }
        },
        "book_walls": {
            "futures": {
                "1m": {"imbalance": -0.1},
                "m15": {"imbalance": -0.2, "walls": [], "ladder": [{"price": 1700}], "persistence": {"untouched_bid_usd": 100.0, "untouched_ask_usd": 500.0, "max_wall_age_ms": 4500000}},
                "4h": {"imbalance": -0.45},
            },
            "spot": {},
            "live_binance": {"imbalance": -0.15, "grid": 1},
        },
        "footprint": {
            "history_7d": {"cum_delta_7d": 120000.0, "recent_bars_tail": [{"ts": 1, "diag_buy_3x": 1, "diag_sell_3x": 4}, {"ts": 2, "diag_buy_3x": 0, "diag_sell_3x": 0}]},
            "history_1h": {"cum_delta": -550000.0},
            "multi_tf": {"m5": {"buckets_available": True}, "m15": {"buckets_available": True}, "h4": {"buckets_available": True}},
        },
        "funding_and_oi": {},
    }
    (assets_dir / "ETH.json").write_text(json.dumps(sample), encoding="utf-8")
    (fp_dir / "signals_7d.json").write_text(json.dumps({"signals": [{"t": "abs"}], "by_bt": {"time": {"count": 1}}}), encoding="utf-8")
    (fp_dir / "summary_1h.json").write_text(json.dumps({"cum_delta": -550000.0, "rows": []}), encoding="utf-8")

    out, err = eng.audit_symbol_16p("ETH", assets_dir=str(assets_dir), footprint_dir=str(tmp_path / "footprint"))
    assert err is None and out is not None
    assert out["8_liquidations"]["stopmap_3d_walls_active_short"] == [1713.76, 1730.65]
    assert out["8_liquidations"]["stopmap_models_present"] == ["m1_coin_24h", "m1_coin_3d", "m2_coin_3d", "m3_coin_3d"]
    assert out["9_book_walls"]["bw_4h_imbalance"] == -0.45
    assert out["9_book_walls"]["untouched_ask_usd"] == 500.0
    assert out["9_book_walls"]["ladder_levels"] == 1
    assert out["9_book_walls"]["live_binance_grid"] == 1
    assert out["11_footprint"]["cum_delta_1h"] == -550000.0
    assert out["11_footprint"]["diag_15m_latest_closed"]["sell_3x"] == 4
    assert sorted(out["11_footprint"]["multi_tf_buckets"]) == ["h4", "m15", "m5"]


def test_16p_engine_reads_all_stopmap_and_cluster_horizons(tmp_path):
    assets_dir = tmp_path / "assets"
    fp_dir = tmp_path / "footprint" / "BTC"
    assets_dir.mkdir(parents=True)
    fp_dir.mkdir(parents=True)

    sample = {
        "symbol": "BTC",
        "feed_type": "coinglass_perp",
        "updated_at": "2026-11-26T01:40:00Z",
        "ticker": {"price": 59700.0, "dir": "up", "source": "live"},
        "market_structure": {"price": {"last": 59700.0}},
        "liquidations": {
            "clusters": {
                "3d": {"stats": {"keyLevels": {"above": [{"price": 60500}], "below": [{"price": 58900}]}, "totals": {"longs": 10e6, "shorts": 15e6}}},
                "14d": {"stats": {"keyLevels": {"above": [{"price": 62100}], "below": [{"price": 57400}]}}},
                "30d": {"stats": {"keyLevels": {"above": [{"price": 64000}], "below": [{"price": 56000}]}}},
            },
            "stopmap": {
                "m1_coin_12h": {"kind": "native_heatmap_grid", "walls": {"max_short": {"price": 60400.0, "mx_usd": 20e6}, "max_long": {"price": 59100.0, "mx_usd": 12e6}}},
                "m1_coin_24h": {"kind": "native_heatmap_grid", "overview": {"largestLevel": 35e6}, "walls": {"max_short": {"price": 60800.0, "mx_usd": 35e6}, "max_long": {"price": 58800.0, "mx_usd": 18e6}}},
                "m1_coin_48h": {"kind": "native_heatmap_grid", "walls": {"max_short": {"price": 61200.0, "mx_usd": 45e6}, "max_long": {"price": 58500.0, "mx_usd": 22e6}}},
                "m1_coin_3d": {"kind": "native_heatmap_grid", "walls": {"active_short_prices": [61200.0], "active_long_prices": [58400.0], "max_short": {"price": 61900.0, "mx_usd": 60e6}, "max_long": {"price": 58400.0, "mx_usd": 30e6}}},
                "m1_coin_7d": {"kind": "native_heatmap_grid", "walls": {"max_short": {"price": 62800.0, "mx_usd": 90e6}, "max_long": {"price": 57800.0, "mx_usd": 55e6}}},
                "m1_coin_14d": {"kind": "native_heatmap_grid", "walls": {"max_short": {"price": 63500.0, "mx_usd": 110e6}, "max_long": {"price": 56900.0, "mx_usd": 70e6}}},
                "m1_coin_30d": {"kind": "native_heatmap_grid", "walls": {"max_short": {"price": 65200.0, "mx_usd": 140e6}, "max_long": {"price": 55500.0, "mx_usd": 95e6}}},
                "m1_coin_90d": {"kind": "native_heatmap_grid", "walls": {"max_short": {"price": 68500.0, "mx_usd": 125e6}, "max_long": {"price": 52000.0, "mx_usd": 80e6}}},
            },
        },
        "book_walls": {"futures": {}, "spot": {}},
        "footprint": {},
        "funding_and_oi": {},
    }
    (assets_dir / "BTC.json").write_text(json.dumps(sample), encoding="utf-8")

    out, err = eng.audit_symbol_16p("BTC", assets_dir=str(assets_dir), footprint_dir=str(tmp_path / "footprint"))
    assert err is None and out is not None
    liq = out["8_liquidations"]
    assert liq["above_14d"] == 62100 and liq["below_14d"] == 57400
    assert liq["widest_short_wall_all_tf"] == 68500.0
    assert liq["widest_long_wall_all_tf"] == 52000.0
    assert liq["max_short_notional_all_tf"]["horizon"] == "m1_coin_30d"
    assert liq["max_long_notional_all_tf"]["horizon"] == "m1_coin_30d"
    assert set(["m1_coin_12h", "m1_coin_24h", "m1_coin_48h", "m1_coin_3d", "m1_coin_7d", "m1_coin_14d", "m1_coin_30d", "m1_coin_90d"]).issubset(liq["stopmap_horizons"].keys())
