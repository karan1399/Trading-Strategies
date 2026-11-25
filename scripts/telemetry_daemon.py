#!/usr/bin/env python3
"""
telemetry_daemon.py — Fast Multi-Tiered Order-Flow & MT5 Full-Universe Telemetry Daemon.

Covers:
 1. ALL Coinglass / Terminal perp assets in data/telemetry/assets/ + every MT5-mapped crypto &
    commodity/metal (so zero files sit stale across restarts or narrow watchlists).
 2. ALL remaining visible MetaTrader 5 assets (Forex_Raw, Indices, Base Metals, Metal Crosses)
    computed directly from live MT5 ticks + M15/H1/H4/D1 OHLCV (EMAs 13/89/233, RSI-14, ATR-14,
    Day VWAP ±1/2/3σ, Session/Day/Week/Month Value Areas, plus Classic/COT/CNBC terminal overlays).
 3. Generates the 4 authoritative datasets + universe_overview.json, sorts stale/missing files first
    on startup, reuses keep-alive HTTP sockets with cache-aware endpoint pacing to avoid Coinglass
    MAX_CG_CONCURRENCY queue timeouts, and pushes git diffs in a non-blocking throttled worker.
"""

import glob
import http.client
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except Exception:
    mt5 = None
    MT5_AVAILABLE = False

BASE_URL = os.environ.get("TERMINAL_BASE_URL", "http://127.0.0.1:3000")
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
TELEMETRY_DIR = os.path.join("data", "telemetry")
ASSETS_DIR = os.path.join(TELEMETRY_DIR, "assets")
FOOTPRINT_DIR = os.path.join(TELEMETRY_DIR, "footprint")

# Tier-1 Prime Assets: full 16-parameter harvest every cycle (30 Crypto + Core TradFi + MT5 Metals/Energy + Top Alts)
PRIME_CURATED_UNIVERSE = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR",
    "SUI", "LTC", "BCH", "TIA", "APT", "ARB", "OP", "INJ", "SEI", "DOT",
    "ATOM", "UNI", "FIL", "ICP", "HBAR", "VET", "TRX", "1000PEPE", "1000SHIB",
    "XAU", "XAG", "CL", "UKO", "NATGAS", "XPT", "XPD", "COPPER",
    "NVDA", "TSLA", "MSTR", "QQQ", "GOOGL", "AAPL", "MSFT", "AMZN",
    "AAVE", "XMR", "ZEC", "ETC", "CRV", "ALGO", "GRT",
]

DEEP_MATRIX_SYMBOLS = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR", "XAU", "NVDA", "CL", "XAG"
]

# Explicit verified mapping: live Blueberry MT5 symbol -> Coinglass / Terminal base symbol
MT5_TO_COINGLASS = {
    "ADAUSD.p": "ADA", "BCHUSD.p": "BCH", "LNKUSD.p": "LINK", "XLMUSD.p": "XLM",
    "AVXUSD.p": "AVAX", "DOGUSD.p": "DOGE", "FILUSD.p": "FIL", "GRTUSD.p": "GRT",
    "NERUSD.p": "NEAR", "SOLUSD.p": "SOL", "TRXUSD.p": "TRX", "UNIUSD.p": "UNI",
    "VETUSD.p": "VET", "XMRUSD.p": "XMR", "BNBUSD.p": "BNB", "AVEUSD.p": "AAVE",
    "ALGUSD.p": "ALGO", "ATMUSD.p": "ATOM", "AXSUSD.p": "AXS", "CHZUSD.p": "CHZ",
    "COMUSD.p": "COMP", "EGLUSD.p": "EGLD", "FLWUSD.p": "FLOW", "IOTUSD.p": "IOTA",
    "KSMUSD.p": "KSM", "NEOUSD.p": "NEO", "XSIUSD.p": "SUSHI", "THTUSD.p": "THETA",
    "XTZUSD.p": "XTZ", "ZECUSD.p": "ZEC", "MANUSD.p": "MANA", "INCUSD.p": "1INCH",
    "ARWUSD.p": "AR", "BATUSD.p": "BAT", "CHRUSD.p": "CHR", "CRVUSD.p": "CRV",
    "ENJUSD.p": "ENJ", "BARUSD.p": "HBAR", "KAVUSD.p": "KAVA", "KNCUSD.p": "KNC",
    "ONTUSD.p": "ONT", "QTMUSD.p": "QTUM", "SNDUSD.p": "SAND", "SKLUSD.p": "SKL",
    "SNXUSD.p": "SNX", "YFIUSD.p": "YFI", "ZILUSD.p": "ZIL", "ZRXUSD.p": "ZRX",
    "DSHUSD.p": "DASH", "BTCUSD.pi": "BTC", "DOTUSD.pi": "DOT", "ETHUSD.pi": "ETH",
    "LTCUSD.pi": "LTC", "XRPUSD.pi": "XRP",
    "XAUUSD.pi": "XAU", "XAGUSD.pi": "XAG",
    "USWTI.p": "CL", "UKBRENT.p": "UKO", "GAS.p": "NATGAS",
    "XPTUSD.pi": "XPT", "XPDUSD.pi": "XPD", "COPPER.p": "COPPER",
}

# MT5 Index CFD stem -> terminal /api/cg/classic & /api/cg/cot symbol
CLASSIC_OVERLAY_MAP = {
    "SP500": "SPX500", "SPX500": "SPX500",
    "NAS100": "NDX100", "NDX100": "NDX100",
    "DJ30": "US30", "GER40": "GER30", "UK100": "UK100",
    "FR40": "FRA40", "STOXX50": "EUSTX50", "SWISS20": "SWI20",
    "NETH25": "NTH25", "JP225": "JP225", "HK50": "HK50",
    "AU200": "AUS200", "US2000": "US2000",
}

_PARSED_URL = urllib.parse.urlparse(BASE_URL)
_HTTP_HOST = _PARSED_URL.hostname or "127.0.0.1"
_HTTP_PORT = _PARSED_URL.port or (443 if _PARSED_URL.scheme == "https" else 3000)
_THREAD_LOCAL = threading.local()

_CACHE_LOCK = threading.Lock()
_BACKTEST_CACHE = {}   # sym -> (ts, data)
_COT_CACHE = {}        # sym -> (ts, data)
_STOPMAP_CACHE = {}    # (sym, mode, rng) -> (ts, data)
_LIQ_CACHE = {}        # (sym, rng) -> (ts, data)
_BW_CACHE = {}         # (sym, mkt_type, itv) -> (ts, data)
_CVD_CACHE = {}        # (sym, mode, itv) -> (ts, data)
_LO_CACHE = {}         # sym -> (ts, data)
_LTO_CACHE = {}        # sym -> (ts, data)
_PERPDEPTH_CACHE = {}  # sym -> (ts, data)
_FP_SIGNALS_CACHE = {} # sym -> (ts, fp_dict, all_sigs)
_FP_BARS_CACHE = {}    # (sym, tf) -> (ts, bars_dict, rows, cum_delta)
_CLASSIC_CACHE = {}    # sym -> (ts, data)
_GIT_LOCK = threading.Lock()
_LAST_GIT_SYNC = 0.0


def _get_conn():
    conn = getattr(_THREAD_LOCAL, "conn", None)
    if conn is None:
        conn = http.client.HTTPConnection(_HTTP_HOST, _HTTP_PORT, timeout=15)
        _THREAD_LOCAL.conn = conn
    return conn


def _reset_conn():
    conn = getattr(_THREAD_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _THREAD_LOCAL.conn = None


def fetch_json(path, timeout=12, retries=1):
    """Persistent HTTP/1.1 Keep-Alive JSON GET with automatic socket & transient 502/503 recovery."""
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            conn = _get_conn()
            conn.timeout = timeout
            conn.request("GET", path, headers={"User-Agent": "Antigravity-TelemetryEngine/4.0", "Connection": "keep-alive"})
            res = conn.getresponse()
            raw = res.read()
            if res.status == 200:
                return json.loads(raw.decode("utf-8"))
            if res.status in (429, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            return {"_err": f"HTTP {res.status}"}
        except Exception as e:
            _reset_conn()
            if attempt + 1 < attempts:
                time.sleep(0.35 * (attempt + 1))
                continue
            return {"_err": str(e)}
    return {"_err": "failed"}


def atomic_write(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_path = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, filepath)


def _mt5_base_sym(name):
    return re.sub(r"\.(pi|p|i|pro|raw|m)$", "", name, flags=re.I).upper()


def _ema_series(closes, period):
    if not closes:
        return None
    k = 2.0 / (period + 1.0)
    ema = float(closes[0])
    for price in closes[1:]:
        ema = float(price) * k + ema * (1.0 - k)
    return round(ema, 6)


def _wilder_rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = float(closes[i]) - float(closes[i - 1])
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _atr(bars, period=14):
    if len(bars) <= period:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = float(bars[i]["high"]), float(bars[i]["low"]), float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return round(atr_val, 6)


def _value_area(bars, n_bins=48):
    if bars is None or len(bars) == 0:
        return None
    lows = [float(b["low"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        px = float(bars[-1]["close"])
        return {"poc": px, "vah": px, "val": px, "high": hi, "low": lo, "volume": 0.0, "bins": 1}
    step = (hi - lo) / n_bins
    bins = [0.0] * n_bins
    for b in bars:
        tp = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        vol = max(float(b["tick_volume"]), 1.0)
        idx = min(n_bins - 1, max(0, int((tp - lo) / step)))
        bins[idx] += vol
    tot = sum(bins)
    if tot <= 0:
        return None
    poc_idx = max(range(n_bins), key=lambda i: bins[i])
    target = tot * 0.70
    acc = bins[poc_idx]
    lo_i = hi_i = poc_idx
    while acc < target and (lo_i > 0 or hi_i < n_bins - 1):
        v_lo = bins[lo_i - 1] if lo_i > 0 else -1.0
        v_hi = bins[hi_i + 1] if hi_i < n_bins - 1 else -1.0
        if v_hi >= v_lo:
            hi_i += 1
            acc += bins[hi_i]
        else:
            lo_i -= 1
            acc += bins[lo_i]
    return {
        "poc": round(lo + (poc_idx + 0.5) * step, 6),
        "vah": round(lo + (hi_i + 1.0) * step, 6),
        "val": round(lo + lo_i * step, 6),
        "high": round(hi, 6),
        "low": round(lo, 6),
        "volume": round(tot, 2),
        "bins": n_bins,
    }


def _day_vwap(bars_m15):
    if bars_m15 is None or len(bars_m15) == 0:
        return None
    now_utc = datetime.now(timezone.utc)
    day_start = int(datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp())
    day_bars = [b for b in bars_m15 if int(b["time"]) >= day_start]
    if not day_bars:
        day_bars = bars_m15[-min(len(bars_m15), 32):]
    sum_pv = 0.0
    sum_v = 0.0
    tps = []
    vols = []
    for b in day_bars:
        tp = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        v = max(float(b["tick_volume"]), 1.0)
        sum_pv += tp * v
        sum_v += v
        tps.append(tp)
        vols.append(v)
    if sum_v <= 0:
        return None
    vwap = sum_pv / sum_v
    var = sum(v * ((tp - vwap) ** 2) for tp, v in zip(tps, vols)) / sum_v
    sd1 = math.sqrt(max(var, 0.0))
    return {
        "anchorTs": int(day_bars[0]["time"]) * 1000,
        "vwap": round(vwap, 6),
        "bands": {
            "sd1": round(sd1, 6),
            "sd2": round(sd1 * 2.0, 6),
            "sd3": round(sd1 * 3.0, 6),
        },
        "open": round(float(day_bars[0]["open"]), 6),
        "high": round(max(float(b["high"]) for b in day_bars), 6),
        "low": round(min(float(b["low"]) for b in day_bars), 6),
        "bars": len(day_bars),
    }


def _build_mt5_quote_dict(sym_obj, tick_obj):
    if not sym_obj:
        return None
    bid = float(tick_obj.bid) if tick_obj and tick_obj.bid else None
    ask = float(tick_obj.ask) if tick_obj and tick_obj.ask else None
    mid = ((bid + ask) / 2.0) if (bid and ask) else (bid or ask)
    spread_pts = round((ask - bid) / sym_obj.point, 1) if (bid and ask and sym_obj.point) else None
    spread_bps = round(((ask - bid) / mid) * 10000.0, 2) if (bid and ask and mid and mid > 0) else None
    return {
        "symbol": sym_obj.name,
        "description": sym_obj.description,
        "category": sym_obj.path.split("\\")[0] if "\\" in sym_obj.path else sym_obj.path,
        "bid": bid,
        "ask": ask,
        "mid": round(mid, sym_obj.digits) if mid else None,
        "spread_pts": spread_pts,
        "spread_bps": spread_bps,
        "digits": sym_obj.digits,
        "point": sym_obj.point,
        "volume_min": sym_obj.volume_min,
        "volume_step": sym_obj.volume_step,
        "contract_size": sym_obj.trade_contract_size,
        "trade_mode": sym_obj.trade_mode,
        "updated_epoch": round(time.time(), 2),
    }


def harvest_mt5_layer():
    """Reads all visible MT5 symbols locally in one pass and computes OHLCV telemetry for MT5-native CFDs/FX."""
    mt5_quotes_by_cg = {}
    mt5_native_profiles = {}
    native_base_syms = set()
    meta_info = {"connected": False, "visible_count": 0, "inactive_skipped": []}

    if not MT5_AVAILABLE:
        return mt5_quotes_by_cg, mt5_native_profiles, native_base_syms, meta_info

    t0 = time.time()
    init_kwargs = {"timeout": 8000}
    if os.path.exists(MT5_TERMINAL_PATH):
        init_kwargs["path"] = MT5_TERMINAL_PATH
    if not mt5.initialize(**init_kwargs):
        meta_info["error"] = str(mt5.last_error())
        return mt5_quotes_by_cg, mt5_native_profiles, native_base_syms, meta_info

    meta_info["connected"] = True
    try:
        all_syms = mt5.symbols_get() or []
        visible_syms = [s for s in all_syms if s.visible]
        meta_info["visible_count"] = len(visible_syms)
        meta_info["inactive_skipped"] = [s.name for s in all_syms if not s.visible]

        # Fetch ticks & compute OHLCV for MT5-native instruments
        cg_matched_count = 0
        for s_obj in visible_syms:
            tk = mt5.symbol_info_tick(s_obj.name)
            q_dict = _build_mt5_quote_dict(s_obj, tk)
            if not q_dict:
                continue

            if s_obj.name in MT5_TO_COINGLASS:
                cg_sym = MT5_TO_COINGLASS[s_obj.name]
                mt5_quotes_by_cg[cg_sym] = q_dict
                cg_matched_count += 1
                continue

            base_sym = _mt5_base_sym(s_obj.name)
            # Avoid colliding if an MT5 CFD base matches a Coinglass key outside MT5_TO_COINGLASS
            native_base_syms.add(base_sym)
            mt5_quotes_by_cg[base_sym] = q_dict

            r_m15 = mt5.copy_rates_from_pos(s_obj.name, mt5.TIMEFRAME_M15, 0, 260)
            r_h1 = mt5.copy_rates_from_pos(s_obj.name, mt5.TIMEFRAME_H1, 0, 260)
            r_h4 = mt5.copy_rates_from_pos(s_obj.name, mt5.TIMEFRAME_H4, 0, 260)
            r_d1 = mt5.copy_rates_from_pos(s_obj.name, mt5.TIMEFRAME_D1, 0, 260)

            px = q_dict["mid"] or (float(r_m15[-1]["close"]) if (r_m15 is not None and len(r_m15)) else None)
            if not px or px <= 0:
                continue

            iso_now = datetime.now(timezone.utc).isoformat()
            closes_m15 = [float(b["close"]) for b in r_m15] if r_m15 is not None else []
            closes_h1 = [float(b["close"]) for b in r_h1] if r_h1 is not None else []
            closes_h4 = [float(b["close"]) for b in r_h4] if r_h4 is not None else []
            closes_d1 = [float(b["close"]) for b in r_d1] if r_d1 is not None else []

            prev_d_close = float(r_d1[-2]["close"]) if (r_d1 is not None and len(r_d1) >= 2) else px
            chg_day_pct = round(((px - prev_d_close) / prev_d_close) * 100.0, 4) if prev_d_close > 0 else 0.0
            day_hi = float(r_d1[-1]["high"]) if (r_d1 is not None and len(r_d1)) else px
            day_lo = float(r_d1[-1]["low"]) if (r_d1 is not None and len(r_d1)) else px

            emas = {}
            rsi_map = {}
            for tf_name, cl in [("m15", closes_m15), ("h1", closes_h1), ("h4", closes_h4), ("d1", closes_d1)]:
                if cl:
                    emas[tf_name] = {
                        "e13": _ema_series(cl, 13),
                        "e89": _ema_series(cl, 89),
                        "e233": _ema_series(cl, 233),
                    }
                    rsi_map[tf_name] = _wilder_rsi(cl, 14)

            day_auc = _value_area(list(r_m15[-96:]) if (r_m15 is not None and len(r_m15)) else None, 48)
            sess_auc = _value_area(list(r_m15[-32:]) if (r_m15 is not None and len(r_m15)) else None, 48)
            week_auc = _value_area(list(r_h1[-168:]) if (r_h1 is not None and len(r_h1)) else None, 60)
            month_auc = _value_area(list(r_h4[-180:]) if (r_h4 is not None and len(r_h4)) else None, 80)
            vwap_obj = _day_vwap(list(r_m15) if r_m15 is not None else None)
            m15_atr14 = _atr(list(r_m15) if r_m15 is not None else [], 14)

            e89_m15 = (emas.get("m15") or {}).get("e89")
            e233_m15 = (emas.get("m15") or {}).get("e233")
            e233_h1 = (emas.get("h1") or {}).get("e233")
            if e89_m15 and e233_m15 and e233_h1:
                if px < e89_m15 and px < e233_m15 and px < e233_h1:
                    regime, direction = "TREND_DOWN", "SHORT"
                elif px > e89_m15 and px > e233_m15 and px > e233_h1:
                    regime, direction = "TREND_UP", "LONG"
                else:
                    regime, direction = "RANGE_BALANCE", "FLAT"
            else:
                regime, direction = "UNKNOWN", "FLAT"

            classic_sym = CLASSIC_OVERLAY_MAP.get(base_sym)
            cot_data = None
            classic_meta = None
            if classic_sym:
                cot_data = _cached_get(_COT_CACHE, classic_sym, 3600, f"/api/cg/cot?sym={classic_sym}&quiet=1")
                cl_res = _cached_get(_CLASSIC_CACHE, classic_sym, 180, f"/api/cg/classic?sym={classic_sym}", timeout=8)
                if cl_res and cl_res.get("ok"):
                    classic_meta = {
                        "sym": classic_sym,
                        "sessionState": cl_res.get("sessionState"),
                        "price": cl_res.get("price"),
                        "chgPct": cl_res.get("chgPct"),
                        "liveBridge": cl_res.get("liveBridge"),
                    }

            profile = {
                "symbol": base_sym,
                "feed_type": "mt5_ohlcv",
                "updated_at": iso_now,
                "updated_epoch": time.time(),
                "ticker": {
                    "price": px,
                    "dir": "up" if chg_day_pct > 0 else ("down" if chg_day_pct < 0 else "flat"),
                    "source": "mt5_live",
                    "updatedAt": int(time.time() * 1000),
                },
                "mt5": q_dict,
                "market_structure": {
                    "price": {
                        "last": px,
                        "prevDayClose": round(prev_d_close, 6),
                        "chgDayPct": chg_day_pct,
                        "dayHigh": round(day_hi, 6),
                        "dayLow": round(day_lo, 6),
                        "m15Atr14": m15_atr14,
                    },
                    "auction": {
                        "session": sess_auc,
                        "day": day_auc,
                        "week": week_auc,
                        "month": month_auc,
                    },
                    "vwap": vwap_obj,
                    "emas": emas,
                    "rsi": rsi_map,
                },
                "narrative_engine": {
                    "regime": regime,
                    "activeModel": "MT5_TC09_EMA_VA",
                    "direction": direction,
                    "grade": "B" if direction in ("SHORT", "LONG") else "C",
                    "confidence": 65 if direction in ("SHORT", "LONG") else 40,
                    "thesis": f"MT5 native {q_dict['category']} ({s_obj.name}) {regime} | M15 ATR14={m15_atr14} | Spread={q_dict['spread_bps']}bps",
                },
                "athena_oracle": None,
                "microstructure_l2_l3": {"mt5_bbo": {"bid": q_dict["bid"], "ask": q_dict["ask"], "spread_bps": q_dict["spread_bps"]}},
                "quant_positioning": None,
                "options_deribit": None,
                "liquidations": {"clusters": {}, "stopmap": {}, "live_prints_tape": None},
                "book_walls": {"futures": {}, "spot": {}},
                "cvd": {"perp": {}, "spot": {}, "split": {}},
                "footprint": {"bars": {}, "aggregated_footprint": None, "signals": {}},
                "large_orders": {"resting_and_executed": {}, "large_taker_orders_100k": None},
                "playbook_cards": classic_meta,
                "backtest_metrics": None,
                "cot_report": cot_data,
                "funding_and_oi": {},
                "coverage_note": "MT5 native OHLCV feed (L2/perp order-flow N/A on broker FX/CFD)",
            }
            atomic_write(os.path.join(ASSETS_DIR, f"{base_sym}.json"), profile)
            mt5_native_profiles[base_sym] = profile

            # Also mirror NDX100/SPX500 classic curated slots when their MT5 equivalents update
            if base_sym == "NAS100":
                mirror = dict(profile, symbol="NDX100")
                atomic_write(os.path.join(ASSETS_DIR, "NDX100.json"), mirror)
                mt5_native_profiles["NDX100"] = mirror
                native_base_syms.add("NDX100")
            elif base_sym == "SP500":
                mirror = dict(profile, symbol="SPX500")
                atomic_write(os.path.join(ASSETS_DIR, "SPX500.json"), mirror)
                mt5_native_profiles["SPX500"] = mirror
                native_base_syms.add("SPX500")

        meta_info["mt5_native_written"] = len(mt5_native_profiles)
        meta_info["mt5_coinglass_matched"] = cg_matched_count
        meta_info["elapsed_s"] = round(time.time() - t0, 2)
    finally:
        mt5.shutdown()

    return mt5_quotes_by_cg, mt5_native_profiles, native_base_syms, meta_info


def _cached_get(cache_dict, key, ttl_secs, endpoint, timeout=10):
    now = time.time()
    hit = None
    if ttl_secs > 0:
        with _CACHE_LOCK:
            hit = cache_dict.get(key)
            if hit and (now - hit[0]) < ttl_secs:
                val = hit[1]
                return None if (val and val.get("_err")) else val
    res = fetch_json(endpoint, timeout=timeout)
    if res and not res.get("_err"):
        with _CACHE_LOCK:
            cache_dict[key] = (now, res)
        return res
    if ttl_secs > 0 and res and res.get("_err") in ("HTTP 400", "HTTP 503"):
        with _CACHE_LOCK:
            cache_dict[key] = (now, res)
    if hit and hit[1] and not hit[1].get("_err"):
        return hit[1]
    return None


def _process_fp_bars(sym, bars, tf_label, out_filename):
    sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
    os.makedirs(sym_fp_dir, exist_ok=True)
    summary_rows = []
    cum_delta = 0.0
    for b in bars:
        fp = b.get("fp")
        b_arr = fp.get("b", []) if fp else []
        s_arr = fp.get("s", []) if fp else []
        buy_v = sum(b_arr) if b_arr else 0.0
        sell_v = sum(s_arr) if s_arr else 0.0
        delta = buy_v - sell_v
        cum_delta += delta
        tot_v = buy_v + sell_v
        delta_pct = (delta / tot_v * 100.0) if tot_v > 0 else 0.0

        poc_p, max_v = None, 0.0
        top_bid = b_arr[-1] if b_arr else None
        top_ask = s_arr[-1] if s_arr else None
        bot_bid = b_arr[0] if b_arr else None
        bot_ask = s_arr[0] if s_arr else None
        finished_high = bool(b_arr and s_arr and len(b_arr) >= 2 and top_bid == 0 and top_ask > 0)
        finished_low = bool(b_arr and s_arr and len(b_arr) >= 2 and bot_ask == 0 and bot_bid > 0)

        if b_arr and s_arr:
            tick = fp.get("tick", 1)
            lo = fp.get("lo", b.get("l"))
            for i in range(min(len(b_arr), len(s_arr))):
                row_v = b_arr[i] + s_arr[i]
                if row_v > max_v:
                    max_v = row_v
                    poc_p = round(lo + i * tick, 4)

        summary_rows.append({
            "ts": b.get("ts"),
            "o": b.get("o"),
            "h": b.get("h"),
            "l": b.get("l"),
            "c": b.get("c"),
            "buy_vol": round(buy_v, 2),
            "sell_vol": round(sell_v, 2),
            "delta": round(delta, 2),
            "delta_pct": round(delta_pct, 2),
            "cum_delta": round(cum_delta, 2),
            "poc_price": poc_p,
            "poc_vol": round(max_v, 2),
            "top_bid": top_bid,
            "top_ask": top_ask,
            "bot_bid": bot_bid,
            "bot_ask": bot_ask,
            "finished_high": finished_high,
            "finished_low": finished_low,
        })

    atomic_write(os.path.join(sym_fp_dir, out_filename), {
        "sym": sym,
        "timeframe": tf_label,
        "days": 7,
        "total_bars": len(summary_rows),
        "start_time": summary_rows[0]["ts"] if summary_rows else None,
        "end_time": summary_rows[-1]["ts"] if summary_rows else None,
        "cum_delta_7d": round(cum_delta, 2),
        "rows": summary_rows,
    })
    return summary_rows, cum_delta


def harvest_single_asset(sym, is_prime=True, mt5_quote=None):
    t0 = time.time()
    iso_now = datetime.now(timezone.utc).isoformat()
    profile = {
        "symbol": sym,
        "feed_type": "coinglass_perp",
        "updated_at": iso_now,
        "updated_epoch": time.time(),
        "mt5": mt5_quote,
        "ticker": None,
        "market_structure": None,
        "narrative_engine": None,
        "athena_oracle": None,
        "microstructure_l2_l3": None,
        "quant_positioning": None,
        "options_deribit": None,
        "liquidations": {"clusters": {}, "stopmap": {}, "live_prints_tape": None},
        "book_walls": {"futures": {}, "spot": {}},
        "cvd": {"perp": {}, "spot": {}, "split": {}},
        "footprint": {"bars": {}, "aggregated_footprint": None, "signals": {}},
        "large_orders": {"resting_and_executed": {}, "large_taker_orders_100k": None},
        "playbook_cards": None,
        "backtest_metrics": None,
        "cot_report": None,
        "funding_and_oi": {},
    }

    # 1. TICKER & MARKET STRUCTURE (retried cleanly + deterministic fallback so prices never blank out)
    mkt = fetch_json(f"/api/cg/market?sym={sym}", timeout=10, retries=2)
    if mkt and mkt.get("ok"):
        profile["market_structure"] = {
            "price": mkt.get("price"),
            "auction": mkt.get("auction"),
            "vwap": mkt.get("vwap"),
            "emas": mkt.get("ema") or mkt.get("emas"),
            "rsi": mkt.get("rsi"),
            "levels": mkt.get("levels"),
        }

    tk = fetch_json(f"/api/cg/ticker?sym={sym}", timeout=8, retries=2)
    if tk and tk.get("ok") and tk.get("price"):
        profile["ticker"] = {
            "price": tk.get("price"),
            "dir": tk.get("dir"),
            "source": tk.get("source"),
            "updatedAt": tk.get("updatedAt"),
        }
    else:
        ms_px = (profile.get("market_structure") or {}).get("price")
        fallback_px = ms_px.get("last") if isinstance(ms_px, dict) else ms_px
        if fallback_px:
            profile["ticker"] = {"price": fallback_px, "dir": "flat", "source": "market_fallback", "updatedAt": int(time.time() * 1000)}
        elif mt5_quote and mt5_quote.get("mid"):
            profile["ticker"] = {"price": mt5_quote["mid"], "dir": "flat", "source": "mt5_fallback", "updatedAt": int(time.time() * 1000)}

    # 2. NARRATIVE ENGINE
    narr = fetch_json(f"/api/cg/narrative?sym={sym}", timeout=8)
    if narr and narr.get("ok"):
        profile["narrative_engine"] = {
            "regime": narr.get("regime"),
            "activeModel": narr.get("activeModel"),
            "direction": narr.get("direction"),
            "grade": narr.get("grade"),
            "confidence": narr.get("confidence"),
            "thesis": narr.get("thesis"),
            "plan": narr.get("plan"),
            "probabilities": {
                "pFill": narr.get("pFill"),
                "pT1": narr.get("pT1"),
                "pMeanRevert": narr.get("pMeanRevert"),
                "evR": narr.get("evR"),
                "bestR": narr.get("bestR"),
            },
        }

    # 3. ATHENA ORACLE (20s computation window so complex venues never silently time out)
    orc = fetch_json(f"/api/cg/oracle?sym={sym}", timeout=16)
    if orc and orc.get("ok"):
        profile["athena_oracle"] = {
            "score": orc.get("score"),
            "verdict": orc.get("verdict"),
            "regime": orc.get("regime"),
            "factors": orc.get("factors"),
            "partial": orc.get("partial"),
        }

    # 4. L2/L3 MICROSTRUCTURE & BBO (+ TradFi perpdepth when available)
    micro = fetch_json(f"/api/cg/micro?sym={sym}", timeout=8)
    if micro and micro.get("ok"):
        profile["microstructure_l2_l3"] = {
            "hl": micro.get("hl"),
            "lighter": micro.get("lighter"),
            "bbo": micro.get("bbo"),
            "missingVenues": micro.get("missingVenues"),
        }
    if sym in ("XAU", "XAG", "XPT", "SPX500", "SP500", "NDX100", "JP225", "HK50", "UKO", "USO", "NATGAS"):
        pd_res = _cached_get(_PERPDEPTH_CACHE, sym, 300, f"/api/cg/perpdepth?sym={sym}&quiet=1", timeout=8)
        if pd_res and pd_res.get("ok") and pd_res.get("supported", True):
            profile["microstructure_l2_l3"] = profile.get("microstructure_l2_l3") or {}
            profile["microstructure_l2_l3"]["tradfi_perpdepth"] = {
                "lead": pd_res.get("lead"),
                "liquidity": pd_res.get("liquidity"),
                "note": pd_res.get("note"),
            }

    # 5. QUANT POSITIONING
    qnt = fetch_json(f"/api/cg/quant?sym={sym}", timeout=8)
    if qnt and qnt.get("ok"):
        profile["quant_positioning"] = {
            "lsRatios": qnt.get("lsRatios"),
            "oiQuadrant": qnt.get("oiQuadrant"),
            "funding": qnt.get("funding"),
            "basis": qnt.get("basis"),
            "notes": qnt.get("notes"),
        }

    # 6. OPTIONS DERIBIT (BTC & ETH — preserve D13 GEX, Gamma Flip, MaxPainStrike & riskReversal25d)
    if sym in ("BTC", "ETH"):
        opt = fetch_json(f"/api/cg/options?sym={sym}", timeout=8)
        if opt and opt.get("ok"):
            rr25 = opt.get("riskReversal25d") or opt.get("skew")
            profile["options_deribit"] = {
                "dvol": opt.get("dvol"),
                "termStructure": opt.get("termStructure"),
                "riskReversal25d": rr25,
                "skew": rr25,
                "maxPain": opt.get("maxPain"),
                "maxPainStrike": opt.get("maxPainStrike"),
                "gammaFlipPrice": opt.get("gammaFlipPrice"),
                "gex": opt.get("gex"),
                "oiWalls": opt.get("oiWalls"),
                "putCallRatio": opt.get("putCallRatio"),
            }

    # 7. LIQUIDATIONS, STOPMAP (24h + 3d + 7d) & LIVE 5M TAPE (D11 & D16)
    ext_ttl = 0 if is_prime else 900
    liq_ranges = ["3d", "7d", "30d"]
    for rng in liq_ranges:
        l_res = _cached_get(_LIQ_CACHE, (sym, rng), ext_ttl, f"/api/cg/liqlevels?sym={sym}&range={rng}", timeout=8)
        if l_res and l_res.get("ok"):
            profile["liquidations"]["clusters"][rng] = {
                "stats": l_res.get("stats"),
                "venues": l_res.get("venues"),
                "livePrice": l_res.get("livePrice"),
            }

    sm_24_ttl = 0 if is_prime else 900
    sm_3d_ttl = 120 if is_prime else 900
    sm_7d_ttl = 300 if is_prime else 1800
    for rng, slot, ttl_s in [
        ("24h", "m1_coin_24h", sm_24_ttl),
        ("3d", "m1_coin_3d", sm_3d_ttl),
        ("7d", "m1_coin_7d", sm_7d_ttl),
    ]:
        sm = _cached_get(_STOPMAP_CACHE, (sym, "coin", rng), ttl_s, f"/api/cg/stopmap?sym={sym}&model=1&mode=coin&range={rng}", timeout=12)
        if sm and sm.get("ok"):
            sm_model = sm.get("model") or sm
            profile["liquidations"]["stopmap"][slot] = {
                "keyLevels": sm_model.get("keyLevels"),
                "overview": sm_model.get("overview"),
                "source": sm.get("source"),
                "stale": sm.get("stale"),
            }
    if sym == "BTC":
        sm_btc = _cached_get(_STOPMAP_CACHE, (sym, "pair", "24h"), 0, "/api/cg/stopmap?sym=BTC&model=1&mode=pair&range=24h", timeout=12)
        if sm_btc and sm_btc.get("ok"):
            sm_btc_model = sm_btc.get("model") or sm_btc
            profile["liquidations"]["stopmap"]["m1_pair_24h"] = {
                "keyLevels": sm_btc_model.get("keyLevels"),
                "overview": sm_btc_model.get("overview"),
                "source": sm_btc.get("source"),
                "stale": sm_btc.get("stale"),
            }

    ll = fetch_json(f"/api/cg/liq-live?sym={sym}", timeout=6)
    if ll and ll.get("ok"):
        ll_asset = ll.get("asset") or {}
        profile["liquidations"]["live_prints_tape"] = {
            "supported": ll_asset.get("supported", True),
            "stats1h": ll_asset.get("stats1h"),
            "stats24h": ll_asset.get("stats24h"),
            "events": (ll_asset.get("events") or [])[:25],
            "ageMs": ll.get("ageMs"),
            "stale": ll.get("stale"),
            "source": ll.get("source"),
        }

    # 8. BOOK WALLS (futures/spot m15/h1 + real-time Binance top-1000 /api/cg/livebook)
    bw_itvs = ["m15", "h1"]
    for mkt_type in ["futures", "spot"]:
        for itv in bw_itvs:
            bw = _cached_get(_BW_CACHE, (sym, mkt_type, itv), ext_ttl, f"/api/cg/bookwalls?sym={sym}&market={mkt_type}&interval={itv}&band=5", timeout=12)
            if bw and bw.get("ok"):
                bw_model = bw.get("model") or bw
                profile["book_walls"][mkt_type][itv] = {
                    "imbalance": bw_model.get("imbalance"),
                    "imbalance5mAgo": bw_model.get("imbalance5mAgo"),
                    "bidDepth": bw_model.get("bidDepth") or (bw_model.get("depth", {}).get("bidUsd") if isinstance(bw_model.get("depth"), dict) else None),
                    "askDepth": bw_model.get("askDepth") or (bw_model.get("depth", {}).get("askUsd") if isinstance(bw_model.get("depth"), dict) else None),
                    "largestBid": bw_model.get("largestBid"),
                    "largestAsk": bw_model.get("largestAsk"),
                    "walls": bw_model.get("walls", [])[:15],
                    "events": bw_model.get("events", [])[:10],
                }
    if is_prime:
        lb = fetch_json(f"/api/cg/livebook?sym={sym}", timeout=6)
        if lb and lb.get("ok"):
            lb_mod = lb.get("model") or {}
            profile["book_walls"]["live_binance"] = {
                "covered": lb.get("covered"),
                "imbalance": lb_mod.get("imbalance"),
                "imbalance5mAgo": lb_mod.get("imbalance5mAgo"),
                "bidDepth": lb_mod.get("bidDepth") or ((lb_mod.get("depth") or {}).get("bidUsd") if isinstance(lb_mod.get("depth"), dict) else None),
                "askDepth": lb_mod.get("askDepth") or ((lb_mod.get("depth") or {}).get("askUsd") if isinstance(lb_mod.get("depth"), dict) else None),
                "largestBid": lb_mod.get("largestBid"),
                "largestAsk": lb_mod.get("largestAsk"),
                "walls": (lb_mod.get("walls") or [])[:15],
                "fapiBanned": lb.get("fapiBanned"),
            }

    # 9. CVD (Prime: perp/spot/split on m15/h1; Extended: perp/spot on m15/h1 cached 900s)
    cvd_modes = ["perp", "spot", "split"] if is_prime else ["perp", "spot"]
    cvd_itvs = ["m15", "h1"]
    for cvd_mode in cvd_modes:
        for itv in cvd_itvs:
            cv = _cached_get(_CVD_CACHE, (sym, cvd_mode, itv), ext_ttl, f"/api/cg/cvd?sym={sym}&mode={cvd_mode}&itv={itv}&anchor=day", timeout=12)
            if cv and cv.get("ok"):
                profile["cvd"][cvd_mode][itv] = {
                    "kpis": cv.get("kpis"),
                    "divergences": cv.get("divergences", []),
                    "quadrant": cv.get("quadrant"),
                }

    # 10. FOOTPRINT BARS & SIGNALS (Prime: every cycle; Extended: cached 900s)
    fp_ttl = 0 if is_prime else 900
    now_t = time.time()
    for tf_code, tf_label, out_fn, prof_key, delta_key in [
        ("m15", "15m", "summary_7d.json", "history_7d", "cum_delta_7d"),
        ("h1", "1h", "summary_1h.json", "history_1h", "cum_delta_1h"),
    ]:
        cached_fp = None
        if fp_ttl > 0:
            with _CACHE_LOCK:
                hit = _FP_BARS_CACHE.get((sym, tf_code))
                if hit and (now_t - hit[0]) < fp_ttl:
                    cached_fp = hit[1]
        if cached_fp is not None:
            profile["footprint"][prof_key] = cached_fp
        else:
            fp_res = fetch_json(f"/api/cg/fp?sym={sym}&itv={tf_code}&limit=300", timeout=15)
            if fp_res and fp_res.get("ok") and fp_res.get("bars"):
                rows, c_delta = _process_fp_bars(sym, fp_res.get("bars", []), tf_label, out_fn)
                fp_meta = {
                    "bars_count": len(rows),
                    delta_key: round(c_delta, 2),
                    "summary_file": f"data/telemetry/footprint/{sym}/{out_fn}",
                    "recent_bars_tail": rows[-8:] if rows else [],
                }
                profile["footprint"][prof_key] = fp_meta
                with _CACHE_LOCK:
                    _FP_BARS_CACHE[(sym, tf_code)] = (now_t, fp_meta)

    # Single-call bt=all (already contains time, vol, and range sets — zero duplicates!)
    sig_ttl = 0 if is_prime else 900
    cached_sig = None
    if sig_ttl > 0:
        with _CACHE_LOCK:
            hit = _FP_SIGNALS_CACHE.get(sym)
            if hit and (now_t - hit[0]) < sig_ttl:
                cached_sig = hit[1]
    if cached_sig is not None:
        profile["footprint"]["signals"] = cached_sig
    else:
        sg = fetch_json(f"/api/cg/signals?sym={sym}&bt=all", timeout=10)
        if sg and sg.get("ok"):
            sets = sg.get("sets", []) or []
            all_sigs = []
            sigs_by_bt = {
                "all": {
                    "counts": sg.get("counts"),
                    "thresholds": sg.get("thresholds"),
                    "sets": sets[:4],
                }
            }
            for s_set in sets:
                bt_key = s_set.get("bt")
                if bt_key:
                    sigs_by_bt[bt_key] = {
                        "counts": sg.get("counts"),
                        "thresholds": sg.get("thresholds"),
                        "sets": [s_set],
                    }
                all_sigs.extend(s_set.get("sigs", []))
            profile["footprint"]["signals"] = sigs_by_bt
            with _CACHE_LOCK:
                _FP_SIGNALS_CACHE[sym] = (now_t, sigs_by_bt)
            sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
            os.makedirs(sym_fp_dir, exist_ok=True)
            atomic_write(os.path.join(sym_fp_dir, "signals_7d.json"), {
                "sym": sym,
                "updated_at": profile["updated_at"],
                "total_signals": len(all_sigs),
                "signals": all_sigs,
            })

    # 11. LARGE ORDERS & D10 LTO (/api/cg/largeorder cached to avoid Coinglass 429; /api/cg/lto uses ?symbol=)
    lo_ttl = 300 if is_prime else 900
    lo = _cached_get(_LO_CACHE, sym, lo_ttl, f"/api/cg/largeorder?symbol=Binance_{sym}USDT&interval=m15&hours=24", timeout=8)
    if lo and lo.get("ok"):
        profile["large_orders"]["resting_and_executed"]["24h"] = (lo.get("groups") or [])[:8]
        if lo.get("supported") is False and lo.get("reason"):
            profile["large_orders"]["largeorder_status"] = lo.get("reason")
    lto_ttl = 180 if is_prime else 900
    lto = _cached_get(_LTO_CACHE, sym, lto_ttl, f"/api/cg/lto?symbol={sym}", timeout=10)
    if lto and lto.get("ok"):
        pe = lto.get("perExchange") or {}
        active_exch = {k: v for k, v in pe.items() if isinstance(v, dict) and v.get("hasData")}
        profile["large_orders"]["large_taker_orders_100k"] = {
            "supported": lto.get("supported"),
            "minVolUsd": lto.get("minVolUsd"),
            "groups": (lto.get("groups") or [])[:12],
            "activeExchanges": list(active_exch.keys()),
            "hlFallback": lto.get("hlFallback"),
            "updatedAt": lto.get("updatedAt"),
        }

    # 12. PLAYBOOK STRATEGY CARDS (Prime only — non-prime skipped to save calls)
    if is_prime:
        cards = fetch_json(f"/api/strategy/cards?sym={sym}", timeout=8)
        if cards and not cards.get("_err"):
            asset_cards = [c for a in cards.get("assets", []) if a.get("sym") == sym for c in a.get("cards", [])]
            profile["playbook_cards"] = {
                "status": "triggered" if cards.get("ok") else "monitoring",
                "cards": asset_cards,
            }

    # 13. BACKTEST METRICS (Cached 30m in RAM; saves 400 MB/cycle)
    bt = _cached_get(_BACKTEST_CACHE, sym, 1800, f"/api/cg/backtest?sym={sym}", timeout=12)
    if bt and bt.get("ok"):
        profile["backtest_metrics"] = {
            "metricsAll": bt.get("metricsAll"),
            "byModel": bt.get("byModel"),
            "byRegime": bt.get("byRegime"),
            "tradeCount": len(bt.get("trades", [])),
        }

    # 14. TRADFI COT (Cached 1h in RAM)
    cot = _cached_get(_COT_CACHE, sym, 3600, f"/api/cg/cot?sym={sym}&quiet=1", timeout=6)
    if cot and not cot.get("_err"):
        profile["cot_report"] = cot

    # 15. FUNDING & OI (Preserve aggregate metrics for D14 4H OI & D15 Annualized Funding APR)
    fnd = fetch_json(f"/api/cg/funding?sym={sym}", timeout=6)
    if fnd and fnd.get("ok"):
        f_agg = dict(fnd.get("aggregate") or {})
        pct8h = f_agg.get("fundingPct8h")
        if pct8h is not None:
            f_agg["apr_pct"] = round(float(pct8h) * 3.0 * 365.0, 2)
        profile["funding_and_oi"]["funding"] = fnd.get("venues")
        profile["funding_and_oi"]["funding_aggregate"] = f_agg

    oi = fetch_json(f"/api/cg/oi?sym={sym}", timeout=6)
    if oi and oi.get("ok"):
        profile["funding_and_oi"]["open_interest"] = oi.get("venues")
        profile["funding_and_oi"]["oi_aggregate"] = oi.get("aggregate")

    target_file = os.path.join(ASSETS_DIR, f"{sym}.json")
    atomic_write(target_file, profile)
    elapsed = round(time.time() - t0, 2)
    file_size_kb = round(os.path.getsize(target_file) / 1024, 1)
    tag = "P" if is_prime else "E"
    print(f"  [telemetry:{tag}] {sym:9s} -> {file_size_kb:6.1f} KB in {elapsed:5.2f}s", flush=True)
    return sym, profile


def generate_authoritative_datasets(harvested_profiles, screener_data):
    """Generates the 4 Authoritative Datasets required by the Trading Desk."""
    print("[telemetry] Generating 4 authoritative datasets...", flush=True)
    iso_now = datetime.now(timezone.utc).isoformat()

    live_daemon_payload = {
        "metadata": {
            "timestamp_utc": iso_now,
            "version": "4.0.0-full-mt5",
            "assets_harvested": len(harvested_profiles),
            "host": "antigravity-local-terminal",
        },
        "profiles": harvested_profiles,
    }
    atomic_write(os.path.join(TELEMETRY_DIR, "live_orderflow_daemon.json"), live_daemon_payload)

    deep_matrix = {}
    for sym in DEEP_MATRIX_SYMBOLS:
        p = harvested_profiles.get(sym)
        if not p:
            continue
        deep_matrix[sym] = {
            "symbol": sym,
            "timestamp_utc": iso_now,
            "ticker": p.get("ticker"),
            "mt5": p.get("mt5"),
            "market_structure": p.get("market_structure"),
            "liquidations": p.get("liquidations"),
            "book_walls": p.get("book_walls"),
            "cvd": p.get("cvd"),
            "footprint_signals": p.get("footprint", {}).get("signals"),
            "footprint_history": p.get("footprint", {}).get("history_7d"),
            "large_orders": p.get("large_orders"),
            "narrative_engine": p.get("narrative_engine"),
            "athena_oracle": p.get("athena_oracle"),
        }
    atomic_write(os.path.join(TELEMETRY_DIR, "deep_orderflow_matrix.json"), deep_matrix)

    full_universe_payload = {
        "metadata": {
            "timestamp_utc": iso_now,
            "total_assets": len(screener_data.get("rows", [])),
            "warm_count": screener_data.get("warmCount"),
            "deep_harvested_count": len(harvested_profiles),
        },
        "screener_rows": screener_data.get("rows", []),
        "deep_profiles": harvested_profiles,
    }
    atomic_write(os.path.join(TELEMETRY_DIR, "full_universe_live_telemetry.json"), full_universe_payload)

    short_candidates = []
    long_candidates = []
    for sym, p in harvested_profiles.items():
        mkt = p.get("market_structure") or {}
        mkt_price = mkt.get("price")
        px = (p.get("ticker") or {}).get("price") or (mkt_price.get("last") if isinstance(mkt_price, dict) else mkt_price)
        if not px or px <= 0:
            continue

        vwap_data = mkt.get("vwap") or {}
        vwap_val = vwap_data.get("vwap")
        vwap_std = vwap_data.get("std") or ((vwap_data.get("bands") or {}).get("sd1"))
        vwap_z = (px - vwap_val) / vwap_std if (vwap_val and vwap_std and vwap_std > 0) else 0.0

        rsi_15m = (mkt.get("rsi") or {}).get("m15") or 50.0
        emas_m15 = (mkt.get("emas") or {}).get("m15") or {}
        e89_m15 = emas_m15.get("e89")
        e233_m15 = emas_m15.get("e233")
        trend_short_tc09 = bool(e89_m15 and e233_m15 and px < e89_m15 and px < e233_m15)
        trend_long_tc09 = bool(e89_m15 and e233_m15 and px > e89_m15 and px > e233_m15)

        day_auc = (mkt.get("auction") or {}).get("day") or {}
        day_val = day_auc.get("val")
        day_vah = day_auc.get("vah")

        cum_delta_7d = (p.get("footprint", {}).get("history_7d") or {}).get("cum_delta_7d", 0.0)
        oracle_score = (p.get("athena_oracle") or {}).get("score")
        oracle_val = oracle_score if oracle_score is not None else 50
        regime = (p.get("narrative_engine") or {}).get("regime", "UNKNOWN")
        largest_level = (p.get("liquidations", {}).get("stopmap", {}).get("m1_coin_24h", {}).get("overview") or {}).get("largestLevel")

        score_short = 0.0
        score_long = 0.0
        if trend_short_tc09:
            score_short += 30.0
            if day_val and px < day_val:
                score_short += 20.0
            if vwap_z < -1.0:
                score_short += min(abs(vwap_z) * 10.0, 25.0)
            if cum_delta_7d < 0:
                score_short += 10.0
            if oracle_val < 48:
                score_short += (50 - oracle_val) * 0.8

        if trend_long_tc09:
            score_long += 30.0
            if day_vah and px > day_vah:
                score_long += 20.0
            if vwap_z > 1.0:
                score_long += min(vwap_z * 10.0, 25.0)
            if cum_delta_7d > 0:
                score_long += 10.0
            if oracle_val > 52:
                score_long += (oracle_val - 50) * 0.8

        row_base = {
            "sym": sym,
            "feed_type": p.get("feed_type", "coinglass_perp"),
            "mt5_sym": (p.get("mt5") or {}).get("symbol"),
            "price": px,
            "vwap_z": round(vwap_z, 2),
            "rsi_15m": round(rsi_15m, 1),
            "cum_delta_7d": round(cum_delta_7d, 2),
            "oracle_score": oracle_val,
            "regime": regime,
            "largest_liq_level": largest_level,
        }
        short_candidates.append(dict(row_base, confluence_short_score=round(score_short, 1)))
        long_candidates.append(dict(row_base, confluence_long_score=round(score_long, 1)))

    short_candidates.sort(key=lambda x: x["confluence_short_score"], reverse=True)
    long_candidates.sort(key=lambda x: x["confluence_long_score"], reverse=True)

    atomic_write(os.path.join(TELEMETRY_DIR, "quant_report_structured.json"), {
        "metadata": {
            "timestamp_utc": iso_now,
            "model": "Multi-Factor Confluence Formulation (Coinglass + Full MT5)",
            "evaluator": "Antigravity Quant Engine",
        },
        "top_short_confluence_candidates": short_candidates[:20],
        "top_long_confluence_candidates": long_candidates[:20],
        "prime_assets_evaluated": len(harvested_profiles),
    })


def update_universe_overview():
    scr = fetch_json("/api/cg/screener", timeout=10)
    risk_summary = fetch_json("/api/cg/risk", timeout=8)
    desk_live = fetch_json("/api/desk/live", timeout=8)
    cnbc_macro = fetch_json("/api/cg/cnbc?syms=SP500,NDX100,EURUSD", timeout=8)
    notifs = fetch_json("/api/cg/notifications?limit=25", timeout=8)
    rules = fetch_json("/api/cg/alert-rules", timeout=8)
    engine_st = fetch_json("/api/cg/engine/status", timeout=8)

    if scr and scr.get("ok"):
        overview_payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_assets": len(scr.get("rows", [])),
            "warm_count": scr.get("warmCount"),
            "macro_quotes": cnbc_macro.get("quotes") if (cnbc_macro and cnbc_macro.get("ok")) else None,
            "engine_status": {
                "started": engine_st.get("started"),
                "backfillDays": engine_st.get("backfillDays"),
                "series": [s.get("name") for s in engine_st.get("series", []) if isinstance(s, dict)] if isinstance(engine_st.get("series"), list) else [],
            } if engine_st else None,
            "portfolio_risk": risk_summary.get("positions") if (risk_summary and risk_summary.get("ok")) else None,
            "desk_live_blotter": {
                "mt5": desk_live.get("mt5"),
                "analysis": desk_live.get("analysis"),
                "orders": desk_live.get("orders"),
            } if (desk_live and desk_live.get("ok")) else None,
            "live_alerts": {
                "count": notifs.get("count"),
                "recent": notifs.get("items", [])[:15],
            } if (notifs and notifs.get("ok")) else None,
            "alert_rules": rules.get("rules") if (rules and rules.get("ok")) else None,
            "rows": scr.get("rows", []),
        }
        atomic_write(os.path.join(TELEMETRY_DIR, "universe_overview.json"), overview_payload)
        return scr
    return {"rows": []}


def _git_sync_worker():
    if not _GIT_LOCK.acquire(blocking=False):
        return
    try:
        subprocess.run(["git", "add", "data/telemetry"], check=False, timeout=25)
        status = subprocess.run(["git", "status", "--porcelain", "data/telemetry"], capture_output=True, text=True, timeout=15)
        if status.stdout.strip():
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            subprocess.run(["git", "commit", "-m", f"telemetry: live full-universe + MT5 snapshot [{ts} UTC]"], check=False, timeout=30)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False, timeout=30)
            res = subprocess.run(["git", "push", "origin", "HEAD:main"], capture_output=True, text=True, timeout=45)
            msg = "pushed cleanly" if res.returncode == 0 else (res.stderr.strip() or res.stdout.strip())
            print(f"[git-sync] {msg} ({ts} UTC)", flush=True)
    except Exception as e:
        print(f"[git-sync] Exception: {e}", flush=True)
    finally:
        _GIT_LOCK.release()


def maybe_trigger_git_sync(git_sync_secs):
    global _LAST_GIT_SYNC
    if git_sync_secs <= 0:
        return
    now = time.time()
    if now - _LAST_GIT_SYNC < git_sync_secs:
        return
    _LAST_GIT_SYNC = now
    threading.Thread(target=_git_sync_worker, daemon=True).start()


def build_coinglass_universe(native_base_syms):
    """Unions PRIME + all MT5 Coinglass mappings + every non-native file in data/telemetry/assets/ sorted stalest-first."""
    existing_stems = {
        os.path.basename(p)[:-5]
        for p in glob.glob(os.path.join(ASSETS_DIR, "*.json"))
    }
    mt5_cg_syms = set(MT5_TO_COINGLASS.values())
    all_cg = (set(PRIME_CURATED_UNIVERSE) | mt5_cg_syms | existing_stems) - set(native_base_syms)
    prime_set = set(PRIME_CURATED_UNIVERSE)

    def sort_key(sym):
        p = os.path.join(ASSETS_DIR, f"{sym}.json")
        mtime = os.path.getmtime(p) if os.path.exists(p) else 0.0
        # Missing / stale (>550s) files sort FIRST so every frozen asset refreshes right away!
        is_stale_or_missing = (time.time() - mtime) > 550
        return (0 if is_stale_or_missing else (1 if sym in prime_set else 2), mtime, sym)

    ordered = sorted(all_cg, key=sort_key)
    return ordered, prime_set


def main():
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(FOOTPRINT_DIR, exist_ok=True)
    once = "--once" in sys.argv
    no_git = "--no-git" in sys.argv
    workers = int(os.environ.get("TELEMETRY_WORKERS", "3"))
    interval = int(os.environ.get("TELEMETRY_INTERVAL_SECS", "10"))
    tail_batch = int(os.environ.get("TELEMETRY_TAIL_BATCH", "20"))
    git_sync_secs = 0 if no_git else int(os.environ.get("TELEMETRY_GIT_SYNC_SECS", "600"))

    print("==================================================================", flush=True)
    print("Starting Full-Universe + All-MT5 Order-Flow Telemetry Daemon v4.1", flush=True)
    print(f"Workers: {workers} | Cycle Pause: {interval}s | Tail Batch: {tail_batch} | Async Git Sync: {git_sync_secs}s", flush=True)
    print("==================================================================", flush=True)

    while True:
        cycle_start = time.time()
        harvested_profiles = {}

        try:
            # 1. Harvest ALL 151 visible MT5 symbols locally (quotes on all + OHLCV telemetry on CFDs/FX/Indices)
            mt5_quotes_by_cg, mt5_native_profiles, native_base_syms, mt5_meta = harvest_mt5_layer()
            harvested_profiles.update(mt5_native_profiles)
            print(f"[mt5-layer] connected={mt5_meta.get('connected')} visible={mt5_meta.get('visible_count')} native_written={mt5_meta.get('mt5_native_written', 0)} inactive={mt5_meta.get('inactive_skipped')} in {mt5_meta.get('elapsed_s', 0)}s", flush=True)

            # 2. Update Universe Overview (Screener, Risk, Alerts)
            screener_data = update_universe_overview()

            # 3. Fast-Lane Prime Tier every cycle + rotating stalest-first Tail batch so Prime assets never breach 600s SLA
            cg_list, prime_set = build_coinglass_universe(native_base_syms)
            prime_cg = [s for s in cg_list if s in prime_set]
            tail_cg = [s for s in cg_list if s not in prime_set]
            sweep_cg = cg_list if once else (prime_cg + tail_cg[:tail_batch])
            print(f"[telemetry] Commencing fast-lane harvest of {len(sweep_cg)}/{len(cg_list)} Coinglass assets ({len(prime_cg)} prime + {len(sweep_cg)-len(prime_cg)} tail) with {workers} workers...", flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(harvest_single_asset, sym, sym in prime_set, mt5_quotes_by_cg.get(sym)): sym
                    for sym in sweep_cg
                }
                for future in as_completed(future_map):
                    sym = future_map[future]
                    try:
                        _, profile = future.result()
                        harvested_profiles[sym] = profile
                    except Exception as e:
                        print(f"  [ERROR] Asset {sym} harvesting failed: {e}", file=sys.stderr, flush=True)

            # 4. Generate Authoritative Consolidated Datasets
            generate_authoritative_datasets(harvested_profiles, screener_data)

            # 5. Non-blocking throttled Git Sync (never stalls telemetry refresh cycles)
            maybe_trigger_git_sync(git_sync_secs)

        except Exception as e:
            print(f"[telemetry-daemon] Cycle exception: {e}", file=sys.stderr, flush=True)

        cycle_dur = round(time.time() - cycle_start, 2)
        print(f"=== Full Sweep Finished in {cycle_dur}s ({len(harvested_profiles)} total assets updated) ===", flush=True)

        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
