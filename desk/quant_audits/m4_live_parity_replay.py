"""Exact live-parity bar-by-bar replay for engine/m4_live_engine.py.

Reuses the exact live functions from engine.m4_live_engine and engine.unified_alpha_pilot
so backtest equity curves reflect the identical signal generation, (-pair_weight, magic)
admission priority, compute_pair_risk_usd, lots_for_risk, m4_portfolio_gate,
update_dd_state hysteresis, max(2.0, tp_r) / 8.0 safety TP, compute_bar_trail_sl
boundary ratchets, cooldown_bars, and max_hold_bars time exits.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from engine import m4_live_engine as m4
from engine.unified_alpha_pilot import SymbolSpec, lots_for_risk, spec_from_fallback, spec_from_mt5
from exchange_feed.backtesting.multi_asset_optimizer import compute_hac_stats
from exchange_feed.backtesting.mt5_shared_account_portfolio_optimizer import prop_mc_20k


ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "exchange_feed" / "data" / "cache"
DEFAULT_OUT = Path(__file__).resolve().with_name("m4_live_parity_results.json")


@dataclass
class PriceTape:
    """Sorted intrabar OHLC arrays used to simulate hard SL/TP and track MFE."""

    ts: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray


@dataclass
class SignalSeries:
    """Vectorized causal signal arrays for one configured M4 pair."""

    pair: dict[str, Any]
    ts: np.ndarray
    end_ts: np.ndarray
    side: np.ndarray
    atr: np.ndarray
    close: np.ndarray
    vof_mult: np.ndarray
    tf_by_end_ts: dict[int, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tf_by_end_ts:
            self.tf_by_end_ts = {
                int(self.end_ts[i]): float(self.atr[i])
                for i in range(len(self.end_ts))
                if np.isfinite(self.atr[i]) and self.atr[i] > 0
            }


@dataclass
class OpenPos:
    """Active simulated position matching MT5Bridge.positions() + state['positions']."""

    ticket: int
    pair: dict[str, Any]
    spec: SymbolSpec
    side: int
    entry: float
    lots: float
    sl_px: float
    tp_px: float
    sd: float
    atr_entry: float
    open_ts: int
    last_managed_b: int
    best_px: float
    spread_px: float
    vof_mult: float
    dd_mult: float
    planned_risk_usd: float
    actual_risk_usd: float
    trail_updates: int = 0

    @property
    def magic(self) -> int:
        return int(self.pair["magic"])

    @property
    def symbol(self) -> str:
        return str(self.pair["mt5_symbol"])

    @property
    def type(self) -> int:
        return 0 if self.side == 1 else 1

    @property
    def sl(self) -> float:
        return self.sl_px

    @property
    def tp(self) -> float:
        return self.tp_px

    @property
    def price_open(self) -> float:
        return self.entry

    @property
    def volume(self) -> float:
        return self.lots

    def current_committed_risk(self) -> float:
        """Matches MT5Bridge.total_sl_risk_usd(): abs(sl - entry) * vol * unit."""
        return abs(self.sl_px - self.entry) * self.lots * self.spec.value_per_price_unit()


def _crypto_signal_key(pair: dict[str, Any]) -> tuple[str, str]:
    sid, tf = str(pair["sid"]), str(pair["tf"])
    if sid == "rsi2_of_reversion":
        tag = "rsi=20.0" if "rsi=20" in str(pair.get("cfg", "")) else "rsi=15.0"
        return (f"rsi2_of_reversion:{tag}", tf)
    return (sid, tf)


def compute_crypto_vof_series(g2: pd.DataFrame, g4: pd.DataFrame, pair: dict[str, Any], side_arr: np.ndarray) -> np.ndarray:
    """Compute causal per-bar vof_mult matching eval_crypto_signals() at each bar's close."""
    n = len(side_arr)
    out = np.ones(n, dtype=float)
    nz = np.where(side_arr != 0)[0]
    if len(nz) == 0:
        return out
    c4 = g4["c"].to_numpy(dtype=float)
    dp4 = g4["d_pct"].to_numpy(dtype=float)
    bi4 = g4["buy_imb"].to_numpy(dtype=np.int64)
    si4 = g4["sell_imb"].to_numpy(dtype=np.int64)
    fp4 = m4.build_fp_context(g4)
    vpin4 = fp4["vpin_rank"]
    end4 = g4["end_ts"].to_numpy(dtype=np.int64)
    imba4 = bi4 - si4
    sum_imba4 = bi4 + si4
    is_2h = str(pair["tf"]).upper() == "2H"
    end2 = g2["end_ts"].to_numpy(dtype=np.int64) if is_2h else end4

    for idx in nz:
        idx4 = int(np.searchsorted(end4, int(end2[idx]), side="right")) - 1 if is_2h else int(idx)
        if idx4 < 0:
            continue
        sd = int(side_arr[idx])
        imb_v = int(imba4[idx4]) if sum_imba4[idx4] > 0 else sd
        out[idx] = float(
            m4.vol_of_conviction_risk_usd(
                c4[max(0, idx4 - 245) : idx4 + 1],
                "H4",
                sd,
                float(dp4[idx4]),
                imb_v,
                float(vpin4[idx4]),
                1.0,
                0.65,
                1.25,
                target_ann_vol=0.55,
                lookback=240,
            )
        )
    return out


def _round_tick(price: float, spec: SymbolSpec) -> float:
    if spec.tick_size <= 0:
        return round(price, spec.digits)
    return round(round(price / spec.tick_size) * spec.tick_size, spec.digits)


def _advance_intrabar(
    positions: list[OpenPos],
    tapes: dict[str, PriceTape],
    t_prev: int,
    t_curr: int,
    on_close,
) -> list[OpenPos]:
    """Scan [t_prev, t_curr) intrabar tape for SL/TP hits & update MFE best_px."""
    if not positions or t_curr <= t_prev:
        return positions
    still_open: list[OpenPos] = []
    for pos in positions:
        tape = tapes.get(pos.symbol)
        sp = pos.spread_px
        deadline = pos.open_ts + int(pos.pair["max_hold_bars"]) * int(pos.pair["tf_sec"])
        end_scan = min(t_curr, deadline)

        if tape is not None and len(tape.ts):
            i0 = int(np.searchsorted(tape.ts, t_prev, side="left"))
            i1 = int(np.searchsorted(tape.ts, end_scan, side="left"))
        else:
            i0, i1 = 0, 0

        closed_at: tuple[int, float, str] | None = None
        if i1 > i0:
            hi_s = tape.high[i0:i1]
            lo_s = tape.low[i0:i1]
            ts_s = tape.ts[i0:i1]
            cl_s = tape.close[i0:i1]

            if pos.side == 1:
                hit_sl = bool(np.any(lo_s <= pos.sl_px))
                hit_tp = bool(np.any(hi_s >= pos.tp_px))
                if not hit_sl and not hit_tp:
                    pos.best_px = max(pos.best_px, float(np.max(hi_s)))
                else:
                    for k in range(len(ts_s)):
                        if lo_s[k] <= pos.sl_px:
                            reason = "TRAIL_SL" if pos.trail_updates > 0 else "SL"
                            closed_at = (int(ts_s[k]), float(pos.sl_px), reason)
                            break
                        if hi_s[k] >= pos.tp_px:
                            closed_at = (int(ts_s[k]), float(pos.tp_px), "TP")
                            break
                        if hi_s[k] > pos.best_px:
                            pos.best_px = float(hi_s[k])
            else:
                ask_hi = hi_s + sp
                ask_lo = lo_s + sp
                hit_sl = bool(np.any(ask_hi >= pos.sl_px))
                hit_tp = bool(np.any(ask_lo <= pos.tp_px))
                if not hit_sl and not hit_tp:
                    pos.best_px = min(pos.best_px, float(np.min(ask_lo)))
                else:
                    for k in range(len(ts_s)):
                        if ask_hi[k] >= pos.sl_px:
                            reason = "TRAIL_SL" if pos.trail_updates > 0 else "SL"
                            closed_at = (int(ts_s[k]), float(pos.sl_px), reason)
                            break
                        if ask_lo[k] <= pos.tp_px:
                            closed_at = (int(ts_s[k]), float(pos.tp_px), "TP")
                            break
                        if ask_lo[k] < pos.best_px:
                            pos.best_px = float(ask_lo[k])

            if closed_at is None and t_curr >= deadline:
                idx = max(0, min(len(tape.ts) - 1, int(np.searchsorted(tape.ts, deadline, side="left")) - 1))
                ex = float(tape.close[idx]) + (0.0 if pos.side == 1 else sp)
                closed_at = (deadline, ex, "TIME_EXIT")
        elif t_curr >= deadline:
            ex = pos.best_px
            closed_at = (deadline, ex, "TIME_EXIT")

        if closed_at is not None:
            on_close(pos, closed_at[0], closed_at[1], closed_at[2])
        else:
            still_open.append(pos)
    return still_open


def simulate_m4_live_parity(
    name: str,
    cfg: dict[str, Any],
    signals: dict[str, SignalSeries],
    tapes: dict[str, PriceTape],
    specs: dict[str, SymbolSpec],
    spreads: dict[str, float],
    *,
    start_equity: float = 2501.80,
    min_ts: int | None = None,
    max_ts: int | None = None,
    override_max_lot_cap: float | None = None,
    mc_runs: int = 0,
) -> dict[str, Any]:
    """Chronological bar-by-bar replay using identical M4LiveEngine step_once logic."""
    gov = cfg["governor"]
    pairs = [p for p in cfg["pairs"] if p.get("enabled", True)]
    ordered_pairs = sorted(pairs, key=lambda x: (-float(x["pair_weight"]), int(x["magic"])))
    pair_by_magic = {int(p["magic"]): p for p in ordered_pairs}

    # Build exact event index: (end_ts, pid) -> signal row index
    sig_at_ts: dict[int, dict[str, int]] = {}
    all_ts_set: set[int] = set()
    for pid, ss in signals.items():
        for idx, ets in enumerate(ss.end_ts):
            t_val = int(ets)
            if min_ts is not None and t_val < min_ts:
                continue
            if max_ts is not None and t_val >= max_ts:
                continue
            all_ts_set.add(t_val)
            sig_at_ts.setdefault(t_val, {})[pid] = idx

    if not all_ts_set:
        return {
            "name": name,
            "metrics": {"trades": 0, "end_equity": start_equity, "cagr_pct": 0.0, "max_dd_pct": 0.0, "calmar": 0.0, "pf": 0.0},
            "trades": [],
            "curve": [],
            "reject_counts": {},
            "by_asset": [],
            "by_pair": [],
        }

    timeline = sorted(all_ts_set)
    equity = float(start_equity)
    peak = float(start_equity)
    dd_state: dict[str, Any] = {"peak_equity": float(start_equity), "dd_mult": 1.0}
    cooldown: dict[str, int] = {}
    positions: list[OpenPos] = []
    trades: list[dict[str, Any]] = []
    curve: list[tuple[int, float]] = [(timeline[0], equity)]
    dd_vals: list[float] = [0.0]
    daily_r_map: dict[int, float] = {}
    reject_counts: dict[str, int] = {}
    by_pair_rej: dict[str, dict[str, int]] = {}
    max_dd_pct = 0.0
    next_ticket = 1
    prev_t = timeline[0]

    def _rec_reject(pid: str, reason: str) -> None:
        reject_counts[reason] = reject_counts.get(reason, 0) + 1
        d = by_pair_rej.setdefault(pid, {})
        d[reason] = d.get(reason, 0) + 1

    def _on_close(pos: OpenPos, close_ts: int, exit_px: float, reason: str) -> None:
        nonlocal equity, peak, max_dd_pct
        pnl_usd = ((exit_px - pos.entry) * pos.side) * pos.lots * pos.spec.value_per_price_unit()
        equity += pnl_usd
        peak = max(peak, equity)
        dd = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)
        dd_vals.append(dd)
        curve.append((close_ts, round(equity, 4)))
        cooldown[str(pos.magic)] = int(close_ts)

        r_realized = pnl_usd / pos.actual_risk_usd if pos.actual_risk_usd > 0 else 0.0
        eq_before = equity - pnl_usd
        r_account = (pnl_usd / (eq_before * (float(gov["risk_pct"]) / 100.0))) if eq_before > 0 else 0.0
        dk = int(close_ts // 86400)
        daily_r_map[dk] = daily_r_map.get(dk, 0.0) + r_account

        trades.append({
            "ticket": pos.ticket,
            "pair_id": pos.pair["pair_id"],
            "magic": pos.magic,
            "mt5_symbol": pos.symbol,
            "category": pos.pair["category"],
            "sid": pos.pair["sid"],
            "tf": pos.pair["tf"],
            "side": "BUY" if pos.side == 1 else "SELL",
            "ts_in": pos.open_ts,
            "ts_out": int(close_ts),
            "entry": pos.entry,
            "exit": exit_px,
            "lots": pos.lots,
            "sl_init": round(pos.entry - pos.side * pos.sd, pos.spec.digits),
            "sl_final": pos.sl_px,
            "tp": pos.tp_px,
            "risk_usd": round(pos.actual_risk_usd, 2),
            "pnl_usd": round(pnl_usd, 2),
            "r_realized": round(r_realized, 3),
            "r_account": round(r_account, 3),
            "vof_mult": round(pos.vof_mult, 3),
            "dd_mult": pos.dd_mult,
            "trail_updates": pos.trail_updates,
            "reason": reason,
        })

    for t in timeline:
        # 1. Advance active positions intrabar over [prev_t, t)
        if positions and t > prev_t:
            positions = _advance_intrabar(positions, tapes, prev_t, t, _on_close)
        prev_t = t

        # 2. Live _manage_open_positions closed-bar management at timestamp t
        bar_map = sig_at_ts.get(t, {})
        if positions:
            next_positions: list[OpenPos] = []
            for pos in positions:
                p = pos.pair
                tf_sec = int(p["tf_sec"])
                if (t - pos.open_ts) >= int(p["max_hold_bars"]) * tf_sec:
                    tape = tapes.get(pos.symbol)
                    ex = pos.best_px
                    if tape is not None and len(tape.ts):
                        idx = max(0, min(len(tape.ts) - 1, int(np.searchsorted(tape.ts, t, side="left")) - 1))
                        ex = float(tape.close[idx]) + (0.0 if pos.side == 1 else pos.spread_px)
                    _on_close(pos, t, ex, "TIME_EXIT")
                    continue

                cur_b = t // tf_sec
                if cur_b != pos.last_managed_b:
                    ss = signals.get(p["pair_id"])
                    latest_atr = ss.tf_by_end_ts.get(t, pos.atr_entry) if ss is not None else pos.atr_entry
                    meta = {"side": pos.side, "entry": pos.entry, "sd": pos.sd, "atr_entry": pos.atr_entry}
                    new_sl = m4.compute_bar_trail_sl(
                        meta, pos.best_px, pos.sl_px, latest_atr,
                        float(p["be_r"]), float(p["be_pad_r"]),
                        bool(p["step_lock"]), float(p["trail_atr"]), bool(p["dyn_atr"]),
                    )
                    if new_sl is not None:
                        new_sl_r = _round_tick(new_sl, pos.spec)
                        if (new_sl_r - pos.sl_px) * pos.side > 1e-9:
                            tape = tapes.get(pos.symbol)
                            cur_px = pos.best_px
                            if tape is not None and len(tape.ts):
                                idx = max(0, min(len(tape.ts) - 1, int(np.searchsorted(tape.ts, t, side="left")) - 1))
                                cur_px = float(tape.close[idx]) + (0.0 if pos.side == 1 else pos.spread_px)
                            if (cur_px - new_sl_r) * pos.side > 0:
                                pos.sl_px = new_sl_r
                                pos.trail_updates += 1
                    pos.last_managed_b = cur_b
                next_positions.append(pos)
            positions = next_positions

        if not bar_map:
            continue

        # 3. Live step_once evaluation in (-pair_weight, magic) priority order
        dd_mult = m4.update_dd_state(
            dd_state, equity, gov["dd_throttle"], float(gov.get("dd_resume_ratio", 0.50))
        )
        committed = sum(pos.current_committed_risk() for pos in positions)

        for pair in ordered_pairs:
            pid = pair["pair_id"]
            sig_idx = bar_map.get(pid)
            if sig_idx is None:
                continue
            ss = signals[pid]
            side = int(ss.side[sig_idx])
            if side == 0:
                continue

            ms = pair["mt5_symbol"]
            spec = specs.get(ms) or spec_from_fallback(cfg, ms)
            spread_px = float(spreads.get(ms, 0.0))
            atr = float(ss.atr[sig_idx])
            close_px = float(ss.close[sig_idx])
            if not math.isfinite(atr) or atr <= 0:
                _rec_reject(pid, "BAD_ATR")
                continue

            if spread_px > float(gov.get("spread_atr_max", 0.45)) * atr:
                _rec_reject(pid, "SPREAD_VETO")
                continue

            vof_mult = float(ss.vof_mult[sig_idx])
            risk_usd = m4.compute_pair_risk_usd(equity, cfg, pair, dd_mult, vof_mult)
            if override_max_lot_cap is not None:
                max_lot_cap = float(override_max_lot_cap)
            else:
                max_lot_cap = float(
                    pair.get("max_lot_risk_usd", equity * float(gov["max_risk_single_trade_pct"]) / 100.0)
                )
            sl_dist = float(pair["sl_atr"]) * atr
            lots, _ = lots_for_risk(spec, sl_dist, risk_usd, max_lot_cap)
            if lots is None:
                _rec_reject(pid, "RISK_REJECT")
                continue

            actual_risk = sl_dist * lots * spec.value_per_price_unit()
            pos_views = [
                SimpleNamespace(magic=p.magic, symbol=p.symbol, type=p.type)
                for p in positions
            ]
            ok_gate, g_reason = m4.m4_portfolio_gate(
                pair, side, pos_views, cfg, pair_by_magic, cooldown, t, equity, committed, actual_risk
            )
            if not ok_gate:
                tag = g_reason.split()[0] if g_reason else "GATE_BLOCK"
                _rec_reject(pid, f"GATE:{tag}")
                continue

            entry = close_px + (spread_px if side == 1 else 0.0)
            sl_px = round(entry - side * sl_dist, spec.digits)
            raw_tp = float(pair["tp_r"]) if float(pair["tp_r"]) > 0.0 else float(gov.get("safety_tp_r_when_trail_only", 8.0))
            tp_mult = max(2.0, raw_tp)
            tp_px = round(entry + side * tp_mult * sl_dist, spec.digits)

            pos = OpenPos(
                ticket=next_ticket,
                pair=pair,
                spec=spec,
                side=side,
                entry=entry,
                lots=float(lots),
                sl_px=float(sl_px),
                tp_px=float(tp_px),
                sd=sl_dist,
                atr_entry=atr,
                open_ts=t,
                last_managed_b=t // int(pair["tf_sec"]),
                best_px=entry,
                spread_px=spread_px,
                vof_mult=vof_mult,
                dd_mult=dd_mult,
                planned_risk_usd=risk_usd,
                actual_risk_usd=actual_risk,
            )
            next_ticket += 1
            positions.append(pos)
            committed += pos.current_committed_risk()

    # Force-close remaining open trades at final timeline mark-to-market for metric accounting
    t_end = timeline[-1]
    for pos in list(positions):
        tape = tapes.get(pos.symbol)
        ex = pos.best_px
        if tape is not None and len(tape.ts):
            ex = float(tape.close[-1]) + (0.0 if pos.side == 1 else pos.spread_px)
        _on_close(pos, t_end, ex, "EOD_MARK")
    positions.clear()

    return _summarize_replay(name, start_equity, equity, max_dd_pct, dd_vals, daily_r_map, trades, curve, reject_counts, by_pair_rej, float(gov["risk_pct"]), mc_runs)


def _summarize_replay(
    name: str,
    start_equity: float,
    end_equity: float,
    max_dd_pct: float,
    dd_vals: list[float],
    daily_r_map: dict[int, float],
    trades: list[dict[str, Any]],
    curve: list[tuple[int, float]],
    reject_counts: dict[str, int],
    by_pair_rej: dict[str, dict[str, int]],
    risk_pct: float,
    mc_runs: int,
) -> dict[str, Any]:
    if not trades:
        return {
            "name": name,
            "metrics": {"start_equity": start_equity, "end_equity": round(end_equity, 2), "trades": 0, "cagr_pct": 0.0, "max_dd_pct": 0.0, "calmar": 0.0, "pf": 0.0, "wr": 0.0, "sharpe": 0.0, "ulcer": 0.0},
            "trades": [],
            "curve": curve,
            "reject_counts": reject_counts,
            "by_pair_rejects": by_pair_rej,
            "by_asset": [],
            "by_pair": [],
        }

    pnl_arr = np.array([t["pnl_usd"] for t in trades], dtype=float)
    r_arr = np.array([t["r_account"] for t in trades], dtype=float)
    gains = float(pnl_arr[pnl_arr > 0].sum())
    losses = float(abs(pnl_arr[pnl_arr <= 0].sum()))
    pf = (gains / losses) if losses > 0 else 9.99
    wr = float((pnl_arr > 0).mean() * 100.0)
    span_s = max(86400.0, float(curve[-1][0] - curve[0][0]))
    yrs = max(0.25, span_s / (365.25 * 86400.0))
    cagr = (((end_equity / start_equity) ** (1.0 / yrs) - 1.0) * 100.0) if end_equity > 0 else -100.0
    calmar = (cagr / max_dd_pct) if max_dd_pct > 0 else 0.0
    ulcer = float(math.sqrt(np.mean(np.square(dd_vals)))) if dd_vals else 0.0
    hac = compute_hac_stats(r_arr)

    d_min, d_max = min(daily_r_map), max(daily_r_map)
    d_vals = np.array([daily_r_map.get(d, 0.0) for d in range(d_min, d_max + 1)], dtype=float)
    sharpe = float(d_vals.mean() / (d_vals.std() + 1e-9) * math.sqrt(365.0)) if len(d_vals) > 10 else 0.0
    down_d = d_vals[d_vals < 0]
    sortino = float(d_vals.mean() / (down_d.std() + 1e-9) * math.sqrt(365.0)) if len(down_d) > 3 else sharpe
    mc_20k = prop_mc_20k(d_vals, risk_pct, n_mc=mc_runs) if mc_runs > 0 else {}

    by_asset_d: dict[str, dict[str, Any]] = {}
    by_pair_d: dict[str, dict[str, Any]] = {}
    for t in trades:
        ba = by_asset_d.setdefault(t["mt5_symbol"], {"symbol": t["mt5_symbol"], "trades": 0, "wins": 0, "pnl_usd": 0.0, "sum_r_acct": 0.0})
        ba["trades"] += 1
        ba["wins"] += 1 if t["pnl_usd"] > 0 else 0
        ba["pnl_usd"] += t["pnl_usd"]
        ba["sum_r_acct"] += t["r_account"]

        bp = by_pair_d.setdefault(t["pair_id"], {"pair_id": t["pair_id"], "magic": t["magic"], "symbol": t["mt5_symbol"], "trades": 0, "wins": 0, "pnl_usd": 0.0, "sum_r_real": 0.0})
        bp["trades"] += 1
        bp["wins"] += 1 if t["pnl_usd"] > 0 else 0
        bp["pnl_usd"] += t["pnl_usd"]
        bp["sum_r_real"] += t["r_realized"]

    for d in by_asset_d.values():
        d["pnl_usd"] = round(d["pnl_usd"], 2)
        d["sum_r_acct"] = round(d["sum_r_acct"], 2)
        d["wr"] = round(100.0 * d["wins"] / d["trades"], 1)
    for d in by_pair_d.values():
        d["pnl_usd"] = round(d["pnl_usd"], 2)
        d["sum_r_real"] = round(d["sum_r_real"], 2)
        d["wr"] = round(100.0 * d["wins"] / d["trades"], 1)

    return {
        "name": name,
        "metrics": {
            "start_equity": round(start_equity, 2),
            "end_equity": round(end_equity, 2),
            "net_pnl_usd": round(end_equity - start_equity, 2),
            "ret_pct": round((end_equity / start_equity - 1.0) * 100.0, 2),
            "cagr_pct": round(cagr, 2),
            "trades": len(trades),
            "wr": round(wr, 1),
            "pf": round(pf, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "calmar": round(calmar, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "ulcer": round(ulcer, 2),
            "t_hac": hac["t_hac"],
            "net_r_acct": round(float(r_arr.sum()), 2),
            "mc_20k": mc_20k,
        },
        "trades": trades,
        "curve": curve,
        "reject_counts": reject_counts,
        "by_pair_rejects": by_pair_rej,
        "by_asset": sorted(by_asset_d.values(), key=lambda x: x["pnl_usd"], reverse=True),
        "by_pair": sorted(by_pair_d.values(), key=lambda x: x["pnl_usd"], reverse=True),
    }


def load_live_market_inputs(
    cfg: dict[str, Any],
    *,
    extend_xau_pre2025: bool = False,
) -> tuple[dict[str, SignalSeries], dict[str, PriceTape], dict[str, SymbolSpec], dict[str, float]]:
    """Build exact live signals, tapes, MT5 specs, and spreads from cache + MT5."""
    import MetaTrader5 as mt5

    chart_map = m4.discover_chart_strategies()
    signals: dict[str, SignalSeries] = {}
    tapes: dict[str, PriceTape] = {}
    specs: dict[str, SymbolSpec] = {}
    spreads: dict[str, float] = {}

    # 1. Crypto cached bars + 1m intrabar tapes
    crypto_cache: dict[str, tuple[pd.DataFrame, pd.DataFrame, dict[tuple[str, str], dict[str, Any]]]] = {}
    for csym, ms in [("BTCUSDT", "BTCUSD.pi"), ("ETHUSDT", "ETHUSD.pi")]:
        g2 = pd.read_parquet(CACHE_DIR / f"{csym}_2h.parquet")
        g4 = pd.read_parquet(CACHE_DIR / f"{csym}_4h.parquet")
        m1 = pd.read_parquet(CACHE_DIR / f"{csym}_m1.parquet")
        tapes[ms] = PriceTape(
            ts=m1["bar_1m"].to_numpy(dtype=np.int64),
            high=m1["h"].to_numpy(dtype=float),
            low=m1["l"].to_numpy(dtype=float),
            close=m1["c"].to_numpy(dtype=float),
        )
        crypto_cache[csym] = (g2, g4, m4.eval_crypto_signals(g2, g4, chart_map))

    # 2. Live MT5 TradFi rates, specs, and spreads
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        for ms in ["SP500.p", "USDJPY.pi", "GAUUSD.pi", "BTCUSD.pi", "ETHUSD.pi"]:
            mt5.symbol_select(ms, True)
            info = mt5.symbol_info(ms)
            sp = spec_from_mt5(ms)
            if sp is None or info is None:
                raise RuntimeError(f"Failed MT5 spec lookup for {ms}")
            specs[ms] = sp
            spreads[ms] = float(info.spread) * float(info.point)

        tf_sigs: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for ms in ["SP500.p", "USDJPY.pi", "GAUUSD.pi"]:
            r1 = m4.M4LiveEngine._rates_to_df(None, mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H1, 0, 26000), 3600)
            tapes[ms] = PriceTape(
                ts=r1["ts"].to_numpy(dtype=np.int64),
                high=r1["high"].to_numpy(dtype=float),
                low=r1["low"].to_numpy(dtype=float),
                close=r1["close"].to_numpy(dtype=float),
            )
            tf_sigs[(ms, "1H")] = m4.eval_tradfi_signals(r1, chart_map)

            if ms == "GAUUSD.pi" and extend_xau_pre2025:
                gau_r4 = m4.M4LiveEngine._rates_to_df(None, mt5.copy_rates_from_pos("GAUUSD.pi", mt5.TIMEFRAME_H4, 0, 20000), 14400)
                gau_start = int(gau_r4["ts"].iloc[0])
                xau_r4 = m4.M4LiveEngine._rates_to_df(None, mt5.copy_rates_from_pos("XAUUSD.pi", mt5.TIMEFRAME_H4, 0, 20000), 14400)
                pre_xau = xau_r4[xau_r4["ts"] < gau_start].copy()
                for k in ("open", "high", "low", "close"):
                    pre_xau[k] = pre_xau[k] * 0.01
                r4 = pd.concat([pre_xau, gau_r4], ignore_index=True)

                xau_r1 = m4.M4LiveEngine._rates_to_df(None, mt5.copy_rates_from_pos("XAUUSD.pi", mt5.TIMEFRAME_H1, 0, 26000), 3600)
                pre_h1 = xau_r1[xau_r1["ts"] < int(r1["ts"].iloc[0])].copy()
                for k in ("open", "high", "low", "close"):
                    pre_h1[k] = pre_h1[k] * 0.01
                comb_h1 = pd.concat([pre_h1, r1], ignore_index=True)
                tapes[ms] = PriceTape(
                    ts=comb_h1["ts"].to_numpy(dtype=np.int64),
                    high=comb_h1["high"].to_numpy(dtype=float),
                    low=comb_h1["low"].to_numpy(dtype=float),
                    close=comb_h1["close"].to_numpy(dtype=float),
                )
            else:
                r4 = m4.M4LiveEngine._rates_to_df(None, mt5.copy_rates_from_pos(ms, mt5.TIMEFRAME_H4, 0, 20000), 14400)
            tf_sigs[(ms, "4H")] = m4.eval_tradfi_signals(r4, chart_map)
    finally:
        mt5.shutdown()

    for pair in cfg["pairs"]:
        if not pair.get("enabled", True):
            continue
        pid = pair["pair_id"]
        if pair["feed_type"] == "binance_footprint":
            g2, g4, csigs = crypto_cache[pair["source_symbol"]]
            raw = csigs[_crypto_signal_key(pair)]
            vof_arr = compute_crypto_vof_series(g2, g4, pair, raw["side"])
            signals[pid] = SignalSeries(
                pair=pair,
                ts=raw["ts"],
                end_ts=raw["end_ts"],
                side=raw["side"],
                atr=raw["atr"],
                close=raw["c"],
                vof_mult=vof_arr,
            )
        else:
            raw = tf_sigs[(pair["mt5_symbol"], pair["tf"])][pair["sid"]]
            signals[pid] = SignalSeries(
                pair=pair,
                ts=raw["ts"],
                end_ts=raw["end_ts"],
                side=raw["side"],
                atr=raw["atr"],
                close=raw["c"],
                vof_mult=np.ones(len(raw["c"]), dtype=float),
            )

    return signals, tapes, specs, spreads


def run_all_scenarios(out_path: Path = DEFAULT_OUT) -> dict[str, Any]:
    desk_cfg = json.loads((ROOT / "engine" / "config_m4_v3b_desk.json").read_text(encoding="utf-8"))
    base_cfg = json.loads((ROOT / "engine" / "config_m4_v3b.json").read_text(encoding="utf-8"))

    # Native 100% live feeds window (GAUUSD.pi begins 2025-01-23 UTC)
    sig_nat, tap_nat, specs, spreads = load_live_market_inputs(desk_cfg, extend_xau_pre2025=False)
    # Extended 2024-2026 & 2021-2026 using 0.01*XAUUSD.pi strictly prior to GAUUSD.pi launch (2025-01-23)
    sig_ext, tap_ext, _, _ = load_live_market_inputs(desk_cfg, extend_xau_pre2025=True)

    ts_2025 = int(pd.Timestamp("2025-01-23", tz="UTC").timestamp())
    ts_2024_08 = int(pd.Timestamp("2024-08-01", tz="UTC").timestamp())
    ts_2024 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
    ts_2021 = int(pd.Timestamp("2021-01-01", tz="UTC").timestamp())

    no_sp_cfg = json.loads(json.dumps(desk_cfg))
    for p in no_sp_cfg["pairs"]:
        if p["mt5_symbol"] == "SP500.p":
            p["enabled"] = False

    runs = [
        ("desk_2501_native_2025_2026", "Live Running M4 Desk ($2,501.80 | Native Live Feeds 2025-01..2026-11)", desk_cfg, sig_nat, tap_nat, 2501.80, ts_2025, None, 20000),
        ("base_2501_native_2025_2026", "Research V3B Baseline ($2,501.80 | Native Live Feeds 2025-01..2026-11)", base_cfg, sig_nat, tap_nat, 2501.80, ts_2025, None, 20000),
        ("desk_2501_no_sp500_2025_2026", "Diagnostic: Live Desk Excl. SP500 1H ($2,501.80 | 2025-01..2026-11)", no_sp_cfg, sig_nat, tap_nat, 2501.80, ts_2025, None, 20000),
        ("desk_2501_clean_2024_08_2026", "Live Running M4 Desk ($2,501.80 | Clean MT5 H1 2024-08..2026-11)", desk_cfg, sig_ext, tap_ext, 2501.80, ts_2024_08, None, 20000),
        ("base_2501_clean_2024_08_2026", "Research V3B Baseline ($2,501.80 | Clean MT5 H1 2024-08..2026-11)", base_cfg, sig_ext, tap_ext, 2501.80, ts_2024_08, None, 20000),
        ("desk_100k_1pct_clean_2024_08_2026", "Desk Rules Scaled ($100K | 1.0% Single Cap | 2 Slots / 1 Crypto | 2024-08..2026-11)", desk_cfg, sig_ext, tap_ext, 100000.0, ts_2024_08, 1000.0, 20000),
        ("base_100k_clean_2024_08_2026", "Research V3B Scaled ($100K | 6 Slots | 2024-08..2026-11)", base_cfg, sig_ext, tap_ext, 100000.0, ts_2024_08, 1250.0, 20000),
        ("desk_2501_oos_2024_2026", "Live Running M4 Desk ($2,501.80 | Raw Frozen OOS 2024-01..2026-11)", desk_cfg, sig_ext, tap_ext, 2501.80, ts_2024, None, 20000),
        ("base_2501_oos_2024_2026", "Research V3B Baseline ($2,501.80 | Raw Frozen OOS 2024-01..2026-11)", base_cfg, sig_ext, tap_ext, 2501.80, ts_2024, None, 20000),
        ("desk_2501_full_2021_2026", "Live Running M4 Desk ($2,501.80 | Raw 2021-01..2026-11)", desk_cfg, sig_ext, tap_ext, 2501.80, ts_2021, None, 20000),
        ("base_2501_full_2021_2026", "Research V3B Baseline ($2,501.80 | Raw 2021-01..2026-11)", base_cfg, sig_ext, tap_ext, 2501.80, ts_2021, None, 20000),
    ]

    scenarios: dict[str, Any] = {}
    for key, label, cfg, sigs, taps, eq0, t0, ov_cap, mc in runs:
        res = simulate_m4_live_parity(
            label, cfg, sigs, taps, specs, spreads,
            start_equity=eq0, min_ts=t0, override_max_lot_cap=ov_cap, mc_runs=mc,
        )
        scenarios[key] = res
        m = res["metrics"]
        mc_d = m.get("mc_20k", {})
        print(
            f"{key:30s} | Tr={m['trades']:4d} End=${m['end_equity']:>9,.2f} ({m['ret_pct']:+6.1f}%) "
            f"CAGR={m['cagr_pct']:+5.1f}% DD={m['max_dd_pct']:5.2f}% PF={m['pf']:4.2f} Cal={m['calmar']:5.2f} "
            f"Sh={m['sharpe']:4.2f} Pass10={mc_d.get('pass10_pct', 0):5.1f}% | Rej={res['reject_counts']}",
            flush=True,
        )

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "specs": {k: vars(v) for k, v in specs.items()},
        "spreads": spreads,
        "scenarios": scenarios,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved live-parity results -> {out_path}", flush=True)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Run 1:1 live-parity replay for engine/m4_live_engine.py")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    run_all_scenarios(args.out)


if __name__ == "__main__":
    main()
