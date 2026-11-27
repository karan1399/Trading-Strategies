#!/usr/bin/env python3
"""
momentum_universe_scanner.py — Comprehensive Momentum & Trend Continuation Scanner.
Scans all universe assets across order-flow telemetry JSONs and live MT5 symbols to identify
the cleanest, most aggressive trend-continuation assets to join along the prevailing trend.
"""

import os
import glob
import json
import MetaTrader5 as mt5

TELEMETRY_ASSETS_DIR = os.path.join("data", "telemetry", "assets")
OVERVIEW_FILE = os.path.join("data", "telemetry", "universe_overview.json")

def load_json(p):
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def scan_momentum():
    files = glob.glob(os.path.join(TELEMETRY_ASSETS_DIR, "*.json"))
    results = []

    for fpath in files:
        sym = os.path.basename(fpath).replace(".json", "")
        d = load_json(fpath)
        if not d:
            continue

        tk = d.get("ticker") or {}
        price = tk.get("price")
        ms = d.get("market_structure") or {}
        ms_p = ms.get("price") or {}
        if not price and ms_p:
            price = ms_p.get("last")
        if not price or price <= 0:
            continue

        chg_24h = ms_p.get("chgDayPct", 0.0)
        vwap_d = ms.get("vwap") or {}
        vwap = vwap_d.get("vwap")
        bands = vwap_d.get("bands") or {}
        sd1 = bands.get("sd1", 0.0)
        vwap_z = ((price - vwap) / sd1) if (vwap and sd1 and sd1 > 0) else 0.0

        auc = ms.get("auction") or {}
        sess = auc.get("session") or {}
        sess_val = sess.get("val")
        sess_vah = sess.get("vah")

        emas = ms.get("emas") or {}
        h1_ema = emas.get("h1") or {}
        e13 = h1_ema.get("e13")
        e89 = h1_ema.get("e89")
        e233 = h1_ema.get("e233")

        # Trend Stack
        trend = "NEUTRAL"
        stack_score = 0.0
        if e13 and e89 and e233:
            if e13 < e89 < e233:
                trend = "BEARISH_MOMENTUM"
                stack_score = 3.0
            elif e13 > e89 > e233:
                trend = "BULLISH_MOMENTUM"
                stack_score = 3.0
            elif e13 < e89:
                trend = "BEARISH_BIAS"
                stack_score = 1.5
            elif e13 > e89:
                trend = "BULLISH_BIAS"
                stack_score = 1.5

        # CVD & Flow
        cvd = d.get("cvd") or {}
        cvd_p = cvd.get("perp") or {}
        h1_cvd = cvd_p.get("h1") or cvd_p.get("m15") or {}
        kpis = h1_cvd.get("kpis") or {}
        buy_ratio = kpis.get("buyRatio", 50.0)

        fp = d.get("footprint") or {}
        h7d = fp.get("history_7d") or {}
        cum_delta_7d = h7d.get("cum_delta_7d", 0.0)

        # Orderbook imbalance
        bw = d.get("book_walls") or {}
        bw_f = bw.get("futures") or {}
        h1_bw = bw_f.get("h1") or bw_f.get("m15") or {}
        imb = h1_bw.get("imbalance", 0.0)

        # Value Area status
        va_breakout = 0.0
        if sess_val and price < sess_val:
            va_breakout = 2.0  # clean breakdown below Value Area Low
        elif sess_vah and price > sess_vah:
            va_breakout = 2.0  # clean breakout above Value Area High

        # Compute Directional Momentum Score
        # For Short Momentum: negative chg_24h, negative vwap_z, low buy_ratio (<50%), negative cum_delta, negative imb
        short_mom_score = 0.0
        if trend in ["BEARISH_MOMENTUM", "BEARISH_BIAS"]:
            short_mom_score += stack_score
            if chg_24h < 0:
                short_mom_score += min(3.0, abs(chg_24h) * 0.5)
            if vwap_z < -0.5:
                short_mom_score += min(3.0, abs(vwap_z))
            if buy_ratio < 48.0:
                short_mom_score += min(2.5, (50.0 - buy_ratio) * 0.25)
            if cum_delta_7d < 0:
                short_mom_score += 1.5
            if imb < -0.05:
                short_mom_score += 1.5
            short_mom_score += va_breakout

        # For Long Momentum: positive chg_24h, positive vwap_z, high buy_ratio (>50%), positive cum_delta, positive imb
        long_mom_score = 0.0
        if trend in ["BULLISH_MOMENTUM", "BULLISH_BIAS"]:
            long_mom_score += stack_score
            if chg_24h > 0:
                long_mom_score += min(3.0, chg_24h * 0.5)
            if vwap_z > 0.5:
                long_mom_score += min(3.0, vwap_z)
            if buy_ratio > 52.0:
                long_mom_score += min(2.5, (buy_ratio - 50.0) * 0.25)
            if cum_delta_7d > 0:
                long_mom_score += 1.5
            if imb > 0.05:
                long_mom_score += 1.5
            long_mom_score += va_breakout

        results.append({
            "sym": sym,
            "price": price,
            "chg_24h": chg_24h,
            "vwap_z": vwap_z,
            "trend": trend,
            "buy_ratio": buy_ratio,
            "cum_delta_7d": cum_delta_7d,
            "imb": imb,
            "va_breakout": va_breakout > 0,
            "short_mom_score": round(short_mom_score, 2),
            "long_mom_score": round(long_mom_score, 2),
            "h1_e89": e89,
            "h1_e233": e233
        })

    # Sort top short momentum and top long momentum
    top_shorts = sorted([r for r in results if r["short_mom_score"] > 3.0], key=lambda x: -x["short_mom_score"])
    top_longs = sorted([r for r in results if r["long_mom_score"] > 3.0], key=lambda x: -x["long_mom_score"])

    return top_shorts, top_longs

def check_mt5_symbols(candidates):
    mt5.initialize()
    verified = []
    for c in candidates:
        sym = c["sym"]
        # Map to MT5 symbol variants
        variants = [
            f"{sym}USD.pi", f"{sym}USD.p", f"{sym}USD", f"{sym}.pi", f"{sym}.p",
            f"{sym}EUR.pi", f"{sym}GBP.pi"
        ]
        mt_sym = None
        info = None
        for v in variants:
            i = mt5.symbol_info(v)
            if i and i.visible:
                mt_sym = v
                info = i
                break
            elif i:
                mt5.symbol_select(v, True)
                i = mt5.symbol_info(v)
                if i and i.visible:
                    mt_sym = v
                    info = i
                    break

        c["mt5_sym"] = mt_sym
        c["mt5_spread"] = info.spread if info else None
        c["mt5_min_lot"] = info.volume_min if info else None
        c["mt5_contract"] = info.trade_contract_size if info else None
        verified.append(c)

    mt5.shutdown()
    return verified

if __name__ == "__main__":
    shorts, longs = scan_momentum()
    print(f"Top Bearish Momentum Assets ({len(shorts)} found):")
    for r in shorts[:15]:
        print(f"  {r['sym']:<10} Score: {r['short_mom_score']:5.2f} | Px: ${r['price']:<10} | Chg: {r['chg_24h']:+6.2f}% | VWAP Z: {r['vwap_z']:+5.2f}σ | Taker Buy: {r['buy_ratio']:4.1f}% | Imb: {r['imb']:+6.3f} | VA Breakdown: {r['va_breakout']}")

    print(f"\nTop Bullish Momentum Assets ({len(longs)} found):")
    for r in longs[:10]:
        print(f"  {r['sym']:<10} Score: {r['long_mom_score']:5.2f} | Px: ${r['price']:<10} | Chg: {r['chg_24h']:+6.2f}% | VWAP Z: {r['vwap_z']:+5.2f}σ | Taker Buy: {r['buy_ratio']:4.1f}% | Imb: {r['imb']:+6.3f}")

    verified_shorts = check_mt5_symbols(shorts[:15])
    print(f"\nMT5 Tradeable Momentum Shorts:")
    for r in verified_shorts:
        if r["mt5_sym"]:
            print(f"  MT5: {r['mt5_sym']:<12} (Score {r['short_mom_score']:5.2f}) | Spread: {r['mt5_spread']} | MinLot: {r['mt5_min_lot']} | Contract: {r['mt5_contract']}")
