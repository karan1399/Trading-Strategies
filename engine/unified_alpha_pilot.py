#!/usr/bin/env python3
"""unified_alpha_pilot — multi-strategy MT5 execution daemon (single file).

Sleeves (magic / audit verdict in desk/STRATEGY_SUITE_QUANT_AUDIT.md):
  42003 htf_gold  : P4 cluster-stall at EMA233, 4H, gold cluster ONLY (one risk unit)
  42002 ivwall    : M15 session-anchored 2-SD wall fade, 13 majors, exotics excluded
  42004 vc_gold   : XAUUSD 4H TTM squeeze blueprint, fixed 4R TP
  42005 orb_nas   : NAS100 15M OR30 @ 14:30 UTC (monitor size)
  42001           : reserved for the CG order-flow terminal (not this engine)

Hard rules (enforced here, not negotiable):
  * every order carries a SERVER-SIDE sl and tp (zero naked positions)
  * fixed-dollar risk: lots sized from live symbol_info; if the structural stop at
    volume_min would risk > risk_usd_max, the trade is REJECTED — the stop is never
    compressed into noise
  * max 2 concurrent positions portfolio-wide, max 1 per sleeve, max 1 gold-cluster
    position across magics 42003/42004
  * signals evaluated strictly on CLOSED bars; entry at the open of the next bar
    (live: first market price after bar close)
  * breakeven lock at +1.0R excursion; IV-wall and ORB time-stops enforced by the engine
  * state persisted to engine/data/unified_state.json + engine/data/unified_journal.csv

CLI:
  python3 unified_alpha_pilot.py --selftest
  python3 unified_alpha_pilot.py --dry      # full pipeline + journal, NO orders (terminal attached)
  python3 unified_alpha_pilot.py --live --i-understand-live-risk
  python3 unified_alpha_pilot.py --status
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ENGINE_DIR)
CONFIG_PATH = os.path.join(ENGINE_DIR, "config.json")
DATA_DIR = os.path.join(ENGINE_DIR, "data")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
from delta_exec import DeltaLive  # noqa: E402
from engine.cg_sl_tp import optimize_bracket_with_cg  # noqa: E402
from strategies._indicators import adx_arr, rsi_wilder_arr  # noqa: E402
from strategies.registry import load_signal_fn  # noqa: E402
from strategies.s03_htf_ema_rejection.strategy import signal_at as _s03_signal_at  # noqa: E402
from strategies.s04_vc_squeeze_breakout.strategy import signal_at as _s04_signal_at  # noqa: E402
from strategies.s06_iv_wall_fade.strategy import (  # noqa: E402
    _broker_day,
    ewma_sigma,
    session_spans_from,
    signal_at as _s06_signal_at,
)
from strategies.s08_vol_target_overlay import vol_target_risk_usd  # noqa: E402
from strategies.s11_or30_london_breakout.strategy import signal_at as _s11_signal_at  # noqa: E402


def _resolve_config_file(cf: str) -> str | None:
    cf_path = cf if os.path.isabs(cf) else os.path.join(ROOT, cf)
    if os.path.exists(cf_path):
        return cf_path
    base = os.path.basename(os.path.dirname(cf_path))
    flat = os.path.join(ROOT, "strategies", base, os.path.basename(cf_path))
    if os.path.exists(flat):
        return flat
    for sub in ("trend", "breakout", "mean_reversion"):
        cand = os.path.join(ROOT, "strategies", "sleeves", sub, base, os.path.basename(cf_path))
        if os.path.exists(cand):
            return cand
    arch = os.path.join(ROOT, "strategies", "archive", "unprofitable", base, os.path.basename(cf_path))
    if os.path.exists(arch):
        return arch
    return None


def load_resolved_config(path: str = CONFIG_PATH) -> dict:
    """Load engine/config.json and hydrate external strategy folder config_file entries."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for sname, s in (cfg.get("sleeves") or {}).items():
        cf = s.get("config_file")
        if cf:
            cf_path = _resolve_config_file(cf)
            if cf_path:
                with open(cf_path, encoding="utf-8") as sf:
                    ext = json.load(sf)
                for k in ("tf", "max_hold_bars", "params", "symbol_params", "symbols", "cooldown_bars", "max_spread_risk_frac"):
                    if k in ext and k not in s:
                        s[k] = ext[k]
                    elif k == "symbols" and not s.get("symbols") and ext.get("symbols"):
                        s["symbols"] = ext["symbols"]
                    elif k == "params" and ext.get("params"):
                        s["params"] = {**ext["params"], **(s.get("params") or {})}
                    elif k == "symbol_params" and ext.get("symbol_params"):
                        s["symbol_params"] = {**ext["symbol_params"], **(s.get("symbol_params") or {})}
    return cfg

MT5_OK = True
try:
    import MetaTrader5 as mt5
except Exception:
    mt5 = None
    MT5_OK = False

TF_MAP = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240}
if MT5_OK:
    TF_MT5 = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
              "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4}


# ============================================================== indicators ==
def ema_arr(c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    out = np.zeros(n)
    if n < period:
        return out
    out[period - 1] = c[:period].mean()
    a = 2.0 / (period + 1.0)
    for i in range(period, n):
        out[i] = a * c[i] + (1 - a) * out[i - 1]
    return out


def sma_arr(c: np.ndarray, period: int) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan)
    if n < period:
        return out
    cs = np.cumsum(np.insert(c, 0, 0.0))
    out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    atr = np.zeros(n)
    if n <= period:
        return atr
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


# ============================================================ state/store ===
class Journal:
    COLS = ["ts_utc", "event", "sleeve", "magic", "symbol", "side", "lots",
            "price", "sl", "tp", "risk_usd", "r_multiple", "retcode", "note"]

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(self.COLS)

    def log(self, event: str, sleeve: str = "", magic: int = 0, symbol: str = "",
            side: str = "", lots: float = 0.0, price: float = 0.0, sl: float = 0.0,
            tp: float = 0.0, risk_usd: float = 0.0, r_multiple: float = 0.0,
            retcode: str = "", note: str = ""):
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                                    event, sleeve, magic, symbol, side, lots, price,
                                    sl, tp, risk_usd, r_multiple, retcode, note])


class StateStore:
    def __init__(self, path: str, strict_live: bool = False):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.state = {"positions": {}, "bars": {}, "cooldown": {}, "counts": {}}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.state = json.load(f)
            except Exception as exc:
                if strict_live and os.path.getsize(path) > 0:
                    raise RuntimeError(f"Corrupt live state file {path}: {exc}") from exc

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.path)

    def get(self, key, default=None):
        return self.state.get(key, default)

    def set(self, key, value):
        self.state[key] = value


# ============================================================ symbol spec ===
@dataclass
class SymbolSpec:
    name: str
    digits: int = 2
    point: float = 0.01
    tick_value: float = 1.0
    tick_size: float = 0.01
    vol_min: float = 0.01
    vol_step: float = 0.01
    vol_max: float = 100.0

    def value_per_price_unit(self) -> float:
        """Account-CCY value of a 1.0 full-price-unit move, per 1.0 lot."""
        if self.tick_size <= 0:
            return 0.0
        return self.tick_value / self.tick_size


def spec_from_mt5(name: str) -> SymbolSpec | None:
    if not MT5_OK:
        return None
    info = mt5.symbol_info(name)
    if info is None:
        return None
    return SymbolSpec(name=name, digits=info.digits, point=info.point,
                      tick_value=info.trade_tick_value, tick_size=info.trade_tick_size,
                      vol_min=info.volume_min, vol_step=info.volume_step,
                      vol_max=info.volume_max)


def spec_from_fallback(cfg: dict, name: str) -> SymbolSpec | None:
    fb = cfg.get("symbol_specs_fallback", {}).get(name)
    if not fb:
        return None
    return SymbolSpec(name=name, digits=fb["digits"], point=10 ** -fb["digits"],
                      tick_value=fb["tick_value"], tick_size=fb["tick_size"],
                      vol_min=fb["vol_min"], vol_step=fb["vol_step"])


# ========================================================== risk governor ===
def normalize_lots(lots: float, step: float) -> float:
    if step <= 0:
        step = 0.01
    k = math.floor(round(lots / step, 8))
    return round(k * step, 8)


def lots_for_risk(spec: SymbolSpec, sl_distance: float, risk_usd: float,
                  risk_usd_max: float) -> tuple[float | None, str]:
    """Size lots so that sl_distance * lots * unit_value ~= risk_usd.

    Returns (lots, "") or (None, reason). NEVER compresses the stop: if the risk at
    volume_min exceeds risk_usd_max the trade is rejected outright.
    """
    if not all(math.isfinite(v) and v > 0 for v in (sl_distance, risk_usd, risk_usd_max)):
        return None, "invalid stop distance or risk budget"
    if (not all(math.isfinite(v) and v > 0 for v in (spec.vol_min, spec.vol_max, spec.vol_step))
            or spec.vol_min > spec.vol_max):
        return None, "invalid lot grid"
    unit = spec.value_per_price_unit()
    if not math.isfinite(unit) or unit <= 0:
        return None, "invalid tick value/size"
    risk_per_lot = sl_distance * unit
    lots = normalize_lots(min(risk_usd, risk_usd_max, spec.vol_max * risk_per_lot) / risk_per_lot,
                          spec.vol_step)
    if lots < spec.vol_min - 1e-9:
        min_risk = spec.vol_min * risk_per_lot
        if min_risk > risk_usd_max + 1e-9:
            return None, (f"min-lot risk ${min_risk:.2f} > cap ${risk_usd_max:.2f} "
                          f"— reject, stop not compressed")
        lots = spec.vol_min
    if lots > spec.vol_max + 1e-9 or lots * risk_per_lot > risk_usd_max + 1e-9:
        return None, "lot grid exceeds broker volume or dollar cap"
    return round(lots, 8), ""


# ======================================================== signal functions ==
# Every signal is evaluated AT INDEX i of CLOSED-bar arrays only (no element > i is
# read), so sig_at(prefix, i) == sig_at(full, i) — asserted by --selftest.
# Return dict: side(+1/-1), rd (stop distance), sl_abs, tp_abs (either may be None
# => caller derives from entry +/- rd * exit_r), note.

def sig_htf_gold_at(o, h, l, c, atr, i, params) -> dict | None:
    """Thin compatibility delegate to modular strategies.s03_htf_ema_rejection."""
    return _s03_signal_at(o, h, l, c, None, atr, i, params)


def sig_vc_at(o, h, l, c, atr, i, params) -> dict | None:
    """Thin compatibility delegate to modular strategies.s04_vc_squeeze_breakout."""
    return _s04_signal_at(o, h, l, c, None, atr, i, params)


def sig_ivwall_at(o, h, l, c, times_min, atr, sig_series, i, params, session_spans: list[float]) -> dict | None:
    """Thin compatibility delegate to modular strategies.s06_iv_wall_fade."""
    return _s06_signal_at(o, h, l, c, times_min, atr, i, params, {"sig_series": sig_series, "session_spans": session_spans})


def sig_orb_at(o, h, l, c, times_min, atr, htf_c_4h, i, params) -> dict | None:
    """Thin compatibility delegate to modular strategies.s11_or30_london_breakout."""
    return _s11_signal_at(o, h, l, c, times_min, atr, i, params, {"htf_c4": htf_c_4h})

# ============================================================ MT5 bridge ====
class MT5Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.connected = False

    def connect(self) -> bool:
        if not MT5_OK:
            print("[bridge] MetaTrader5 python package not available")
            return False
        if self.connected and mt5.terminal_info() is not None:
            return True
        acc = self.cfg.get("account", {})
        kw = {}
        if acc.get("terminal_path"):
            kw["path"] = acc["terminal_path"]
        if acc.get("login"):
            kw.update(login=int(acc["login"]), password=acc["password"], server=acc["server"])
        if not mt5.initialize(**kw):
            print("[bridge] initialize failed:", mt5.last_error())
            self.connected = False
            return False
        self.connected = True
        ai = mt5.account_info()
        if ai:
            print(f"[bridge] connected: #{ai.login} {ai.server} equity={ai.equity:.2f}")
        return True

    def shutdown(self):
        if MT5_OK and self.connected:
            mt5.shutdown()
            self.connected = False

    def ensure_visible(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is not None and not info.visible:
            mt5.symbol_select(symbol, True)

    def rates(self, symbol: str, tf_name: str, count: int):
        r = mt5.copy_rates_from_pos(symbol, TF_MT5[tf_name], 0, count)
        return r if (r is not None and len(r)) else None

    def tick(self, symbol: str):
        t = mt5.symbol_info_tick(symbol)
        return (float(t.ask), float(t.bid)) if t else (None, None)

    def positions(self):
        return list(mt5.positions_get() or [])

    def account_equity(self) -> float:
        ai = mt5.account_info()
        return float(ai.equity) if ai else 0.0

    def total_sl_risk_usd(self, positions: list | None = None) -> float:
        """Sum stop-loss dollar risk across all live MT5 positions and pending orders."""
        total = 0.0
        pos_list = positions if positions is not None else self.positions()
        for item in pos_list:
            sl = float(getattr(item, "sl", 0.0) or 0.0)
            entry = float(getattr(item, "price_open", 0.0) or 0.0)
            vol = float(getattr(item, "volume", 0.0) or 0.0)
            sym = getattr(item, "symbol", "")
            if sl <= 0 or vol <= 0 or not sym:
                continue
            info = mt5.symbol_info(sym)
            if info and info.trade_tick_size > 0 and info.trade_tick_value > 0:
                total += abs(sl - entry) * vol * (info.trade_tick_value / info.trade_tick_size)
        for ord_item in (mt5.orders_get() or []):
            sl = float(getattr(ord_item, "sl", 0.0) or 0.0)
            entry = float(getattr(ord_item, "price_open", 0.0) or 0.0)
            vol = float(getattr(ord_item, "volume_current", 0.0) or getattr(ord_item, "volume_initial", 0.0) or 0.0)
            sym = getattr(ord_item, "symbol", "")
            if sl <= 0 or vol <= 0 or not sym:
                continue
            info = mt5.symbol_info(sym)
            if info and info.trade_tick_size > 0 and info.trade_tick_value > 0:
                total += abs(sl - entry) * vol * (info.trade_tick_value / info.trade_tick_size)
        return total

    def daily_realized_usd(self) -> float:
        """Return net realized profit+swap+commission for the current UTC day."""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(day_start, now) or []
        return float(sum(d.profit + d.swap + d.commission for d in deals if d.entry in (1, 2)))

    def send_bracket(self, spec: SymbolSpec, side: int, lots: float, price: float,
                     sl: float, tp: float, magic: int, comment: str, deviation: int):
        """Market entry WITH server-side sl/tp. Filling-mode fallback: IOC->FOK->RETURN."""
        last = None
        for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": spec.name,
                   "volume": float(lots), "type": mt5.ORDER_TYPE_BUY if side == 1 else mt5.ORDER_TYPE_SELL,
                   "price": float(price), "sl": float(sl), "tp": float(tp),
                   "deviation": int(deviation), "magic": int(magic),
                   "comment": comment[:26], "type_time": mt5.ORDER_TIME_GTC,
                   "type_filling": filling}
            res = mt5.order_send(req)
            if res is None:
                continue
            if res.retcode == mt5.TRADE_RETCODE_DONE:
                return res
            if res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                return res
            last = res
        return last

    def modify_sl(self, ticket: int, sl: float, tp: float):
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket),
               "sl": float(sl), "tp": float(tp)}
        return mt5.order_send(req)

    def close_position(self, position, deviation: int):
        t = mt5.symbol_info_tick(position.symbol)
        if t is None:
            return None
        is_buy = position.type == (mt5.POSITION_TYPE_BUY if MT5_OK else 0)
        req = {"action": mt5.TRADE_ACTION_DEAL, "position": int(position.ticket),
               "symbol": position.symbol, "volume": float(position.volume),
               "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
               "price": float(t.bid) if is_buy else float(t.ask),
               "deviation": int(deviation), "magic": int(position.magic),
               "comment": "pilot-close", "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": mt5.ORDER_FILLING_IOC}
        return mt5.order_send(req)


# ============================================================== governor ====
class Governor:
    def __init__(self, cfg: dict, journal: Journal, state: StateStore, bridge: MT5Bridge | None):
        self.cfg = cfg
        self.gov = cfg["governor"]
        self.journal = journal
        self.state = state
        self.bridge = bridge

    @staticmethod
    def _is_gold_symbol(symbol: str) -> bool:
        s = (symbol or "").upper()
        return s.startswith("GAU") or s.startswith("XAU")

    @staticmethod
    def _is_crypto_symbol(symbol: str) -> bool:
        s = (symbol or "").upper()
        return any(s.startswith(p) for p in ("BTC", "ETH", "BCH", "DSH", "ZEC", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVX", "UNI", "AVE", "XMR", "LNK", "LTC"))

    def gates(self, magic: int, positions: list, planned_risk_usd: float = 0.0,
              committed_risk_usd: float | None = None,
              daily_pnl_usd: float | None = None,
              equity_usd: float | None = None,
              symbol: str = "") -> tuple[bool, str]:
        ours = [p for p in positions if getattr(p, "magic", 0) in self.our_magics()]
        max_glob = int(self.gov.get("max_global_positions") or 0)
        if max_glob > 0 and len(ours) >= max_glob:
            return False, f"global cap {max_glob} reached"
        max_sleeve = int(self.gov.get("max_per_sleeve") or 0)
        if max_sleeve > 0 and len([p for p in ours if p.magic == magic]) >= max_sleeve:
            return False, "sleeve already has a position"
        gold = self.gov.get("gold_cluster_magics", [])
        gold_max = int(self.gov.get("gold_cluster_max_positions") or 0)
        is_gold_req = self._is_gold_symbol(symbol) if symbol else (magic in gold)
        if gold_max > 0 and is_gold_req:
            ngold = len([
                p for p in ours
                if (self._is_gold_symbol(getattr(p, "symbol", "")) if getattr(p, "symbol", "") else (p.magic in gold))
            ])
            if ngold >= gold_max:
                return False, "gold-cluster cap (42003+42004 share one risk unit)"
        crypto_max = int(self.gov.get("crypto_cluster_max_positions") or 0)
        if crypto_max > 0 and symbol and self._is_crypto_symbol(symbol):
            ncrypto = len([p for p in ours if self._is_crypto_symbol(getattr(p, "symbol", ""))])
            if ncrypto >= crypto_max:
                return False, f"crypto-cluster cap ({crypto_max}) reached"
        eq = float(equity_usd) if equity_usd is not None else (
            self.bridge.account_equity() if self.bridge is not None else 0.0
        )
        floor = float(self.cfg.get("account", {}).get("equity_floor_usd", 0))
        if eq and eq < floor:
            return False, f"equity {eq:.2f} below floor {floor:.2f} — trading halted"
        max_risk_pct = float(self.gov.get("max_total_open_risk_pct") or 0.0)
        if max_risk_pct > 0 and eq > 0:
            committed = float(committed_risk_usd) if committed_risk_usd is not None else (
                self.bridge.total_sl_risk_usd(positions) if self.bridge is not None else 0.0
            )
            cap_usd = eq * max_risk_pct / 100.0
            if committed + float(planned_risk_usd) > cap_usd + 1e-9:
                return False, (
                    f"aggregate SL risk ${committed + float(planned_risk_usd):.2f} > "
                    f"{max_risk_pct:.2f}% cap (${cap_usd:.2f})"
                )
        dd_halt = float(self.gov.get("daily_dd_halt_usd") or 0.0)
        if dd_halt < 0:
            day_pnl = float(daily_pnl_usd) if daily_pnl_usd is not None else (
                self.bridge.daily_realized_usd() if self.bridge is not None else 0.0
            )
            if day_pnl <= dd_halt:
                return False, f"daily realized ${day_pnl:.2f} <= halt ${dd_halt:.2f}"
        return True, ""

    def our_magics(self) -> set:
        out = set()
        for name, s in self.cfg.get("sleeves", {}).items():
            # Include disabled sleeves too so legacy open tickets still count toward caps.
            if s.get("magic") is not None:
                out.add(int(s["magic"]))
        return out

    @staticmethod
    def register(state: StateStore, ticket: int, sleeve: str, entry: float, sl: float,
                 be_enabled: bool = True, tf: str = "H4"):
        rd = abs(entry - sl)
        state.state["positions"][str(ticket)] = {
            "sleeve": sleeve, "entry": float(entry), "r0": float(rd),
            "be_done": False, "be_enabled": bool(be_enabled), "tf": tf, "open_ts": int(time.time()),
        }

    def be_lock(self, positions: list, tick_fn) -> int:
        """Breakeven lock at +1R (respects per-position and per-sleeve be_lock_enabled); returns modifications."""
        n = 0
        trig = float(self.gov.get("be_lock_trigger_r", 1.0))
        buf = float(self.gov.get("be_lock_buffer_r", 0.05))
        sleeves_cfg = self.cfg.get("sleeves") or {}
        for p in positions:
            meta = self.state.state["positions"].get(str(p.ticket))
            if not meta or meta.get("be_done"):
                continue
            slv = sleeves_cfg.get(meta.get("sleeve", ""), {})
            if not meta.get("be_enabled", slv.get("be_lock_enabled", True)):
                continue
            r0 = float(meta.get("r0", 0)) or abs(p.price_open - p.sl if p.sl else 0)
            if r0 <= 0:
                continue
            is_buy = (p.type == 0)
            tick = tick_fn(p.symbol)
            if tick is None:
                continue
            cur = tick[1] if is_buy else tick[0]   # bid for buy, ask for sell
            prof_r = (cur - p.price_open) / r0 if is_buy else (p.price_open - cur) / r0
            if prof_r >= trig:
                be = p.price_open + (buf * r0 if is_buy else -buf * r0)
                if (is_buy and be > p.sl) or (not is_buy and be < p.sl):
                    res = self.bridge.modify_sl(p.ticket, be, p.tp) if self.bridge else None
                    ok = (res is not None and getattr(res, "retcode", 0) == 10009)
                    self.journal.log("BE_LOCK", sleeve=meta.get("sleeve", ""), magic=p.magic,
                                     symbol=p.symbol, lots=p.volume, price=be,
                                     r_multiple=round(prof_r, 2), retcode=str(getattr(res, 'retcode', 'dry' if self.bridge is None else 'none')))
                    if ok or self.bridge is None:
                        meta["be_done"] = True
                        n += 1
        return n


# ======================================================== session helpers ===
def spread_guard(symbol: str, spread_price: float, atr14: float, sleeve_cfg: dict) -> tuple[bool, str]:
    if spread_price <= 0:
        return False, "no live spread"
    if spread_price > float(sleeve_cfg.get("spread_frac_atr_max", 1.0)) * atr14:
        return False, f"spread {spread_price:.5f} > {sleeve_cfg['spread_frac_atr_max']}xATR"
    return True, ""


def count_today(state: StateStore, key: str, today: str) -> int:
    return int(state.state.get("counts", {}).get(f"{key}|{today}", 0))


def bump_today(state: StateStore, key: str, today: str):
    state.state.setdefault("counts", {})
    k = f"{key}|{today}"
    state.state["counts"][k] = int(state.state["counts"].get(k, 0)) + 1


# ================================================================= loop =====
def build_signal(sleeve: str, closed, sleeve_cfg: dict, params: dict, htf_c4: np.ndarray | None, sym: str = ""):
    """Evaluate the last closed bar (index -1 of the closed arrays)."""
    o = closed["open"].astype(float); h = closed["high"].astype(float)
    l = closed["low"].astype(float); c = closed["close"].astype(float)
    times = closed["time"].astype(np.int64) // 1000  # epoch seconds
    times_min = times // 60
    atr = wilder_atr(h, l, c, 14)
    i = len(c) - 1
    if sleeve == "htf_gold":
        return sig_htf_gold_at(o, h, l, c, atr, i, params)
    if sleeve == "vc_gold":
        return sig_vc_at(o, h, l, c, atr, i, params)
    if sleeve == "ivwall":
        sig = ewma_sigma(c, float(sleeve_cfg.get("vol_lam", 0.94)))
        spans = session_spans_from(times_min, int(sleeve_cfg.get("broker_utc_offset", 2)))
        return sig_ivwall_at(o, h, l, c, times_min, atr, sig, i,
                             {**sleeve_cfg, "broker_utc_offset": int(sleeve_cfg.get("broker_utc_offset", 2))}, spans)
    if sleeve == "orb_nas":
        return sig_orb_at(o, h, l, c, times_min, atr, htf_c4, i, params)
    mod_path = sleeve_cfg.get("module")
    if mod_path:
        fn = load_signal_fn(mod_path)
        sym_ovr = (sleeve_cfg.get("symbol_params") or {}).get(sym) or {}
        eff_params = {**(sleeve_cfg.get("params") or {}), **(params if params is not sleeve_cfg else {}), **sym_ovr}
        return fn(o, h, l, c, times_min, atr, i, eff_params, {"htf_c4": htf_c4})
    return None


def _ml_score_pilot_signal(
    sym: str,
    sname: str,
    sym_tf: str,
    closed,
    spec: SymbolSpec,
    side: int,
    entry: float,
    live_spread: float,
    cfg: dict,
) -> dict | None:
    """Score matched-domain H4/H2 signals with the persisted Time-Decay Expected-R ML Bundle."""
    ml_cfg = cfg.get("ml_gate") or {}
    if not ml_cfg.get("enabled", False) or sym_tf not in ("H4", "H2"):
        return None
    dom = set(ml_cfg.get("domain_symbols") or [])
    if dom and sym not in dom:
        return None
    try:
        from pathlib import Path
        from datetime import datetime, timezone
        from exchange_feed.backtesting.ml_meta_engine import FINAL_BUNDLE_PATH, score_live_signal
    except Exception:
        return None

    bpath_raw = ml_cfg.get("bundle_path")
    bpath = Path(bpath_raw) if (bpath_raw and os.path.isabs(bpath_raw)) else (
        Path(ROOT) / bpath_raw if bpath_raw else FINAL_BUNDLE_PATH
    )
    if not bpath.is_file():
        return None

    c = closed["close"].astype(float)
    h = closed["high"].astype(float)
    l = closed["low"].astype(float)
    o = closed["open"].astype(float)
    if len(c) < 240:
        return None

    atr_arr = wilder_atr(h, l, c, 14)
    atr_i = float(atr_arr[-1])
    c_i = float(c[-1])
    if atr_i <= 1e-12 or c_i <= 1e-12:
        return None

    e89 = ema_arr(c, 89)
    e233 = ema_arr(c, 233)
    adx_series = adx_arr(h, l, c, 14)
    rng_i = max(float(h[-1] - l[-1]), 1e-12)
    wick_adv = ((float(h[-1]) - max(float(o[-1]), c_i)) / rng_i) if side > 0 else ((min(float(o[-1]), c_i) - float(l[-1])) / rng_i)
    sp_bps = (live_spread / max(entry, 1e-9)) * 10000.0
    natr_bps = max(1.0, atr_i / c_i * 10000.0)
    ts_bar = int(closed["time"][-1])
    dt = datetime.fromtimestamp(ts_bar, tz=timezone.utc)
    hr_rad = 2.0 * math.pi * (dt.hour + dt.minute / 60.0) / 24.0
    dow_rad = 2.0 * math.pi * dt.weekday() / 7.0

    # Rolling 48-bar VWAP Z-score + Williams %R(14) + RSI(2) + ATR percentile
    vw48_std = float(np.std(c[-48:])) if len(c) >= 48 else float(np.std(c))
    vw48_mean = float(np.mean(c[-48:])) if len(c) >= 48 else float(np.mean(c))
    vwz = (c_i - vw48_mean) / max(vw48_std, 1e-12)
    hh14 = float(np.max(h[-14:]))
    ll14 = float(np.min(l[-14:]))
    wr14 = -100.0 * (hh14 - c_i) / max(hh14 - ll14, 1e-12)
    rsi2_arr = rsi_wilder_arr(c, 2)
    rsi2 = float(rsi2_arr[-1]) if np.isfinite(rsi2_arr[-1]) else 50.0
    atr_win100 = atr_arr[-100:] if len(atr_arr) >= 100 else atr_arr
    atr_rk = float(np.mean(atr_win100 <= atr_i))
    atr_win20 = atr_arr[-20:] if len(atr_arr) >= 20 else atr_arr

    # Optional fresh disk telemetry enrichment (funding & book wall imbalance)
    cg_sym = sym.split(".")[0][:3] if sym.endswith(".pi") or sym.endswith(".p") else sym
    if cg_sym == "GAU":
        cg_sym = "XAU"
    fund_bp, imb_diff = 0.0, 0.0
    tel_p = os.path.join(ROOT, "data", "telemetry", "assets", f"{cg_sym}.json")
    if os.path.exists(tel_p):
        try:
            with open(tel_p, encoding="utf-8") as tf:
                td = json.load(tf)
            if (time.time() - float(td.get("fetched_at_ts") or td.get("updated_ts") or 0)) <= 900:
                fund_bp = float(((td.get("funding_oi") or {}).get("oi_weighted_8h_pct") or 0.0)) * 100.0
                imb_diff = float((((td.get("book_walls") or {}).get("futures") or {}).get("m15") or {}).get("imbalance_pct") or 0.0) / 100.0
        except Exception:
            pass

    s_f = float(side)
    slv_l = sname.lower()
    is_mr = 1.0 if ("rsi2" in slv_l or "ivwall" in slv_l or "fade" in slv_l or "sweep" in slv_l or "imb2" in slv_l) else 0.0
    feat_row = {
        "signed_clv": s_f * ((2.0 * c_i - float(h[-1]) - float(l[-1])) / rng_i),
        "signed_imb_diff": s_f * imb_diff,
        "signed_vwz48": s_f * vwz,
        "signed_ret1_atr": s_f * (c_i - float(c[-2])) / atr_i,
        "signed_ret3_atr": s_f * (c_i - float(c[-4])) / atr_i,
        "signed_ret12_atr": s_f * (c_i - float(c[-13])) / atr_i if len(c) >= 13 else 0.0,
        "atr_pct_rk": atr_rk,
        "atr_exp_ratio": atr_i / max(float(np.mean(atr_win20)), 1e-12),
        "bar_range_atr": rng_i / atr_i,
        "wick_adv_pct": wick_adv,
        "vpin_rank": 0.5,
        "vpm_rk": 0.5,
        "avg_sz_rk": 0.5,
        "btc_vpin": 0.5,
        "vol_rel20": 1.0,
        "vr4": 1.0,
        "signed_dist_e89": s_f * (c_i - float(e89[-1])) / atr_i,
        "signed_dist_e233": s_f * (c_i - float(e233[-1])) / atr_i,
        "signed_ema_spread": s_f * (float(e89[-1]) - float(e233[-1])) / atr_i,
        "signed_e89_slope3": s_f * (float(e89[-1]) - float(e89[-4])) / atr_i,
        "signed_rsi2": (50.0 - rsi2) * s_f,
        "signed_wr14": s_f * (wr14 + 50.0),
        "adx14": float(adx_series[-1]) if np.isfinite(adx_series[-1]) else 20.0,
        "signed_fund_bp": s_f * fund_bp,
        "sp_bps": sp_bps,
        "sp_to_atr_bps": sp_bps / natr_bps,
        "hr_sin": math.sin(hr_rad),
        "hr_cos": math.cos(hr_rad),
        "dow_sin": math.sin(dow_rad),
        "dow_cos": math.cos(dow_rad),
        "side": s_f,
        "is_2h": 1.0 if sym_tf == "H2" else 0.0,
        "is_mr_sleeve": is_mr,
        "is_metal": 1.0 if ("XAU" in sym or "XAG" in sym or "GAU" in sym) else 0.0,
    }
    return score_live_signal(feat_row, seq_arr=None, bundle_path=bpath,
                             min_expected_r=float(ml_cfg.get("min_expected_r", -0.10)))


def run_loop(cfg: dict, mode: str, loop_seconds: int | None = None):
    bridge = MT5Bridge(cfg)
    if mode == "live" or MT5_OK:
        if not bridge.connect():
            print("cannot attach MT5 terminal — for no-terminal validation run --selftest")
            return
    journal = Journal(os.path.join(ROOT, cfg["runtime"]["journal_csv"]))
    state = StateStore(os.path.join(ROOT, cfg["runtime"]["state_json"]), strict_live=(mode == "live"))
    gov = Governor(cfg, journal, state, bridge if mode == "live" else None)
    d_cfg = cfg.get("delta") or {}
    delta_bridge = DeltaLive(d_cfg.get("symbol_map")) if (mode == "live" and d_cfg.get("enabled", False)) else None
    if delta_bridge is not None:
        d_ok, d_eq, d_av, d_err = delta_bridge.interlock_check()
        print(f"[delta] {'OK equity=$' + f'{d_eq:.2f} avail=${d_av:.2f}' if d_ok else 'ILK-01 HALT (MT5 unaffected): ' + d_err}", flush=True)
    gov.journal.log("ENGINE_START", note=f"mode={mode}")
    poll = int(loop_seconds or cfg["runtime"]["poll_seconds"])
    bars_back = int(cfg["runtime"]["bars_back"])
    offset = int(cfg["account"].get("broker_utc_offset", 2))
    dev = int(cfg["account"].get("deviation_points", 20))
    print(f"[pilot] mode={mode} poll={poll}s sleeves=" +
          ",".join(n for n, s in cfg["sleeves"].items() if s.get("enabled")))
    try:
        while True:
            if mode == "live" and MT5_OK and mt5.terminal_info() is None:
                gov.journal.log("BRIDGE_DOWN", note="mt5.terminal_info() is None; attempting reconnect")
                bridge.connect()
                time.sleep(poll)
                continue
            now = time.time()
            utc_today = time.strftime("%Y-%m-%d", time.gmtime(now))
            # ------- entries (new closed bar per sleeve/symbol)
            for sname, s in cfg["sleeves"].items():
                if not s.get("enabled", False):
                    continue
                magic = int(s["magic"])
                for sym in (list(s.get("symbols", {}).keys()) if isinstance(s.get("symbols"), dict)
                            else (s.get("symbols") or ([s["symbol"]] if "symbol" in s else []))):
                    base_params = s["symbols"][sym] if isinstance(s.get("symbols"), dict) else s
                    sym_ovr = (s.get("symbol_params") or {}).get(sym) or {}
                    sym_tf = str(sym_ovr.get("tf") or (base_params.get("tf") if isinstance(base_params, dict) else None) or s["tf"])
                    eff_be_enabled = bool(sym_ovr.get("be_lock_enabled", (base_params.get("be_lock_enabled") if isinstance(base_params, dict) else None) if (isinstance(base_params, dict) and "be_lock_enabled" in base_params) else s.get("be_lock_enabled", True)))
                    key = f"{sname}:{sym}"
                    raw = bridge.rates(sym, sym_tf, bars_back)
                    if raw is None or len(raw) < 60:
                        continue
                    bridge.ensure_visible(sym)
                    closed = raw[:-1]                      # forming bar excluded
                    t0 = int(closed["time"][-1])
                    if state.get("bars", {}).get(key) == t0:
                        continue
                    spec = spec_from_mt5(sym) or spec_from_fallback(cfg, sym)
                    if spec is None:
                        state.set("bars", {**state.get("bars", {}), key: t0})
                        continue
                    # ---- signal
                    htf_c4 = None
                    if sname == "orb_nas":
                        r4 = bridge.rates(sym, s.get("htf_tf", "H4"), 400)
                        if r4 is not None:
                            htf_c4 = r4["close"].astype(float)[:-1]
                    sig = build_signal(sname, closed, s, base_params, htf_c4, sym=sym)
                    if sig is None:
                        state.set("bars", {**state.get("bars", {}), key: t0})
                        continue
                    # ---- rate limiting
                    if sname == "ivwall":
                        sess_today = time.strftime("%Y-%m-%d", time.gmtime(now + offset * 3600))
                        if count_today(state, key, sess_today) >= int(s.get("max_trades_session", 2)):
                            state.set("bars", {**state.get("bars", {}), key: t0})
                            continue
                    if sname == "orb_nas" and count_today(state, key, utc_today) >= int(s.get("max_trades_day", 1)):
                        state.set("bars", {**state.get("bars", {}), key: t0})
                        continue
                    cd_key = f"cooldown|{key}"
                    until = float(state.get("cooldown", {}).get(cd_key, 0))
                    tf_min = TF_MAP[sym_tf]
                    if t0 / 60 + tf_min < until:
                        state.set("bars", {**state.get("bars", {}), key: t0})
                        continue
                    # ---- spread guard (live spread; retry on transient tick=None before consuming bar)
                    ask, bid = bridge.tick(sym)
                    if ask is None or bid is None:
                        journal.log("TICK_RETRY", sleeve=sname, magic=magic, symbol=sym, note="tick None; will retry bar")
                        continue
                    state.set("bars", {**state.get("bars", {}), key: t0})
                    live_spread = ask - bid
                    if sname == "ivwall":
                        closed_c = closed["close"].astype(float)
                        closed_h = closed["high"].astype(float)
                        closed_l = closed["low"].astype(float)
                        atr14 = wilder_atr(closed_h, closed_l, closed_c, 14)[-1]
                        if live_spread > float(s.get("spread_frac_atr_max", 1.0)) * atr14:
                            journal.log("SPREAD_SKIP", sleeve=sname, magic=magic, symbol=sym,
                                        note=f"spread {live_spread:.5f} > {s['spread_frac_atr_max']}xATR {atr14:.5f}")
                            continue
                        med_pips = float(s.get("spread_median_pips", {}).get(sym, 1.0))
                        med_price = med_pips * 10 ** -spec.digits
                        if live_spread > float(s.get("spread_model_mult", 1.5)) * med_price:
                            journal.log("SPREAD_SKIP", sleeve=sname, magic=magic, symbol=sym,
                                        note=f"spread {live_spread:.5f} > {s['spread_model_mult']}x model {med_price:.5f}")
                            continue
                    # ---- bracket geometry from the real entry price
                    side = int(sig["side"])
                    entry = ask if side == 1 else bid
                    if sig.get("sl_abs") is not None:
                        sl = float(sig["sl_abs"])
                    else:
                        rd0 = float(sig["rd"])
                        sl = entry - rd0 if side == 1 else entry + rd0
                    rd = abs(entry - sl)
                    if rd <= 0:
                        continue
                    closed_atr14 = float(wilder_atr(
                        closed["high"].astype(float),
                        closed["low"].astype(float),
                        closed["close"].astype(float), 14)[-1])
                    min_sl_atr = float(cfg["governor"].get("min_sl_atr_mult", 1.0))
                    if sname not in ("ivwall", "orb_nas") and closed_atr14 > 0:
                        noise_floor = max(min_sl_atr * closed_atr14, 3.0 * live_spread)
                        if rd + 1e-12 < noise_floor:
                            journal.log("SL_NOISE_REJECT", sleeve=sname, magic=magic, symbol=sym,
                                        note=f"rd {rd:.5f} < noise_floor {noise_floor:.5f}")
                            continue
                    max_sf = float(s.get("max_spread_risk_frac") or 0.0)
                    if max_sf > 0.0 and (live_spread * 2.0) / rd > max_sf:
                        journal.log("SPREAD_SKIP", sleeve=sname, magic=magic, symbol=sym,
                                    note=f"2x_spread {2*live_spread:.5f} > {max_sf:.2f}x risk {rd:.5f}")
                        continue
                    if sig.get("tp_abs") is not None:
                        tp = float(sig["tp_abs"])
                    else:
                        tp = entry + float(sig["exit_r"]) * rd if side == 1 else entry - float(sig["exit_r"]) * rd
                    tp_r = abs(tp - entry) / rd
                    min_r = float(cfg["governor"]["min_tp_r"])
                    if sname != "ivwall" and tp_r + 1e-9 < min_r:   # ivwall carries the audited halfway-back exception
                        journal.log("REJECT", sleeve=sname, magic=magic, symbol=sym,
                                    note=f"tp {tp_r:.2f}R < min {min_r}R")
                        continue
                    if (side == 1 and not (sl < entry < tp)) or (side == -1 and not (tp < entry < sl)):
                        journal.log("REJECT", sleeve=sname, magic=magic, symbol=sym, note="bracket sides invalid")
                        continue
                    # ---- Live CG Order-Flow SL/TP Optimization (cold-zone SL + wall/magnet TP)
                    cg_opt_cfg = cfg.get("cg_optimizer") or {"enabled": True}
                    if cg_opt_cfg.get("enabled", True):
                        closed_atr = float(wilder_atr(
                            closed["high"].astype(float),
                            closed["low"].astype(float),
                            closed["close"].astype(float), 14)[-1])
                        cg_res = optimize_bracket_with_cg(
                            symbol=sym, side=side, entry=entry, sl=sl, tp=tp, atr=closed_atr,
                            live_spread=live_spread, min_tp_r=min_r,
                            exempt_min_r=(sname == "ivwall"),
                            max_spread_risk_frac=max_sf, spec=spec,
                            risk_usd_max=float(cfg["governor"]["risk_usd_max"]),
                            digits=spec.digits,
                            cg_cfg={**cg_opt_cfg, "use_http": (mode == "live")},
                        )
                        if cg_res.sl_adjusted or cg_res.tp_adjusted:
                            sl, tp, rd, tp_r = cg_res.sl, cg_res.tp, cg_res.rd, cg_res.tp_r
                            journal.log("CG_SLTP_OPT", sleeve=sname, magic=magic, symbol=sym,
                                        price=entry, sl=sl, tp=tp, r_multiple=round(tp_r, 2),
                                        note=f"{cg_res.source}: {cg_res.note}")
                            print(f"[cg-opt] {sname} {sym} {cg_res.note} ({cg_res.source})")
                    # ---- sizing + governor (exact planned SL dollar risk gated BEFORE any venue routing)
                    risk_usd = float(cfg["governor"]["risk_usd_target"])
                    s8 = cfg.get("s8_vol_target") or {}
                    if s8.get("enabled"):
                        risk_usd = vol_target_risk_usd(
                            closed["close"].astype(float), sym_tf, risk_usd,
                            float(cfg["governor"].get("risk_usd_min", 5.0)),
                            float(cfg["governor"].get("risk_usd_max", 6.0)),
                            target_ann_vol=float(s8.get("target_ann_vol", 0.22)),
                            lookback=int(s8.get("lookback", 20)),
                        )
                    ml_res = _ml_score_pilot_signal(sym, sname, sym_tf, closed, spec, side, entry, live_spread, cfg)
                    ml_tag = ""
                    if ml_res and ml_res.get("ok"):
                        if not ml_res.get("approve_regime", True):
                            journal.log("ML_VETO", sleeve=sname, magic=magic, symbol=sym,
                                        side="BUY" if side == 1 else "SELL", price=entry, sl=sl, tp=tp,
                                        note=f"E[R]={ml_res['expected_r']:+.3f} ({ml_res['tier']})")
                            continue
                        r_min = float(cfg["governor"].get("risk_usd_min", 5.0))
                        r_max = float(cfg["governor"].get("risk_usd_max", 6.0))
                        risk_usd = round(min(r_max, max(r_min, risk_usd * float(ml_res.get("risk_mult", 1.0)))), 2)
                        ml_tag = f" ML_ER={ml_res['expected_r']:+.3f}({ml_res['tier']})"
                    lots, why = lots_for_risk(spec, rd, risk_usd, float(cfg["governor"]["risk_usd_max"]))
                    if lots is None:
                        journal.log("RISK_REJECT", sleeve=sname, magic=magic, symbol=sym,
                                    lots=0.0, price=entry, sl=sl, tp=tp, note=why)
                        continue
                    actual_risk = float(lots) * float(rd) * spec.value_per_price_unit()
                    ok, why = gov.gates(magic, bridge.positions() if mode == "live" else [],
                                        planned_risk_usd=actual_risk, symbol=sym)
                    if not ok:
                        journal.log("GATE_SKIP", sleeve=sname, magic=magic, symbol=sym, note=why)
                        continue
                    # ---- Parallel Delta Exchange India routing (gated strictly after Governor checks) ----
                    if delta_bridge is not None and delta_bridge.map_symbol(sym):
                        d_bars = state.get("delta_bars", {})
                        if d_bars.get(key) != t0:
                            d_res = delta_bridge.send_bracket(
                                sym, "BUY" if side == 1 else "SELL", entry, sl, tp,
                                risk_pct=float(d_cfg.get("risk_per_trade_pct", 1.0)),
                                risk_max_pct=float(d_cfg.get("risk_max_pct", 1.0)),
                                max_portfolio_risk_pct=float(d_cfg.get("max_portfolio_risk_pct", 2.0)),
                                max_concurrent=int(d_cfg.get("max_concurrent", 2)),
                                client_order_id=f"{sname[:6]}_{t0}",
                                order_type="market",
                            )
                            if d_res.get("status") in ("OK", "SKIP"):
                                state.set("delta_bars", {**d_bars, key: t0})
                            journal.log(f"DELTA_{d_res.get('status')}", sleeve=sname, magic=magic, symbol=sym,
                                        side="BUY" if side == 1 else "SELL", lots=float(d_res.get("size", 0)),
                                        price=entry, sl=sl, tp=tp, note=d_res.get("why") or str(d_res.get("order_id", "")))
                            print(f"[delta] {d_res.get('status')} {sname} {sym}->{d_res.get('symbol')} size={d_res.get('size',0)} {d_res.get('why','')}")
                    risk_usd = actual_risk
                    comment = f"{sname[:8]} {sig['note'][:14]}"
                    if ml_tag:
                        sig = {**sig, "note": f"{sig['note']}{ml_tag}"}
                    if mode == "live":
                        res = bridge.send_bracket(spec, side, lots, entry, sl, tp, magic, comment, dev)
                        ret = getattr(res, "retcode", None)
                        if ret != 10009:
                            journal.log("ORDER_FAIL", sleeve=sname, magic=magic, symbol=sym,
                                        side="BUY" if side == 1 else "SELL", lots=lots, price=entry,
                                        sl=sl, tp=tp, retcode=str(ret),
                                        note=str(getattr(res, "comment", "")))
                            continue
                        ticket = getattr(res, "order", 0)
                        Governor.register(state, ticket, sname, entry, sl, be_enabled=eff_be_enabled, tf=sym_tf)
                        bump_today(state, key, sess_today if sname == "ivwall" else utc_today)
                        state.state["cooldown"][cd_key] = t0 / 60 + int(s.get("cooldown_bars", 6)) * tf_min
                        journal.log("ORDER_SENT", sleeve=sname, magic=magic, symbol=sym,
                                    side="BUY" if side == 1 else "SELL", lots=lots, price=entry,
                                    sl=sl, tp=tp, risk_usd=risk_usd, r_multiple=round(tp_r, 2),
                                    retcode=str(ret), note=f"ticket={ticket} {sig['note']}")
                        print(f"[pilot] {sname} {sym} {'BUY' if side==1 else 'SELL'} {lots} @ {entry} sl={sl} tp={tp}")
                    else:
                        journal.log("DRY_SIGNAL", sleeve=sname, magic=magic, symbol=sym,
                                    side="BUY" if side == 1 else "SELL", lots=lots, price=entry,
                                    sl=sl, tp=tp, risk_usd=risk_usd, r_multiple=round(tp_r, 2),
                                    retcode="dry", note=sig["note"])
                        print(f"[dry] {sname} {sym} {'BUY' if side==1 else 'SELL'} {lots} @ {entry} sl={sl} tp={tp} ({tp_r:.1f}R)")
            # ------- position management (every poll): BE lock + pending pruner + time stops
            if mode == "live":
                prune_ids = set(int(x) for x in (cfg.get("governor", {}).get("prune_pending_tickets") or []))
                if prune_ids:
                    for ord_item in (mt5.orders_get() or []):
                        if int(ord_item.ticket) in prune_ids:
                            res_rm = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ord_item.ticket)})
                            rc_rm = getattr(res_rm, "retcode", None)
                            if rc_rm == 10009:
                                journal.log("PRUNE_PENDING", symbol=ord_item.symbol, retcode="10009",
                                            note=f"auto-cancelled queued stale limit #{ord_item.ticket}")
                                print(f"[pilot] PRUNED stale limit #{ord_item.ticket} {ord_item.symbol}")
                positions = bridge.positions()
                live_tix = {str(p.ticket) for p in positions}
                for st_k in [k for k in list(state.state.get("positions", {})) if k not in live_tix]:
                    state.state["positions"].pop(st_k, None)
                gov.be_lock(positions, lambda sym: bridge.tick(sym))
                for p in positions:
                    if p.magic not in gov.our_magics():
                        continue
                    if p.magic == 42002:  # ivwall: flat before broker day end
                        mins_left = (22 * 60) - (now / 60 + offset * 60) % 1440
                        if mins_left <= int(cfg["sleeves"]["ivwall"].get("flat_before_broker_day_end_min", 15)):
                            res = bridge.close_position(p, dev)
                            journal.log("TIME_STOP", sleeve="ivwall", magic=42002, symbol=p.symbol,
                                        retcode=str(getattr(res, "retcode", "")), note="session end")
                    if p.magic == 42005:  # orb: EOD flat
                        hhmm = time.strftime("%H:%M", time.gmtime(now))
                        if hhmm >= cfg["sleeves"]["orb_nas"].get("eod_utc", "20:55"):
                            res = bridge.close_position(p, dev)
                            journal.log("TIME_STOP", sleeve="orb_nas", magic=42005, symbol=p.symbol,
                                        retcode=str(getattr(res, "retcode", "")), note="eod")
                    # Generic max_hold_bars time-stop for sleeves specifying it (e.g. ig_s2_rsi2_meanrev)
                    meta_p = state.state.get("positions", {}).get(str(p.ticket))
                    if meta_p and meta_p.get("open_ts"):
                        slv_name = meta_p.get("sleeve", "")
                        slv_cfg = cfg["sleeves"].get(slv_name, {})
                        sym_ovr_p = (slv_cfg.get("symbol_params") or {}).get(p.symbol) or {}
                        mh_bars = int(sym_ovr_p.get("max_hold_bars") or slv_cfg.get("max_hold_bars") or 0)
                        if mh_bars > 0:
                            eff_tf = meta_p.get("tf") or slv_cfg.get("tf", "H4")
                            max_sec = mh_bars * TF_MAP.get(eff_tf, 240) * 60
                            if (now - float(meta_p["open_ts"])) >= max_sec:
                                res = bridge.close_position(p, dev)
                                journal.log("TIME_STOP", sleeve=slv_name, magic=p.magic, symbol=p.symbol,
                                            retcode=str(getattr(res, "retcode", "")), note=f"max_hold_{mh_bars}")
            state.save()
            time.sleep(poll)
    except KeyboardInterrupt:
        print("[pilot] stopped by user")
    finally:
        gov.journal.log("ENGINE_STOP", note=f"mode={mode}")
        bridge.shutdown()
        state.save()


# ============================================================= selftest =====
def _load_parquet(symbol: str, tf: str):
    import pandas as pd
    tf_dir = {"M1": "1M", "M5": "5M", "M15": "15M", "H1": "1H", "H4": "4H"}.get(tf, tf)
    p = os.path.join(ROOT, "historical_data", "mt5_parquet", tf_dir, symbol + ".parquet")
    df = pd.read_parquet(p).sort_values("datetime")
    return df


def _closed_arrays(df, limit=None):
    import pandas as pd
    if limit:
        df = df.head(limit)
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    t = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
    # unit-safe epoch minutes: parquet may carry datetime64[ns] OR [us]
    times_min = (t.astype("datetime64[s]").astype("int64") // 60).to_numpy()
    return o, h, l, c, times_min


def selftest() -> bool:
    import pandas as pd
    cfg = load_resolved_config(CONFIG_PATH)
    failures = []
    print("=" * 88)
    print("SELFTEST — causal determinism, brackets, risk math, governor, S8 vol-target")
    print("=" * 88)

    # ---- S1: causality — signal at bar k identical when computed on prefix vs full
    print("\nS1 causal determinism (prefix == full at every probe):")
    cases = [
        ("GAUUSD.pi", "4H", "htf_gold", cfg["sleeves"]["htf_gold"]["symbols"]["GAUUSD.pi"]),
        ("GAUUSD.pi", "4H", "vc_gold", cfg["sleeves"]["vc_gold"]["symbols"]["GAUUSD.pi"]),
        ("EURUSD.pi", "15M", "ivwall", cfg["sleeves"]["ivwall"]),
        ("EURUSD.pi", "15M", "orb_nas", cfg["sleeves"]["orb_nas"]),
        ("UKBRENT.p", "4H", "ig_s7_daily_pivot", cfg["sleeves"]["ig_s7_daily_pivot"]),
        ("UKBRENT.p", "4H", "ig_s5_zscore_breakout", cfg["sleeves"]["ig_s5_zscore_breakout"]),
        ("UNIUSD.p",  "4H", "ig_s1_williams_pullback", cfg["sleeves"]["ig_s1_williams_pullback"]),
        ("AU200.p",   "1H", "ig_s2_rsi2_meanrev", cfg["sleeves"]["ig_s2_rsi2_meanrev"]),
        ("GAUUSD.pi", "4H", "ig_s9_donchian_cta", cfg["sleeves"]["ig_s9_donchian_cta"]),
        ("ETHUSD.pi", "4H", "ig_s10_ema_pullback", cfg["sleeves"]["ig_s10_ema_pullback"]),
    ]
    for sym, tf, sname, params in cases:
        df = _load_parquet(sym, tf)
        o, h, l, c, tm = _closed_arrays(df)
        htf_c4_full, tm4_close = None, None
        tf_m = TF_MAP.get(tf, 15)
        if sname == "orb_nas":
            df4 = _load_parquet(sym, "H4")
            _, _, _, htf_c4_full, tm4_open = _closed_arrays(df4)
            tm4_close = tm4_open + 240
        n = len(c)
        probes = sorted(set([min(350, n - 2), n // 2, n - 51, n - 30, n - 2]))
        sig_series = ewma_sigma(c, 0.94) if sname == "ivwall" else None
        mod_fn = load_signal_fn(params["module"]) if isinstance(params, dict) and params.get("module") else None
        bad = 0
        for k in probes:
            k_eff = min(k, 4999)
            def _causal_h4(bar_idx: int, tt_arr):
                if htf_c4_full is None or tm4_close is None:
                    return None
                n4 = int(np.searchsorted(tm4_close, tt_arr[bar_idx] + tf_m, side="right"))
                return htf_c4_full[:n4]
            def run(arrs, bar_idx: int, sigs=None):
                oo, hh, ll, cc, tt = arrs
                atr = wilder_atr(hh, ll, cc, 14)
                hc4 = _causal_h4(bar_idx, tt)
                if sname == "htf_gold":
                    return sig_htf_gold_at(oo, hh, ll, cc, atr, bar_idx, params)
                if sname == "vc_gold":
                    return sig_vc_at(oo, hh, ll, cc, atr, bar_idx, params)
                if sname == "ivwall":
                    spans = session_spans_from(tt[: bar_idx + 1], 2)
                    return sig_ivwall_at(oo, hh, ll, cc, tt, atr, sigs, bar_idx,
                                         {**params, "broker_utc_offset": 2}, spans)
                if sname == "orb_nas":
                    return sig_orb_at(oo, hh, ll, cc, tt, atr, hc4, bar_idx, params)
                return mod_fn(oo, hh, ll, cc, tt, atr, bar_idx, params.get("params", {}), {"htf_c4": hc4})
            full = run((o, h, l, c, tm), k, sig_series)
            pref = run((o[:k_eff + 1], h[:k_eff + 1], l[:k_eff + 1], c[:k_eff + 1], tm[:k_eff + 1]),
                       k_eff, sig_series[:k_eff + 1] if sname == "ivwall" else None)
            same = ((full is None and pref is None) or
                    (full is not None and pref is not None and
                     full["side"] == pref["side"] and full["note"] == pref["note"]))
            if not same:
                bad += 1
        status = "OK" if bad == 0 else f"FAIL ({bad}/{len(probes)} probes differ)"
        print(f"  {sname:<24} {sym:<10} {tf}: {status}")
        if bad:
            failures.append(f"causality {sname}")

    # ---- S2: bracket integrity + next-bar-open + R-minimum in a replay harness
    print("\nS2 replay harness (entry=next open, server-side brackets, TP>=2R except ivwall):")
    import math as m
    def replay(sym, tf, sname, params, exit_default_r):
        df = _load_parquet(sym, tf)
        dg = int(pd.Series(df["close"].astype(str)).str.extract(r"\.(\d+)")[0].fillna("").str.rstrip("0").str.len().mode().iloc[0])
        sp = df["spread"].to_numpy(float) * 10.0 ** -dg
        nz = sp[sp > 0]
        spr = np.where(sp > 0, sp, float(np.median(nz)) if len(nz) else 0.0)
        o, h, l, c, tm = _closed_arrays(df)
        htf_c4_full, tm4_close = None, None
        tf_m = TF_MAP.get(tf, 15)
        if sname == "orb_nas":
            _, _, _, htf_c4_full, tm4_open = _closed_arrays(_load_parquet(sym, "H4"))
            tm4_close = tm4_open + 240
        n = len(c)
        atr = wilder_atr(h, l, c, 14)
        sig_series = ewma_sigma(c, 0.94) if sname == "ivwall" else None
        mod_fn = load_signal_fn(params["module"]) if isinstance(params, dict) and params.get("module") else None
        trades, naked = [], 0
        i0 = max(210, n - 1000) if sname.startswith("ig_") else (60 if sname != "vc_gold" else max(60, n - 2000))
        i = i0
        cool = 0
        while i < n - 2:
            if i < cool:
                i += 1
                continue
            htf_c4 = None
            if htf_c4_full is not None and tm4_close is not None:
                htf_c4 = htf_c4_full[:int(np.searchsorted(tm4_close, tm[i] + tf_m, side="right"))]
            if sname == "htf_gold":
                sig = sig_htf_gold_at(o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1], atr, i, params)
            elif sname == "vc_gold":
                sig = sig_vc_at(o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1], atr, i, params)
            elif sname == "ivwall":
                spans = session_spans_from(tm[:i + 1], 2)
                sig = sig_ivwall_at(o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1], tm[:i + 1], atr,
                                    sig_series[:i + 1], i, {**params, "broker_utc_offset": 2}, spans)
            elif sname == "orb_nas":
                sig = sig_orb_at(o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1], tm[:i + 1], atr, htf_c4, i, params)
            else:
                sig = mod_fn(o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1], tm[:i + 1], atr, i, params.get("params", {}), {"htf_c4": htf_c4})
            if sig is None:
                i += 1
                continue
            side = sig["side"]
            entry = o[i + 1]                      # next bar open — enforced
            sl = float(sig["sl_abs"]) if sig.get("sl_abs") is not None else \
                entry - float(sig["rd"]) if side == 1 else entry + float(sig["rd"])
            rd = abs(entry - sl)
            tp = float(sig["tp_abs"]) if sig.get("tp_abs") is not None else \
                entry + float(sig.get("exit_r") or exit_default_r) * rd if side == 1 else \
                entry - float(sig.get("exit_r") or exit_default_r) * rd
            if sl == 0 or tp == 0 or (side == 1 and not (sl < entry < tp)) or (side == -1 and not (tp < entry < sl)):
                i += 1
                continue
            if sname == "ivwall":
                em = sig.get("em", 0)
                if rd > float(params.get("stop_cap_em", 1.2)) * em or \
                        abs(tp - entry) / rd < float(params.get("min_rr", 0.3)):
                    i += 1
                    continue
            else:
                if abs(tp - entry) / rd + 1e-9 < float(cfg["governor"]["min_tp_r"]):
                    i += 1
                    continue
            naked += 0 if (sl and tp) else 1
            # adverse-first exits
            j = i + 1
            r_net, why = None, ""
            while j < n:
                stopped = (l[j] <= sl) if side == 1 else (h[j] >= sl)
                hit = (h[j] >= tp) if side == 1 else (l[j] <= tp)
                bull = c[j] >= o[j]
                seq = (("L", l[j]), ("H", h[j])) if bull else (("H", h[j]), ("L", l[j]))
                exited, xp = False, 0.0
                for tt_, px in seq:
                    if side == 1 and tt_ == "L" and px <= sl:
                        exited, xp = True, sl
                        break
                    if side == 1 and tt_ == "H" and px >= tp:
                        exited, xp = True, tp
                        break
                    if side == -1 and tt_ == "H" and px >= sl:
                        exited, xp = True, sl
                        break
                    if side == -1 and tt_ == "L" and px <= tp:
                        exited, xp = True, tp
                        break
                if exited:
                    r_raw = (xp - entry) / rd if side == 1 else (entry - xp) / rd
                    cost = (spr[i + 1] + spr[j]) / rd
                    r_net, why = r_raw - cost, "sl/tp"
                    break
                j += 1
            if r_net is not None:
                trades.append({"r": r_net, "tp_r": abs(tp - entry) / rd})
                cool = j + int(params.get("cooldown_bars", 6) or 6)
                i = j + 1
                continue
            i += 1
        tot = sum(t["r"] for t in trades)
        min_tp_ok = all(t["tp_r"] >= float(cfg["governor"]["min_tp_r"]) - 1e-9 for t in trades) if sname != "ivwall" else True
        return {"n": len(trades), "r": round(tot, 1), "naked": naked, "tp_ok": min_tp_ok}

    for sym, tf, sname, params in cases:
        r = replay(sym, tf, sname, params, 2.0)
        ok = r["naked"] == 0 and r["tp_ok"]
        print(f"  {sname:<10} {sym:<10} {tf}: n={r['n']:<4} sumR={r['r']:<8} naked={r['naked']} tp>=minR={r['tp_ok']} -> {'OK' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"brackets {sname}")

    # ---- S3: risk sizing (fixed $5-6, reject-never-compress)
    print("\nS3 risk sizing (fixed $5.50 target, $6.00 cap):")
    fb = cfg["symbol_specs_fallback"]
    tests = [
        ("SOLUSD.p", 5.0), ("BTCUSD.pi", 5.5), ("BTCUSD.pi", 800.0), ("XAUUSD.pi", 5.5),
        ("EURUSD.pi", 0.0050), ("NAS100.p", 50.0)]
    for name, dist in tests:
        spec = SymbolSpec(name=name, digits=fb[name]["digits"], point=10 ** -fb[name]["digits"],
                          tick_value=fb[name]["tick_value"], tick_size=fb[name]["tick_size"],
                          vol_min=fb[name]["vol_min"], vol_step=fb[name]["vol_step"])
        lots, why = lots_for_risk(spec, dist, 5.5, 6.0)
        if lots is None:
            print(f"  {name:<10} stop={dist:<8} -> REJECT ({why})")
            if "BTCUSD" in name and dist == 800.0:
                pass
            else:
                failures.append(f"risk {name} unexpected reject")
        else:
            risk = lots * dist * spec.value_per_price_unit()
            ok = 4.99 <= risk <= 6.01
            print(f"  {name:<10} stop={dist:<8} -> {lots} lots, risk ${risk:.2f} -> {'OK' if ok else 'FAIL'}")
            if not ok:
                failures.append(f"risk {name} {risk}")

    # ---- S4: governor caps
    print("\nS4 governor caps:")

    class FakePos:
        def __init__(self, magic, ticket=1):
            self.magic, self.ticket = magic, ticket

    gov = Governor(cfg, Journal(os.path.join(DATA_DIR, "selftest_journal.csv")),
                   StateStore(os.path.join(DATA_DIR, "selftest_state.json")), None)
    unlim_cfg = {**cfg, "governor": {**cfg["governor"], "max_global_positions": 0,
                                       "max_per_sleeve": 0, "gold_cluster_max_positions": 0}}
    gov_unlim = Governor(unlim_cfg, gov.journal, gov.state, None)
    cases_unlim = [
        ("empty book, any entry", [], 42003, True),
        ("unlimited global allows 3rd+", [FakePos(42002), FakePos(42005)], 42003, True),
        ("unlimited sleeve allows multi-sym", [FakePos(42003)], 42003, True),
        ("unlimited gold cluster passes", [FakePos(42004)], 42003, True),
    ]
    for label, pos, magic, expect in cases_unlim:
        ok, why = gov_unlim.gates(magic, pos)
        print(f"  [unlim]    {label:<34} -> {'PASS' if ok else 'REJECT'} ({why})"[:100])
        if ok != expect:
            failures.append(f"governor: {label}")
    capped_cfg = {**cfg, "governor": {**cfg["governor"], "max_global_positions": 2,
                                       "max_per_sleeve": 1, "gold_cluster_max_positions": 1,
                                       "gold_cluster_magics": [42003, 42004]}}
    gov_capped = Governor(capped_cfg, gov.journal, gov.state, None)
    cases_g = [
        ("capped 2 global -> 3rd rejected", [FakePos(42002), FakePos(42005)], 42003, False),
        ("capped same sleeve -> 2nd rejected", [FakePos(42003)], 42003, False),
        ("capped gold cluster -> rejected", [FakePos(42004)], 42003, False),
        ("capped non-gold coexist", [FakePos(42002)], 42005, True),
    ]
    for label, pos, magic, expect in cases_g:
        ok, why = gov_capped.gates(magic, pos)
        print(f"  [cap test] {label:<34} -> {'PASS' if ok else 'REJECT'} ({why})"[:100])
        if ok != expect:
            failures.append(f"governor: {label}")

    # ---- S5: determinism (two identical runs)
    print("\nS5 determinism (replay twice):")
    a = replay("GAUUSD.pi", "4H", "htf_gold", cfg["sleeves"]["htf_gold"]["symbols"]["GAUUSD.pi"], 2.5)
    b = replay("GAUUSD.pi", "4H", "htf_gold", cfg["sleeves"]["htf_gold"]["symbols"]["GAUUSD.pi"], 2.5)
    same = a == b
    print(f"  htf_gold replay A==B -> {'OK' if same else 'FAIL'} ({a})")
    if not same:
        failures.append("determinism")
    s7a = replay("UKBRENT.p", "4H", "ig_s7_daily_pivot", cfg["sleeves"]["ig_s7_daily_pivot"], 3.0)
    s7b = replay("UKBRENT.p", "4H", "ig_s7_daily_pivot", cfg["sleeves"]["ig_s7_daily_pivot"], 3.0)
    same7 = s7a == s7b
    print(f"  ig_s7_daily_pivot replay A==B -> {'OK' if same7 else 'FAIL'} ({s7a})")
    if not same7:
        failures.append("determinism s7")
    s9a = replay("GAUUSD.pi", "4H", "ig_s9_donchian_cta", cfg["sleeves"]["ig_s9_donchian_cta"], 3.0)
    s9b = replay("GAUUSD.pi", "4H", "ig_s9_donchian_cta", cfg["sleeves"]["ig_s9_donchian_cta"], 3.0)
    if s9a != s9b:
        failures.append("determinism s9")
    print(f"  ig_s9_donchian_cta replay A==B -> {'OK' if s9a == s9b else 'FAIL'} ({s9a})")

    # ---- S6: IG-S8 Volatility-Targeting risk overlay bounds
    print("\nS6 IG-S8 volatility targeting ($5.00..$6.00 bounded risk):")
    calm_c = 100.0 + np.linspace(0, 1.0, 40) + 0.05 * np.sin(np.arange(40))
    wild_c = 100.0 + np.cumsum(np.where(np.arange(40) % 2 == 0, 3.0, -2.8))
    r_calm = vol_target_risk_usd(calm_c, "H4", 5.5, 5.0, 6.0)
    r_wild = vol_target_risk_usd(wild_c, "H4", 5.5, 5.0, 6.0)
    ok_s8 = (5.0 <= r_wild <= r_calm <= 6.0)
    print(f"  calm=${r_calm:.2f}, high_vol=${r_wild:.2f} -> {'OK' if ok_s8 else 'FAIL'}")
    if not ok_s8:
        failures.append("s8_vol_target")

    # cleanup selftest artifacts
    for f in ("selftest_journal.csv", "selftest_state.json"):
        p = os.path.join(DATA_DIR, f)
        if os.path.exists(p):
            os.remove(p)

    print("\n" + ("SELFTEST: PASS" if not failures else f"SELFTEST: FAIL — {failures}"))
    return not failures


# ================================================================== CLI =====
def main():
    ap = argparse.ArgumentParser(description="unified multi-strategy MT5 pilot")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry", action="store_true", help="full pipeline + journal, no orders (terminal attached)")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--i-understand-live-risk", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--loop-seconds", type=int, default=None)
    args = ap.parse_args()

    cfg = load_resolved_config(CONFIG_PATH)
    if args.selftest:
        sys.exit(0 if selftest() else 1)
    if args.status:
        jp = os.path.join(ROOT, cfg["runtime"]["journal_csv"])
        sp = os.path.join(ROOT, cfg["runtime"]["state_json"])
        print("journal:", jp, os.path.getsize(jp) if os.path.exists(jp) else "(empty)")
        print("state:", sp, os.path.getsize(sp) if os.path.exists(sp) else "(none)")
        if os.path.exists(jp):
            with open(jp) as f:
                rows = list(csv.reader(f))
            print(f"journal rows: {max(0, len(rows) - 1)}; last 5:")
            for r in rows[-5:]:
                print("   ", r)
        return
    if args.live and not args.i_understand_live_risk:
        print("--live requires --i-understand-live-risk")
        sys.exit(2)
    mode = "live" if args.live else "dry"
    run_loop(cfg, mode, args.loop_seconds)


if __name__ == "__main__":
    main()

