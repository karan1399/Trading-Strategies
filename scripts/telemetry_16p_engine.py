#!/usr/bin/env python3
"""
telemetry_16p_engine.py — Deterministic 16-Parameter Telemetry & Footprint Audit Engine
Enforces WS-08 (freshness <= 600s), WS-10 (zero intermediate files), WS-11 (statistical ranking),
WS-12 (all 16 parameters), WS-14 (TradFi + Crypto universe scan), WS-15 (no performative checks), and TC-09.
"""

import argparse
import glob
import json
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_CURATED = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR",
    "SUI", "LTC", "BCH", "TIA", "APT", "ARB", "OP", "INJ", "SEI", "DOT",
    "ATOM", "UNI", "FIL", "ICP", "HBAR", "VET", "TRX", "1000PEPE", "1000SHIB",
    "XAU", "XAG", "NVDA", "TSLA", "MSTR", "QQQ", "GOOGL", "AAPL", "MSFT", "AMZN", "CL",
]


def _http_get_json(url: str, timeout_s: float = 2.0) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-16P-Continuation/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return None


def _live_continue_enrich(
    sym: str,
    d: Dict[str, Any],
    base_url: str = "http://127.0.0.1:3000",
    timeout_s: float = 2.0,
) -> bool:
    """In-memory live HTTP continuation (WS-10: zero disk writes) for stale/incomplete fields."""
    if d.get("feed_type") == "mt5_ohlcv":
        return False
    base = base_url.rstrip("/")
    enriched = False

    mkt = _http_get_json(f"{base}/api/cg/market?sym={sym}", timeout_s=timeout_s)
    if mkt and mkt.get("ok"):
        d["market_structure"] = {
            "price": mkt.get("price"),
            "auction": mkt.get("auction"),
            "vwap": mkt.get("vwap"),
            "emas": mkt.get("ema") or mkt.get("emas"),
            "rsi": mkt.get("rsi"),
            "levels": mkt.get("levels"),
        }
        enriched = True

    sm3 = _http_get_json(f"{base}/api/cg/stopmap?sym={sym}&model=1&mode=coin&range=3d", timeout_s=timeout_s)
    if sm3 and sm3.get("ok"):
        smod = sm3.get("model") or sm3
        liq = d.setdefault("liquidations", {})
        smap = liq.setdefault("stopmap", {})
        smap["m1_coin_3d"] = {
            "keyLevels": smod.get("keyLevels"),
            "overview": smod.get("overview"),
            "source": sm3.get("source"),
            "stale": sm3.get("stale"),
        }
        enriched = True

    ll = _http_get_json(f"{base}/api/cg/liq-live?sym={sym}", timeout_s=timeout_s)
    if ll and ll.get("ok"):
        la = ll.get("asset") or {}
        d.setdefault("liquidations", {})["live_prints_tape"] = {
            "supported": la.get("supported", True),
            "stats1h": la.get("stats1h"),
            "stats24h": la.get("stats24h"),
            "events": (la.get("events") or [])[:25],
            "ageMs": ll.get("ageMs"),
            "stale": ll.get("stale"),
            "source": ll.get("source"),
        }
        enriched = True

    lb = _http_get_json(f"{base}/api/cg/livebook?sym={sym}", timeout_s=timeout_s)
    if lb and lb.get("ok"):
        lmod = lb.get("model") or {}
        d.setdefault("book_walls", {})["live_binance"] = {
            "covered": lb.get("covered"),
            "imbalance": lmod.get("imbalance"),
            "imbalance5mAgo": lmod.get("imbalance5mAgo"),
            "bidDepth": lmod.get("bidDepth") or ((lmod.get("depth") or {}).get("bidUsd") if isinstance(lmod.get("depth"), dict) else None),
            "askDepth": lmod.get("askDepth") or ((lmod.get("depth") or {}).get("askUsd") if isinstance(lmod.get("depth"), dict) else None),
            "largestBid": lmod.get("largestBid"),
            "largestAsk": lmod.get("largestAsk"),
            "walls": (lmod.get("walls") or [])[:15],
        }
        enriched = True

    fnd = _http_get_json(f"{base}/api/cg/funding?sym={sym}", timeout_s=timeout_s)
    oi = _http_get_json(f"{base}/api/cg/oi?sym={sym}", timeout_s=timeout_s)
    foi = d.setdefault("funding_and_oi", {})
    if fnd and fnd.get("ok"):
        f_agg = dict(fnd.get("aggregate") or {})
        if f_agg.get("fundingPct8h") is not None:
            f_agg["apr_pct"] = round(float(f_agg["fundingPct8h"]) * 3.0 * 365.0, 2)
        foi["funding"] = fnd.get("venues")
        foi["funding_aggregate"] = f_agg
        enriched = True
    if oi and oi.get("ok"):
        foi["open_interest"] = oi.get("venues")
        foi["oi_aggregate"] = oi.get("aggregate")
        enriched = True

    if sym in ("BTC", "ETH"):
        opt = _http_get_json(f"{base}/api/cg/options?sym={sym}", timeout_s=timeout_s)
        if opt and opt.get("ok"):
            rr25 = opt.get("riskReversal25d") or opt.get("skew")
            d["options_deribit"] = {
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
            enriched = True

    return enriched


def _extract_shelf_extremes(clusters: dict, rng: str) -> Tuple[Optional[float], Optional[float]]:
    kl = (((clusters.get(rng) or {}).get("stats") or {}).get("keyLevels")) or {}
    above = kl.get("above") or []
    below = kl.get("below") or []
    p_above = above[0].get("price") if above and isinstance(above[0], dict) else None
    p_below = below[0].get("price") if below and isinstance(below[0], dict) else None
    return p_above, p_below


def _compute_lsr(clusters: dict, smap: dict) -> Optional[float]:
    tots = ((clusters.get("3d") or {}).get("stats") or {}).get("totals") or {}
    lg = float(tots.get("longs") or 0)
    sh = float(tots.get("shorts") or 0)
    if lg <= 0 and sh <= 0:
        ov = ((smap.get("m1_coin_3d") or smap.get("m1_coin_24h") or {}).get("overview")) or {}
        lg = float(ov.get("longs") or 0)
        sh = float(ov.get("shorts") or 0)
    if lg > 0 and sh > 0:
        return round(max(lg, sh) / min(lg, sh), 2)
    return None


def _compute_5m_liq_usd(live_tape: dict, now_ms: Optional[float] = None) -> Optional[float]:
    events = live_tape.get("events") or []
    if not events and not live_tape.get("stats1h"):
        return None
    ref_ms = now_ms if now_ms is not None else (time.time() * 1000.0)
    cutoff = ref_ms - 300_000.0
    total = 0.0
    for ev in events:
        if isinstance(ev, dict) and float(ev.get("t") or 0) >= cutoff:
            total += float(ev.get("usd") or 0)
    return round(total, 2)


def _inspect_closed_finished_auctions(sym: str, footprint_dir: str, tf_file: str) -> Dict[str, Any]:
    path = os.path.join(footprint_dir, sym, tf_file)
    if not os.path.exists(path):
        return {"available": False, "finished_highs": 0, "finished_lows": 0, "latest": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("rows") or []
        closed = rows[:-1] if len(rows) > 1 else rows
        fin_h = 0
        fin_l = 0
        latest_tag = None
        for r in closed[-12:]:
            tb = r.get("top_bid")
            ta = r.get("top_ask")
            bb = r.get("bot_bid")
            ba = r.get("bot_ask")
            if tb is not None and ta is not None and tb == 0 and ta > 0:
                fin_h += 1
                latest_tag = f"HIGH_0x{round(ta, 1)}@{r.get('h')}"
            if bb is not None and ba is not None and ba == 0 and bb > 0:
                fin_l += 1
                latest_tag = f"LOW_{round(bb, 1)}x0@{r.get('l')}"
        closed_tail = closed[-1] if closed else {}
        return {
            "available": len(closed) > 0,
            "bars": len(rows),
            "cum_delta": data.get("cum_delta") if data.get("cum_delta") is not None else data.get("cum_delta_7d"),
            "finished_highs": fin_h,
            "finished_lows": fin_l,
            "latest": latest_tag,
            "latest_closed_diag": {
                "buy_3x": closed_tail.get("diag_buy_3x"),
                "sell_3x": closed_tail.get("diag_sell_3x"),
                "buy_stack_zones": closed_tail.get("buy_stack_zones") or [],
                "sell_stack_zones": closed_tail.get("sell_stack_zones") or [],
            } if closed_tail else None,
        }
    except Exception:
        return {"available": False, "finished_highs": 0, "finished_lows": 0, "latest": None}


def audit_symbol_16p(
    sym: str,
    assets_dir: str = "data/telemetry/assets",
    footprint_dir: str = "data/telemetry/footprint",
    max_stale_sec: int = 600,
    live_continue: bool = False,
    base_url: str = "http://127.0.0.1:3000",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    fpath = os.path.join(assets_dir, f"{sym}.json")
    if not os.path.exists(fpath):
        return None, f"Asset file missing: {fpath}"

    try:
        age_sec = round(time.time() - os.path.getmtime(fpath), 1)
        with open(fpath, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return None, f"File read error: {e}"

    is_fresh = age_sec <= max_stale_sec
    needs_http = live_continue and (
        not is_fresh
        or not ((d.get("liquidations") or {}).get("stopmap") or {}).get("m1_coin_3d")
        or not ((d.get("liquidations") or {}).get("live_prints_tape"))
        or not ((d.get("funding_and_oi") or {}).get("oi_aggregate"))
    )
    live_http_ok = _live_continue_enrich(sym, d, base_url=base_url) if needs_http else False
    effective_fresh = is_fresh or live_http_ok
    p: Dict[str, Any] = {
        "_meta": {
            "sym": sym,
            "feed_type": d.get("feed_type", "coinglass_perp"),
            "mt5_sym": (d.get("mt5") or {}).get("symbol"),
            "age_sec": age_sec,
            "is_fresh": is_fresh,
            "live_http_enriched": live_http_ok,
            "effective_fresh": effective_fresh,
            "continuation": "DISK_FRESH" if is_fresh else ("DISK_STALE+LIVE_HTTP" if live_http_ok else "DISK_STALE_OFFLINE"),
            "updated_at": d.get("updated_at"),
        }
    }

    # 1. Ticker
    t = d.get("ticker") or {}
    p["1_ticker"] = {
        "price": t.get("price"),
        "dir": t.get("dir", "flat"),
        "source": t.get("source", "unknown"),
    }

    # 2. Market Structure (Auction VAL/POC/VAH, VWAP +-sd, Multi-TF EMA 89/233, RSI)
    ms = d.get("market_structure") or {}
    auc = ms.get("auction") or {}
    vwap_obj = ms.get("vwap") or {}
    emas = ms.get("emas") or {}
    rsi = ms.get("rsi") or {}
    day_auc = auc.get("day") or {}
    sess_auc = auc.get("session") or {}
    m15_ema = emas.get("m15") or {}
    h1_ema = emas.get("h1") or {}
    h4_ema = emas.get("h4") or {}

    ms_price = ms.get("price")
    last_p = (ms_price.get("last") if isinstance(ms_price, dict) else ms_price) or p["1_ticker"]["price"]
    day_val = day_auc.get("val")
    day_poc = day_auc.get("poc")
    day_vah = day_auc.get("vah")
    vwap_val = vwap_obj.get("vwap")
    sd1 = (vwap_obj.get("bands") or {}).get("sd1")
    z_score = round((last_p - vwap_val) / sd1, 2) if (last_p and vwap_val and sd1 and sd1 > 0) else 0.0

    va_loc = "UNKNOWN"
    if last_p and day_val and day_vah:
        if last_p < day_val:
            va_loc = "BELOW_VAL"
        elif last_p > day_vah:
            va_loc = "ABOVE_VAH"
        elif day_poc and last_p < day_poc:
            va_loc = "IN_VA_DN"
        else:
            va_loc = "IN_VA_UP"

    e89_m15 = m15_ema.get("e89")
    e233_m15 = m15_ema.get("e233")
    e89_h1 = h1_ema.get("e89")
    e233_h1 = h1_ema.get("e233")
    e233_h4 = h4_ema.get("e233")

    m15_short = bool(last_p and e89_m15 and e233_m15 and last_p < e89_m15 and last_p < e233_m15)
    m15_long = bool(last_p and e89_m15 and e233_m15 and last_p > e89_m15 and last_p > e233_m15)
    h1_short = bool(last_p and e233_h1 and last_p < e233_h1)
    h1_long = bool(last_p and e233_h1 and last_p > e233_h1)

    p["2_market_structure"] = {
        "last": last_p,
        "va_loc": va_loc,
        "day_val": day_val,
        "day_poc": day_poc,
        "day_vah": day_vah,
        "sess_poc": sess_auc.get("poc"),
        "sess_vah": sess_auc.get("vah"),
        "sess_val": sess_auc.get("val"),
        "week_poc": (auc.get("week") or {}).get("poc"),
        "month_poc": (auc.get("month") or {}).get("poc"),
        "levels": ms.get("levels"),
        "vwap": vwap_val,
        "sd1": sd1,
        "vwap_z": z_score,
        "e13_h4": h4_ema.get("e13"),
        "e89_m15": e89_m15,
        "e233_m15": e233_m15,
        "e89_h1": e89_h1,
        "e233_h1": e233_h1,
        "e89_h4": h4_ema.get("e89"),
        "e233_h4": e233_h4,
        "e13_d1": (emas.get("d1") or {}).get("e13"),
        "e89_d1": (emas.get("d1") or {}).get("e89"),
        "e233_d1": (emas.get("d1") or {}).get("e233"),
        "tc09_dir": "SHORT" if (m15_short and h1_short) else ("LONG" if (m15_long and h1_long) else ("M15_SHORT" if m15_short else ("M15_LONG" if m15_long else "MIXED"))),
        "rsi_m15": rsi.get("m15"),
        "rsi_h1": rsi.get("h1"),
        "rsi_h4": rsi.get("h4"),
        "rsi_d1": rsi.get("d1"),
    }

    # 3. Narrative Engine
    ne = d.get("narrative_engine") or {}
    p["3_narrative_engine"] = {
        "regime": ne.get("regime"),
        "model": ne.get("activeModel"),
        "dir": ne.get("direction"),
        "confidence": ne.get("confidence"),
        "grade": ne.get("grade"),
        "evR": ((ne.get("probabilities") or {}).get("evR")),
    }

    # 4. Athena Oracle
    athena = d.get("athena_oracle") or {}
    p["4_athena_oracle"] = {
        "score": athena.get("score"),
        "verdict": athena.get("verdict"),
        "factors": {f.get("id"): f.get("score") for f in (athena.get("factors") or []) if isinstance(f, dict)},
    }

    # 5. Microstructure L2/L3 (Coinglass + MT5 BBO + TradFi perpdepth)
    micro = d.get("microstructure_l2_l3") or {}
    hl = micro.get("hl") or {}
    mt5_q = d.get("mt5") or {}
    p["5_microstructure"] = {
        "mark": hl.get("mark") or mt5_q.get("mid"),
        "oracle": hl.get("oracle"),
        "mark_vs_oracle_pct": hl.get("markVsOraclePct"),
        "vol24h_usd": hl.get("vol24hUsd"),
        "mt5_spread_bps": mt5_q.get("spread_bps"),
        "tradfi_perpdepth": (micro.get("tradfi_perpdepth") or {}).get("lead"),
    }

    # 6. Quant Positioning (15m/1h/4h L/S + OI quadrant + D01 Basis)
    qp = d.get("quant_positioning") or {}
    ls15 = (qp.get("lsRatios") or {}).get("15m") or {}
    ls1h = (qp.get("lsRatios") or {}).get("1h") or {}
    ls4h = (qp.get("lsRatios") or {}).get("4h") or {}
    p["6_quant_positioning"] = {
        "ls_ratio_15m": (ls15.get("global") or {}).get("ratio"),
        "top_accounts_ratio": (ls15.get("topAccounts") or {}).get("ratio"),
        "ls_ratio_1h": (ls1h.get("global") or {}).get("ratio"),
        "ls_ratio_4h": (ls4h.get("global") or {}).get("ratio"),
        "oi_quadrant": qp.get("oiQuadrant"),
        "basis": qp.get("basis"),
    }

    # 7. Options Deribit (BTC/ETH only — D13 DVOL, GEX, Gamma Flip, MaxPain, 25d Skew)
    opt = d.get("options_deribit") or {}
    rr25 = opt.get("riskReversal25d") or opt.get("skew")
    gex_obj = opt.get("gex") if isinstance(opt, dict) else None
    p["7_options_deribit"] = {
        "dvol": (opt.get("dvol") or {}).get("value") if isinstance(opt, dict) else None,
        "risk_reversal_25d": rr25.get("value") if isinstance(rr25, dict) else rr25,
        "gamma_flip_price": opt.get("gammaFlipPrice") if isinstance(opt, dict) else None,
        "gex_regime": gex_obj.get("regime") if isinstance(gex_obj, dict) else None,
        "net_gex_1pct_usd": gex_obj.get("netGexUsdPer1Pct") if isinstance(gex_obj, dict) else None,
        "max_pain_strike": opt.get("maxPainStrike") if isinstance(opt, dict) else None,
        "term_structure": opt.get("termStructure") if isinstance(opt, dict) else None,
        "coverage": "BTC/ETH only" if sym in ("BTC", "ETH") else "Unsupported (Non-BTC/ETH)",
    }

    # 8. Liquidations (3d/7d/30d clusters + Model 1 Stopmap 24h/3d/7d keyLevels + D12 LSR + D16 5m tape)
    liq = d.get("liquidations") or {}
    clusters = liq.get("clusters") or {}
    smap = liq.get("stopmap") or {}
    a3, b3 = _extract_shelf_extremes(clusters, "3d")
    a7, b7 = _extract_shelf_extremes(clusters, "7d")
    a30, b30 = _extract_shelf_extremes(clusters, "30d")
    sm24 = smap.get("m1_coin_24h") or {}
    sm3 = smap.get("m1_coin_3d") or {}
    sm3_kl = sm3.get("keyLevels") or {}
    sm3_walls = sm3.get("walls") if isinstance(sm3.get("walls"), dict) else {}
    live_tape = liq.get("live_prints_tape") or {}
    p["8_liquidations"] = {
        "above_3d": a3, "below_3d": b3,
        "above_7d": a7, "below_7d": b7,
        "above_30d": a30, "below_30d": b30,
        "stopmap_24h_largest": (sm24.get("overview") or {}).get("largestLevel"),
        "stopmap_3d_above": [x.get("price") for x in (sm3_kl.get("above") or [])[:5] if isinstance(x, dict)],
        "stopmap_3d_below": [x.get("price") for x in (sm3_kl.get("below") or [])[:5] if isinstance(x, dict)],
        "stopmap_3d_walls_active_short": sm3_walls.get("active_short_prices") or [],
        "stopmap_3d_walls_active_long": sm3_walls.get("active_long_prices") or [],
        "stopmap_3d_max_short": sm3_walls.get("max_short"),
        "stopmap_3d_max_long": sm3_walls.get("max_long"),
        "stopmap_models_present": sorted(smap.keys()),
        "lsr_3d": _compute_lsr(clusters, smap),
        "liq_5m_pace_usd": _compute_5m_liq_usd(live_tape) if live_tape else None,
        "liq_24h_usd": (live_tape.get("stats24h") or {}).get("usd") if live_tape else None,
    }

    # 9. Book Walls (Futures + Spot 1m/5m/m15/1h/4h + Live Binance top-1000 D09)
    bw = d.get("book_walls") or {}
    bw_futs = bw.get("futures") or {}
    bw_fut = bw_futs.get("m15") or {}
    bw_spot = (bw.get("spot") or {}).get("m15") or {}
    bw_1m = bw_futs.get("1m") or {}
    bw_h1 = bw_futs.get("1h") or bw_futs.get("h1") or {}
    bw_4h = bw_futs.get("4h") or {}
    bw_live = bw.get("live_binance") or {}
    bw_persist = bw_fut.get("persistence") if isinstance(bw_fut.get("persistence"), dict) else {}
    p["9_book_walls"] = {
        "futures_intervals": sorted(bw_futs.keys()),
        "imbalance": bw_fut.get("imbalance"),
        "imbalance_5m_ago": bw_fut.get("imbalance5mAgo"),
        "bid_depth": bw_fut.get("bidDepth"),
        "ask_depth": bw_fut.get("askDepth"),
        "spot_imbalance": bw_spot.get("imbalance"),
        "bw_1m_imbalance": bw_1m.get("imbalance"),
        "h1_imbalance": bw_h1.get("imbalance"),
        "bw_4h_imbalance": bw_4h.get("imbalance"),
        "live_binance_imbalance": bw_live.get("imbalance"),
        "live_binance_grid": bw_live.get("grid"),
        "largest_bid": bw_fut.get("largestBid") or bw_live.get("largestBid"),
        "largest_ask": bw_fut.get("largestAsk") or bw_live.get("largestAsk"),
        "wall_count": len(bw_fut.get("walls") or []) + len(bw_spot.get("walls") or []),
        "ladder_levels": len(bw_fut.get("ladder") or []),
        "untouched_bid_usd": bw_persist.get("untouched_bid_usd"),
        "untouched_ask_usd": bw_persist.get("untouched_ask_usd"),
        "max_wall_age_ms": bw_persist.get("max_wall_age_ms"),
    }

    # 10. CVD (Perp + Spot + Split across m15/h1 D08)
    cvd = d.get("cvd") or {}
    perp_k = (((cvd.get("perp") or {}).get("m15") or {}).get("kpis")) or {}
    spot_k = (((cvd.get("spot") or {}).get("m15") or {}).get("kpis")) or {}
    perp_h1 = (((cvd.get("perp") or {}).get("h1") or {}).get("kpis")) or {}
    buy = perp_k.get("perpBuy", 0) or 0
    sell = perp_k.get("perpSell", 0) or 0
    p["10_cvd"] = {
        "perp_buy": buy,
        "perp_sell": sell,
        "perp_delta": buy - sell,
        "spot_delta": (spot_k.get("spotBuy", 0) or 0) - (spot_k.get("spotSell", 0) or 0),
        "perp_h1_delta": (perp_h1.get("perpBuy", 0) or 0) - (perp_h1.get("perpSell", 0) or 0),
        "divs_15m": len((((cvd.get("perp") or {}).get("m15") or {}).get("divergences")) or []),
        "latest_div": perp_k.get("latestDiv"),
    }

    # 11. Footprint (Signals 7D + 15m/1h Finished Auction Zero-Print + Diagonal Imbalances + Multi-TF)
    fp_counts = {"abs": 0, "trap": 0, "exh": 0, "div": 0}
    fp_by_bt: Dict[str, Any] = {}
    fp_file = os.path.join(footprint_dir, sym, "signals_7d.json")
    if os.path.exists(fp_file):
        try:
            with open(fp_file, "r", encoding="utf-8") as fs:
                fp_data = json.load(fs)
                fp_by_bt = fp_data.get("by_bt") or {}
                for s in fp_data.get("signals", []):
                    t_code = s.get("t", "")
                    if t_code in fp_counts:
                        fp_counts[t_code] += 1
        except Exception:
            pass
    fa_15m = _inspect_closed_finished_auctions(sym, footprint_dir, "summary_7d.json")
    fa_1h = _inspect_closed_finished_auctions(sym, footprint_dir, "summary_1h.json")
    fp_obj = d.get("footprint") or {}
    h7_obj = fp_obj.get("history_7d") or {}
    h1_obj = fp_obj.get("history_1h") or {}
    tail_rows = h7_obj.get("recent_bars_tail") or []
    closed_row = tail_rows[-2] if len(tail_rows) >= 2 else (tail_rows[-1] if tail_rows else {})
    diag_closed = (
        {
            "buy_3x": closed_row.get("diag_buy_3x"),
            "sell_3x": closed_row.get("diag_sell_3x"),
            "buy_stack_zones": closed_row.get("buy_stack_zones") or [],
            "sell_stack_zones": closed_row.get("sell_stack_zones") or [],
        }
        if closed_row and closed_row.get("diag_buy_3x") is not None
        else fa_15m.get("latest_closed_diag")
    )
    mtf = fp_obj.get("multi_tf") or {}
    mtf_buckets = sorted([k for k, v in mtf.items() if isinstance(v, dict) and v.get("buckets_available")])
    cum_delta_1h = (
        h1_obj.get("cum_delta_1h")
        if h1_obj.get("cum_delta_1h") is not None
        else (h1_obj.get("cum_delta") if h1_obj.get("cum_delta") is not None else (h1_obj.get("cum_delta_7d") if h1_obj.get("cum_delta_7d") is not None else fa_1h.get("cum_delta")))
    )
    p["11_footprint"] = {
        "signals_7d": fp_counts,
        "signal_counts_by_bt": fp_by_bt,
        "fa_15m": fa_15m,
        "fa_1h": fa_1h,
        "cum_delta_7d": h7_obj.get("cum_delta_7d") if h7_obj.get("cum_delta_7d") is not None else h7_obj.get("cum_delta"),
        "cum_delta_1h": cum_delta_1h,
        "diag_15m_latest_closed": diag_closed,
        "multi_tf_buckets": mtf_buckets,
    }

    # 12. Large Orders & D10 LTO
    lo = d.get("large_orders") or {}
    lto_obj = lo.get("large_taker_orders_100k") or {}
    p["12_large_orders"] = {
        "count_24h": len((lo.get("resting_and_executed") or {}).get("24h") or []),
        "lto_supported": lto_obj.get("supported") if isinstance(lto_obj, dict) else None,
        "lto_groups": len(lto_obj.get("groups") or []) if isinstance(lto_obj, dict) else 0,
        "lto_active_exchanges": lto_obj.get("activeExchanges") if isinstance(lto_obj, dict) else [],
    }

    # 13. Playbook Cards
    pb = d.get("playbook_cards") or {}
    p["13_playbook_cards"] = {
        "status": pb.get("status", "none"),
        "count": len(pb.get("cards") or []),
    }

    # 14. Backtest Metrics
    bm = d.get("backtest_metrics") or {}
    ma = bm.get("metricsAll") or {}
    p["14_backtest_metrics"] = {
        "trades": ma.get("trades"),
        "win_rate_pct": ma.get("winRatePct"),
        "profit_factor": ma.get("profitFactor"),
    }

    # 15. COT Report
    cot = d.get("cot_report") or {}
    p["15_cot_report"] = {
        "supported": cot.get("supported", False),
        "reason": cot.get("reason", "N/A"),
    }

    # 16. Funding & OI (D14 4H OI + D15 Annualized Funding APR)
    foi = d.get("funding_and_oi") or {}
    f_list = foi.get("funding") or []
    oi_list = foi.get("open_interest") or []
    f_agg = foi.get("funding_aggregate") or {}
    oi_agg = foi.get("oi_aggregate") or {}
    binance_rate = next((x.get("rate8h") for x in f_list if x.get("name") == "Binance"), None)
    weighted_8h = f_agg.get("fundingPct8h")
    apr_pct = f_agg.get("apr_pct")
    if apr_pct is None and weighted_8h is not None:
        apr_pct = round(float(weighted_8h) * 3.0 * 365.0, 2)
    elif apr_pct is None and binance_rate is not None:
        apr_pct = round(float(binance_rate) * 3.0 * 365.0, 2)
    p["16_funding_and_oi"] = {
        "binance_rate_8h": binance_rate,
        "weighted_rate_8h": weighted_8h,
        "annualized_apr_pct": apr_pct,
        "oi_usd": oi_agg.get("oiUsd"),
        "oi_delta_1h_pct": oi_agg.get("oiDelta1hPct"),
        "oi_delta_4h_pct": oi_agg.get("oiDelta4hPct"),
        "oi_delta_24h_pct": oi_agg.get("oiDelta24hPct"),
        "funding_venues": len(f_list),
        "oi_venues": len(oi_list),
    }

    # WS-11 Composite Quantitative Confluence Score (0.00 - 10.00)
    score = 5.0
    tc09 = p["2_market_structure"]["tc09_dir"]
    athena_s = p["4_athena_oracle"]["score"]
    delta_15m = p["10_cvd"]["perp_delta"]

    if tc09 == "SHORT":
        score += 2.0
        if va_loc == "BELOW_VAL":
            score += 1.2
        elif va_loc == "IN_VA_DN":
            score += 0.6
        if z_score < -1.0:
            score += 0.6
        if delta_15m < 0:
            score += 0.4
        if athena_s is not None and athena_s < 42:
            score += 0.4
        if fa_15m["finished_highs"] > 0 or fa_1h["finished_highs"] > 0:
            score += 0.4
    elif tc09 == "LONG":
        score += 2.0
        if va_loc == "ABOVE_VAH":
            score += 1.2
        elif va_loc == "IN_VA_UP":
            score += 0.6
        if z_score > 1.0:
            score += 0.6
        if delta_15m > 0:
            score += 0.4
        if athena_s is not None and athena_s > 58:
            score += 0.4
        if fa_15m["finished_lows"] > 0 or fa_1h["finished_lows"] > 0:
            score += 0.4
    elif tc09 in ("M15_SHORT", "M15_LONG"):
        score += 1.0

    if fp_counts["trap"] > 4 or fp_counts["abs"] > 10:
        score += 0.3

    p["_meta"]["composite_score"] = round(min(score, 9.95), 2)
    return p, None


def audit_universe(
    symbols: Optional[List[str]] = None,
    include_all_files: bool = False,
    mt5_only: bool = False,
    live_continue: bool = False,
) -> Dict[str, Any]:
    assets_dir = "data/telemetry/assets"
    if include_all_files or mt5_only:
        symbols = sorted(os.path.basename(f).replace(".json", "") for f in glob.glob(os.path.join(assets_dir, "*.json")))
    elif symbols is None:
        symbols = DEFAULT_CURATED

    audited = {}
    stale = []
    missing = []
    for s in symbols:
        res, err = audit_symbol_16p(s, assets_dir=assets_dir, live_continue=live_continue)
        if err:
            missing.append((s, err))
        elif res:
            if mt5_only and not res["_meta"].get("mt5_sym"):
                continue
            audited[s] = res
            if not res["_meta"]["effective_fresh"]:
                stale.append((s, res["_meta"]["age_sec"]))
    return {"audited": audited, "stale": stale, "missing": missing}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic 16-Parameter Telemetry Engine")
    parser.add_argument("--sym", type=str, help="Single symbol detailed 16-parameter JSON output")
    parser.add_argument("--all", action="store_true", help="Audit all files in data/telemetry/assets/ instead of curated 40")
    parser.add_argument("--mt5", action="store_true", help="Audit all MT5-available asset files in data/telemetry/assets/")
    parser.add_argument("--live-continue", action="store_true", help="Automatically continue via live :3000 HTTP endpoints when disk cache is stale or missing subfields")
    parser.add_argument("--top", type=int, default=20, help="Show top N ranked candidates")
    parser.add_argument("--verify-gate", action="store_true", help="Print empirical WS-15 audit verification stats")
    args = parser.parse_args()

    if args.sym:
        res, err = audit_symbol_16p(args.sym.upper(), live_continue=True)
        print(json.dumps(res or {"error": err}, indent=2))
        raise SystemExit(0 if res else 1)

    out = audit_universe(include_all_files=args.all, mt5_only=args.mt5, live_continue=args.live_continue)
    audited = out["audited"]
    fresh_items = [(sym, d) for sym, d in audited.items() if d["_meta"]["effective_fresh"]]
    fresh_items.sort(key=lambda kv: kv[1]["_meta"]["composite_score"], reverse=True)

    print("=" * 150)
    print(f"16-PARAMETER TELEMETRY MATRIX | Effective Fresh: {len(fresh_items)} | Offline Stale(>600s): {len(out['stale'])} | Missing: {len(out['missing'])}")
    print("=" * 150)
    print("%-8s | %-10s | %-6s | %-10s | %-9s | %-5s | %-7s | %-5s | %-14s | %-6s | %-6s | %-7s | %-5s" % (
        "SYM", "LAST", "Z-VWAP", "VA LOC", "TC-09", "ATH", "L/S-15m", "CVD", "FP (A/T/E/D)", "APR%", "OI4H%", "DVOL", "SCORE"
    ))
    print("-" * 150)
    for sym, d in fresh_items[: args.top]:
        ms = d["2_market_structure"]
        ath = d["4_athena_oracle"].get("score")
        ls = d["6_quant_positioning"].get("ls_ratio_15m")
        cvd_d = d["10_cvd"]["perp_delta"]
        fp = d["11_footprint"]["signals_7d"]
        fp_str = f"{fp['abs']}/{fp['trap']}/{fp['exh']}/{fp['div']}"
        apr = d["16_funding_and_oi"].get("annualized_apr_pct")
        oi4 = d["16_funding_and_oi"].get("oi_delta_4h_pct")
        dvol = d["7_options_deribit"].get("dvol")
        print("%-8s | %-10.4f | %-6.2f | %-10s | %-9s | %-5s | %-7s | %-5s | %-14s | %-6s | %-6s | %-7s | %-5.2f" % (
            sym,
            ms["last"] or 0.0,
            ms["vwap_z"],
            ms["va_loc"],
            ms["tc09_dir"],
            str(ath) if ath is not None else "-",
            f"{ls:.2f}" if ls is not None else "-",
            str(cvd_d),
            fp_str,
            f"{apr:.1f}" if apr is not None else "-",
            f"{oi4:+.2f}" if oi4 is not None else "-",
            f"{dvol:.1f}" if dvol is not None else "N/A",
            d["_meta"]["composite_score"],
        ))
    if args.verify_gate:
        disk_fresh_n = sum(1 for _, v in audited.items() if v["_meta"]["is_fresh"])
        http_cont_n = sum(1 for _, v in audited.items() if v["_meta"].get("live_http_enriched"))
        print("-" * 150)
        print(f"WS-15 GATE STATS: Total={len(audited)} | DiskFresh<=600s={disk_fresh_n} | LiveHttpContinuation={http_cont_n} | OfflineStale={len(out['stale'])}")
        if out["stale"]:
            print("STALE SYMBOLS VETOED (WS-08):", out["stale"][:10])
