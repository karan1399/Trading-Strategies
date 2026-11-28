"""Unit & causality tests for upgraded 10-sleeve Alpha Baskets and CG SL/TP optimizer."""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.cg_sl_tp import optimize_bracket_with_cg, resolve_cg_symbol  # noqa: E402
from engine.unified_alpha_pilot import SymbolSpec, load_resolved_config  # noqa: E402
from strategies.registry import load_signal_fn  # noqa: E402


def test_config_loads_all_10_sleeves():
    cfg = load_resolved_config()
    sleeves = cfg["sleeves"]
    expected = {
        "htf_gold", "ivwall", "vc_gold", "orb_nas",
        "ig_s7_daily_pivot", "ig_s5_zscore_breakout",
        "ig_s1_williams_pullback", "ig_s2_rsi2_meanrev",
        "ig_s9_donchian_cta", "ig_s10_ema_pullback",
    }
    disabled = {name for name in expected if not sleeves.get(name, {}).get("enabled", True)}
    assert expected.issubset(sleeves.keys())
    for name in expected:
        assert sleeves[name]["enabled"] is (name not in disabled)
        assert sleeves[name].get("display_name") and sleeves[name].get("generic_id")
        assert len(sleeves[name]["symbols"]) >= 1
    from strategies.core.registry import generic_strategy_name
    assert generic_strategy_name("orb_nas") == "Intraday Breakout — London Open Range"
    assert generic_strategy_name("Whale_p70") == "Order-Flow — Large Whale Execution"
    assert cfg["governor"]["max_total_open_risk_pct"] == 1.0
    assert cfg["governor"]["daily_dd_halt_usd"] == -80.0


def test_governor_enforces_aggregate_sl_risk_and_daily_dd(tmp_path):
    from engine.unified_alpha_pilot import Governor, Journal, StateStore

    cfg = load_resolved_config()
    gov = Governor(cfg, Journal(str(tmp_path / "j.csv")), StateStore(str(tmp_path / "s.json")), None)

    ok, why = gov.gates(42009, [], planned_risk_usd=5.30, committed_risk_usd=23.53, daily_pnl_usd=-6.55, equity_usd=2535.0)
    assert ok is False and "aggregate SL risk" in why

    ok2, why2 = gov.gates(42009, [], planned_risk_usd=5.30, committed_risk_usd=18.00, daily_pnl_usd=-6.55, equity_usd=2535.0)
    assert ok2 is True and why2 == ""

    ok3, why3 = gov.gates(42009, [], planned_risk_usd=5.30, committed_risk_usd=10.00, daily_pnl_usd=-81.00, equity_usd=2535.0)
    assert ok3 is False and "daily realized" in why3


def test_cg_sl_cold_zone_respects_risk_cap_and_min_r():
    spec = SymbolSpec(name="ETHUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    # 1600 entry, initial sl=1580 (rd=20, min_lot_risk=$0.20), tp=1670 (3.5R)
    # Liquidation cluster just past SL at 1576 -> safe to widen to 1576 - max(0.15*10, 0.06*20)=1574.5 (rd=25.5, min_lot_risk=$0.255 <= $6.0, tp_r=(1663.0-1600)/25.5 >= 2.0)
    ctx = {
        "source": "mock",
        "liq_below": [(1576.0, 82.0)],
        "liq_above": [],
        "ask_walls": [(1665.0, 2_000_000.0)],
        "bid_walls": [],
        "auction_nodes": [],
    }
    res = optimize_bracket_with_cg(
        "ETHUSD.pi", side=1, entry=1600.0, sl=1580.0, tp=1670.0, atr=10.0,
        live_spread=0.5, min_tp_r=2.0, max_spread_risk_frac=0.25,
        spec=spec, risk_usd_max=6.0, digits=2, cg_ctx=ctx,
    )
    assert res.sl_adjusted is True
    assert res.sl < 1576.0
    assert res.tp_adjusted is True
    assert res.tp < 1665.0  # front-ran opposing ask wall
    assert res.tp_r >= 2.0
    assert res.sl < 1600.0 < res.tp


def test_cg_sl_refuses_widening_past_risk_cap():
    # SOLUSD min_lot 0.01 at value_per_price_unit=100 -> 1.0 price move = $1.00/0.01lot
    spec = SymbolSpec(name="SOLUSD.p", digits=3, point=0.001, tick_value=1.0, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    # Initial rd=5.5 (min_lot_risk=$5.50 <= $6.00). Cluster at 94.0 would push rd to 6.8 (min_lot_risk=$6.80 > $6.00) -> MUST NOT widen!
    ctx = {
        "source": "mock",
        "liq_below": [(94.0, 90.0)],
        "liq_above": [], "ask_walls": [], "bid_walls": [], "auction_nodes": [],
    }
    res = optimize_bracket_with_cg(
        "SOLUSD.p", side=1, entry=100.0, sl=94.5, tp=116.5, atr=2.0,
        live_spread=0.05, min_tp_r=2.0, max_spread_risk_frac=0.25,
        spec=spec, risk_usd_max=6.0, digits=3, cg_ctx=ctx,
    )
    assert res.sl_adjusted is False
    assert res.sl == 94.5


def test_cg_scale_guard_blocks_unit_mismatch():
    # GAUUSD (~113.8) passed XAU troy oz prices (~3540) must NEVER corrupt bracket!
    assert resolve_cg_symbol("GAUUSD.pi") in ("GAUUSD", "XAU")
    ctx = {
        "source": "mock",
        "liq_below": [(3460.0, 95.0)],
        "liq_above": [(3600.0, 95.0)],
        "ask_walls": [(3580.0, 5_000_000.0)],
        "bid_walls": [], "auction_nodes": [3530.0],
    }
    res = optimize_bracket_with_cg(
        "GAUUSD.pi", side=1, entry=113.80, sl=111.50, tp=120.70, atr=1.10,
        live_spread=0.05, min_tp_r=2.0, digits=3, cg_ctx=ctx,
    )
    assert res.sl_adjusted is False and res.tp_adjusted is False
    assert res.sl == 111.50 and res.tp == 120.70


def test_s5_s9_s10_causality_on_real_parquet():
    df = pd.read_parquet(os.path.join(ROOT, "historical_data", "mt5_parquet", "4H", "GAUUSD.pi.parquet")).sort_values("datetime")
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    t = (pd.to_datetime(df["datetime"], utc=True).dt.as_unit("s").astype("int64") // 60).to_numpy()
    from strategies._indicators import wilder_atr
    atr = wilder_atr(h, l, c, 14)

    for mod_path in (
        "strategies.s05_zscore_breakout.strategy",
        "strategies.s09_donchian_trend_cta.strategy",
        "strategies.s10_ema_pullback.strategy",
        "strategies.s03_htf_ema_rejection.strategy",
        "strategies.s04_vc_squeeze_breakout.strategy",
        "strategies.s11_or30_london_breakout.strategy",
        # Also verify legacy alias still resolves transparently
        "strategies.ig_s7_daily_pivot.strategy",
    ):
        fn = load_signal_fn(mod_path)
        for k in (350, len(c) // 2, len(c) - 10, len(c) - 2):
            full_sig = fn(o, h, l, c, t, atr, k, {}, {})
            pref_sig = fn(o[: k + 1], h[: k + 1], l[: k + 1], c[: k + 1], t[: k + 1], atr[: k + 1], k, {}, {})
            assert (full_sig is None) == (pref_sig is None)
            if full_sig is not None:
                assert full_sig["side"] == pref_sig["side"]
                assert full_sig["note"] == pref_sig["note"]


def test_strategy_stack_layering_and_vol_overlay():
    from strategies import STRATEGY_CATALOG, StackLayer, StrategyStack

    assert len(STRATEGY_CATALOG) >= 11
    df = pd.read_parquet(os.path.join(ROOT, "historical_data", "mt5_parquet", "4H", "GAUUSD.pi.parquet")).sort_values("datetime")
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    t = (pd.to_datetime(df["datetime"], utc=True).dt.as_unit("s").astype("int64") // 60).to_numpy()
    from strategies._indicators import wilder_atr
    atr = wilder_atr(h, l, c, 14)

    stack = StrategyStack([
        StackLayer("s03_htf_ema_rejection"),
        StackLayer("s05_zscore_breakout"),
        StackLayer("s07_daily_pivot_breakout"),
        StackLayer("s09_donchian_trend_cta"),
        StackLayer("s10_ema_pullback"),
    ])
    risk_usd = stack.size_risk_usd(c)
    assert 5.0 <= risk_usd <= 6.0
    res = stack.evaluate(o, h, l, c, t, atr, len(c) - 2, mode="all")
    assert isinstance(res, list)


def test_fetch_cg_context_ingests_http_stopmap_keylevels(monkeypatch):
    from engine import cg_sl_tp

    cg_sl_tp._TTL_CACHE.clear()
    monkeypatch.setattr(cg_sl_tp, "_load_disk_asset", lambda _sym: {})

    def fake_http(url: str, timeout_s: float = 1.2):
        if "/api/cg/stopmap" in url:
            return {
                "ok": True,
                "model": {
                    "overview": {"largestLevel": 40_000_000.0},
                    "keyLevels": {
                        "above": [
                            {"price": 1729.60, "usd": 40_000_000.0},
                            {"price": 1735.93, "usd": 28_000_000.0},
                        ],
                        "below": [{"price": 1607.16, "usd": 36_000_000.0}],
                    },
                },
            }
        return None

    monkeypatch.setattr(cg_sl_tp, "_http_get_json", fake_http)
    ctx = cg_sl_tp.fetch_cg_context("ETHUSD.pi", use_http=True)
    assert "http_stopmap" in ctx["source"]
    assert (1729.60, 100.0) in ctx["liq_above"]
    assert (1735.93, 70.0) in ctx["liq_above"]
    assert (1607.16, 90.0) in ctx["liq_below"]


def test_audit_hardening_c1_h1_h2_h3_h5(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import numpy as np
    import strategies
    from engine import cg_sl_tp
    from engine.unified_alpha_pilot import Governor, Journal, StateStore, load_resolved_config
    from exchange_feed.backtesting.fast_kernels import sim_1m_trades_nb

    # C1: strategies.__init__ exposes importlib.util
    assert hasattr(strategies.importlib, "util")

    # H1 + Crypto cluster: blocks GAU across magics & blocks 2nd Crypto while allowing Oil/Index
    cfg = load_resolved_config()
    gov = Governor(cfg, Journal(str(tmp_path / "j.csv")), StateStore(str(tmp_path / "s.json")), None)
    open_gau = [SimpleNamespace(magic=42003, symbol="GAUUSD.pi")]
    ok_g2, why_g2 = gov.gates(42008, open_gau, planned_risk_usd=5.0, committed_risk_usd=5.0, equity_usd=2500.0, symbol="GAUCNH.pi")
    ok_eth, _ = gov.gates(42008, open_gau, planned_risk_usd=5.0, committed_risk_usd=5.0, equity_usd=2500.0, symbol="ETHUSD.pi")
    assert ok_g2 is False and "gold-cluster" in why_g2
    assert ok_eth is True
    open_eth = [SimpleNamespace(magic=42006, symbol="ETHUSD.pi")]
    ok_bch, why_bch = gov.gates(42010, open_eth, planned_risk_usd=5.0, committed_risk_usd=5.0, equity_usd=2500.0, symbol="BCHUSD.p")
    ok_oil, _ = gov.gates(42011, open_eth, planned_risk_usd=5.0, committed_risk_usd=5.0, equity_usd=2500.0, symbol="USWTI.p")
    assert ok_bch is False and "crypto-cluster" in why_bch
    assert ok_oil is True

    # H3: Failed live BE modify_sl returning None must NOT set be_done=True
    st = StateStore(str(tmp_path / "s2.json"))
    st.state["positions"]["999"] = {"sleeve": "htf_gold", "entry": 100.0, "r0": 2.0, "be_done": False, "be_enabled": True}
    fake_bridge = SimpleNamespace(modify_sl=lambda *_a, **_k: None)
    gov_live = Governor(cfg, Journal(str(tmp_path / "j2.csv")), st, fake_bridge)
    cnt = gov_live.be_lock([SimpleNamespace(ticket=999, magic=42003, symbol="GAUUSD.pi", volume=0.01, type=0, price_open=100.0, sl=98.0, tp=106.0)], lambda _s: (102.5, 102.4))
    assert cnt == 0 and st.state["positions"]["999"]["be_done"] is False

    # H5: Stale disk telemetry (>900s) is rejected by _load_disk_asset
    stale_dir = tmp_path / "assets"
    stale_dir.mkdir()
    (stale_dir / "ETH.json").write_text(json.dumps({"fetched_at_ts": 1000.0, "liquidations": {"clusters": {}}}), encoding="utf-8")
    monkeypatch.setattr(cg_sl_tp, "TELEMETRY_ASSETS_DIR", str(stale_dir))
    assert cg_sl_tp._load_disk_asset("ETH", max_age_s=900.0) == {}

    # H2: Forming-bar ATR spike must NOT widen completed prior bar trail (i>=235 loop)
    n_bars = 242
    tf_sec = 14400
    ts_tf = np.arange(n_bars, dtype=np.int64) * tf_sec
    end_tf = ts_tf + tf_sec
    atr_tf = np.full(n_bars, 1.0, dtype=np.float64)
    atr_tf[238:] = 50.0  # spike on bar 238 must NOT affect trail evaluated at bar 238 start
    e89 = np.full(n_bars, 90.0)
    e233 = np.full(n_bars, 80.0)
    c_tf = np.full(n_bars, 100.0)
    side_arr = np.zeros(n_bars, dtype=np.int8)
    side_arr[236] = 1  # enters at end of 236 = open of bar 237 (237 * tf_sec)
    m1_ts = np.arange(0, n_bars * tf_sec, 60, dtype=np.int64)
    m1_o = np.full(len(m1_ts), 100.0)
    m1_h = np.full(len(m1_ts), 100.0)
    m1_l = np.full(len(m1_ts), 100.0)
    m1_c = np.full(len(m1_ts), 100.0)
    m1_h[237 * (tf_sec // 60) : 238 * (tf_sec // 60)] = 105.0
    m1_l[238 * (tf_sec // 60) + 2 :] = 97.0
    res_nb = sim_1m_trades_nb(
        ts_tf, end_tf, atr_tf, e89, e233, c_tf, side_arr,
        m1_ts, m1_o, m1_h, m1_l, m1_c,
        tf_sec, 10.0, 0.0, 2.0, 0.0, 0.05, False, 48, 1, True,
        False, 0.0, False, 0.0, 0.0, 5, 0.0, 0.0, np.empty(0, np.int64), np.empty(0, np.float64),
    )
    assert len(res_nb[0]) == 1 and float(res_nb[6][0]) == 103.0
