"""Parity & regression tests verifying m4_live_parity_replay matches engine/m4_live_engine.py 1:1."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from desk.quant_audits.m4_live_parity_replay import PriceTape, SignalSeries, simulate_m4_live_parity
from engine import m4_live_engine as m4
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk


ROOT = Path(__file__).resolve().parents[2]


def _make_pair(magic: int, pid: str, weight: float, cap: float = 6.0, **over) -> dict:
    base = {
        "pair_id": pid,
        "magic": magic,
        "source_symbol": over.get("source_symbol", "ETHUSDT"),
        "mt5_symbol": over.get("mt5_symbol", "ETHUSD.pi"),
        "category": over.get("category", "CRYPTO"),
        "feed_type": over.get("feed_type", "binance_footprint"),
        "sid": over.get("sid", "ema_pullback"),
        "tf": over.get("tf", "4H"),
        "tf_sec": over.get("tf_sec", 14400),
        "bucket": over.get("bucket", "PULLBACK"),
        "cfg": "test",
        "sl_atr": over.get("sl_atr", 2.0),
        "tp_r": over.get("tp_r", 4.0),
        "trail_atr": over.get("trail_atr", 0.0),
        "be_r": over.get("be_r", 1.0),
        "be_pad_r": over.get("be_pad_r", 0.05),
        "step_lock": over.get("step_lock", False),
        "dyn_atr": over.get("dyn_atr", True),
        "max_hold_bars": over.get("max_hold_bars", 10),
        "pair_weight": weight,
        "max_lot_risk_usd": cap,
        "enabled": True,
    }
    base.update(over)
    return base


def _make_cfg(pairs: list[dict], **gov_over) -> dict:
    gov = {
        "risk_pct": 0.8,
        "max_risk_single_trade_pct": 1.0,
        "max_total_open_risk_pct": 1.0,
        "max_global": 2,
        "max_per_asset": 1,
        "max_crypto_total": 1,
        "max_crypto_same_side": 1,
        "bucket_caps": {"BREAKOUT": 2, "PULLBACK": 2, "MEANREV": 3},
        "dd_throttle": [4.5, 6.5],
        "dd_resume_ratio": 0.5,
        "safety_tp_r_when_trail_only": 8.0,
        "cooldown_bars": 1,
        "spread_atr_max": 0.45,
        "deviation": 30,
    }
    gov.update(gov_over)
    return {"account": {"expected_login": 5126976, "equity_floor_usd": 1800.0}, "governor": gov, "pairs": pairs}


def test_desk_cap_blocks_btc_min_lot_while_eth_and_gau_pass():
    btc_spec = SymbolSpec("BTCUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    eth_spec = SymbolSpec("ETHUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    gau_spec = SymbolSpec("GAUUSD.pi", digits=4, point=0.0001, tick_value=0.01, tick_size=0.0001, vol_min=0.01, vol_step=0.01)

    p_btc = _make_pair(43004, "BTC_P", 1.4, mt5_symbol="BTCUSD.pi", sl_atr=3.0, cap=6.0)
    p_eth = _make_pair(43005, "ETH_P", 1.15, mt5_symbol="ETHUSD.pi", sl_atr=2.5, cap=6.0)
    p_gau = _make_pair(43009, "GAU_P", 0.99, mt5_symbol="GAUUSD.pi", category="METAL", mt5="GAUUSD.pi", sl_atr=2.0, cap=6.0)

    cfg = _make_cfg([p_btc, p_eth, p_gau])
    ts = np.array([10_000, 14_400, 28_800], dtype=np.int64)
    ets = ts + 14_400
    sigs = {
        "BTC_P": SignalSeries(p_btc, ts, ets, np.array([1, 0, 0], np.int8), np.array([1000.0, 1000.0, 1000.0]), np.array([90000.0, 90000.0, 90000.0]), np.ones(3)),
        "ETH_P": SignalSeries(p_eth, ts, ets, np.array([1, 0, 0], np.int8), np.array([40.0, 40.0, 40.0]), np.array([3000.0, 3000.0, 3000.0]), np.ones(3)),
        "GAU_P": SignalSeries(p_gau, ts, ets, np.array([1, 0, 0], np.int8), np.array([1.2, 1.2, 1.2]), np.array([100.0, 100.0, 100.0]), np.ones(3)),
    }
    tapes = {
        "BTCUSD.pi": PriceTape(ets, np.full(3, 90000.0), np.full(3, 90000.0), np.full(3, 90000.0)),
        "ETHUSD.pi": PriceTape(ets, np.full(3, 3000.0), np.full(3, 3000.0), np.full(3, 3000.0)),
        "GAUUSD.pi": PriceTape(ets, np.full(3, 100.0), np.full(3, 100.0), np.full(3, 100.0)),
    }
    specs = {"BTCUSD.pi": btc_spec, "ETHUSD.pi": eth_spec, "GAUUSD.pi": gau_spec}
    res = simulate_m4_live_parity("cap_test", cfg, sigs, tapes, specs, {"BTCUSD.pi": 0.0, "ETHUSD.pi": 0.0, "GAUUSD.pi": 0.0})

    assert [t["pair_id"] for t in res["trades"]] == ["ETH_P", "GAU_P"]
    assert res["reject_counts"]["RISK_REJECT"] == 1
    assert res["by_pair_rejects"]["BTC_P"]["RISK_REJECT"] == 1


def test_priority_weight_and_one_crypto_gate_enforced():
    eth_spec = SymbolSpec("ETHUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    btc_spec = SymbolSpec("BTCUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)

    p_low = _make_pair(43005, "ETH_LOW_W", 0.6, mt5_symbol="ETHUSD.pi", sl_atr=2.0, cap=6.0)
    p_hi = _make_pair(43010, "BTC_HI_W", 1.4, mt5_symbol="BTCUSD.pi", sl_atr=1.0, cap=25.0)
    cfg = _make_cfg([p_low, p_hi], max_total_open_risk_pct=6.0)

    ets = np.array([14_400, 28_800, 43_200], dtype=np.int64)
    ts = ets - 14_400
    sigs = {
        "ETH_LOW_W": SignalSeries(p_low, ts, ets, np.array([1, 0, 0], np.int8), np.array([40.0, 40.0, 40.0]), np.array([3000.0, 3000.0, 3000.0]), np.ones(3)),
        "BTC_HI_W": SignalSeries(p_hi, ts, ets, np.array([1, 0, 0], np.int8), np.array([100.0, 100.0, 100.0]), np.array([50000.0, 50000.0, 50000.0]), np.ones(3)),
    }
    tapes = {
        "ETHUSD.pi": PriceTape(ets, np.full(3, 3000.0), np.full(3, 3000.0), np.full(3, 3000.0)),
        "BTCUSD.pi": PriceTape(ets, np.full(3, 50000.0), np.full(3, 50000.0), np.full(3, 50000.0)),
    }
    res = simulate_m4_live_parity("prio", cfg, sigs, tapes, {"ETHUSD.pi": eth_spec, "BTCUSD.pi": btc_spec}, {"ETHUSD.pi": 0.0, "BTCUSD.pi": 0.0})
    assert [t["pair_id"] for t in res["trades"]] == ["BTC_HI_W"]
    assert res["by_pair_rejects"]["ETH_LOW_W"].get("GATE:max_crypto_total") == 1


def test_end_to_end_parity_against_harnessed_live_step_once(monkeypatch):
    """Run real M4LiveEngine.step_once() with an in-memory MT5 harness and assert exact parity with simulate_m4_live_parity."""
    eth_spec = SymbolSpec("ETHUSD.pi", digits=2, point=0.01, tick_value=0.01, tick_size=0.01, vol_min=0.01, vol_step=0.01)
    sp_spec = SymbolSpec("SP500.p", digits=2, point=0.01, tick_value=0.1, tick_size=0.01, vol_min=0.01, vol_step=0.01)

    p_eth = _make_pair(43005, "ETH_PULL", 1.4, mt5_symbol="ETHUSD.pi", sl_atr=2.0, tp_r=0.0, trail_atr=1.5, be_r=1.0, be_pad_r=0.05, step_lock=True, max_hold_bars=5, cap=6.0)
    p_sp = _make_pair(43001, "SP_DON", 1.0, mt5_symbol="SP500.p", category="INDEX", feed_type="mt5_ohlc", tf="4H", tf_sec=14400, sl_atr=2.0, tp_r=3.0, trail_atr=0.0, be_r=1.0, max_hold_bars=5, cap=6.0)
    cfg = _make_cfg([p_eth, p_sp], max_global=2, max_total_open_risk_pct=1.0, safety_tp_r_when_trail_only=8.0)

    # 6 closed 4H signal bars (open ts >= 86400, end_ts = open + 14400).
    # Note: signal bar 0 closes at 100800 -> positions open at 100800; tape bar i represents [ets[i], ets[i]+14400).
    base = 100_800
    eth_ts = base + np.arange(6, dtype=np.int64) * 14_400
    eth_ets = eth_ts + 14_400
    eth_c = np.array([2000.0, 2060.0, 2110.0, 2090.0, 2070.0, 2065.0], dtype=float)
    # Tape bars open at ets[0..4] (closing at ets[1..5])
    tape_ts = eth_ets[:-1]
    eth_h = np.array([2070.0, 2120.0, 2100.0, 2080.0, 2070.0], dtype=float)
    eth_l = np.array([2005.0, 2060.0, 2080.0, 2055.0, 2050.0], dtype=float)
    eth_tape_c = eth_c[1:]
    eth_sig = np.array([1, 0, 0, 0, 0, 0], dtype=np.int8)
    eth_atr = np.full(6, 20.0)

    sp_ts = eth_ts.copy()
    sp_ets = eth_ets.copy()
    sp_c = np.array([5000.0, 5020.0, 5075.0, 5060.0, 5055.0, 5050.0], dtype=float)
    sp_h = np.array([5030.0, 5085.0, 5065.0, 5060.0, 5055.0], dtype=float)
    sp_l = np.array([4998.0, 5020.0, 5050.0, 5045.0, 5040.0], dtype=float)
    sp_tape_c = sp_c[1:]
    sp_sig = np.array([1, 0, 0, 0, 0, 0], dtype=np.int8)
    sp_atr = np.full(6, 10.0)

    sigs = {
        "ETH_PULL": SignalSeries(p_eth, eth_ts, eth_ets, eth_sig, eth_atr, eth_c, np.ones(6)),
        "SP_DON": SignalSeries(p_sp, sp_ts, sp_ets, sp_sig, sp_atr, sp_c, np.ones(6)),
    }
    tapes = {
        "ETHUSD.pi": PriceTape(tape_ts, eth_h, eth_l, eth_tape_c),
        "SP500.p": PriceTape(tape_ts, sp_h, sp_l, sp_tape_c),
    }
    specs = {"ETHUSD.pi": eth_spec, "SP500.p": sp_spec}
    spreads = {"ETHUSD.pi": 0.0, "SP500.p": 0.0}

    replay_res = simulate_m4_live_parity("parity", cfg, sigs, tapes, specs, spreads, start_equity=2501.80)

    # Harness real M4LiveEngine step-by-step across exact same bar timeline
    clock = [int(eth_ets[0])]
    bar_idx = [0]
    live_eq = [2501.80]
    live_pos: list[SimpleNamespace] = []
    live_trades: list[dict] = []
    tick_id = [100]

    fake_mt5_specs = {
        "ETHUSD.pi": SimpleNamespace(digits=2, point=0.01, trade_tick_value=0.01, trade_tick_value_loss=0.01, trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0, trade_stops_level=0, trade_freeze_level=0),
        "SP500.p": SimpleNamespace(digits=2, point=0.01, trade_tick_value=0.1, trade_tick_value_loss=0.1, trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=50.0, trade_stops_level=0, trade_freeze_level=0),
    }
    prices_now = {"ETHUSD.pi": float(eth_c[0]), "SP500.p": float(sp_c[0])}

    monkeypatch.setattr(m4.time, "time", lambda: float(clock[0]))
    monkeypatch.setattr(m4.mt5, "terminal_info", lambda: SimpleNamespace(trade_allowed=True))
    monkeypatch.setattr(m4.mt5, "account_info", lambda: SimpleNamespace(login=5126976, equity=live_eq[0]))
    monkeypatch.setattr(m4.mt5, "symbol_info", lambda s: fake_mt5_specs[s])
    monkeypatch.setattr(m4.mt5, "symbol_info_tick", lambda s: SimpleNamespace(time=clock[0], ask=prices_now[s], bid=prices_now[s]))
    monkeypatch.setattr(m4.mt5, "orders_get", lambda: [])
    monkeypatch.setattr(m4, "spec_from_mt5", lambda s: specs[s])

    class FakeBridge:
        def connect(self):
            return True
        def ensure_visible(self, s):
            return True
        def positions(self):
            return list(live_pos)
        def total_sl_risk_usd(self, pos_list=None):
            return sum(abs(p.sl - p.price_open) * p.volume * specs[p.symbol].value_per_price_unit() for p in (pos_list if pos_list is not None else live_pos))
        def tick(self, s):
            px = prices_now[s]
            return px, px
        def send_bracket(self, spec, side, lots, entry, sl_px, tp_px, magic, comment, dev):
            t = tick_id[0]
            tick_id[0] += 1
            live_pos.append(SimpleNamespace(ticket=t, magic=magic, symbol=spec.name, type=(0 if side == 1 else 1), price_open=entry, sl=sl_px, tp=tp_px, volume=lots, time=clock[0]))
            return SimpleNamespace(retcode=10009, order=t, price=entry)
        def modify_sl(self, ticket, new_sl, tp):
            for p in live_pos:
                if p.ticket == ticket:
                    p.sl = new_sl
            return SimpleNamespace(retcode=10009)
        def close_position(self, p, dev):
            px = prices_now[p.symbol]
            sd = 1 if p.type == 0 else -1
            pnl = (px - p.price_open) * sd * p.volume * specs[p.symbol].value_per_price_unit()
            live_eq[0] += pnl
            live_trades.append({"magic": p.magic, "entry": p.price_open, "exit": px, "lots": p.volume, "pnl_usd": round(pnl, 2), "reason": "TIME_EXIT"})
            live_pos[:] = [x for x in live_pos if x.ticket != p.ticket]
            return SimpleNamespace(retcode=10009)

    eng = object.__new__(m4.M4LiveEngine)
    eng.cfg = cfg
    eng.pairs = cfg["pairs"]
    eng.pair_by_magic = {int(p["magic"]): p for p in cfg["pairs"]}
    eng.live = True
    eng.dry = False
    eng.bridge = FakeBridge()
    eng.state = SimpleNamespace(state={"bars": {}, "positions": {}, "cooldown": {}, "peak_equity": 2501.80, "dd_mult": 1.0}, save=lambda: None)
    eng.journal = SimpleNamespace(log=lambda *a, **k: None)

    def _fake_gather():
        i = bar_idx[0]
        pl = {
            "ETH_PULL": {"ts": int(eth_ts[i]), "end_ts": int(eth_ets[i]), "side": int(eth_sig[i]), "atr": float(eth_atr[i]), "c": float(eth_c[i]), "vof_mult": 1.0},
            "SP_DON": {"ts": int(sp_ts[i]), "end_ts": int(sp_ets[i]), "side": int(sp_sig[i]), "atr": float(sp_atr[i]), "c": float(sp_c[i]), "vof_mult": 1.0},
        }
        al = {"ETHUSD.pi": {14400: float(eth_atr[i])}, "SP500.p": {14400: float(sp_atr[i])}}
        return pl, al

    eng._gather_signals = _fake_gather

    for idx in range(len(eth_ets)):
        if idx > 0:
            # Intrabar server bracket settlement across [ets[idx-1], ets[idx]) using tape bar idx-1
            t_i = idx - 1
            remain = []
            for p in live_pos:
                sd = 1 if p.type == 0 else -1
                hi = eth_h[t_i] if p.symbol == "ETHUSD.pi" else sp_h[t_i]
                lo = eth_l[t_i] if p.symbol == "ETHUSD.pi" else sp_l[t_i]
                hit_px = None
                if sd == 1 and lo <= p.sl:
                    hit_px = p.sl
                elif sd == 1 and hi >= p.tp:
                    hit_px = p.tp
                if hit_px is not None:
                    pnl = (hit_px - p.price_open) * sd * p.volume * specs[p.symbol].value_per_price_unit()
                    live_eq[0] += pnl
                    live_trades.append({"magic": p.magic, "entry": p.price_open, "exit": hit_px, "lots": p.volume, "pnl_usd": round(pnl, 2), "reason": "BRACKET"})
                else:
                    if str(p.ticket) in eng.state.state["positions"]:
                        eng.state.state["positions"][str(p.ticket)]["best_px"] = max(eng.state.state["positions"][str(p.ticket)]["best_px"], float(hi))
                    remain.append(p)
            live_pos[:] = remain
        bar_idx[0] = idx
        clock[0] = int(eth_ets[idx])
        prices_now["ETHUSD.pi"] = float(eth_c[idx])
        prices_now["SP500.p"] = float(sp_c[idx])
        eng.step_once()

    for p in list(live_pos):
        eng.bridge.close_position(p, 30)

    assert len(replay_res["trades"]) == len(live_trades) == 2
    by_m_rep = {t["magic"]: t for t in replay_res["trades"]}
    by_m_liv = {t["magic"]: t for t in live_trades}
    for magic in (43001, 43005):
        assert by_m_rep[magic]["entry"] == pytest.approx(by_m_liv[magic]["entry"])
        assert by_m_rep[magic]["lots"] == pytest.approx(by_m_liv[magic]["lots"])
        assert by_m_rep[magic]["exit"] == pytest.approx(by_m_liv[magic]["exit"])
        assert by_m_rep[magic]["pnl_usd"] == pytest.approx(by_m_liv[magic]["pnl_usd"])
    assert replay_res["metrics"]["end_equity"] == pytest.approx(round(live_eq[0], 2))


def test_real_configs_enforce_desk_bounds():
    desk = json.loads((ROOT / "engine" / "config_m4_v3b_desk.json").read_text(encoding="utf-8"))
    gov = desk["governor"]
    assert gov["max_global"] == 2
    assert gov["max_per_asset"] == 1
    assert gov["max_crypto_total"] == 1
    assert gov["max_total_open_risk_pct"] == 1.0
    assert all(p["max_lot_risk_usd"] == 6.0 for p in desk["pairs"])
