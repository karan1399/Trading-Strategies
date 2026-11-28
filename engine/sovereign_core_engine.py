"""D_Sovereign_Core Dedicated Live MT5 Strategy Engine.

Runs the configured Sovereign portfolio (`engine/config_sovereign_2500.json`)
with strict session schedule separation:
  - CRYPTO pairs (`category == "CRYPTO"`): trade 24/7 uninterrupted (incl. weekends).
  - Non-CRYPTO pairs (`FOREX`, `METALS`/`GOLD_GRAM`, `INDEX`, `ENERGY`):
      * Halt new entries starting Friday `friday_entry_cutoff_utc` (default 19:00 UTC)
      * Automatically force-close open positions on Friday >= `friday_close_utc` (default 20:30 UTC)
      * Stay in weekend blackout until Sunday `sunday_reopen_utc` (default 22:05 UTC).

Reuses verified causal signal builders, WS footprint collector, sizing, Corr-ERC,
S08 vol-targeting, DD hysteresis, and bar-trail SL logic from `engine.m4_live_engine`.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import sys
import time
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import m4_live_engine as core
from engine.sovereign_challengers import challenger_signals
from engine.unified_alpha_pilot import spec_from_fallback, spec_from_mt5

DEFAULT_CONFIG_PATH = ROOT / "engine" / "config_sovereign_2500.json"
DEFAULT_STATE_PATH = ROOT / "engine" / "data" / "sovereign_core_state.json"
DEFAULT_JOURNAL_PATH = ROOT / "engine" / "data" / "sovereign_core_journal.csv"
INTERLOCK_DIR = ROOT / "desk" / "events"
MT5_DESK_RISK_CAP_USD = 6.0

DEFAULT_SESSION_POLICY: dict[str, Any] = {
    "crypto_24_7": True,
    "crypto_categories": ["CRYPTO"],
    "friday_close_non_crypto": True,
    "friday_entry_cutoff_utc": "19:00",
    "friday_close_utc": "20:30",
    "sunday_reopen_utc": "22:05",
}


def _hhmm_to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.strip().split(":", 1)
    return int(hh) * 60 + int(mm)


def is_crypto_pair(pair: dict[str, Any], cfg: dict[str, Any] | None = None) -> bool:
    """Return True iff the pair belongs to a 24/7 crypto asset category."""
    pol = (cfg or {}).get("session_policy") or DEFAULT_SESSION_POLICY
    cats = {str(c).upper() for c in (pol.get("crypto_categories") or ["CRYPTO"])}
    return str(pair.get("category", "")).upper() in cats


def non_crypto_schedule_state(now_ts: int, cfg: dict[str, Any] | None = None) -> str:
    """Classify current UTC schedule state for non-crypto assets.

    Returns one of: 'ACTIVE', 'FRIDAY_CUTOFF', 'FRIDAY_CLOSE', 'WEEKEND_BLACKOUT'.
    """
    pol = {**DEFAULT_SESSION_POLICY, **((cfg or {}).get("session_policy") or {})}
    if not pol.get("friday_close_non_crypto", True):
        return "ACTIVE"

    dt = datetime.fromtimestamp(int(now_ts), tz=timezone.utc)
    wd = dt.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    mins = dt.hour * 60 + dt.minute

    if wd == 4:  # Friday
        close_m = _hhmm_to_minutes(str(pol["friday_close_utc"]))
        cut_m = min(close_m, _hhmm_to_minutes(str(pol["friday_entry_cutoff_utc"])))
        if mins >= close_m:
            return "FRIDAY_CLOSE"
        if mins >= cut_m:
            return "FRIDAY_CUTOFF"
        return "ACTIVE"

    if wd == 5:  # Saturday
        return "WEEKEND_BLACKOUT"

    if wd == 6:  # Sunday prior to reopen
        reopen_m = _hhmm_to_minutes(str(pol["sunday_reopen_utc"]))
        if mins < reopen_m:
            return "WEEKEND_BLACKOUT"

    return "ACTIVE"


class SovereignProtectionBridge(core.MT5Bridge):
    def __init__(self, cfg, allow_mutation):
        super().__init__(cfg)
        self.allow_mutation = allow_mutation

    def close_position(self, position, deviation):
        if self.allow_mutation("close"):
            return super().close_position(position, deviation)
        return None

    def modify_sl(self, ticket, sl, tp):
        if self.allow_mutation("modify_sl"):
            return super().modify_sl(ticket, sl, tp)
        return None


class SovereignCoreEngine(core.M4LiveEngine):
    """D_Sovereign_Core live execution engine: 24/7 Crypto + Friday-close Non-Crypto."""

    def __init__(self, cfg_path, state_path, journal_path, dry=False, live=False):
        super().__init__(cfg_path, state_path, journal_path, dry=dry, live=live)
        self.bridge = SovereignProtectionBridge(self.cfg, self._allow_protective_mutation)

    def _allow_protective_mutation(self, action: str) -> bool:
        if self.dry or not self.live or self.state.state.get("interlock_rearm_required"):
            return False
        if not getattr(core.mt5.terminal_info(), "trade_allowed", False):
            self._record_interlock_failure(int(time.time()))
            self.state.state["interlock_rearm_required"] = True
            self.state.save()
            return False
        self.journal.log("INTERLOCK_OK", note=f"terminal_info().trade_allowed=True before {action}")
        return True

    def _gather_signals(self):
        payloads, atr = super()._gather_signals()
        for pair in self.pairs:
            if "signal_params" not in pair:
                continue
            source = self._crypto_tfs.get(pair["source_symbol"])
            if source is None:
                continue
            frame = source[0 if pair["tf"] == "2H" else 1]
            if frame.empty:
                continue
            result = challenger_signals(frame, pair)
            payloads[pair["pair_id"]] = {
                "ts": int(result["ts"][-1]), "end_ts": int(result["end_ts"][-1]),
                "side": int(result["side"][-1]), "atr": float(result["atr"][-1]),
                "c": float(result["c"][-1]), "vof_mult": 1.0,
            }
        return payloads, atr

    def _flatten_non_crypto_for_friday(self, positions: list[Any]) -> None:
        """Close all open non-crypto D_Sovereign_Core positions when Friday close arrives."""
        gov = self.cfg["governor"]
        dev = int(gov.get("deviation", 30))
        for p in positions:
            magic = int(getattr(p, "magic", 0))
            pair = self.pair_by_magic.get(magic)
            if pair is None or is_crypto_pair(pair, self.cfg):
                continue

            pid = pair["pair_id"]
            side = 1 if int(getattr(p, "type", 0)) == 0 else -1
            lots = float(getattr(p, "volume", 0.0))
            cur_px = float(getattr(p, "price_current", 0.0) or getattr(p, "price_open", 0.0))

            if self.dry or not self.live:
                self.journal.log(
                    "DRY_FRIDAY_CLOSE", sleeve=pid, magic=magic, symbol=p.symbol,
                    side="BUY" if side == 1 else "SELL", lots=lots, price=cur_px,
                    note="Friday pre-weekend flatten (dry)",
                )
                continue

            res = self.bridge.close_position(p, dev)
            rc = getattr(res, "retcode", -1)
            self.journal.log(
                "FRIDAY_CLOSE", sleeve=pid, magic=magic, symbol=p.symbol,
                side="BUY" if side == 1 else "SELL", lots=lots, price=cur_px,
                retcode=str(rc), note="Friday pre-weekend flatten",
            )

    def _record_interlock_failure(self, now_s: int) -> None:
        utc = datetime.fromtimestamp(now_s, tz=timezone.utc)
        record = dict(ts_utc=utc.isoformat(), host=socket.gethostname(), venue="mt5",
                      probe="terminal_info().trade_allowed", value=False, retcode_observed=None,
                      batch_ids=["sovereign_friday_flatten", "sovereign_protective_management"])
        INTERLOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = INTERLOCK_DIR / f"interlock_{utc:%Y%m%d_%H%M%S}.json"
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2)
        except FileExistsError:
            pass
        self.journal.log("INTERLOCK_BLOCK", note=str(path.name))

    def _positions_or_unknown(self) -> list[Any] | None:
        if getattr(self, "_offline_replay", False):
            return self.bridge.positions()
        positions = core.mt5.positions_get()
        return list(positions) if positions is not None else None

    def _committed_live_risk(self, positions, orders) -> float | None:
        if orders is None:
            return None
        total = 0.
        # MT5 uses the corresponding BUY/SELL codes for position type 0/1.
        buys = {0, 2, 4, 6}
        sells = {1, 3, 5, 7}
        for is_pending, rows in ((False, positions), (True, orders)):
            for item in rows:
                kind = int(getattr(item, "type", -1))
                if kind not in buys | sells or (not is_pending and kind not in (0, 1)):
                    return None
                side = 1 if kind in buys else -1
                entry = float(getattr(item, "price_open", 0.) or 0.)
                mark = entry if is_pending else float(getattr(item, "price_current", 0.) or 0.)
                stop = float(getattr(item, "sl", 0.) or 0.)
                volume = float(getattr(item, "volume_current" if is_pending else "volume", 0.) or 0.)
                if not all(math.isfinite(v) and v > 0. for v in (entry, mark, stop, volume)):
                    return None
                stress_bps = float(self.cfg["governor"].get("risk_cost_reserve_bps", 10.0))
                adverse = stop - side * (.20 * abs(entry - stop) + mark * stress_bps / 10000.)
                pnl = core.mt5.order_calc_profit(0 if side == 1 else 1, item.symbol, volume, mark, adverse)
                if pnl is None or not math.isfinite(pnl):
                    return None
                total += max(0., -pnl)
        return total

    def step_once(self, now_ts: int | None = None) -> dict[str, Any]:
        if not self.bridge.connect():
            raise RuntimeError("MT5 connection failed")
        ti = core.mt5.terminal_info()
        ai = core.mt5.account_info()
        if ai is None:
            raise RuntimeError("MT5 account_info() returned None")
        exp_login = int(self.cfg.get("account", {}).get("expected_login") or 0)
        if exp_login and int(ai.login) != exp_login:
            raise RuntimeError(f"MT5 login mismatch: connected #{ai.login} != expected #{exp_login}")

        now_s = int(time.time()) if now_ts is None else int(now_ts)
        non_crypto_sched = non_crypto_schedule_state(now_s, self.cfg)
        paper = getattr(self, "_offline_replay", False)
        trade_allowed = bool(getattr(ti, "trade_allowed", False))
        if non_crypto_sched in {"FRIDAY_CLOSE", "WEEKEND_BLACKOUT"}:
            self.state.state["weekend_flatten_due"] = True
        if not trade_allowed:
            if self.live and not self.dry and not paper:
                self._record_interlock_failure(now_s)
                self.state.state["interlock_rearm_required"] = True
            self.state.save()
            return dict(engine="D_Sovereign_Core", equity=float(ai.equity), dd_mult=1., trade_allowed=False,
                        crypto_schedule="HALTED", non_crypto_schedule=non_crypto_sched, open_pos=0,
                        committed_risk=None, signals_evaluated=0, active_triggers=[])

        equity = float(ai.equity)
        dd_mult = core.update_dd_state(
            self.state.state, equity,
            self.cfg["governor"]["dd_throttle"],
            float(self.cfg["governor"].get("dd_resume_ratio", 0.50)),
        )
        positions = self._positions_or_unknown()
        if positions is None:
            self.journal.log("STATE_UNAVAILABLE", note="positions_get failed")
            self.state.save()
            return dict(engine="D_Sovereign_Core", equity=equity, dd_mult=dd_mult, trade_allowed=trade_allowed,
                        crypto_schedule="HALTED", non_crypto_schedule=non_crypto_sched, open_pos=0,
                        committed_risk=None, signals_evaluated=0, active_triggers=[])
        may_manage = not self.state.state.get("interlock_rearm_required", False)
        if may_manage and self.state.state.get("weekend_flatten_due") and non_crypto_sched != "WEEKEND_BLACKOUT":
            before_flatten = {int(p.ticket): int(p.magic) for p in positions if int(p.magic) in self.pair_by_magic}
            self._flatten_non_crypto_for_friday(positions)
            positions = self._positions_or_unknown()
            if positions is None:
                self.state.save()
                raise RuntimeError("Position readback unavailable after weekend flatten")
            for ticket in before_flatten.keys() - {int(p.ticket) for p in positions}:
                self.state.state["cooldown"][str(before_flatten[ticket])] = now_s
            if not any((pair := self.pair_by_magic.get(int(p.magic))) and not is_crypto_pair(pair, self.cfg)
                       for p in positions):
                self.state.state["weekend_flatten_due"] = False

        try:
            payloads, atr_lookup = self._gather_signals()
        except Exception as exc:
            self.journal.log("SIGNAL_UNAVAILABLE", note=str(exc))
            payloads, atr_lookup = {}, {}

        if may_manage:
            self._manage_open_positions(positions, atr_lookup)

        positions = self._positions_or_unknown()
        if positions is None:
            self.state.save()
            raise RuntimeError("Position readback unavailable after protective management")
        # Use the exact snapshot being valued. A failed refresh must never count as a flat book.
        committed = (self.bridge.total_sl_risk_usd(positions) if paper
                     else self._committed_live_risk(positions, core.mt5.orders_get()))
        gov = self.cfg["governor"]
        max_entry_lag_sec = 900
        crypto_24_7 = bool((self.cfg.get("session_policy") or DEFAULT_SESSION_POLICY).get("crypto_24_7", True))

        summary: dict[str, Any] = {
            "engine": "D_Sovereign_Core",
            "equity": equity,
            "dd_mult": dd_mult,
            "trade_allowed": bool(getattr(ti, "trade_allowed", False)),
            "crypto_schedule": "24/7_ACTIVE" if crypto_24_7 else non_crypto_sched,
            "non_crypto_schedule": non_crypto_sched,
            "open_pos": len(positions),
            "committed_risk": round(committed, 2) if committed is not None else None,
            "signals_evaluated": len(payloads),
            "active_triggers": [],
        }
        if committed is None or self.state.state.get("interlock_rearm_required"):
            self.journal.log("STATE_UNAVAILABLE", note="pending orders unknown or operator re-arm required")
            self.state.save()
            return summary

        for pair in sorted(self.pairs, key=lambda x: (-float(x["pair_weight"]), int(x["magic"]))):
            pid = pair["pair_id"]
            pl = payloads.get(pid)
            if pl is None:
                continue
            prev_ts = int(self.state.state["bars"].get(pid, 0) or 0)
            bar_ts = int(pl["ts"])
            if bar_ts <= prev_ts:
                continue
            side = int(pl["side"])
            if side == 0:
                self.state.state["bars"][pid] = bar_ts
                continue

            is_crypto = is_crypto_pair(pair, self.cfg)
            if (not is_crypto or not crypto_24_7) and non_crypto_sched != "ACTIVE":
                self.state.state["bars"][pid] = bar_ts
                self.journal.log(
                    non_crypto_sched, sleeve=pid, magic=pair["magic"],
                    symbol=pair["mt5_symbol"], side="BUY" if side == 1 else "SELL",
                    note=f"non-crypto entry skipped ({non_crypto_sched})",
                )
                continue

            lag = now_s - int(pl["end_ts"])
            summary["active_triggers"].append({
                "pair_id": pid, "side": side, "bar_ts": bar_ts, "lag_s": lag,
                "atr": round(pl["atr"], 5), "vof": pl["vof_mult"],
            })

            if lag < 0:
                continue
            if lag > max_entry_lag_sec:
                self.state.state["bars"][pid] = bar_ts
                self.journal.log("STALE_SKIP", sleeve=pid, magic=pair["magic"], symbol=pair["mt5_symbol"],
                                 side="BUY" if side == 1 else "SELL", note=f"lag={lag}s > {max_entry_lag_sec}s")
                continue

            ms = pair["mt5_symbol"]
            self.bridge.ensure_visible(ms)
            spec = spec_from_mt5(ms) or (spec_from_fallback(self.cfg, ms) if paper else None)
            tick = core.mt5.symbol_info_tick(ms)
            if tick is None or not 0 <= now_s - int(tick.time) <= int(gov.get("max_quote_age_sec", 60)):
                continue
            ask, bid = self.bridge.tick(ms) if paper else (float(tick.ask), float(tick.bid))
            if (spec is None or ask is None or bid is None or not all(math.isfinite(v) for v in (ask, bid))
                    or not 0 < bid <= ask):
                continue
            if not paper:
                info = core.mt5.symbol_info(ms)
                loss_tick = float(getattr(info, "trade_tick_value_loss", 0.) or 0.)
                if not math.isfinite(loss_tick) or loss_tick <= 0.:
                    continue
                spec = replace(spec, tick_value=loss_tick)
            self.state.state["bars"][pid] = bar_ts

            spread_px = ask - bid
            atr = float(pl["atr"])
            if not np.isfinite(atr) or atr <= 0:
                continue
            if spread_px > float(gov.get("spread_atr_max", 0.45)) * atr:
                self.journal.log("SPREAD_VETO", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 note=f"sp={spread_px:.5f} > {gov['spread_atr_max']}*ATR")
                continue

            sl_ovr = pl.get("sl_dist_override")
            sl_dist = float(sl_ovr) if (sl_ovr is not None and float(sl_ovr) > 0.0) else float(pair["sl_atr"]) * atr
            max_sp_sl = float(gov.get("max_spread_sl_ratio", 0.0) or 0.0)
            if max_sp_sl > 0.0 and spread_px > max_sp_sl * sl_dist:
                self.journal.log("SPREAD_VETO", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 note=f"sp={spread_px:.5f} > {max_sp_sl:.2f}*SL({sl_dist:.5f})")
                continue

            ml_mult = self._eval_ml_gate(pair, ms, side, ask, bid, spread_px)
            if ml_mult is None:
                continue

            corr_mult = (
                core.compute_corr_erc_mult(pair, positions, self.pair_by_magic, gov.get("corr_matrix"))
                if gov.get("use_corr_erc", False) else 1.0
            )
            risk_usd = core.compute_pair_risk_usd(
                equity, self.cfg, pair, dd_mult, float(pl["vof_mult"]), extra_mult=ml_mult * corr_mult
            )
            entry = ask if side == 1 else bid
            if not math.isfinite(sl_dist) or sl_dist <= 0 or spec.tick_size <= 0:
                continue
            round_away = math.floor if side == 1 else math.ceil
            sl_px = round(round_away((entry - side * sl_dist) / spec.tick_size) * spec.tick_size, spec.digits)
            sl_dist = abs(entry - sl_px)
            equity_cap = equity * float(gov["max_risk_single_trade_pct"]) / 100.0
            desk_cap = min(equity_cap, MT5_DESK_RISK_CAP_USD,
                           float(pair.get("max_lot_risk_usd", MT5_DESK_RISK_CAP_USD)))
            stress_dist = 1.20 * sl_dist + entry * float(gov.get("risk_cost_reserve_bps", 10.0)) / 10000.0
            lots, l_err = core.lots_for_risk(spec, stress_dist, risk_usd, desk_cap)
            if lots is None:
                self.journal.log("RISK_REJECT", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 risk_usd=risk_usd, note=l_err)
                continue

            actual_risk = stress_dist * lots * spec.value_per_price_unit()
            ok_gate, g_reason = core.m4_portfolio_gate(
                pair, side, positions, self.cfg, self.pair_by_magic,
                self.state.state.get("cooldown", {}), now_s, equity, committed, actual_risk,
            )
            if not ok_gate:
                self.journal.log("GATE_BLOCK", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 side="BUY" if side == 1 else "SELL", risk_usd=actual_risk, note=g_reason)
                continue

            raw_tp = float(pair["tp_r"]) if float(pair["tp_r"]) > 0.0 else float(gov.get("safety_tp_r_when_trail_only", 8.0))
            round_target = math.ceil if side == 1 else math.floor
            tp_px = round(round_target((entry + side * max(2.0, raw_tp) * sl_dist) / spec.tick_size) * spec.tick_size,
                          spec.digits)

            if self.dry or not self.live:
                self.journal.log("DRY_ORDER", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 side="BUY" if side == 1 else "SELL", lots=lots, price=entry,
                                 sl=sl_px, tp=tp_px, risk_usd=round(actual_risk, 2),
                                 note=f"w={pair['pair_weight']} dd={dd_mult} vof={pl['vof_mult']}")
                continue

            if not paper:
                self.journal.log("DESK_PROPOSAL", sleeve=pid, magic=pair["magic"], symbol=ms,
                                 side="BUY" if side == 1 else "SELL", lots=lots, price=entry, sl=sl_px, tp=tp_px,
                                 risk_usd=round(actual_risk, 2), note="operator gateway and stop-shelf evidence required")
                continue

            res = self.bridge.send_bracket(
                spec, side, lots, entry, sl_px, tp_px,
                int(pair["magic"]), f"SOV:{pair['sid'][:19]}", int(gov["deviation"]),
            )
            rc = getattr(res, "retcode", -1)
            self.journal.log("ENTRY", sleeve=pid, magic=pair["magic"], symbol=ms,
                             side="BUY" if side == 1 else "SELL", lots=lots, price=entry,
                             sl=sl_px, tp=tp_px, risk_usd=round(actual_risk, 2), retcode=str(rc),
                             note=f"w={pair['pair_weight']} vof={pl['vof_mult']}")
            if rc == 10009:
                positions = self.bridge.positions()
                committed = self.bridge.total_sl_risk_usd(positions)
                tick_id = str(getattr(res, "order", ""))
                fill_px = float(getattr(res, "price", entry) or entry)
                self.state.state["positions"][tick_id] = {
                    "pair_id": pid, "magic": int(pair["magic"]), "mt5_symbol": ms,
                    "side": side, "entry": fill_px, "sd": sl_dist, "atr_entry": atr,
                    "lots": lots, "open_ts": now_s, "last_managed_b": now_s // int(pair["tf_sec"]),
                    "best_px": fill_px,
                }

        self.state.save()
        return summary

    def _eval_ml_gate(
        self, pair: dict[str, Any], ms: str, side: int, ask: float, bid: float, spread_px: float
    ) -> float | None:
        """Return ML risk multiplier or None if vetoed by CatBoost/Ensemble gate."""
        ml_cfg = self.cfg.get("ml_gate") or {}
        if not (ml_cfg.get("enabled", False) and pair.get("feed_type") == "binance_footprint"):
            return 1.0
        dom = set(ml_cfg.get("domain_symbols") or [])
        if dom and ms not in dom:
            return 1.0
        tfs_pair = self._crypto_tfs.get(str(pair["source_symbol"]).upper())
        if tfs_pair is None:
            return 1.0

        from exchange_feed.backtesting.ml_meta_engine import (
            FINAL_BUNDLE_PATH,
            build_live_ml_feature_vector,
            score_live_signal,
        )

        tf_k = 2 if str(pair.get("tf", "4H")).upper() == "2H" else 4
        tf_df = tfs_pair[0] if tf_k == 2 else tfs_pair[1]
        mid_px = max(1e-9, 0.5 * (ask + bid))
        sp_bps_live = (spread_px / mid_px) * 10000.0
        is_met = 1.0 if ("XAU" in ms or "XAG" in ms or "GAU" in ms) else 0.0
        bpath_raw = ml_cfg.get("bundle_path")
        bpath = (ROOT / bpath_raw) if (bpath_raw and not Path(str(bpath_raw)).is_absolute()) else (
            Path(bpath_raw) if bpath_raw else FINAL_BUNDLE_PATH
        )
        vec = build_live_ml_feature_vector(
            tf_df, bar_idx=-1, side=float(side), sleeve=str(pair["sid"]),
            tf_k=tf_k, sp_bps=sp_bps_live, is_metal=is_met,
            btc_ref=self._btc_macro_ref, bundle_path=bpath, cfg_str=str(pair.get("cfg", "")),
        )
        if vec is None:
            return 1.0
        ml_res = score_live_signal(
            vec[0], vec[1], bundle_path=bpath,
            min_expected_r=float(ml_cfg.get("min_expected_r", -0.10)),
            model_name=ml_cfg.get("model_name"),
        )
        if ml_res.get("ok") and not ml_res.get("approve_regime", True):
            self.journal.log(
                "ML_VETO", sleeve=pair["pair_id"], magic=pair["magic"], symbol=ms,
                side="BUY" if side == 1 else "SELL",
                note=f"E[R]={ml_res['expected_r']:+.3f} ({ml_res['tier']})",
            )
            return None
        return float(ml_res.get("risk_mult", 1.0)) if ml_res.get("ok") else 1.0

    def run_loop(self) -> None:
        self.ws_collector.start()
        time.sleep(2.0)
        poll_s = float(self.cfg["governor"].get("poll_seconds", 30))
        hb = 0
        try:
            while True:
                t0 = time.time()
                try:
                    sm = self.step_once()
                    if hb % 10 == 0 or sm["active_triggers"]:
                        max_g = int(self.cfg["governor"]["max_global"])
                        ws_st = "up" if self.ws_collector.connected else "down"
                        print(
                            f"[D-SOVEREIGN-CORE {datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                            f"eq=${sm['equity']:.2f} dd_mult={sm['dd_mult']} pos={sm['open_pos']}/{max_g} "
                            f"risk=${sm['committed_risk']:.2f} crypto={sm['crypto_schedule']} "
                            f"non_crypto={sm['non_crypto_schedule']} ws={ws_st} trig={sm['active_triggers']}",
                            flush=True,
                        )
                    hb += 1
                except Exception as exc:
                    print(f"[D-SOVEREIGN-CORE ERR] {exc}", flush=True)
                    import traceback
                    traceback.print_exc()
                time.sleep(max(1.0, poll_s - (time.time() - t0)))
        except (KeyboardInterrupt, SystemExit):
            print("[D-SOVEREIGN-CORE] Termination signal received, shutting down cleanly", flush=True)
        finally:
            self.bridge.shutdown()
            self.ws_collector.stop()


def print_status(cfg_path: Path, state_path: Path) -> None:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    now_s = int(time.time())
    sched = non_crypto_schedule_state(now_s, cfg)
    crypto_cnt = sum(1 for p in cfg["pairs"] if p.get("enabled", True) and is_crypto_pair(p, cfg))
    non_crypto_cnt = sum(1 for p in cfg["pairs"] if p.get("enabled", True) and not is_crypto_pair(p, cfg))
    st = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    print(f"=== D_Sovereign_Core Status ({datetime.fromtimestamp(now_s, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}) ===")
    print(f"Config        : {cfg_path.name} ({crypto_cnt} crypto 24/7 pairs, {non_crypto_cnt} non-crypto pairs)")
    gov = cfg["governor"]
    print(f"Risk / Heat   : {gov['risk_pct']:.2f}% target | ${MT5_DESK_RISK_CAP_USD:.2f} stressed cap "
          f"per trade | {gov['max_total_open_risk_pct']}% max book heat | max_global={gov['max_global']}")
    print(f"Schedule      : CRYPTO=24/7 ACTIVE | NON_CRYPTO={sched}")
    print(f"Tracked State : peak=${st.get('peak_equity_usd', 0.0):.2f} dd_mult={st.get('dd_mult', 1.0)} open_meta={len(st.get('positions', {}))}")


def run_selftest(cfg_path: Path) -> None:
    core.run_selftest(cfg_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    pol = cfg.get("session_policy") or {}
    assert pol.get("crypto_24_7") is True, "crypto_24_7 must be enabled"
    assert pol.get("friday_close_non_crypto") is True, "friday_close_non_crypto must be enabled"

    def _ts(s: str) -> int:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())

    # 2026-11-27 is Friday, 2026-11-28 is Saturday, 2026-11-29 is Sunday, 2026-11-30 is Monday
    assert non_crypto_schedule_state(_ts("2026-11-27 18:59"), cfg) == "ACTIVE"
    assert non_crypto_schedule_state(_ts("2026-11-27 19:00"), cfg) == "FRIDAY_CUTOFF"
    assert non_crypto_schedule_state(_ts("2026-11-27 20:29"), cfg) == "FRIDAY_CUTOFF"
    assert non_crypto_schedule_state(_ts("2026-11-27 20:30"), cfg) == "FRIDAY_CLOSE"
    assert non_crypto_schedule_state(_ts("2026-11-28 12:00"), cfg) == "WEEKEND_BLACKOUT"
    assert non_crypto_schedule_state(_ts("2026-11-29 22:04"), cfg) == "WEEKEND_BLACKOUT"
    assert non_crypto_schedule_state(_ts("2026-11-29 22:05"), cfg) == "ACTIVE"
    assert non_crypto_schedule_state(_ts("2026-11-30 04:00"), cfg) == "ACTIVE"

    crypto_p = [p for p in cfg["pairs"] if p["mt5_symbol"] == "ETHUSD.pi"][0]
    metal_p = [p for p in cfg["pairs"] if p["mt5_symbol"] == "GAUUSD.pi"][0]
    assert is_crypto_pair(crypto_p, cfg) is True
    assert is_crypto_pair(metal_p, cfg) is False
    print(f"D_SOVEREIGN_CORE SELFTEST PASS: {len(cfg['pairs'])} configured pairs and UTC session boundaries verified.",
          flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="D_Sovereign_Core Live MT5 Strategy Engine")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--state")
    ap.add_argument("--journal")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--i-understand-live-risk", action="store_true")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    profile = json.loads(cfg_path.read_text(encoding="utf-8"))
    research = bool(profile.get("_meta", {}).get("research_only"))
    if research and args.live:
        ap.error("Research profiles cannot manage live positions")
    state_default = DEFAULT_STATE_PATH.with_name(f"{cfg_path.stem}_state.json") if research else DEFAULT_STATE_PATH
    journal_default = DEFAULT_JOURNAL_PATH.with_name(f"{cfg_path.stem}_journal.csv") if research else DEFAULT_JOURNAL_PATH
    state_path = Path(args.state) if args.state else state_default
    journal_path = Path(args.journal) if args.journal else journal_default
    if research and (state_path.resolve() == DEFAULT_STATE_PATH.resolve()
                     or journal_path.resolve() == DEFAULT_JOURNAL_PATH.resolve()):
        ap.error("Research profiles require independent state and journal files")

    if args.selftest:
        run_selftest(cfg_path)
        return 0
    if args.status:
        print_status(cfg_path, state_path)
        return 0
    if args.live and not args.i_understand_live_risk:
        print("Refusing --live without --i-understand-live-risk", file=sys.stderr)
        return 2

    eng = SovereignCoreEngine(
        cfg_path, state_path, journal_path,
        dry=(args.dry or not args.live), live=bool(args.live),
    )
    if args.once or args.dry:
        try:
            sm = eng.step_once()
            print("ONCE SUMMARY:", json.dumps(sm, indent=2), flush=True)
        finally:
            eng.bridge.shutdown()
        return 0

    eng.run_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
