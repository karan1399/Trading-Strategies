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
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_CURATED = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR",
    "SUI", "LTC", "BCH", "TIA", "APT", "ARB", "OP", "INJ", "SEI", "DOT",
    "ATOM", "UNI", "FIL", "ICP", "HBAR", "VET", "TRX", "1000PEPE", "1000SHIB",
    "XAU", "XAG", "NVDA", "TSLA", "MSTR", "QQQ", "GOOGL", "AAPL", "MSFT", "AMZN", "CL",
]


def _extract_shelf_extremes(clusters: dict, rng: str) -> Tuple[Optional[float], Optional[float]]:
    kl = (((clusters.get(rng) or {}).get("stats") or {}).get("keyLevels")) or {}
    above = kl.get("above") or []
    below = kl.get("below") or []
    p_above = above[0].get("price") if above and isinstance(above[0], dict) else None
    p_below = below[0].get("price") if below and isinstance(below[0], dict) else None
    return p_above, p_below


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
        return {
            "available": len(closed) > 0,
            "bars": len(rows),
            "finished_highs": fin_h,
            "finished_lows": fin_l,
            "latest": latest_tag,
        }
    except Exception:
        return {"available": False, "finished_highs": 0, "finished_lows": 0, "latest": None}


def audit_symbol_16p(
    sym: str,
    assets_dir: str = "data/telemetry/assets",
    footprint_dir: str = "data/telemetry/footprint",
    max_stale_sec: int = 600,
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
    p: Dict[str, Any] = {
        "_meta": {
            "sym": sym,
            "feed_type": d.get("feed_type", "coinglass_perp"),
            "mt5_sym": (d.get("mt5") or {}).get("symbol"),
            "age_sec": age_sec,
            "is_fresh": is_fresh,
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
        "vwap": vwap_val,
        "sd1": sd1,
        "vwap_z": z_score,
        "e89_m15": e89_m15,
        "e233_m15": e233_m15,
        "e89_h1": e89_h1,
        "e233_h1": e233_h1,
        "e233_h4": e233_h4,
        "tc09_dir": "SHORT" if (m15_short and h1_short) else ("LONG" if (m15_long and h1_long) else ("M15_SHORT" if m15_short else ("M15_LONG" if m15_long else "MIXED"))),
        "rsi_m15": rsi.get("m15"),
        "rsi_h1": rsi.get("h1"),
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

    # 5. Microstructure L2/L3 (Coinglass + MT5 BBO)
    micro = d.get("microstructure_l2_l3") or {}
    hl = micro.get("hl") or {}
    mt5_q = d.get("mt5") or {}
    p["5_microstructure"] = {
        "mark": hl.get("mark") or mt5_q.get("mid"),
        "oracle": hl.get("oracle"),
        "mark_vs_oracle_pct": hl.get("markVsOraclePct"),
        "vol24h_usd": hl.get("vol24hUsd"),
        "mt5_spread_bps": mt5_q.get("spread_bps"),
    }

    # 6. Quant Positioning
    qp = d.get("quant_positioning") or {}
    ls15 = (qp.get("lsRatios") or {}).get("15m") or {}
    p["6_quant_positioning"] = {
        "ls_ratio_15m": (ls15.get("global") or {}).get("ratio"),
        "top_accounts_ratio": (ls15.get("topAccounts") or {}).get("ratio"),
        "oi_quadrant": qp.get("oiQuadrant"),
    }

    # 7. Options Deribit (BTC/ETH only)
    opt = d.get("options_deribit") or {}
    p["7_options_deribit"] = {
        "dvol": (opt.get("dvol") or {}).get("value") if isinstance(opt, dict) else None,
        "coverage": "BTC/ETH only" if sym in ("BTC", "ETH") else "Unsupported (Non-BTC/ETH)",
    }

    # 8. Liquidations (3d, 7d, 30d shelves + Stopmap m1_coin_24h)
    liq = d.get("liquidations") or {}
    clusters = liq.get("clusters") or {}
    a3, b3 = _extract_shelf_extremes(clusters, "3d")
    a7, b7 = _extract_shelf_extremes(clusters, "7d")
    a30, b30 = _extract_shelf_extremes(clusters, "30d")
    sm = (liq.get("stopmap") or {}).get("m1_coin_24h") or {}
    p["8_liquidations"] = {
        "above_3d": a3, "below_3d": b3,
        "above_7d": a7, "below_7d": b7,
        "above_30d": a30, "below_30d": b30,
        "stopmap_largest": (sm.get("overview") or {}).get("largestLevel"),
    }

    # 9. Book Walls
    bw = d.get("book_walls") or {}
    bw_fut = (bw.get("futures") or {}).get("m15") or {}
    p["9_book_walls"] = {
        "imbalance": bw_fut.get("imbalance"),
        "imbalance_5m_ago": bw_fut.get("imbalance5mAgo"),
        "bid_depth": bw_fut.get("bidDepth"),
        "ask_depth": bw_fut.get("askDepth"),
    }

    # 10. CVD (Perp + Spot)
    cvd = d.get("cvd") or {}
    perp_k = (((cvd.get("perp") or {}).get("m15") or {}).get("kpis")) or {}
    spot_k = (((cvd.get("spot") or {}).get("m15") or {}).get("kpis")) or {}
    buy = perp_k.get("perpBuy", 0) or 0
    sell = perp_k.get("perpSell", 0) or 0
    p["10_cvd"] = {
        "perp_buy": buy,
        "perp_sell": sell,
        "perp_delta": buy - sell,
        "spot_delta": (spot_k.get("spotBuy", 0) or 0) - (spot_k.get("spotSell", 0) or 0),
        "divs_15m": len((((cvd.get("perp") or {}).get("m15") or {}).get("divergences")) or []),
    }

    # 11. Footprint (Signals 7D + 15m/1h Finished Auction Zero-Print)
    fp_counts = {"abs": 0, "trap": 0, "exh": 0, "div": 0}
    fp_file = os.path.join(footprint_dir, sym, "signals_7d.json")
    if os.path.exists(fp_file):
        try:
            with open(fp_file, "r", encoding="utf-8") as fs:
                fp_data = json.load(fs)
                for s in fp_data.get("signals", []):
                    t_code = s.get("t", "")
                    if t_code in fp_counts:
                        fp_counts[t_code] += 1
        except Exception:
            pass
    fa_15m = _inspect_closed_finished_auctions(sym, footprint_dir, "summary_7d.json")
    fa_1h = _inspect_closed_finished_auctions(sym, footprint_dir, "summary_1h.json")
    p["11_footprint"] = {
        "signals_7d": fp_counts,
        "fa_15m": fa_15m,
        "fa_1h": fa_1h,
    }

    # 12. Large Orders
    lo = d.get("large_orders") or {}
    p["12_large_orders"] = {
        "count_24h": len((lo.get("resting_and_executed") or {}).get("24h") or []),
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

    # 16. Funding & OI
    foi = d.get("funding_and_oi") or {}
    f_list = foi.get("funding") or []
    oi_list = foi.get("open_interest") or []
    binance_rate = next((x.get("rate8h") for x in f_list if x.get("name") == "Binance"), None)
    p["16_funding_and_oi"] = {
        "binance_rate_8h": binance_rate,
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


def audit_universe(symbols: Optional[List[str]] = None, include_all_files: bool = False, mt5_only: bool = False) -> Dict[str, Any]:
    assets_dir = "data/telemetry/assets"
    if include_all_files or mt5_only:
        symbols = sorted(os.path.basename(f).replace(".json", "") for f in glob.glob(os.path.join(assets_dir, "*.json")))
    elif symbols is None:
        symbols = DEFAULT_CURATED

    audited = {}
    stale = []
    missing = []
    for s in symbols:
        res, err = audit_symbol_16p(s, assets_dir=assets_dir)
        if err:
            missing.append((s, err))
        elif res:
            if mt5_only and not res["_meta"].get("mt5_sym"):
                continue
            audited[s] = res
            if not res["_meta"]["is_fresh"]:
                stale.append((s, res["_meta"]["age_sec"]))
    return {"audited": audited, "stale": stale, "missing": missing}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic 16-Parameter Telemetry Engine")
    parser.add_argument("--sym", type=str, help="Single symbol detailed 16-parameter JSON output")
    parser.add_argument("--all", action="store_true", help="Audit all files in data/telemetry/assets/ instead of curated 40")
    parser.add_argument("--mt5", action="store_true", help="Audit all MT5-available asset files in data/telemetry/assets/")
    parser.add_argument("--top", type=int, default=20, help="Show top N ranked candidates")
    parser.add_argument("--verify-gate", action="store_true", help="Print empirical WS-15 audit verification stats")
    args = parser.parse_args()

    if args.sym:
        res, err = audit_symbol_16p(args.sym.upper())
        print(json.dumps(res or {"error": err}, indent=2))
        raise SystemExit(0 if res else 1)

    out = audit_universe(include_all_files=args.all, mt5_only=args.mt5)
    audited = out["audited"]
    fresh_items = [(sym, d) for sym, d in audited.items() if d["_meta"]["is_fresh"]]
    fresh_items.sort(key=lambda kv: kv[1]["_meta"]["composite_score"], reverse=True)

    print("=" * 136)
    print(f"16-PARAMETER TELEMETRY MATRIX | Fresh: {len(fresh_items)} | Stale(>600s): {len(out['stale'])} | Missing: {len(out['missing'])}")
    print("=" * 136)
    print("%-8s | %-10s | %-6s | %-10s | %-9s | %-5s | %-7s | %-4s | %-20s | %-6s | %-5s" % (
        "SYM", "LAST", "Z-VWAP", "VA LOC", "TC-09", "ATH", "L/S-15m", "CVD", "FP (A/T/E/D)", "DVOL", "SCORE"
    ))
    print("-" * 136)
    for sym, d in fresh_items[: args.top]:
        ms = d["2_market_structure"]
        ath = d["4_athena_oracle"].get("score")
        ls = d["6_quant_positioning"].get("ls_ratio_15m")
        cvd_d = d["10_cvd"]["perp_delta"]
        fp = d["11_footprint"]["signals_7d"]
        fp_str = f"{fp['abs']}/{fp['trap']}/{fp['exh']}/{fp['div']}"
        dvol = d["7_options_deribit"].get("dvol")
        print("%-8s | %-10.4f | %-6.2f | %-10s | %-9s | %-5s | %-7s | %-4s | %-20s | %-6s | %-5.2f" % (
            sym,
            ms["last"] or 0.0,
            ms["vwap_z"],
            ms["va_loc"],
            ms["tc09_dir"],
            str(ath) if ath is not None else "-",
            f"{ls:.2f}" if ls is not None else "-",
            str(cvd_d),
            fp_str,
            f"{dvol:.1f}" if dvol is not None else "N/A",
            d["_meta"]["composite_score"],
        ))
    if args.verify_gate:
        print("-" * 136)
        print(f"WS-15 GATE STATS: Total Checked={len(audited)} | Fresh<=600s={len(fresh_items)} | Stale={len(out['stale'])}")
        if out["stale"]:
            print("STALE SYMBOLS VETOED (WS-08):", out["stale"][:10])
