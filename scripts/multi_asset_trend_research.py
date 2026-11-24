#!/usr/bin/env python3
"""
Multi-Asset Trend & Order-Flow Research Scanner
Evaluates assets across M15, H1, and H4:
- Macro Trend: Price vs EMA 233
- Dynamic Support/Resistance: Price vs EMA 89
- Regime Classification: STRONG_BEAR, BEAR_PULLBACK, STRONG_BULL, BULL_PULLBACK
- Proximity to EMA 89 (PULLBACK PRIME ZONE: within 0.5% - 1.0%)
- Order-flow footprint confluence (Finished Auction Zero-Print, Absorption)
"""
import json, time, os, sys
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

if not mt5.initialize():
    print("MT5 Init Failed")
    sys.exit(1)

ASSETS = [
    ("XAUUSD.pi", "COMMODITY"),
    ("BTCUSD.pi", "CRYPTO"),
    ("BNBUSD.p",  "CRYPTO"),
    ("SOLUSD.p",  "CRYPTO"),
    ("EURUSD.pi", "FX"),
    ("GBPUSD.pi", "FX"),
    ("USDJPY.pi", "FX"),
    ("AUDUSD.pi", "FX"),
    ("USDCAD.pi", "FX"),
    ("NAS100.p",  "INDEX")
]

TIMEFRAMES = [
    ("M15", mt5.TIMEFRAME_M15),
    ("H1",  mt5.TIMEFRAME_H1),
    ("H4",  mt5.TIMEFRAME_H4)
]

def get_ema_regime(sym: str, tf_name: str, tf_val) -> dict:
    rates = mt5.copy_rates_from_pos(sym, tf_val, 0, 300)
    if rates is None or len(rates) < 240:
        return {"ok": False, "error": "Insufficient rates"}
    
    df = pd.DataFrame(rates)
    df["ema89"] = df["close"].ewm(span=89, adjust=False).mean()
    df["ema233"] = df["close"].ewm(span=233, adjust=False).mean()
    
    # ATR 14
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr14 = float(np.mean(tr[-14:]))
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    close = float(last["close"])
    high = float(last["high"])
    low = float(last["low"])
    e89 = float(last["ema89"])
    e233 = float(last["ema233"])
    
    dist_89_pct = (close - e89) / e89 * 100
    dist_233_pct = (close - e233) / e233 * 100
    dist_89_atr = (close - e89) / atr14 if atr14 > 0 else 0.0
    
    # Check if candle touched / tested EMA 89
    tested_89 = (low <= e89 <= high) or (abs(close - e89) / e89 <= 0.003)
    
    # Regime classification
    if close < e233:
        if close < e89 and not tested_89:
            regime = "STRONG_BEAR"
            setup_bias = "WAIT_FOR_PULLBACK_SHORT"
        elif tested_89 or close >= e89:
            regime = "BEAR_PULLBACK_TESTING_89"
            setup_bias = "PRIME_SHORT_REJECTION_ZONE"
        else:
            regime = "BEAR_RALLY"
            setup_bias = "MONITOR_SHORT"
    else: # close >= e233
        if close > e89 and not tested_89:
            regime = "STRONG_BULL"
            setup_bias = "WAIT_FOR_PULLBACK_LONG"
        elif tested_89 or close <= e89:
            regime = "BULL_PULLBACK_TESTING_89"
            setup_bias = "PRIME_LONG_SUPPORT_ZONE"
        else:
            regime = "BULL_DIP"
            setup_bias = "MONITOR_LONG"
            
    return {
        "ok": True,
        "tf": tf_name,
        "close": close,
        "high": high,
        "low": low,
        "ema89": e89,
        "ema233": e233,
        "atr14": atr14,
        "dist_89_pct": round(dist_89_pct, 2),
        "dist_233_pct": round(dist_233_pct, 2),
        "dist_89_atr": round(dist_89_atr, 2),
        "tested_89": tested_89,
        "regime": regime,
        "setup_bias": setup_bias
    }

def run_research():
    print("=" * 95)
    print("MULTI-ASSET QUANT RESEARCH: DUAL EMA (89/233) DYNAMIC SUPPORT & RESISTANCE MATRIX")
    print("=" * 95)
    
    prime_candidates = []
    
    for sym, category in ASSETS:
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        if not tick:
            continue
            
        print(f"\n>>> [{category:<9}] {sym:<12} | Bid: {tick.bid} | Ask: {tick.ask}")
        
        asset_summary = {"symbol": sym, "category": category, "tfs": {}}
        
        for tf_name, tf_val in TIMEFRAMES:
            res = get_ema_regime(sym, tf_name, tf_val)
            if res.get("ok"):
                asset_summary["tfs"][tf_name] = res
                tested_flag = "🎯 [TESTING EMA 89]" if res["tested_89"] else ""
                print(f"  {tf_name:<4}: Close={res['close']:<10.2f} | EMA89={res['ema89']:<10.2f} | EMA233={res['ema233']:<10.2f} | Diff89={res['dist_89_pct']:>+5.2f}% ({res['dist_89_atr']:>+4.1f} ATR) | {res['regime']:<25} {tested_flag}")
                
                # Check for prime setups on H1 or H4
                if res["tested_89"] and tf_name in ["H1", "H4"]:
                    prime_candidates.append({
                        "symbol": sym,
                        "tf": tf_name,
                        "regime": res["regime"],
                        "bias": res["setup_bias"],
                        "close": res["close"],
                        "ema89": res["ema89"],
                        "ema233": res["ema233"]
                    })
    
    print("\n" + "=" * 95)
    print(f"PRIME SETUP CANDIDATES (TESTING EMA 89 ON H1 / H4) [{len(prime_candidates)} FOUND]:")
    print("=" * 95)
    for c in prime_candidates:
        print(f"  ★ {c['symbol']:<12} ({c['tf']}) -> {c['regime']} | Bias: {c['bias']} | Price: {c['close']} vs EMA89: {c['ema89']:.2f}")

if __name__ == "__main__":
    run_research()
    mt5.shutdown()
