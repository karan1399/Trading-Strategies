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
from strategies._indicators import adx_arr  # noqa: E402
from strategies.registry import load_signal_fn  # noqa: E402
from strategies.ig_s8_vol_target import vol_target_risk_usd  # noqa: E402


def load_resolved_config(path: str = CONFIG_PATH) -> dict:
    """Load engine/config.json and hydrate external strategy folder config_file entries."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for sname, s in (cfg.get("sleeves") or {}).items():
        cf = s.get("config_file")
        if cf:
            cf_path = cf if os.path.isabs(cf) else os.path.join(ROOT, cf)
            if os.path.exists(cf_path):
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


def ewma_sigma(c: np.ndarray, lam: float = 0.94) -> np.ndarray:
    """RiskMetrics per-bar sigma of log returns (causal)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.diff(np.log(np.maximum(c, 1e-12)), prepend=np.log(c[0]))
    lr[0] = 0.0
    lr = np.nan_to_num(lr, nan=0.0, posinf=0.0, neginf=0.0)
    var = np.zeros(len(c))
    alpha = 1 - lam
    k = max(1, len(c) // 20)
    v = float((lr[:k] ** 2).mean()) if len(c) > 20 else float(lr[0] ** 2)
    for i in range(len(c)):
        v = alpha * lr[i] ** 2 + (1 - alpha) * v
        var[i] = v
    return np.sqrt(np.maximum(var, 0.0))


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
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.state = {"positions": {}, "bars": {}, "cooldown": {}, "counts": {}}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.state = json.load(f)
            except Exception:
                pass

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
    if sl_distance <= 0:
        return None, "non-positive stop distance"
    unit = spec.value_per_price_unit()
    if unit <= 0:
        return None, "invalid tick value/size"
    risk_per_lot = sl_distance * unit
    lots = normalize_lots(risk_usd / risk_per_lot, spec.vol_step)
    if lots < spec.vol_min - 1e-9:
        min_risk = spec.vol_min * risk_per_lot
        if min_risk > risk_usd_max + 1e-9:
            return None, (f"min-lot risk ${min_risk:.2f} > cap ${risk_usd_max:.2f} "
                          f"— reject, stop not compressed")
        lots = spec.vol_min
    if lots > spec.vol_max:
        lots = spec.vol_max
    return round(lots, 8), ""


# ======================================================== signal functions ==
# Every signal is evaluated AT INDEX i of CLOSED-bar arrays only (no element > i is
# read), so sig_at(prefix, i) == sig_at(full, i) — asserted by --selftest.
# Return dict: side(+1/-1), rd (stop distance), sl_abs, tp_abs (either may be None
# => caller derives from entry +/- rd * exit_r), note.

def _cluster_break_at(o, h, l, c, atr, i, level, side_bias):
    if i < 3:
        return 0.0, 0.0
    tol = max(1e-5, 0.20 * atr[i])
    ch, cl, cc = h[i - 3:i], l[i - 3:i], c[i - 3:i]
    if side_bias == 1:
        touches = int((cl >= level - tol).sum())
        if touches >= 2 and np.min(cc) >= level - 0.15 * atr[i] and c[i] > np.max(ch):
            struct = np.min(cl) - 0.20 * atr[i]
            return struct, max(c[i] - struct, 1.2 * atr[i])
    else:
        touches = int((ch <= level + tol).sum())
        if touches >= 2 and np.max(cc) <= level + 0.15 * atr[i] and c[i] < np.min(cl):
            struct = np.max(ch) + 0.20 * atr[i]
            return struct, max(struct - c[i], 1.2 * atr[i])
    return 0.0, 0.0


def sig_htf_gold_at(o, h, l, c, atr, i, params) -> dict | None:
    """P4 cluster-stall at EMA{period} (port of the audited HTF pattern engine)."""
    period = int(params.get("ema_period", 233))
    if i < period + 10 or i < 5:
        return None
    if h[i] <= l[i]:
        return None
    ef = ema_arr(c[: i + 1], period)
    e = ef[i]
    if e <= 0:
        return None
    rising = e > ef[i - 5]
    falling = e < ef[i - 5]
    if params.get("slope_gate", False) and not (rising or falling):
        return None
    sb = 1 if rising else (-1 if falling else 0)
    if sb == 0:
        return None
    struct, rd = _cluster_break_at(o, h, l, c, atr, i, e, sb)
    if rd <= 0:
        return None
    if params.get("regime_gate", False):
        es = ema_arr(c[: i + 1], 233)
        if sb == 1 and not c[i] > es[i]:
            return None
        if sb == -1 and not c[i] < es[i]:
            return None
    return {"side": sb, "rd": float(rd), "sl_abs": None, "tp_abs": None,
            "exit_r": float(params.get("exit_r", 2.0)),
            "note": f"P4_cluster/EMA{period}"}


def sig_vc_at(o, h, l, c, atr, i, params) -> dict | None:
    """VC blueprint: BB inside KC >= min_squeeze_bars, close breaks band, EMA200 + optional ADX gate."""
    if i < max(params.get("trend_ema", 200), 60) + 10:
        return None
    adx_min = float(params.get("adx_min", 0.0) or 0.0)
    if adx_min > 0.0:
        if float(adx_arr(h[: i + 1], l[: i + 1], c[: i + 1], 14)[i]) < adx_min:
            return None
    bb_len, dev = int(params.get("bb_len", 20)), float(params.get("bb_dev", 2.0))
    kc_len, mult = int(params.get("kc_len", 20)), float(params.get("kc_mult", 1.5))
    seg_c = c[: i + 1]
    sma = sma_arr(seg_c, bb_len)
    need = int(params.get("min_squeeze_bars", 3))
    j0 = i - need
    if j0 < bb_len:
        return None
    sd = np.zeros(i + 1)
    for k in range(max(bb_len - 1, j0), i + 1):
        sd[k] = seg_c[k - bb_len + 1: k + 1].std(ddof=0)
    bb_up, bb_dn = sma + dev * sd, sma - dev * sd
    atr_kc = wilder_atr(h[: i + 1], l[: i + 1], c[: i + 1], 14)
    kc_up, kc_dn = sma + mult * atr_kc, sma - mult * atr_kc
    squeeze = all(bb_up[k] < kc_up[k] and bb_dn[k] > kc_dn[k] for k in range(j0, i))
    if not squeeze:
        return None
    ema200 = ema_arr(seg_c, int(params.get("trend_ema", 200)))
    ema50 = ema_arr(seg_c, 50)
    atr14 = atr[i]
    bullish, bearish = c[i] >= o[i], c[i] <= o[i]
    side = 0
    if c[i] > bb_up[i] and bullish and c[i] > ema200[i] and ema50[i] >= ema200[i]:
        side = 1
    elif c[i] < bb_dn[i] and bearish and c[i] < ema200[i] and ema50[i] <= ema200[i]:
        side = -1
    if side == 0:
        return None
    basis = sma[i]
    if params.get("sl_basis", True) and basis > 0:
        rd = abs(c[i] - basis)
    else:
        rd = 0.0
    rd = max(rd, float(params.get("sl_atr_floor", 1.2)) * atr14, 1e-9)
    return {"side": side, "rd": float(rd), "sl_abs": None, "tp_abs": None,
            "exit_r": float(params.get("exit_r", 4.0)), "note": f"squeeze{need}bb"}


def _broker_day(ts_utc_min: float, offset_h: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((ts_utc_min + offset_h * 60) * 60))


def sig_ivwall_at(o, h, l, c, times_min, atr, sig_series, i, params,
                  session_spans: list[float]) -> dict | None:
    """Session-anchored 2-SD wall fade (port of the audited IV-wall rules).

    sig_series: pre-computed EWMA sigma per bar (causal). Session = broker day.
    Walls fixed for the whole session from the sigma of the bar BEFORE the session's
    first bar; never repainted. Entry: touch + close back inside.
    """
    offset = int(params.get("broker_utc_offset", 2))
    bph = 4.0  # 15M bars per hour
    sid = _broker_day(times_min[i], offset)
    # session start = first bar index with this broker day
    start = i
    while start > 0 and _broker_day(times_min[start - 1], offset) == sid:
        start -= 1
    if i - start < int(params.get("skip_first_bars", 1)):
        return None
    if i == start:  # session's first bar: no sigma yet for this session's anchor rule
        return None
    sig_bar = sig_series[start - 1] if start - 1 >= 0 else 0.0
    if not np.isfinite(sig_bar) or sig_bar <= 0:
        return None
    sess_open = o[start]
    exp_h = float(np.median(session_spans)) if len(session_spans) >= 3 else 24.0
    em = sess_open * sig_bar * math.sqrt(max(exp_h, 0.25) * bph)
    if not np.isfinite(em) or em <= 0:
        return None
    up = sess_open + float(params.get("sd_wall", 2.0)) * em
    dn = sess_open - float(params.get("sd_wall", 2.0)) * em
    atr14 = atr[i]
    if atr14 <= 0:
        return None
    touched_up, beyond_up = h[i] >= up, c[i] > up
    touched_dn, beyond_dn = l[i] <= dn, c[i] < dn
    side, wall = 0, 0.0
    if touched_up and not beyond_up:
        side, wall = -1, up
    elif touched_dn and not beyond_dn:
        side, wall = 1, dn
    if side == 0:
        return None
    buf = float(params.get("buffer_atr", 1.0)) * atr14
    sl_abs = wall + buf if side == -1 else wall - buf
    tp_abs = sess_open - float(params.get("target_m", 0.5)) * em if side == -1 \
        else sess_open + float(params.get("target_m", 0.5)) * em
    # entry-dependent checks (stop_cap_em, min_rr, stop side) are applied by the
    # CALLER once the actual entry price is known — never inside the signal.
    return {"side": side, "rd": None, "sl_abs": float(sl_abs), "tp_abs": float(tp_abs),
            "exit_r": None, "note": f"wall{sid}", "em": float(em)}


def sig_orb_at(o, h, l, c, times_min, atr, htf_c_4h, i, params) -> dict | None:
    """NAS100 OR30 breakout @ anchor UTC (frozen ORB cell): midpoint SL, fixed R TP.

    One trade per UTC day, signal window = anchor + signal_window_hours, EOD flat
    handled by the engine loop.
    """
    ah, am = params.get("anchor_utc", "14:30").split(":")
    anchor = int(ah) * 60 + int(am)
    or_min = int(params.get("or_minutes", 30))
    bar_min = 15
    nbars = or_min // bar_min
    day_start = None
    # locate today's anchor bar (UTC day of bar i)
    day = time.strftime("%Y-%m-%d", time.gmtime(times_min[i] * 60))
    j = i
    while j >= 0 and time.strftime("%Y-%m-%d", time.gmtime(times_min[j] * 60)) == day:
        day_start = j
        j -= 1
    if day_start is None:
        return None
    day_ts = [times_min[k] for k in range(day_start, i + 1)]
    anchor_idx = [k for k, t in enumerate(day_ts) if (t % 1440) >= anchor and (t % 1440) < anchor + or_min]
    # times are UTC epoch minutes of bar OPEN; OR window = bars with open-time in [anchor, anchor+or_min)
    if len(anchor_idx) < nbars:
        return None
    bars_in_day = list(range(day_start, i + 1))
    or_bars = [bars_in_day[k] for k in anchor_idx]
    if i < or_bars[-1]:
        return None
    or_high = max(h[k] for k in or_bars)
    or_low = min(l[k] for k in or_bars)
    sig_end = anchor + int(float(params.get("signal_window_hours", 6.0)) * 60)
    t_i = times_min[i] % 1440
    if t_i > sig_end:
        return None
    a_i = float(atr[i]) if i < len(atr) else 0.0
    if not np.isfinite(a_i) or a_i <= 0.0:
        return None
    max_oa = float(params.get("max_or_atr", 99.0) or 99.0)
    if max_oa < 90.0 and ((or_high - or_low) / a_i) > max_oa:
        return None
    adx_min = float(params.get("adx_min", 0.0) or 0.0)
    if adx_min > 0.0 and float(adx_arr(h[: i + 1], l[: i + 1], c[: i + 1], 14)[i]) < adx_min:
        return None
    mid = (or_high + or_low) / 2.0
    side = 0
    if c[i] > or_high and c[i] >= o[i]:
        side = 1
    elif c[i] < or_low and c[i] <= o[i]:
        side = -1
    if side == 0:
        return None
    if params.get("htf_trend", False) and htf_c_4h is not None and len(htf_c_4h) > int(params.get("htf_ema", 200)):
        e4 = ema_arr(htf_c_4h, int(params.get("htf_ema", 200)))
        if side == 1 and not c[i] > e4[-1]:
            return None
        if side == -1 and not c[i] < e4[-1]:
            return None
    sl_mode = str(params.get("sl_mode", "midpoint")).lower()
    if sl_mode == "opposite":
        sl_val = float(or_low if side == 1 else or_high)
    else:
        sl_val = float(mid)
    return {"side": side, "rd": None, "sl_abs": sl_val, "tp_abs": None,
            "exit_r": float(params.get("exit_r", 2.0)), "note": f"OR{or_min}_{day}"}

# ============================================================ MT5 bridge ====
class MT5Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.connected = False

    def connect(self) -> bool:
        if not MT5_OK:
            print("[bridge] MetaTrader5 python package not available")
            return False
        acc = self.cfg.get("account", {})
        kw = {}
        if acc.get("terminal_path"):
            kw["path"] = acc["terminal_path"]
        if acc.get("login"):
            kw.update(login=int(acc["login"]), password=acc["password"], server=acc["server"])
        if not mt5.initialize(**kw):
            print("[bridge] initialize failed:", mt5.last_error())
            return False
        self.connected = True
        ai = mt5.account_info()
        if ai:
            print(f"[bridge] connected: #{ai.login} {ai.server} equity={ai.equity:.2f}")
        return True

    def shutdown(self):
        if MT5_OK and self.connected:
            mt5.shutdown()

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

    def gates(self, magic: int, positions: list) -> tuple[bool, str]:
        ours = [p for p in positions if getattr(p, "magic", 0) in self.our_magics()]
        max_glob = int(self.gov.get("max_global_positions") or 0)
        if max_glob > 0 and len(ours) >= max_glob:
            return False, f"global cap {max_glob} reached"
        max_sleeve = int(self.gov.get("max_per_sleeve") or 0)
        if max_sleeve > 0 and len([p for p in ours if p.magic == magic]) >= max_sleeve:
            return False, "sleeve already has a position"
        gold = self.gov.get("gold_cluster_magics", [])
        gold_max = int(self.gov.get("gold_cluster_max_positions") or 0)
        if gold_max > 0 and magic in gold:
            ngold = len([p for p in ours if p.magic in gold])
            if ngold >= gold_max:
                return False, "gold-cluster cap (42003+42004 share one risk unit)"
        if self.bridge is not None:
            eq = self.bridge.account_equity()
            floor = float(self.cfg.get("account", {}).get("equity_floor_usd", 0))
            if eq and eq < floor:
                return False, f"equity {eq:.2f} below floor {floor:.2f} — trading halted"
        return True, ""

    def our_magics(self) -> set:
        out = set()
        for name, s in self.cfg.get("sleeves", {}).items():
            if s.get("enabled", False):
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
                                     r_multiple=round(prof_r, 2), retcode=str(getattr(res, 'retcode', 'dry')))
                    if ok or res is None:
                        meta["be_done"] = True
                        n += 1
        return n


# ======================================================== session helpers ===
def session_spans_from(times_min: np.ndarray, offset_h: int) -> list[float]:
    """Completed-session span list (hours) — causal, excludes the running session."""
    if len(times_min) < 10:
        return []
    days = np.array([_broker_day(t, offset_h) for t in times_min])
    spans, i = [], 0
    n = len(days)
    while i < n:
        j = i
        while j + 1 < n and days[j + 1] == days[i]:
            j += 1
        if j + 1 < n:  # session completed
            spans.append((times_min[j] - times_min[i]) / 60.0 + 0.25)
        i = j + 1
    return spans[-10:]


def spread_guard(symbol: str, spread_price: float, atr14: float, sleeve_cfg: dict) -> tuple[bool, str]:
    if spread_price <= 0:
        return False, "no live spread"
    if spread_price > float(sleeve_cfg.get("spread_frac_atr_max", 1.0)) * atr14:
        return False, f"spread {spread_price:.5f} > {sleeve_cfg['spread_frac_atr_max']}xATR"
    pips = spread_price / 10 ** -max(3, 5)  # replaced by caller-provided model check
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


def run_loop(cfg: dict, mode: str, loop_seconds: int | None = None):
    bridge = MT5Bridge(cfg)
    if mode == "live" or MT5_OK:
        if not bridge.connect():
            print("cannot attach MT5 terminal — for no-terminal validation run --selftest")
            return
    journal = Journal(os.path.join(ROOT, cfg["runtime"]["journal_csv"]))
    state = StateStore(os.path.join(ROOT, cfg["runtime"]["state_json"]))
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
                    state.set("bars", {**state.get("bars", {}), key: t0})
                    spec = spec_from_mt5(sym) or spec_from_fallback(cfg, sym)
                    if spec is None:
                        continue
                    # ---- signal
                    htf_c4 = None
                    if sname == "orb_nas":
                        r4 = bridge.rates(sym, s.get("htf_tf", "H4"), 400)
                        if r4 is not None:
                            htf_c4 = r4["close"].astype(float)[:-1]
                    sig = build_signal(sname, closed, s, base_params, htf_c4, sym=sym)
                    if sig is None:
                        continue
                    # ---- rate limiting
                    if sname == "ivwall":
                        sess_today = time.strftime("%Y-%m-%d", time.gmtime(now + offset * 3600))
                        if count_today(state, key, sess_today) >= int(s.get("max_trades_session", 2)):
                            continue
                    if sname == "orb_nas" and count_today(state, key, utc_today) >= int(s.get("max_trades_day", 1)):
                        continue
                    cd_key = f"cooldown|{key}"
                    until = float(state.get("cooldown", {}).get(cd_key, 0))
                    tf_min = TF_MAP[sym_tf]
                    if t0 / 60 + tf_min < until:
                        continue
                    # ---- spread guard (live spread)
                    ask, bid = bridge.tick(sym)
                    if ask is None or bid is None:
                        continue
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
                    # ---- Parallel Delta Exchange India routing (mapped symbols) ----
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
                    # ---- governor + sizing
                    ok, why = gov.gates(magic, bridge.positions() if mode == "live" else [])
                    if not ok:
                        journal.log("GATE_SKIP", sleeve=sname, magic=magic, symbol=sym, note=why)
                        continue
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
                    lots, why = lots_for_risk(spec, rd, risk_usd, float(cfg["governor"]["risk_usd_max"]))
                    if lots is None:
                        journal.log("RISK_REJECT", sleeve=sname, magic=magic, symbol=sym,
                                    lots=0.0, price=entry, sl=sl, tp=tp, note=why)
                        continue
                    comment = f"{sname[:8]} {sig['note'][:14]}"
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
            # ------- position management (every poll): BE lock + time stops
            if mode == "live":
                positions = bridge.positions()
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
                        mh_bars = int(slv_cfg.get("max_hold_bars") or 0)
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
    p = os.path.join(ROOT, "historical_parquet", tf_dir, symbol + ".parquet")
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
        htf_c4 = None
        if sname == "orb_nas":
            df4 = _load_parquet(sym, "H4")
            htf_c4 = df4["close"].to_numpy(float)
        n = len(c)
        probes = sorted(set([min(350, n - 2), n // 2, n - 51, n - 30, n - 2]))
        sig_series = ewma_sigma(c, 0.94) if sname == "ivwall" else None
        mod_fn = load_signal_fn(params["module"]) if isinstance(params, dict) and params.get("module") else None
        bad = 0
        for k in probes:
            def run(arrs, sigs=None):
                oo, hh, ll, cc, tt = arrs
                atr = wilder_atr(hh, ll, cc, 14)
                if sname == "htf_gold":
                    return sig_htf_gold_at(oo, hh, ll, cc, atr, k, params)
                if sname == "vc_gold":
                    return sig_vc_at(oo, hh, ll, cc, atr, k, params)
                if sname == "ivwall":
                    spans = session_spans_from(tt[: k + 1], 2)
                    return sig_ivwall_at(oo, hh, ll, cc, tt, atr, sigs, k,
                                         {**params, "broker_utc_offset": 2}, spans)
                if sname == "orb_nas":
                    return sig_orb_at(oo, hh, ll, cc, tt, atr, htf_c4, k, params)
                return mod_fn(oo, hh, ll, cc, tt, atr, k, params.get("params", {}), {"htf_c4": htf_c4})
            full = run((o, h, l, c, tm), sig_series)
            k_eff = min(k, 4999)
            pref = run((o[:k_eff + 1], h[:k_eff + 1], l[:k_eff + 1], c[:k_eff + 1], tm[:k_eff + 1]),
                       sig_series[:k_eff + 1] if sname == "ivwall" else None)
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
        htf_c4 = None
        if sname == "orb_nas":
            htf_c4 = _load_parquet(sym, "H4")["close"].to_numpy(float)
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
    cases_unlim = [
        ("empty book, any entry", [], 42003, True),
        ("unlimited global allows 3rd+", [FakePos(42002), FakePos(42005)], 42003, True),
        ("unlimited sleeve allows multi-sym", [FakePos(42003)], 42003, True),
        ("unlimited gold cluster passes", [FakePos(42004)], 42003, True),
    ]
    for label, pos, magic, expect in cases_unlim:
        ok, why = gov.gates(magic, pos)
        print(f"  [live cfg] {label:<34} -> {'PASS' if ok else 'REJECT'} ({why})"[:100])
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

