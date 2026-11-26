#!/usr/bin/env python3
"""
telemetry_daemon.py — High-Speed Async Zero-Leak Order-Flow & MT5 Telemetry Engine (v5.0).

Stack:
  - winloop + asyncio + aiohttp (pooled HTTP keep-alive, bounded semaphore <= 4
    so Next.js :3000 MAX_CG_CONCURRENCY=6 is never saturated or 429'd)
  - orjson (zero-copy fast JSON serialization) + NumPy (via telemetry_analytics)
  - BoundedTTLCache + psutil RSS & host-RAM guard + per-asset streaming writes
    with immediate object release so heap stays flat across infinite cycles.

Captures FULL CG Terminal UI tab parity for ALL assets:
  1. Overview / Market / Narrative / Oracle / Micro L2 / Quant / Funding & OI
  2. Stopmap: Model 1 (24h, 3d, 7d + BTC pair 24h) WITH exact `buildWalls()`
     top-24 walls (`active` vs swept, `maxShort`, `maxLong`, `keyLevels`,
     `overview`, `cumulative`, `profile`, `lastColumn`) + Model 2 & Model 3
     (`clusters`, `bands`, `totals`, `profile`, `cumulative`, or native walls)
  3. Liq Levels: 3d, 7d, 30d clusters (`bands`, `levTiers`, `keyLevels`, `totals`)
     + real-time `liq-live` tape
  4. Book Walls: futures & spot across `1m`, `5m`, `m15`, `1h`, `4h` (smart 4h
     limit=60 & 30m TTL) with `walls`, `events`, `ladder`, `strip_recent`,
     `persistence` + grid-aligned `/api/cg/livebook?sym=...&band=5&grid=...`
  5. Footprint (`data/telemetry/assets/<SYM>.json` AND `data/telemetry/footprint/<SYM>/`):
     - `bars_15m_7d.json` (672-bar 7d raw bars via fast incremental 32-bar tail splice)
     - `bars_1h.json` (168-bar 7d 1h raw bars via incremental 12-bar tail splice)
     - `summary_7d.json` & `summary_1h.json` (with `cum_delta_1h` fixed + `diagonal_imbalances` 3:1/2:1 + `finished_high/low`)
     - `summary_mtf.json` (m5, m15, m30, h1, h4 multi-TF footprint rollup)
     - `signals_7d.json` (`all`, `time`, `vol`, `range` sets, `thresholds`, `counts`)
  6. Visible MT5 symbols (FX, Indices, Commodities/Metals) local OHLCV + COT + Classic overlays.
"""
from __future__ import annotations

import asyncio
import gc
import glob
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import aiohttp
import psutil

from scripts.telemetry_analytics import (
    BoundedTTLCache,
    build_compact_bookwalls,
    compute_diagonal_imbalances,
    dumps_bytes,
    summarize_stopmap_payload,
)

try:
    import orjson

    def loads_bytes(raw: bytes) -> Any:
        return orjson.loads(raw)

except Exception:  # pragma: no cover
    import json

    def loads_bytes(raw: bytes) -> Any:
        return json.loads(raw.decode("utf-8"))

try:
    import winloop  # type: ignore
except Exception:
    winloop = None

try:
    import MetaTrader5 as mt5

    MT5_AVAILABLE = True
except Exception:
    mt5 = None
    MT5_AVAILABLE = False

BASE_URL = os.environ.get("TERMINAL_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
TELEMETRY_DIR = os.path.join("data", "telemetry")
ASSETS_DIR = os.path.join(TELEMETRY_DIR, "assets")
FOOTPRINT_DIR = os.path.join(TELEMETRY_DIR, "footprint")

# Tier-1 Prime Assets: full real-time harvest every cycle
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

CLASSIC_OVERLAY_MAP = {
    "SP500": "SPX500", "SPX500": "SPX500",
    "NAS100": "NDX100", "NDX100": "NDX100",
    "DJ30": "US30", "GER40": "GER30", "UK100": "UK100",
    "FR40": "FRA40", "STOXX50": "EUSTX50", "SWISS20": "SWI20",
    "NETH25": "NTH25", "JP225": "JP225", "HK50": "HK50",
    "AU200": "AUS200", "US2000": "US2000",
}

PERPDEPTH_SYMS = frozenset({"XAU", "XAG", "XPT", "SPX500", "SP500", "NDX100", "JP225", "HK50", "UKO", "USO", "NATGAS"})

# Strictly bounded LRU + byte-budget caches (combined ceiling < 28 MB RAM — zero leaks)
CACHES: Dict[str, BoundedTTLCache] = {
    "backtest": BoundedTTLCache(max_entries=120, max_bytes=3 * 1024 * 1024),
    "cot": BoundedTTLCache(max_entries=64, max_bytes=2 * 1024 * 1024),
    "stopmap": BoundedTTLCache(max_entries=600, max_bytes=16 * 1024 * 1024),
    "liq": BoundedTTLCache(max_entries=300, max_bytes=6 * 1024 * 1024),
    "bw": BoundedTTLCache(max_entries=180, max_bytes=6 * 1024 * 1024),
    "cvd": BoundedTTLCache(max_entries=160, max_bytes=2 * 1024 * 1024),
    "lo": BoundedTTLCache(max_entries=120, max_bytes=2 * 1024 * 1024),
    "lto": BoundedTTLCache(max_entries=120, max_bytes=2 * 1024 * 1024),
    "perpdepth": BoundedTTLCache(max_entries=32, max_bytes=1 * 1024 * 1024),
    "cards": BoundedTTLCache(max_entries=120, max_bytes=2 * 1024 * 1024),
    "fp_sig": BoundedTTLCache(max_entries=140, max_bytes=3 * 1024 * 1024),
    "fp_meta": BoundedTTLCache(max_entries=220, max_bytes=4 * 1024 * 1024),
    "classic": BoundedTTLCache(max_entries=32, max_bytes=1 * 1024 * 1024),
}

_GIT_LOCK = threading.Lock()
_LAST_GIT_SYNC = 0.0
_WRITE_LOCK = threading.Lock()


def atomic_write(filepath: str, data: Any, indent: bool = False) -> int:
    """Serialize via fast orjson and atomically replace target file. Returns bytes written."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    raw = dumps_bytes(data, indent=indent)
    tmp_path = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    try:
        for attempt in range(6):
            try:
                os.replace(tmp_path, filepath)
                return len(raw)
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    return len(raw)


def _read_json_if_exists(filepath: str) -> Optional[Any]:
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "rb") as f:
            return loads_bytes(f.read())
    except Exception:
        return None


def _mt5_base_sym(name: str) -> str:
    return re.sub(r"\.(pi|p|i|pro|raw|m)$", "", name, flags=re.I).upper()


def _ema_series(closes: List[float], period: int) -> Optional[float]:
    if not closes:
        return None
    k = 2.0 / (period + 1.0)
    ema = float(closes[0])
    for price in closes[1:]:
        ema = float(price) * k + ema * (1.0 - k)
    return round(ema, 6)


def _wilder_rsi(closes: List[float], period: int = 14) -> Optional[float]:
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
    return round(100.0 - (100.0 / (1.0 + avg_g / avg_l)), 2)


def _atr(bars: Any, period: int = 14) -> Optional[float]:
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


def _value_area(bars: Any, n_bins: int = 48) -> Optional[Dict[str, Any]]:
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


def _day_vwap(bars_m15: Any) -> Optional[Dict[str, Any]]:
    if bars_m15 is None or len(bars_m15) == 0:
        return None
    now_utc = datetime.now(timezone.utc)
    day_start = int(datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp())
    day_bars = [b for b in bars_m15 if int(b["time"]) >= day_start]
    if not day_bars:
        day_bars = bars_m15[-min(len(bars_m15), 32):]
    sum_pv = 0.0
    sum_v = 0.0
    tps, vols = [], []
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
        "bands": {"sd1": round(sd1, 6), "sd2": round(sd1 * 2.0, 6), "sd3": round(sd1 * 3.0, 6)},
        "open": round(float(day_bars[0]["open"]), 6),
        "high": round(max(float(b["high"]) for b in day_bars), 6),
        "low": round(min(float(b["low"]) for b in day_bars), 6),
        "bars": len(day_bars),
    }


def _build_mt5_quote_dict(sym_obj: Any, tick_obj: Any) -> Optional[Dict[str, Any]]:
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


def build_compact_asset_summary(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Extract lightweight (~2 KB) summary of a harvested asset without retaining heavy arrays."""
    sym = profile.get("symbol")
    mkt = profile.get("market_structure") or {}
    mkt_px = mkt.get("price")
    px = (profile.get("ticker") or {}).get("price") or (
        mkt_px.get("last") if isinstance(mkt_px, dict) else mkt_px
    )
    liq = profile.get("liquidations") or {}
    smap = liq.get("stopmap") or {}
    sm3 = smap.get("m1_coin_3d") or smap.get("m1_coin_24h") or {}
    walls_meta = sm3.get("walls") if isinstance(sm3.get("walls"), dict) else {}
    bw_fut_m15 = ((profile.get("book_walls") or {}).get("futures") or {}).get("m15") or {}
    fp = profile.get("footprint") or {}
    h7 = fp.get("history_7d") or {}
    h1 = fp.get("history_1h") or {}
    foi = profile.get("funding_and_oi") or {}

    # Compute cross-horizon widest short/long wall prices from all present Model 1 coin horizons
    all_max_shorts = []
    all_max_longs = []
    for k, sm_obj in smap.items():
        if not isinstance(sm_obj, dict) or not str(k).startswith("m1_coin_"):
            continue
        w_obj = sm_obj.get("walls") if isinstance(sm_obj.get("walls"), dict) else {}
        if isinstance(w_obj.get("max_short"), dict) and w_obj["max_short"].get("price") is not None:
            all_max_shorts.append((k, w_obj["max_short"]))
        if isinstance(w_obj.get("max_long"), dict) and w_obj["max_long"].get("price") is not None:
            all_max_longs.append((k, w_obj["max_long"]))

    return {
        "symbol": sym,
        "feed_type": profile.get("feed_type"),
        "updated_at": profile.get("updated_at"),
        "updated_epoch": profile.get("updated_epoch"),
        "asset_file": f"data/telemetry/assets/{sym}.json",
        "price": px,
        "mt5": profile.get("mt5"),
        "market_structure": mkt,
        "narrative_engine": profile.get("narrative_engine"),
        "athena_oracle": profile.get("athena_oracle"),
        "options_deribit": {
            "dvol": (profile.get("options_deribit") or {}).get("dvol"),
            "riskReversal25d": (profile.get("options_deribit") or {}).get("riskReversal25d"),
            "gammaFlipPrice": (profile.get("options_deribit") or {}).get("gammaFlipPrice"),
            "maxPainStrike": (profile.get("options_deribit") or {}).get("maxPainStrike"),
            "gex": (profile.get("options_deribit") or {}).get("gex"),
        } if profile.get("options_deribit") else None,
        "stopmap_summary": {
            "horizons_present": sorted(smap.keys()),
            "largest_24h": ((smap.get("m1_coin_24h") or {}).get("overview") or {}).get("largestLevel"),
            "m1_3d_max_short": walls_meta.get("max_short"),
            "m1_3d_max_long": walls_meta.get("max_long"),
            "m1_3d_active_short_prices": walls_meta.get("active_short_prices", [])[:6],
            "m1_3d_active_long_prices": walls_meta.get("active_long_prices", [])[:6],
            "all_tf_max_short": max(all_max_shorts, key=lambda item: float(item[1].get("mx_usd") or 0.0))[1] if all_max_shorts else None,
            "all_tf_max_long": max(all_max_longs, key=lambda item: float(item[1].get("mx_usd") or 0.0))[1] if all_max_longs else None,
        },
        "book_walls_summary": {
            "m15_imbalance": bw_fut_m15.get("imbalance"),
            "m15_persistence": bw_fut_m15.get("persistence"),
            "intervals_captured": list(((profile.get("book_walls") or {}).get("futures") or {}).keys()),
        },
        "footprint_summary": {
            "cum_delta_7d": h7.get("cum_delta_7d"),
            "cum_delta_1h": h1.get("cum_delta_1h"),
            "bars_7d": h7.get("bars_count"),
            "bars_1h": h1.get("bars_count"),
            "signal_counts": ((fp.get("signals") or {}).get("all") or {}).get("counts"),
            "multi_tf": fp.get("multi_tf"),
        },
        "funding_aggregate": foi.get("funding_aggregate"),
        "oi_aggregate": foi.get("oi_aggregate"),
    }


def _harvest_mt5_sync(classic_fetcher: Any = None) -> Tuple[Dict[str, Any], Dict[str, Any], Set[str], Dict[str, Any]]:
    """Read all visible MT5 symbols synchronously on startup of each cycle and return compact summaries."""
    mt5_quotes_by_cg: Dict[str, Any] = {}
    mt5_summaries: Dict[str, Any] = {}
    mt5_classic_pending: List[Tuple[str, str, Dict[str, Any]]] = []
    native_base_syms: Set[str] = set()
    meta_info: Dict[str, Any] = {"connected": False, "visible_count": 0, "inactive_skipped": 0}

    if not MT5_AVAILABLE:
        return mt5_quotes_by_cg, mt5_summaries, native_base_syms, meta_info

    t0 = time.time()
    init_kwargs: Dict[str, Any] = {"timeout": 8000}
    if os.path.exists(MT5_TERMINAL_PATH):
        init_kwargs["path"] = MT5_TERMINAL_PATH
    if not mt5.initialize(**init_kwargs):
        meta_info["error"] = str(mt5.last_error())
        return mt5_quotes_by_cg, mt5_summaries, native_base_syms, meta_info

    meta_info["connected"] = True
    try:
        all_syms = mt5.symbols_get() or []
        visible_syms = [s for s in all_syms if s.visible]
        meta_info["visible_count"] = len(visible_syms)
        meta_info["inactive_skipped"] = len(all_syms) - len(visible_syms)

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

            emas, rsi_map = {}, {}
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
                    "auction": {"session": sess_auc, "day": day_auc, "week": week_auc, "month": month_auc},
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
                "playbook_cards": None,
                "backtest_metrics": None,
                "cot_report": None,
                "funding_and_oi": {},
                "coverage_note": "MT5 native OHLCV feed (L2/perp order-flow N/A on broker FX/CFD)",
            }

            classic_sym = CLASSIC_OVERLAY_MAP.get(base_sym)
            if classic_sym:
                mt5_classic_pending.append((base_sym, classic_sym, profile))
            else:
                atomic_write(os.path.join(ASSETS_DIR, f"{base_sym}.json"), profile)
                mt5_summaries[base_sym] = build_compact_asset_summary(profile)

        # Defer classic/COT overlay hydration if any until after MT5 session closes cleanly
        for base_sym, classic_sym, profile in mt5_classic_pending:
            cot_hit = CACHES["cot"].get(classic_sym, 3600)
            cl_hit = CACHES["classic"].get(classic_sym, 180)
            if cot_hit:
                profile["cot_report"] = cot_hit
            if cl_hit and cl_hit.get("ok"):
                profile["playbook_cards"] = {
                    "sym": classic_sym,
                    "sessionState": cl_hit.get("sessionState"),
                    "price": cl_hit.get("price"),
                    "chgPct": cl_hit.get("chgPct"),
                    "liveBridge": cl_hit.get("liveBridge"),
                }
            atomic_write(os.path.join(ASSETS_DIR, f"{base_sym}.json"), profile)
            mt5_summaries[base_sym] = build_compact_asset_summary(profile)
            if base_sym == "NAS100":
                mirror = dict(profile, symbol="NDX100")
                atomic_write(os.path.join(ASSETS_DIR, "NDX100.json"), mirror)
                mt5_summaries["NDX100"] = build_compact_asset_summary(mirror)
                native_base_syms.add("NDX100")
            elif base_sym == "SP500":
                mirror = dict(profile, symbol="SPX500")
                atomic_write(os.path.join(ASSETS_DIR, "SPX500.json"), mirror)
                mt5_summaries["SPX500"] = build_compact_asset_summary(mirror)
                native_base_syms.add("SPX500")

        meta_info["mt5_native_written"] = len(mt5_summaries)
        meta_info["mt5_coinglass_matched"] = cg_matched_count
        meta_info["elapsed_s"] = round(time.time() - t0, 2)
    finally:
        mt5.shutdown()

    return mt5_quotes_by_cg, mt5_summaries, native_base_syms, meta_info


def _merge_fp_bars(existing_bars: List[Dict[str, Any]], new_bars: List[Dict[str, Any]], max_keep: int) -> List[Dict[str, Any]]:
    """Merge existing cached bars with freshly fetched tail bars by timestamp `ts` without duplication."""
    by_ts: Dict[int, Dict[str, Any]] = {}
    for b in existing_bars:
        if isinstance(b, dict) and b.get("ts") is not None:
            by_ts[int(b["ts"])] = b
    for b in new_bars:
        if isinstance(b, dict) and b.get("ts") is not None:
            ts_val = int(b["ts"])
            prev = by_ts.get(ts_val)
            # Prefer freshly fetched bar, especially if it carries fp buckets
            if prev is None or b.get("fp") or not prev.get("fp"):
                by_ts[ts_val] = b
    ordered = sorted(by_ts.values(), key=lambda x: int(x["ts"]))
    return ordered[-max_keep:] if max_keep > 0 and len(ordered) > max_keep else ordered


def _summarize_fp_bars_with_diag(
    sym: str,
    bars: List[Dict[str, Any]],
    tf_label: str,
    summary_filename: str,
    delta_key: str,
    include_diag_levels: bool = True,
) -> Tuple[List[Dict[str, Any]], float, int]:
    """
    Compute per-bar Volume/Delta/POC/Finished-Auction + exact Diagonal Imbalances
    for every bar in `bars`, write summary file atomically, and return (rows, cum_delta, with_fp_count).
    """
    sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
    os.makedirs(sym_fp_dir, exist_ok=True)
    summary_rows: List[Dict[str, Any]] = []
    cum_delta = 0.0
    with_fp_count = 0

    for b in bars:
        fp = b.get("fp")
        b_arr = fp.get("b", []) if isinstance(fp, dict) else []
        s_arr = fp.get("s", []) if isinstance(fp, dict) else []
        if b_arr and s_arr:
            with_fp_count += 1
        buy_v = float(sum(b_arr)) if b_arr else 0.0
        sell_v = float(sum(s_arr)) if s_arr else 0.0
        delta = buy_v - sell_v
        cum_delta += delta
        tot_v = buy_v + sell_v
        delta_pct = (delta / tot_v * 100.0) if tot_v > 0 else 0.0

        poc_p: Optional[float] = None
        max_v = 0.0
        top_bid = b_arr[-1] if b_arr else None
        top_ask = s_arr[-1] if s_arr else None
        bot_bid = b_arr[0] if b_arr else None
        bot_ask = s_arr[0] if s_arr else None
        finished_high = bool(b_arr and s_arr and len(b_arr) >= 2 and top_bid == 0 and (top_ask or 0) > 0)
        finished_low = bool(b_arr and s_arr and len(b_arr) >= 2 and bot_ask == 0 and (bot_bid or 0) > 0)

        if b_arr and s_arr:
            tick = float(fp.get("tick") or 1.0)
            lo = float(fp.get("lo") or b.get("l") or 0.0)
            for i in range(min(len(b_arr), len(s_arr))):
                row_v = float(b_arr[i]) + float(s_arr[i])
                if row_v > max_v:
                    max_v = row_v
                    poc_p = round(lo + (i + 0.5) * tick, 6)

        diag = compute_diagonal_imbalances(fp) if (fp and include_diag_levels) else {
            "buy_3x_count": 0, "sell_3x_count": 0, "buy_2x_count": 0, "sell_2x_count": 0,
            "buy_3x_levels": [], "sell_3x_levels": [], "buy_stack_zones": [], "sell_stack_zones": [],
        }

        row_obj: Dict[str, Any] = {
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
            "diag_buy_3x": diag.get("buy_3x_count", 0),
            "diag_sell_3x": diag.get("sell_3x_count", 0),
            "diag_buy_2x": diag.get("buy_2x_count", 0),
            "diag_sell_2x": diag.get("sell_2x_count", 0),
            "buy_stack_zones": diag.get("buy_stack_zones", []),
            "sell_stack_zones": diag.get("sell_stack_zones", []),
        }
        if include_diag_levels and (len(bars) - len(summary_rows) <= 24):
            row_obj["diag_buy_3x_levels"] = diag.get("buy_3x_levels", [])
            row_obj["diag_sell_3x_levels"] = diag.get("sell_3x_levels", [])
        summary_rows.append(row_obj)

    payload: Dict[str, Any] = {
        "sym": sym,
        "timeframe": tf_label,
        "days": 7,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_bars": len(summary_rows),
        "with_fp_bars": with_fp_count,
        "start_time": summary_rows[0]["ts"] if summary_rows else None,
        "end_time": summary_rows[-1]["ts"] if summary_rows else None,
        "cum_delta": round(cum_delta, 2),
        delta_key: round(cum_delta, 2),
        "rows": summary_rows,
    }
    if delta_key != "cum_delta_7d":
        payload["cum_delta_7d"] = round(cum_delta, 2)  # legacy key alias
    atomic_write(os.path.join(sym_fp_dir, summary_filename), payload)
    return summary_rows, cum_delta, with_fp_count


class AsyncTelemetryHarvester:
    """Pooled aiohttp client with separate fast vs Coinglass-heavy semaphores & zero 429 overload."""

    def __init__(self, concurrency: int = 6, cg_heavy_concurrency: int = 3, force_full_fp: bool = False):
        self.fast_sem = asyncio.Semaphore(max(2, min(concurrency, 8)))
        self.cg_heavy_sem = asyncio.Semaphore(max(1, min(cg_heavy_concurrency, 4)))
        self.fapi_sm_sem = asyncio.Semaphore(1)
        self.force_full_fp = force_full_fp
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "AsyncTelemetryHarvester":
        timeout = aiohttp.ClientTimeout(total=25, connect=5)
        connector = aiohttp.TCPConnector(limit=16, limit_per_host=12, ttl_dns_cache=300, enable_cleanup_closed=True)
        self.session = aiohttp.ClientSession(
            base_url=BASE_URL,
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": "Antigravity-TelemetryEngine/5.1", "Connection": "keep-alive"},
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def fetch_json(
        self,
        path: str,
        timeout: float = 12.0,
        retries: int = 1,
        heavy: bool = False,
        fapi_pace: bool = False,
    ) -> Optional[Dict[str, Any]]:
        assert self.session is not None
        attempts = max(1, retries + 1)
        gate = self.fapi_sm_sem if fapi_pace else (self.cg_heavy_sem if heavy else self.fast_sem)
        for attempt in range(attempts):
            try:
                async with gate:
                    req_t0 = time.time()
                    async with self.session.get(path, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        raw = await resp.read()
                        elapsed = time.time() - req_t0
                        if fapi_pace and elapsed > 0.30:
                            # Polite pacing only on upstream network hits (cache hits return immediately)
                            await asyncio.sleep(0.35)
                        elif heavy:
                            await asyncio.sleep(0.22)
                        if resp.status == 200:
                            return loads_bytes(raw)
                        if resp.status in (429, 502, 503, 504) and attempt + 1 < attempts:
                            await asyncio.sleep(0.65 * (attempt + 1))
                            continue
                        return {"_err": f"HTTP {resp.status}"}
            except Exception as e:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.35 * (attempt + 1))
                    continue
                return {"_err": str(e)}
        return {"_err": "failed"}

    async def cached_get(
        self,
        cache_name: str,
        key: Any,
        ttl_secs: float,
        endpoint: str,
        timeout: float = 12.0,
        transform: Optional[Any] = None,
        heavy: bool = False,
        unsupported_ttl_secs: float = 3600.0,
        fapi_pace: bool = False,
    ) -> Optional[Any]:
        cache = CACHES[cache_name]
        if ttl_secs > 0:
            hit = cache.get(key, ttl_secs=ttl_secs, allow_stale=False)
            if hit is not None:
                if isinstance(hit, dict) and hit.get("_err"):
                    return None
                # Do not serve long-cached cooldown fallback stubs once brief 45s cooldown window has elapsed
                if (
                    cache_name == "stopmap"
                    and isinstance(hit, dict)
                    and "cooldown-fallback" in str(hit.get("source") or "")
                ):
                    short_hit = cache.get(key, ttl_secs=45.0, allow_stale=False)
                    if short_hit is not None:
                        return short_hit
                else:
                    return hit

        res = await self.fetch_json(endpoint, timeout=timeout, heavy=heavy, fapi_pace=fapi_pace)
        if res and not res.get("_err"):
            val = transform(res) if transform else res
            # Cache permanently unsupported assets longer; retry temporary binance-fapi & cooldown fallbacks quickly
            if isinstance(val, dict) and val.get("supported") is False:
                effective_ttl = unsupported_ttl_secs
            elif isinstance(val, dict) and res.get("source") in ("binance-fapi", "hl-xyz") and cache_name == "bw":
                effective_ttl = min(ttl_secs, 45.0)
            elif cache_name == "stopmap" and isinstance(val, dict) and "cooldown-fallback" in str(val.get("source") or ""):
                effective_ttl = min(ttl_secs, 45.0)
            else:
                effective_ttl = ttl_secs
            if effective_ttl > 0 and val is not None:
                cache.set(key, val)
            return val
        if ttl_secs > 0 and res and res.get("_err") in ("HTTP 400", "HTTP 503"):
            cache.set(key, res, approx_bytes=128)
        stale_hit = cache.get(key, ttl_secs=ttl_secs, allow_stale=True)
        if stale_hit is not None and not (isinstance(stale_hit, dict) and stale_hit.get("_err")):
            return stale_hit
        return None

    async def refresh_classic_caches(self) -> None:
        unique_classic = sorted(set(CLASSIC_OVERLAY_MAP.values()))
        tasks = []
        for cs in unique_classic:
            if CACHES["cot"].get(cs, 3600) is None:
                tasks.append(self.cached_get("cot", cs, 3600, f"/api/cg/cot?sym={cs}&quiet=1", timeout=6))
            if CACHES["classic"].get(cs, 180) is None:
                tasks.append(self.cached_get("classic", cs, 180, f"/api/cg/classic?sym={cs}", timeout=8))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def update_universe_overview(self) -> Dict[str, Any]:
        scr, risk_summary, desk_live, cnbc_macro, notifs, rules, engine_st = await asyncio.gather(
            self.fetch_json("/api/cg/screener", timeout=10),
            self.fetch_json("/api/cg/risk", timeout=8),
            self.fetch_json("/api/desk/live", timeout=8),
            self.fetch_json("/api/cg/cnbc?syms=SP500,NDX100,EURUSD", timeout=8),
            self.fetch_json("/api/cg/notifications?limit=25", timeout=8),
            self.fetch_json("/api/cg/alert-rules", timeout=8),
            self.fetch_json("/api/cg/engine/status", timeout=8),
        )
        if scr and scr.get("ok"):
            overview_payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total_assets": len(scr.get("rows", [])),
                "warm_count": scr.get("warmCount"),
                "macro_quotes": cnbc_macro.get("quotes") if (cnbc_macro and cnbc_macro.get("ok")) else None,
                "engine_status": {
                    "started": engine_st.get("started"),
                    "backfillDays": engine_st.get("backfillDays"),
                    "series": [s.get("name") for s in engine_st.get("series", []) if isinstance(s, dict)]
                    if isinstance(engine_st.get("series"), list)
                    else [],
                }
                if engine_st
                else None,
                "portfolio_risk": risk_summary.get("positions") if (risk_summary and risk_summary.get("ok")) else None,
                "desk_live_blotter": {
                    "mt5": desk_live.get("mt5"),
                    "analysis": desk_live.get("analysis"),
                    "orders": desk_live.get("orders"),
                }
                if (desk_live and desk_live.get("ok"))
                else None,
                "live_alerts": {
                    "count": notifs.get("count"),
                    "recent": (notifs.get("items") or [])[:15],
                }
                if (notifs and notifs.get("ok"))
                else None,
                "alert_rules": rules.get("rules") if (rules and rules.get("ok")) else None,
                "rows": scr.get("rows", []),
            }
            atomic_write(os.path.join(TELEMETRY_DIR, "universe_overview.json"), overview_payload)
            return scr
        return {"rows": []}

    async def _harvest_fp_timeframe(
        self,
        sym: str,
        itv: str,
        tf_label: str,
        raw_filename: Optional[str],
        summary_filename: str,
        delta_key: str,
        target_bars: int,
        delta_limit: int,
        is_prime: bool,
        ttl_secs: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Harvest raw footprint bars using incremental disk splice (0 MB permanent heap!),
        compute exact diagonal imbalances + POC + finished auctions, and return metadata.
        """
        cache_key = (sym, itv)
        if ttl_secs > 0:
            hit = CACHES["fp_meta"].get(cache_key, ttl_secs=ttl_secs)
            if hit is not None:
                return hit

        sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
        raw_path = os.path.join(sym_fp_dir, raw_filename) if raw_filename else None
        existing_bars: List[Dict[str, Any]] = []
        raw_age = 1e9
        if raw_path and os.path.exists(raw_path):
            raw_age = time.time() - os.path.getmtime(raw_path)
            existing_obj = _read_json_if_exists(raw_path)
            if isinstance(existing_obj, dict):
                existing_bars = existing_obj.get("bars") or []
            elif isinstance(existing_obj, list):
                existing_bars = existing_obj

        existing_has_fp = any(isinstance(b, dict) and b.get("fp") for b in existing_bars)
        need_full = (
            self.force_full_fp
            or (not raw_path)
            or (not existing_has_fp)
            or (len(existing_bars) < min(120, target_bars // 2))
            or (raw_age > 3600)
        )
        init_lim = target_bars if (self.force_full_fp or itv != "m15") else min(target_bars, 300)
        fetch_lim = init_lim if need_full else delta_limit
        fp_res = await self.fetch_json(
            f"/api/cg/fp?sym={sym}&itv={itv}&limit={fetch_lim}",
            timeout=24 if fetch_lim > 200 else 14,
            heavy=True,
        )
        if (not fp_res or fp_res.get("_err") or not fp_res.get("bars")) and fetch_lim > 150:
            fp_res = await self.fetch_json(f"/api/cg/fp?sym={sym}&itv={itv}&limit=150", timeout=18, heavy=True)
        if not (fp_res and fp_res.get("ok") and fp_res.get("bars")):
            stale_meta = CACHES["fp_meta"].get(cache_key, ttl_secs=ttl_secs, allow_stale=True)
            if stale_meta is not None:
                return stale_meta
            if existing_bars:
                rows, c_delta, with_fp_cnt = _summarize_fp_bars_with_diag(
                    sym, existing_bars, tf_label, summary_filename, delta_key, include_diag_levels=True
                )
                return {
                    "bars_count": len(rows),
                    "with_fp_bars": with_fp_cnt,
                    "buckets_available": with_fp_cnt > 0,
                    "source": "disk_cache_stale_preserved",
                    delta_key: round(c_delta, 2),
                    "cum_delta": round(c_delta, 2),
                    "summary_file": f"data/telemetry/footprint/{sym}/{summary_filename}",
                    "bars_file": f"data/telemetry/footprint/{sym}/{raw_filename}" if raw_filename else None,
                    "recent_bars_tail": rows[-(8 if itv == "m15" else 6):] if rows else [],
                }
            return None

        new_bars = fp_res.get("bars") or []
        new_has_fp = bool(fp_res.get("bucketsAvailable") and any(isinstance(b, dict) and b.get("fp") for b in new_bars))
        # Non-clobbering guard: never overwrite good Coinglass fp buckets on disk with empty binance-fapi fallback rows!
        if existing_has_fp and not new_has_fp:
            rows, c_delta, with_fp_cnt = _summarize_fp_bars_with_diag(
                sym, existing_bars, tf_label, summary_filename, delta_key, include_diag_levels=True
            )
            fp_meta = {
                "bars_count": len(rows),
                "with_fp_bars": with_fp_cnt,
                "buckets_available": True,
                "source": "coinglass_preserved_on_fallback",
                delta_key: round(c_delta, 2),
                "cum_delta": round(c_delta, 2),
                "summary_file": f"data/telemetry/footprint/{sym}/{summary_filename}",
                "bars_file": f"data/telemetry/footprint/{sym}/{raw_filename}" if raw_filename else None,
                "recent_bars_tail": rows[-(8 if itv == "m15" else 6):] if rows else [],
            }
            del existing_bars
            return fp_meta

        # Self-healing guard: if existing bars were all fallback placeholders and Coinglass succeeded now, replace cleanly
        base_bars = existing_bars if existing_has_fp else []
        merged_bars = _merge_fp_bars(base_bars, new_bars, max_keep=target_bars)
        rows, c_delta, with_fp_cnt = _summarize_fp_bars_with_diag(
            sym, merged_bars, tf_label, summary_filename, delta_key, include_diag_levels=True
        )

        # Persist raw bars file WITH fp arrays for Prime or stale files (streamed cleanly, deleted after write)
        if raw_path and merged_bars:
            atomic_write(raw_path, {
                "sym": sym,
                "timeframe": tf_label,
                "source": fp_res.get("source"),
                "bucketsAvailable": fp_res.get("bucketsAvailable"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total_bars": len(merged_bars),
                "with_fp_bars": with_fp_cnt,
                "bars": merged_bars,
            })

        tail_count = 8 if itv == "m15" else 6
        fp_meta = {
            "bars_count": len(rows),
            "with_fp_bars": with_fp_cnt,
            "buckets_available": fp_res.get("bucketsAvailable"),
            "source": fp_res.get("source"),
            delta_key: round(c_delta, 2),
            "cum_delta": round(c_delta, 2),
            "summary_file": f"data/telemetry/footprint/{sym}/{summary_filename}",
            "bars_file": f"data/telemetry/footprint/{sym}/{raw_filename}" if raw_filename else None,
            "recent_bars_tail": rows[-tail_count:] if rows else [],
        }
        del merged_bars
        del existing_bars
        if ttl_secs > 0:
            CACHES["fp_meta"].set(cache_key, fp_meta)
        return fp_meta

    async def harvest_single_asset(self, sym: str, is_prime: bool = True, mt5_quote: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
        t0 = time.time()
        iso_now = datetime.now(timezone.utc).isoformat()
        existing_profile = _read_json_if_exists(os.path.join(ASSETS_DIR, f"{sym}.json"))
        existing_smap: Dict[str, Any] = {}
        existing_keep: Dict[str, Any] = {}
        if isinstance(existing_profile, dict):
            raw_smap = (existing_profile.get("liquidations") or {}).get("stopmap")
            if isinstance(raw_smap, dict):
                existing_smap = {
                    k: v for k, v in raw_smap.items()
                    if isinstance(v, dict) and k != "rolling_archive_shared_across"
                }
            for k in (
                "market_structure", "narrative_engine", "athena_oracle",
                "microstructure_l2_l3", "quant_positioning", "options_deribit",
                "playbook_cards", "backtest_metrics", "cot_report", "funding_and_oi",
            ):
                if existing_profile.get(k):
                    existing_keep[k] = existing_profile[k]
            if (existing_profile.get("liquidations") or {}).get("clusters"):
                existing_keep["liq_clusters"] = existing_profile["liquidations"]["clusters"]
            if (existing_profile.get("liquidations") or {}).get("live_prints_tape"):
                existing_keep["liq_tape"] = existing_profile["liquidations"]["live_prints_tape"]
            if existing_profile.get("book_walls"):
                existing_keep["book_walls"] = existing_profile["book_walls"]
            if existing_profile.get("cvd"):
                existing_keep["cvd"] = existing_profile["cvd"]
            if (existing_profile.get("footprint") or {}).get("signals"):
                existing_keep["fp_signals"] = existing_profile["footprint"]["signals"]
        del existing_profile
        profile: Dict[str, Any] = {
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

        # Group 1: Core real-time endpoints concurrently
        mkt_task = self.fetch_json(f"/api/cg/market?sym={sym}", timeout=12, retries=2)
        tk_task = self.fetch_json(f"/api/cg/ticker?sym={sym}", timeout=8, retries=2)
        narr_task = self.fetch_json(f"/api/cg/narrative?sym={sym}", timeout=8)
        orc_task = self.fetch_json(f"/api/cg/oracle?sym={sym}", timeout=16)
        micro_task = self.fetch_json(f"/api/cg/micro?sym={sym}", timeout=8)
        qnt_task = self.fetch_json(f"/api/cg/quant?sym={sym}", timeout=8)
        fnd_task = self.fetch_json(f"/api/cg/funding?sym={sym}", timeout=8)
        oi_task = self.fetch_json(f"/api/cg/oi?sym={sym}", timeout=8)
        ll_task = self.fetch_json(f"/api/cg/liq-live?sym={sym}", timeout=6)

        mkt, tk, narr, orc, micro, qnt, fnd, oi, ll = await asyncio.gather(
            mkt_task, tk_task, narr_task, orc_task, micro_task, qnt_task, fnd_task, oi_task, ll_task
        )

        if mkt and mkt.get("ok"):
            profile["market_structure"] = {
                "price": mkt.get("price"),
                "auction": mkt.get("auction"),
                "vwap": mkt.get("vwap"),
                "emas": mkt.get("ema") or mkt.get("emas"),
                "rsi": mkt.get("rsi"),
                "levels": mkt.get("levels"),
            }
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

        if orc and orc.get("ok"):
            profile["athena_oracle"] = {
                "score": orc.get("score"),
                "verdict": orc.get("verdict"),
                "regime": orc.get("regime"),
                "factors": orc.get("factors"),
                "partial": orc.get("partial"),
            }

        if micro and micro.get("ok"):
            profile["microstructure_l2_l3"] = {
                "hl": micro.get("hl"),
                "lighter": micro.get("lighter"),
                "bbo": micro.get("bbo"),
                "missingVenues": micro.get("missingVenues"),
            }
        if sym in PERPDEPTH_SYMS:
            pd_res = await self.cached_get("perpdepth", sym, 300, f"/api/cg/perpdepth?sym={sym}&quiet=1", timeout=8)
            if pd_res and pd_res.get("ok") and pd_res.get("supported", True):
                profile["microstructure_l2_l3"] = profile.get("microstructure_l2_l3") or {}
                profile["microstructure_l2_l3"]["tradfi_perpdepth"] = {
                    "lead": pd_res.get("lead"),
                    "liquidity": pd_res.get("liquidity"),
                    "note": pd_res.get("note"),
                }

        if qnt and qnt.get("ok"):
            prev_q = existing_keep.get("quant_positioning") or {}
            new_ls = dict(qnt.get("lsRatios") or {})
            prev_ls = prev_q.get("lsRatios") or {}
            for tf_k, p_val in prev_ls.items():
                cur_val = new_ls.get(tf_k)
                if (not isinstance(cur_val, dict) or cur_val.get("global") is None) and isinstance(p_val, dict) and p_val.get("global") is not None:
                    new_ls[tf_k] = p_val
            new_oiq = dict(qnt.get("oiQuadrant") or {})
            prev_oiq = prev_q.get("oiQuadrant") or {}
            for tf_k in ("1h", "4h", "24h"):
                if new_oiq.get(tf_k) is None and prev_oiq.get(tf_k) is not None:
                    new_oiq[tf_k] = prev_oiq[tf_k]
            new_basis = dict(qnt.get("basis") or {})
            prev_basis = prev_q.get("basis") or {}
            for bk in ("perpPrice", "basisPct"):
                if new_basis.get(bk) is None and prev_basis.get(bk) is not None:
                    new_basis[bk] = prev_basis[bk]
            profile["quant_positioning"] = {
                "lsRatios": new_ls,
                "oiQuadrant": new_oiq,
                "funding": qnt.get("funding") or prev_q.get("funding"),
                "basis": new_basis,
                "notes": qnt.get("notes"),
            }

        if fnd and fnd.get("ok"):
            f_agg = dict(fnd.get("aggregate") or {})
            pct8h = f_agg.get("fundingPct8h")
            if pct8h is not None:
                f_agg["apr_pct"] = round(float(pct8h) * 3.0 * 365.0, 2)
            profile["funding_and_oi"]["funding"] = fnd.get("venues")
            profile["funding_and_oi"]["funding_aggregate"] = f_agg

        if oi and oi.get("ok"):
            profile["funding_and_oi"]["open_interest"] = oi.get("venues")
            profile["funding_and_oi"]["oi_aggregate"] = oi.get("aggregate")

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

        # Group 2: Options (BTC/ETH), Clusters, Stopmaps, Book Walls, CVD, LTO, Backtest, COT, Signals
        ext_ttl = 0.0 if is_prime else 900.0
        is_deep_core = sym in DEEP_MATRIX_SYMBOLS
        tasks: Dict[str, Any] = {}

        if sym in ("BTC", "ETH"):
            tasks["opt"] = self.fetch_json(f"/api/cg/options?sym={sym}", timeout=8)

        # Liquidation Clusters 3d / 7d / 14d / 30d (fast cache/REST)
        for rng in ("3d", "7d", "14d", "30d"):
            tasks[f"liq_{rng}"] = self.cached_get(
                "liq", (sym, rng), 90.0 if is_prime else 900.0,
                f"/api/cg/liqlevels?sym={sym}&range={rng}", timeout=12, heavy=False
            )

        # Fetch Model 1 24h (all) & Model 1 3d (Prime) with polite Coinglass fapi pacing
        sm_first_tasks = [
            self.cached_get(
                "stopmap",
                (sym, "1", "coin", "24h"),
                180.0 if is_prime else 900.0,
                f"/api/cg/stopmap?sym={sym}&model=1&mode=coin&range=24h",
                timeout=22,
                transform=summarize_stopmap_payload,
                heavy=True,
                fapi_pace=True,
            )
        ]
        if is_prime:
            sm_first_tasks.append(
                self.cached_get(
                    "stopmap",
                    (sym, "1", "coin", "3d"),
                    300.0,
                    f"/api/cg/stopmap?sym={sym}&model=1&mode=coin&range=3d",
                    timeout=22,
                    transform=summarize_stopmap_payload,
                    heavy=True,
                    fapi_pace=True,
                )
            )
        sm_first_res = await asyncio.gather(*sm_first_tasks, return_exceptions=True)
        sm_m1_24h = sm_first_res[0] if not isinstance(sm_first_res[0], Exception) else None
        sm_m1_3d = sm_first_res[1] if (len(sm_first_res) > 1 and not isinstance(sm_first_res[1], Exception)) else None

        def _merge_stopmap_slot(slot_name: str, new_val: Optional[Dict[str, Any]]) -> None:
            prev_val = existing_smap.get(slot_name)
            prev_is_native = isinstance(prev_val, dict) and prev_val.get("kind") == "native_heatmap_grid"
            new_is_native = isinstance(new_val, dict) and new_val.get("kind") == "native_heatmap_grid"
            if new_is_native:
                profile["liquidations"]["stopmap"][slot_name] = new_val
            elif prev_is_native:
                preserved = dict(prev_val)
                preserved["preserved_on_fallback"] = True
                profile["liquidations"]["stopmap"][slot_name] = preserved
            elif new_val:
                profile["liquidations"]["stopmap"][slot_name] = new_val
            elif prev_val:
                preserved = dict(prev_val)
                preserved["preserved_on_fallback"] = True
                profile["liquidations"]["stopmap"][slot_name] = preserved

        # Seed existing valid stopmap slots first so transient fallback never erases multi-horizon data
        for ex_slot in list(existing_smap.keys()):
            _merge_stopmap_slot(ex_slot, None)
        _merge_stopmap_slot("m1_coin_24h", sm_m1_24h)
        if is_prime:
            _merge_stopmap_slot("m1_coin_3d", sm_m1_3d)

        is_native_sm = bool(
            (sm_m1_24h and sm_m1_24h.get("kind") == "native_heatmap_grid")
            or (sm_m1_3d and sm_m1_3d.get("kind") == "native_heatmap_grid")
            or any(
                isinstance(v, dict) and v.get("kind") == "native_heatmap_grid"
                for v in existing_smap.values()
            )
        )
        sm_specs: List[Tuple[str, str, str, str, float]] = []
        if is_native_sm or is_deep_core:
            sm_specs.append(("m1_coin_7d", "1", "coin", "7d", 900.0 if is_prime else 2400.0))
            if not is_prime:
                sm_specs.append(("m1_coin_3d", "1", "coin", "3d", 1200.0))
            if is_deep_core:
                sm_specs.extend([
                    ("m2_coin_3d", "2", "coin", "3d", 900.0),
                    ("m3_coin_3d", "3", "coin", "3d", 900.0),
                ])
            # Extended multi-horizon heatmaps (30d/14d/48h/12h/90d) with smart cold-miss staggering to avoid fapi 429
            ext_specs = [
                ("m1_coin_30d", "1", "coin", "30d", 1800.0 if is_prime else 3600.0),
                ("m1_coin_14d", "1", "coin", "14d", 1800.0 if is_prime else 3600.0),
                ("m1_coin_48h", "1", "coin", "48h", 900.0 if is_prime else 2400.0),
                ("m1_coin_12h", "1", "coin", "12h", 900.0 if is_prime else 2400.0),
                ("m1_coin_90d", "1", "coin", "90d", 3600.0),
            ]
            # Prioritize missing horizons first, then stale ones; bounded cold network fetches per asset cycle
            ext_specs.sort(
                key=lambda sp: (
                    1 if (isinstance(existing_smap.get(sp[0]), dict) and existing_smap[sp[0]].get("kind") == "native_heatmap_grid") else 0,
                    0 if sp[3] in ("30d", "14d", "48h", "12h") else 1,
                )
            )
            cold_budget = int(os.environ.get("TELEMETRY_SM_COLD_BUDGET", "3"))
            cold_used = 0
            for slot, mdl, mode, rng, ttl_s in ext_specs:
                cache_hit = CACHES["stopmap"].get((sym, mdl, mode, rng), ttl_secs=ttl_s, allow_stale=False)
                if cache_hit is not None or cold_budget <= 0 or cold_used < cold_budget:
                    sm_specs.append((slot, mdl, mode, rng, ttl_s))
                    if cache_hit is None:
                        cold_used += 1
        if sym == "BTC":
            sm_specs.append(("m1_pair_24h", "1", "pair", "24h", 180.0))

        for slot, mdl, mode, rng, ttl_s in sm_specs:
            tasks[f"sm_{slot}"] = self.cached_get(
                "stopmap",
                (sym, mdl, mode, rng),
                ttl_s,
                f"/api/cg/stopmap?sym={sym}&model={mdl}&mode={mode}&range={rng}",
                timeout=22,
                transform=summarize_stopmap_payload,
                heavy=True,
                fapi_pace=True,
            )

        # Book Walls across timeframes (m15/1h + spot m15 for all; 1m/5m/spot-1h for top 26 Core Flow; 4h for Deep Core)
        is_core_flow = is_deep_core or sym in {
            "SUI", "UNI", "OP", "ARB", "APT", "DOT", "LTC", "BCH", "INJ", "TIA", "QQQ", "TSLA", "MSTR", "GOOGL"
        }
        bw_specs = [
            ("futures", "m15", 240.0 if is_prime else 900.0, 150),
            ("futures", "1h", 420.0 if is_prime else 900.0, 150),
            ("spot", "m15", 300.0 if is_prime else 900.0, 150),
        ]
        if is_core_flow:
            bw_specs.extend([
                ("futures", "1m", 480.0, 150),
                ("futures", "5m", 600.0, 150),
                ("spot", "1h", 900.0, 150),
            ])
        if is_deep_core:
            bw_specs.append(("futures", "4h", 1800.0, 60))

        for mkt_type, itv, ttl_s, lim in bw_specs:
            tasks[f"bw_{mkt_type}_{itv}"] = self.cached_get(
                "bw",
                (sym, mkt_type, itv, lim),
                ttl_s,
                f"/api/cg/bookwalls?sym={sym}&market={mkt_type}&interval={itv}&limit={lim}&band=5",
                timeout=28,
                transform=build_compact_bookwalls,
                heavy=True,
            )

        # CVD (perp/spot/split on m15/h1)
        cvd_modes = ("perp", "spot", "split") if is_prime else ("perp", "spot")
        for c_mode in cvd_modes:
            for c_itv in ("m15", "h1"):
                tasks[f"cvd_{c_mode}_{c_itv}"] = self.cached_get(
                    "cvd", (sym, c_mode, c_itv), 90.0 if is_prime else 900.0,
                    f"/api/cg/cvd?sym={sym}&mode={c_mode}&itv={c_itv}&anchor=day", timeout=14, heavy=False
                )

        # Large orders, LTO, Playbook cards, Backtest, COT, Signals
        tasks["lo"] = self.cached_get("lo", sym, 300.0 if is_prime else 900.0, f"/api/cg/largeorder?symbol=Binance_{sym}USDT&interval=m15&hours=24", timeout=10, heavy=False)
        tasks["lto"] = self.cached_get("lto", sym, 180.0 if is_prime else 900.0, f"/api/cg/lto?symbol={sym}", timeout=12, heavy=False)
        if is_prime:
            tasks["cards"] = self.cached_get("cards", sym, 600.0, f"/api/strategy/cards?sym={sym}", timeout=8, heavy=False)
        tasks["bt"] = self.cached_get(
            "backtest",
            sym,
            1800.0,
            f"/api/cg/backtest?sym={sym}",
            timeout=12,
            transform=lambda b: {
                "metricsAll": b.get("metricsAll"),
                "byModel": b.get("byModel"),
                "byRegime": b.get("byRegime"),
                "tradeCount": len(b.get("trades") or []),
            },
        )
        tasks["cot"] = self.cached_get("cot", sym, 3600.0, f"/api/cg/cot?sym={sym}&quiet=1", timeout=6)
        tasks["sg"] = self.cached_get("fp_sig", sym, 90.0 if is_prime else 900.0, f"/api/cg/signals?sym={sym}&bt=all", timeout=14, heavy=False)

        results_list = await asyncio.gather(*tasks.values(), return_exceptions=True)
        res_map = {k: (v if not isinstance(v, Exception) else None) for k, v in zip(tasks.keys(), results_list)}

        # Populate Options
        opt = res_map.get("opt")
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

        # Populate Liq Clusters (3d / 7d / 14d / 30d)
        for rng in ("3d", "7d", "14d", "30d"):
            l_res = res_map.get(f"liq_{rng}")
            if l_res and l_res.get("ok"):
                profile["liquidations"]["clusters"][rng] = {
                    "stats": l_res.get("stats"),
                    "venues": l_res.get("venues"),
                    "livePrice": l_res.get("livePrice"),
                }

        # Populate Stopmaps (non-clobbering: preserve native grids on transient fallback)
        for slot, _, _, _, _ in sm_specs:
            _merge_stopmap_slot(slot, res_map.get(f"sm_{slot}"))
        if sm_m1_24h and not is_native_sm and "m1_coin_3d" not in profile["liquidations"]["stopmap"]:
            # Honest annotation when symbol relies on local rolling snapshot archive (range/model-invariant)
            profile["liquidations"]["stopmap"]["rolling_archive_shared_across"] = ["m1_coin_12h", "m1_coin_24h", "m1_coin_48h", "m1_coin_3d", "m1_coin_7d", "m1_coin_14d", "m1_coin_30d"]

        # Populate Book Walls & grid-aligned live_binance
        m15_grid = None
        for mkt_type, itv, _, _ in bw_specs:
            bw_val = res_map.get(f"bw_{mkt_type}_{itv}")
            if bw_val:
                profile["book_walls"][mkt_type][itv] = bw_val
                if mkt_type == "futures" and itv == "m15" and bw_val.get("grid"):
                    m15_grid = bw_val.get("grid")

        if is_prime:
            lb_ep = f"/api/cg/livebook?sym={sym}&band=5&oneshot=1" + (f"&grid={m15_grid}" if m15_grid else "")
            lb = await self.fetch_json(lb_ep, timeout=7, retries=1)
            if lb and lb.get("ok"):
                lb_compact = build_compact_bookwalls(lb, strip_tail=2, max_ladder=120)
                lb_compact["covered"] = lb.get("covered")
                lb_compact["fapiBanned"] = lb.get("fapiBanned")
                profile["book_walls"]["live_binance"] = lb_compact

        # Populate CVD
        for c_mode in cvd_modes:
            for c_itv in ("m15", "h1"):
                cv = res_map.get(f"cvd_{c_mode}_{c_itv}")
                if cv and cv.get("ok"):
                    profile["cvd"][c_mode][c_itv] = {
                        "kpis": cv.get("kpis"),
                        "divergences": cv.get("divergences", []),
                        "quadrant": cv.get("quadrant"),
                    }

        # Populate Large Orders / LTO / Cards / Backtest / COT (compact large_orders sub-lists <= 35 rows to avoid 500KB bloat)
        def _compact_groups(grps: Any, max_groups: int = 12, max_items: int = 35) -> List[Dict[str, Any]]:
            out_g: List[Dict[str, Any]] = []
            for g in (grps or [])[:max_groups]:
                if not isinstance(g, dict):
                    continue
                row_g = dict(g)
                if isinstance(g.get("list"), list) and len(g["list"]) > max_items:
                    row_g["total_list_len"] = len(g["list"])
                    row_g["list"] = g["list"][:max_items]
                out_g.append(row_g)
            return out_g

        prev_lo = existing_keep.get("large_orders") or {}
        lo = res_map.get("lo")
        if lo and lo.get("ok"):
            grps_lo = _compact_groups(lo.get("groups"), 12, 35)
            if grps_lo:
                profile["large_orders"]["resting_and_executed"]["24h"] = grps_lo
            elif isinstance(prev_lo.get("resting_and_executed"), dict) and prev_lo["resting_and_executed"].get("24h"):
                profile["large_orders"]["resting_and_executed"]["24h"] = prev_lo["resting_and_executed"]["24h"]
            if lo.get("reason"):
                profile["large_orders"]["largeorder_status"] = lo.get("reason")
        elif isinstance(prev_lo.get("resting_and_executed"), dict) and prev_lo["resting_and_executed"].get("24h"):
            profile["large_orders"]["resting_and_executed"]["24h"] = prev_lo["resting_and_executed"]["24h"]

        prev_lto = prev_lo.get("large_taker_orders_100k")
        lto = res_map.get("lto")
        if lto and lto.get("ok"):
            pe = lto.get("perExchange") or {}
            active_exch = [k for k, v in pe.items() if isinstance(v, dict) and v.get("hasData")]
            grps_lto = _compact_groups(lto.get("groups"), 12, 35)
            if not grps_lto and isinstance(prev_lto, dict) and prev_lto.get("groups"):
                preserved_lto = dict(prev_lto)
                if lto.get("hlFallback"):
                    preserved_lto["hlFallback"] = lto.get("hlFallback")
                preserved_lto["preserved_on_fallback"] = True
                profile["large_orders"]["large_taker_orders_100k"] = preserved_lto
            else:
                profile["large_orders"]["large_taker_orders_100k"] = {
                    "supported": lto.get("supported"),
                    "minVolUsd": lto.get("minVolUsd"),
                    "groups": grps_lto,
                    "activeExchanges": active_exch,
                    "hlFallback": lto.get("hlFallback"),
                    "updatedAt": lto.get("updatedAt"),
                }
        elif isinstance(prev_lto, dict):
            preserved_lto = dict(prev_lto)
            preserved_lto["preserved_on_fallback"] = True
            profile["large_orders"]["large_taker_orders_100k"] = preserved_lto
        cards = res_map.get("cards")
        if cards and not cards.get("_err"):
            asset_cards = [c for a in cards.get("assets", []) if a.get("sym") == sym for c in a.get("cards", [])]
            profile["playbook_cards"] = {"status": "triggered" if cards.get("ok") else "monitoring", "cards": asset_cards}
        bt = res_map.get("bt")
        if bt:
            profile["backtest_metrics"] = bt
        cot = res_map.get("cot")
        if cot and not cot.get("_err"):
            profile["cot_report"] = cot

        # Signals (`all`, `time`, `vol`, `range`) -> save both profile & signals_7d.json
        sg = res_map.get("sg")
        if sg and sg.get("ok"):
            sets = sg.get("sets") or []
            all_sigs: List[Dict[str, Any]] = []
            sigs_by_bt: Dict[str, Any] = {
                "all": {
                    "counts": sg.get("counts"),
                    "thresholds": sg.get("thresholds"),
                    "sets": [
                        {
                            "bt": s.get("bt"),
                            "label": s.get("label"),
                            "count": len(s.get("sigs") or []),
                            "sigs": (s.get("sigs") or [])[-20:],
                        }
                        for s in sets if isinstance(s, dict)
                    ],
                }
            }
            for s_set in sets:
                if not isinstance(s_set, dict):
                    continue
                bt_key = s_set.get("bt")
                s_list = s_set.get("sigs") or []
                if bt_key:
                    sigs_by_bt[bt_key] = {
                        "counts": sg.get("counts"),
                        "thresholds": sg.get("thresholds"),
                        "label": s_set.get("label"),
                        "count": len(s_list),
                        "sigs": s_list[-20:],
                    }
                for item in s_list:
                    if isinstance(item, dict):
                        all_sigs.append(dict(item, bt=bt_key))
            sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
            os.makedirs(sym_fp_dir, exist_ok=True)
            sig_path = os.path.join(sym_fp_dir, "signals_7d.json")
            existing_sig_recent = False
            if len(all_sigs) == 0 and os.path.exists(sig_path) and (time.time() - os.path.getmtime(sig_path)) < 1800:
                prev_sig = _read_json_if_exists(sig_path)
                if isinstance(prev_sig, dict) and (prev_sig.get("total_signals") or 0) > 0:
                    existing_sig_recent = True
            if not existing_sig_recent:
                profile["footprint"]["signals"] = sigs_by_bt
                atomic_write(sig_path, {
                    "sym": sym,
                    "updated_at": iso_now,
                    "counts": sg.get("counts"),
                    "thresholds": sg.get("thresholds"),
                    "total_signals": len(all_sigs),
                    "by_bt": {s.get("bt"): {"label": s.get("label"), "count": len(s.get("sigs") or [])} for s in sets if isinstance(s, dict)},
                    "signals": all_sigs,
                })

        # Footprint Timeframes (15m 7d incremental splice + 1h 7d incremental splice; extra m5/m30/h4 only if buckets supported!)
        h7_meta, h1_meta = await asyncio.gather(
            self._harvest_fp_timeframe(
                sym, "m15", "15m", "bars_15m_7d.json", "summary_7d.json", "cum_delta_7d",
                target_bars=672, delta_limit=32, is_prime=is_prime, ttl_secs=0.0 if is_prime else 900.0
            ),
            self._harvest_fp_timeframe(
                sym, "h1", "1h", "bars_1h.json", "summary_1h.json", "cum_delta_1h",
                target_bars=168, delta_limit=12, is_prime=is_prime, ttl_secs=180.0 if is_prime else 900.0
            ),
        )
        if h7_meta:
            profile["footprint"]["history_7d"] = h7_meta
        if h1_meta:
            profile["footprint"]["history_1h"] = h1_meta

        mtf_summary: Dict[str, Any] = {}
        if h7_meta:
            mtf_summary["m15"] = self._compact_tf_rollup(h7_meta)
        if h1_meta:
            mtf_summary["h1"] = self._compact_tf_rollup(h1_meta)

        has_buckets = bool((h7_meta and h7_meta.get("buckets_available")) or (h1_meta and h1_meta.get("buckets_available")))
        if is_prime and has_buckets:
            extra_tfs = [("m5", "5m", 300.0), ("m30", "30m", 600.0), ("h4", "4h", 900.0)]
            extra_res = await asyncio.gather(
                *(self._probe_fp_summary_light(sym, itv, lbl, ttl_s) for itv, lbl, ttl_s in extra_tfs),
                return_exceptions=True,
            )
            for (itv, _, _), r in zip(extra_tfs, extra_res):
                if not isinstance(r, Exception) and r:
                    mtf_summary[itv] = r
        if mtf_summary:
            profile["footprint"]["multi_tf"] = mtf_summary
            sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
            os.makedirs(sym_fp_dir, exist_ok=True)
            atomic_write(os.path.join(sym_fp_dir, "summary_mtf.json"), {
                "sym": sym,
                "updated_at": iso_now,
                "timeframes": mtf_summary,
            })

        # Preserve prior valid non-empty sections on transient upstream 429/fapi cooldowns
        for k in (
            "market_structure", "narrative_engine", "athena_oracle",
            "microstructure_l2_l3", "quant_positioning", "options_deribit",
            "playbook_cards", "backtest_metrics", "cot_report",
        ):
            if not profile.get(k) and existing_keep.get(k):
                profile[k] = existing_keep[k]
        prev_foi = existing_keep.get("funding_and_oi") or {}
        if isinstance(prev_foi, dict):
            for foi_k in ("funding", "funding_aggregate", "open_interest", "oi_aggregate"):
                if not profile["funding_and_oi"].get(foi_k) and prev_foi.get(foi_k):
                    profile["funding_and_oi"][foi_k] = prev_foi[foi_k]
        if not profile["liquidations"]["clusters"] and existing_keep.get("liq_clusters"):
            profile["liquidations"]["clusters"] = existing_keep["liq_clusters"]
        if not profile["liquidations"]["live_prints_tape"] and existing_keep.get("liq_tape"):
            profile["liquidations"]["live_prints_tape"] = existing_keep["liq_tape"]
        for mkt_k in ("futures", "spot"):
            if not profile["book_walls"][mkt_k] and ((existing_keep.get("book_walls") or {}).get(mkt_k)):
                profile["book_walls"][mkt_k] = existing_keep["book_walls"][mkt_k]
        if not profile["book_walls"].get("live_binance") and ((existing_keep.get("book_walls") or {}).get("live_binance")):
            profile["book_walls"]["live_binance"] = existing_keep["book_walls"]["live_binance"]
        for c_k in ("perp", "spot", "split"):
            if not profile["cvd"][c_k] and ((existing_keep.get("cvd") or {}).get(c_k)):
                profile["cvd"][c_k] = existing_keep["cvd"][c_k]
        if not profile["footprint"]["signals"] and existing_keep.get("fp_signals"):
            profile["footprint"]["signals"] = existing_keep["fp_signals"]
        del existing_keep

        # Stream-write full asset profile immediately and keep ONLY compact 2 KB summary in RAM!
        target_file = os.path.join(ASSETS_DIR, f"{sym}.json")
        bytes_written = atomic_write(target_file, profile)
        summary = build_compact_asset_summary(profile)
        del profile
        elapsed = round(time.time() - t0, 2)
        tag = "P" if is_prime else "E"
        print(f"  [telemetry:{tag}] {sym:9s} -> {bytes_written/1024:6.1f} KB in {elapsed:5.2f}s", flush=True)
        return sym, summary

    @staticmethod
    def _compact_tf_rollup(meta: Dict[str, Any]) -> Dict[str, Any]:
        tail = meta.get("recent_bars_tail") or []
        closed = tail[-2] if len(tail) >= 2 else (tail[-1] if tail else None)
        return {
            "bars_count": meta.get("bars_count"),
            "with_fp_bars": meta.get("with_fp_bars"),
            "buckets_available": meta.get("buckets_available"),
            "source": meta.get("source"),
            "cum_delta": meta.get("cum_delta"),
            "latest_closed": closed,
        }

    async def _probe_fp_summary_light(self, sym: str, itv: str, tf_label: str, ttl_secs: float) -> Optional[Dict[str, Any]]:
        key = (sym, f"mtf_{itv}")
        hit = CACHES["fp_meta"].get(key, ttl_secs=ttl_secs)
        if hit is not None:
            return hit
        res = await self.fetch_json(f"/api/cg/fp?sym={sym}&itv={itv}&limit=48", timeout=10, heavy=True)
        if not (res and res.get("ok") and res.get("bars")):
            return CACHES["fp_meta"].get(key, ttl_secs=ttl_secs, allow_stale=True)
        bars = res.get("bars") or []
        cum_delta = 0.0
        with_fp = 0
        last_closed_row = None
        for idx, b in enumerate(bars):
            fp = b.get("fp")
            b_arr = fp.get("b", []) if isinstance(fp, dict) else []
            s_arr = fp.get("s", []) if isinstance(fp, dict) else []
            if b_arr and s_arr:
                with_fp += 1
            buy_v = float(sum(b_arr)) if b_arr else 0.0
            sell_v = float(sum(s_arr)) if s_arr else 0.0
            delta = buy_v - sell_v
            cum_delta += delta
            if idx == max(0, len(bars) - 2):
                diag = compute_diagonal_imbalances(fp) if fp else {}
                last_closed_row = {
                    "ts": b.get("ts"),
                    "buy_vol": round(buy_v, 2),
                    "sell_vol": round(sell_v, 2),
                    "delta": round(delta, 2),
                    "diag_buy_3x": diag.get("buy_3x_count", 0),
                    "diag_sell_3x": diag.get("sell_3x_count", 0),
                }
        out = {
            "timeframe": tf_label,
            "bars_count": len(bars),
            "with_fp_bars": with_fp,
            "buckets_available": res.get("bucketsAvailable"),
            "source": res.get("source"),
            "cum_delta": round(cum_delta, 2),
            "latest_closed": last_closed_row,
        }
        CACHES["fp_meta"].set(key, out)
        return out


def generate_authoritative_datasets(summaries: Dict[str, Dict[str, Any]], screener_data: Dict[str, Any]) -> None:
    """
    Writes compact (< 3 MB total!) authoritative datasets so downstream callers stay
    100% compatible without duplicating heavy per-asset raw arrays in RAM.
    """
    print("[telemetry] Writing compact authoritative datasets...", flush=True)
    iso_now = datetime.now(timezone.utc).isoformat()

    # 1. Compact live_orderflow_daemon.json (summaries + pointers instead of 200 MB raw bloat)
    atomic_write(os.path.join(TELEMETRY_DIR, "live_orderflow_daemon.json"), {
        "metadata": {
            "timestamp_utc": iso_now,
            "version": "5.0.0-async-zero-leak",
            "assets_harvested": len(summaries),
            "host": "antigravity-local-terminal",
        },
        "profiles": summaries,
    })

    # 2. deep_orderflow_matrix.json (reads fresh on-disk assets for DEEP_MATRIX_SYMBOLS, bounded <= 14 assets)
    deep_matrix: Dict[str, Any] = {}
    for sym in DEEP_MATRIX_SYMBOLS:
        p = _read_json_if_exists(os.path.join(ASSETS_DIR, f"{sym}.json"))
        if not isinstance(p, dict):
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
            "footprint_signals": (p.get("footprint") or {}).get("signals"),
            "footprint_history": (p.get("footprint") or {}).get("history_7d"),
            "footprint_history_1h": (p.get("footprint") or {}).get("history_1h"),
            "large_orders": p.get("large_orders"),
            "narrative_engine": p.get("narrative_engine"),
            "athena_oracle": p.get("athena_oracle"),
        }
        del p
    atomic_write(os.path.join(TELEMETRY_DIR, "deep_orderflow_matrix.json"), deep_matrix)
    del deep_matrix

    # 3. full_universe_live_telemetry.json (preserves both screener.rows AND screener_rows + compact summaries)
    rows = screener_data.get("rows", []) if isinstance(screener_data, dict) else []
    atomic_write(os.path.join(TELEMETRY_DIR, "full_universe_live_telemetry.json"), {
        "metadata": {
            "timestamp_utc": iso_now,
            "version": "5.0.0-async-zero-leak",
            "total_assets": len(rows),
            "warm_count": screener_data.get("warmCount") if isinstance(screener_data, dict) else None,
            "deep_harvested_count": len(summaries),
        },
        "screener": {"rows": rows, "warmCount": screener_data.get("warmCount") if isinstance(screener_data, dict) else None},
        "screener_rows": rows,
        "deep_profiles": summaries,
    })

    # 4. quant_report_structured.json
    short_candidates, long_candidates = [], []
    for sym, s in summaries.items():
        mkt = s.get("market_structure") or {}
        px = s.get("price")
        if not px or px <= 0:
            continue
        vwap_data = mkt.get("vwap") or {}
        vwap_val = vwap_data.get("vwap")
        vwap_std = vwap_data.get("std") or ((vwap_data.get("bands") or {}).get("sd1"))
        vwap_z = (px - vwap_val) / vwap_std if (vwap_val and vwap_std and vwap_std > 0) else 0.0
        rsi_15m = (mkt.get("rsi") or {}).get("m15") or 50.0
        emas_m15 = (mkt.get("emas") or {}).get("m15") or {}
        e89_m15, e233_m15 = emas_m15.get("e89"), emas_m15.get("e233")
        trend_short_tc09 = bool(e89_m15 and e233_m15 and px < e89_m15 and px < e233_m15)
        trend_long_tc09 = bool(e89_m15 and e233_m15 and px > e89_m15 and px > e233_m15)
        day_auc = (mkt.get("auction") or {}).get("day") or {}
        day_val, day_vah = day_auc.get("val"), day_auc.get("vah")
        fp_sum = s.get("footprint_summary") or {}
        cum_delta_7d = fp_sum.get("cum_delta_7d") or 0.0
        oracle_score = (s.get("athena_oracle") or {}).get("score")
        oracle_val = oracle_score if oracle_score is not None else 50
        regime = (s.get("narrative_engine") or {}).get("regime", "UNKNOWN")
        largest_level = (s.get("stopmap_summary") or {}).get("largest_24h")

        score_short, score_long = 0.0, 0.0
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
            "feed_type": s.get("feed_type", "coinglass_perp"),
            "mt5_sym": (s.get("mt5") or {}).get("symbol"),
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
            "model": "Multi-Factor Confluence Formulation (Coinglass + Full MT5 + UI Tab Parity)",
            "evaluator": "Antigravity Quant Engine v5.0",
        },
        "top_short_confluence_candidates": short_candidates[:20],
        "top_long_confluence_candidates": long_candidates[:20],
        "prime_assets_evaluated": len(summaries),
    })


def _git_sync_worker() -> None:
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


def maybe_trigger_git_sync(git_sync_secs: int) -> None:
    global _LAST_GIT_SYNC
    if git_sync_secs <= 0:
        return
    now = time.time()
    if now - _LAST_GIT_SYNC < git_sync_secs:
        return
    _LAST_GIT_SYNC = now
    threading.Thread(target=_git_sync_worker, daemon=True).start()


def build_coinglass_universe(native_base_syms: Set[str]) -> Tuple[List[str], Set[str]]:
    existing_stems = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(ASSETS_DIR, "*.json"))}
    mt5_cg_syms = set(MT5_TO_COINGLASS.values())
    all_cg = (set(PRIME_CURATED_UNIVERSE) | mt5_cg_syms | existing_stems) - set(native_base_syms)
    prime_set = set(PRIME_CURATED_UNIVERSE)

    def sort_key(sym: str) -> Tuple[int, float, str]:
        p = os.path.join(ASSETS_DIR, f"{sym}.json")
        mtime = os.path.getmtime(p) if os.path.exists(p) else 0.0
        is_stale_or_missing = (time.time() - mtime) > 550
        return (0 if is_stale_or_missing else (1 if sym in prime_set else 2), mtime, sym)

    return sorted(all_cg, key=sort_key), prime_set


async def enforce_memory_guard(harvester: AsyncTelemetryHarvester, baseline_rss_mb: float) -> Tuple[float, float, Optional[int]]:
    """Monitor Python RSS, host free RAM, and :3000 heap; evict caches before pressure rises."""
    gc.collect()
    proc = psutil.Process()
    rss_mb = proc.memory_info().rss / (1024 * 1024)
    vm = psutil.virtual_memory()
    avail_gb = vm.available / (1024 * 1024 * 1024)
    caches_mb = sum(c.stats()["mb"] for c in CACHES.values())

    next_heap_mb: Optional[int] = None
    mem_resp = await harvester.fetch_json("/api/cg/debug/mem", timeout=4, retries=0)
    if mem_resp and isinstance(mem_resp.get("mem"), dict):
        next_heap_mb = mem_resp["mem"].get("heapUsed")

    if rss_mb > 320.0 or avail_gb < 1.0:
        evicted = sum(c.trim_to(0.25) for c in CACHES.values())
        gc.collect()
        rss_after = proc.memory_info().rss / (1024 * 1024)
        print(f"  [mem-guard] TRIMMED {evicted} cache entries | rss {rss_mb:.1f}->{rss_after:.1f} MB | host_avail={avail_gb:.2f}GB", flush=True)
        rss_mb = rss_after

    print(
        f"[mem] rss={rss_mb:.1f}MB (delta={rss_mb - baseline_rss_mb:+.1f}MB) | host_avail={avail_gb:.2f}GB | caches={caches_mb:.2f}MB | next_heap={next_heap_mb}MB",
        flush=True,
    )
    return rss_mb, avail_gb, next_heap_mb


async def async_main() -> None:
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(FOOTPRINT_DIR, exist_ok=True)

    once = "--once" in sys.argv
    no_git = "--no-git" in sys.argv
    full_fp = "--full-fp" in sys.argv
    bench_cycles = 0
    if "--bench-cycles" in sys.argv:
        idx = sys.argv.index("--bench-cycles")
        if idx + 1 < len(sys.argv):
            bench_cycles = max(1, int(sys.argv[idx + 1]))

    concurrency = int(os.environ.get("TELEMETRY_CONCURRENCY", os.environ.get("TELEMETRY_WORKERS", "4")))
    cg_heavy_concurrency = int(os.environ.get("TELEMETRY_CG_CONCURRENCY", "2"))
    worker_count = int(os.environ.get("TELEMETRY_ASSET_WORKERS", "2"))
    interval = int(os.environ.get("TELEMETRY_INTERVAL_SECS", "12"))
    tail_batch = int(os.environ.get("TELEMETRY_TAIL_BATCH", "10"))
    git_sync_secs = 0 if no_git else int(os.environ.get("TELEMETRY_GIT_SYNC_SECS", "600"))

    baseline_rss = psutil.Process().memory_info().rss / (1024 * 1024)
    loop_name = "winloop" if winloop is not None else asyncio.get_running_loop().__class__.__name__

    print("==================================================================", flush=True)
    print(f"Starting Async Zero-Leak Order-Flow + MT5 Telemetry Engine v5.0 ({loop_name})", flush=True)
    print(f"Workers: {worker_count} | Sem(fast/cg): {concurrency}/{cg_heavy_concurrency} | Pause: {interval}s | Tail Batch: {tail_batch} | Git Sync: {git_sync_secs}s | Start RSS: {baseline_rss:.1f}MB", flush=True)
    print("==================================================================", flush=True)

    cycle_num = 0
    async with AsyncTelemetryHarvester(concurrency=concurrency, cg_heavy_concurrency=cg_heavy_concurrency, force_full_fp=full_fp) as harvester:
        while True:
            cycle_num += 1
            cycle_start = time.time()
            harvested_summaries: Dict[str, Dict[str, Any]] = {}

            try:
                # 1. Refresh Classic/COT cache + Read ALL visible MT5 symbols concurrently
                _, mt5_out, screener_data = await asyncio.gather(
                    harvester.refresh_classic_caches(),
                    asyncio.to_thread(_harvest_mt5_sync),
                    harvester.update_universe_overview(),
                )
                mt5_quotes_by_cg, mt5_summaries, native_base_syms, mt5_meta = mt5_out
                harvested_summaries.update(mt5_summaries)
                print(
                    f"[mt5-layer] connected={mt5_meta.get('connected')} visible={mt5_meta.get('visible_count')} native_written={mt5_meta.get('mt5_native_written', 0)} inactive={mt5_meta.get('inactive_skipped')} in {mt5_meta.get('elapsed_s', 0)}s",
                    flush=True,
                )

                # 2. Fast-Lane Prime Tier + rotating stalest-first Tail batch via continuous queue (no straggler barrier!)
                cg_list, prime_set = build_coinglass_universe(native_base_syms)
                prime_cg = [s for s in cg_list if s in prime_set]
                tail_cg = [s for s in cg_list if s not in prime_set]
                sweep_cg = cg_list if once else (prime_cg + tail_cg[:tail_batch])
                print(
                    f"[telemetry] Cycle #{cycle_num}: harvesting {len(sweep_cg)}/{len(cg_list)} Coinglass assets ({len(prime_cg)} prime + {len(sweep_cg)-len(prime_cg)} tail)...",
                    flush=True,
                )

                queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
                for s in sweep_cg:
                    queue.put_nowait(s)
                for _ in range(worker_count):
                    queue.put_nowait(None)

                async def _queue_worker() -> None:
                    while True:
                        sym_req = await queue.get()
                        try:
                            if sym_req is None:
                                return
                            sym_k, summary = await harvester.harvest_single_asset(
                                sym_req, sym_req in prime_set, mt5_quotes_by_cg.get(sym_req)
                            )
                            harvested_summaries[sym_k] = summary
                            await asyncio.sleep(0.25)
                        except Exception as exc:
                            print(f"  [ERROR] Asset {sym_req} harvesting failed: {exc}", file=sys.stderr, flush=True)
                        finally:
                            queue.task_done()

                await asyncio.gather(*(_queue_worker() for _ in range(worker_count)))

                # 3. Generate compact Authoritative Datasets
                generate_authoritative_datasets(harvested_summaries, screener_data)

                # 4. Async Git Sync
                maybe_trigger_git_sync(git_sync_secs)

            except Exception as e:
                print(f"[telemetry-daemon] Cycle exception: {e}", file=sys.stderr, flush=True)

            total_assets = len(harvested_summaries)
            harvested_summaries.clear()
            del harvested_summaries

            _, avail_gb, next_heap_mb = await enforce_memory_guard(harvester, baseline_rss)
            cycle_dur = round(time.time() - cycle_start, 2)
            print(f"=== Cycle #{cycle_num} Finished in {cycle_dur}s ({total_assets} total assets updated) ===", flush=True)

            if once or (bench_cycles > 0 and cycle_num >= bench_cycles):
                break

            pause_s = interval
            if (next_heap_mb and next_heap_mb > 1800) or avail_gb < 1.2:
                pause_s = max(interval, 20)
                print(f"[mem-guard] Next.js/host memory warm (heap={next_heap_mb}MB, avail={avail_gb:.2f}GB); pacing {pause_s}s", flush=True)
            await asyncio.sleep(pause_s)


def main() -> None:
    if winloop is not None:
        winloop.install()
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
