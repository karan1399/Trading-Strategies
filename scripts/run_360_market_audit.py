#!/usr/bin/env python3
"""
run_360_market_audit.py — Deep 360-degree quantitative audit of all 130 telemetry JSON files.
Ranks universe by multi-factor confluence score and maps against Blueberry MT5 symbols.
"""

import os
import glob
import json
import MetaTrader5 as mt5

ASSETS_DIR = os.path.join("data", "telemetry", "assets")

def audit_360():
    files = glob.glob(os.path.join(ASSETS_DIR, "*.json"))
    print(f"Total asset JSON profiles found: {len(files)}")
    
    # Check live MT5 symbols available
    mt5.initialize()
    mt5_symbols = {s.name: s for s in mt5.symbols_get() or []}
    
    results = []
    
    for fpath in files:
        sym = os.path.basename(fpath).replace(".json", "")
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
            
        t = d.get("ticker") or {}
        ms = d.get("market_structure") or {}
        ne = d.get("narrative_engine") or {}
        ao = d.get("athena_oracle") or {}
        qp = d.get("quant_positioning") or {}
        fp = d.get("footprint") or {}
        liq = d.get("liquidations") or {}
        
        px = t.get("price")
        if not px:
            px = ms.get("price")
        if not px:
            continue
        try:
            px = float(px)
        except Exception:
            continue
            
        vwap_obj = ms.get("vwap") or {}
        vwap = float(vwap_obj.get("vwap") or px)
        bands = vwap_obj.get("bands") or {}
        sd1 = float(bands.get("sd1") or (px * 0.01))
        z_score = (px - vwap) / sd1 if sd1 > 0 else 0.0
        
        auction = ms.get("auction") or {}
        session_auc = auction.get("session") or {}
        poc = float(session_auc.get("poc") or px)
        val = float(session_auc.get("val") or px)
        vah = float(session_auc.get("vah") or px)
        
        regime = ne.get("regime", "UNKNOWN")
        direction = ne.get("direction", "NEUTRAL")
        grade = ne.get("grade", "C")
        conf = float(ne.get("confidence") or 50)
        
        athena_score = float(ao.get("score") or 50)
        verdict = ao.get("verdict", "")
        
        # Quant positioning
        ls_ratios = qp.get("lsRatios") or {}
        m15_ls = ls_ratios.get("15m") or {}
        taker = m15_ls.get("taker") or {}
        taker_ratio = float(taker.get("buySellRatio") or 1.0)
        
        oi_quad = qp.get("oiQuadrant") or {}
        h1_quad = oi_quad.get("1h", {}).get("quadrant", "UNKNOWN") if isinstance(oi_quad.get("1h"), dict) else "UNKNOWN"
        
        # Confluence Score calculation
        score = 0.0
        bias = "NEUTRAL"
        
        if regime in ["TREND_DOWN", "BREAKDOWN"] or direction == "SHORT":
            score += 2.5
            bias = "BEARISH"
        elif regime in ["TREND_UP", "BREAKOUT"] or direction == "LONG":
            score += 2.5
            bias = "BULLISH"
            
        if bias == "BEARISH" and z_score < -1.0:
            score += min(2.5, abs(z_score))
        elif bias == "BULLISH" and z_score > 1.0:
            score += min(2.5, z_score)
            
        if bias == "BEARISH" and px < val:
            score += 2.0  # Value area breakdown
        elif bias == "BULLISH" and px > vah:
            score += 2.0  # Value area breakout
            
        if bias == "BEARISH" and taker_ratio < 0.95:
            score += 1.5
        elif bias == "BULLISH" and taker_ratio > 1.05:
            score += 1.5
            
        if bias == "BEARISH" and athena_score < 40:
            score += 1.5
        elif bias == "BULLISH" and athena_score > 60:
            score += 1.5
            
        # Check MT5 tradeability
        mt5_match = None
        for candidate in [sym + ".p", sym + ".pi", sym + "USD.p", sym + "USD.pi"]:
            if candidate in mt5_symbols:
                mt5_match = candidate
                break
                
        results.append({
            "symbol": sym,
            "mt5_symbol": mt5_match,
            "price": px,
            "bias": bias,
            "score": round(score, 2),
            "regime": regime,
            "direction": direction,
            "grade": grade,
            "confidence": conf,
            "z_score": round(z_score, 2),
            "taker_ratio": round(taker_ratio, 2),
            "oi_quad": h1_quad,
            "val": val,
            "poc": poc,
            "vah": vah,
            "athena_score": athena_score
        })
        
    results.sort(key=lambda x: x["score"], reverse=True)
    
    print("\n" + "=" * 105)
    print(f"{'SYMBOL':<10} | {'BIAS':<7} | {'SCORE':<5} | {'PRICE':<10} | {'Z-SCORE':<8} | {'TAKER':<6} | {'MT5 SYMBOL':<12} | {'REGIME'}")
    print("=" * 105)
    for r in results[:20]:
        mt5_s = r['mt5_symbol'] or "Delta/Web"
        print(f"{r['symbol']:<10} | {r['bias']:<7} | {r['score']:<5} | {r['price']:<10} | {r['z_score']:<7}σ | {r['taker_ratio']:<6} | {mt5_s:<12} | {r['regime']}")
        
    print("\nTop MT5-Tradeable High-Confluence Setups:")
    mt5_tradeable = [r for r in results if r["mt5_symbol"]]
    for r in mt5_tradeable[:10]:
        info = mt5_symbols.get(r["mt5_symbol"])
        spread = info.spread if info else 0
        minlot = info.volume_min if info else 0.01
        contract = info.trade_contract_size if info else 1.0
        print(f"  {r['mt5_symbol']:<12} ({r['bias']:<7} Score: {r['score']:<4}) | Price: {r['price']} | Spread: {spread} pts | MinLot: {minlot} | Contract: {contract}")
        
    mt5.shutdown()
    return results

if __name__ == "__main__":
    audit_360()
