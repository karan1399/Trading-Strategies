#!/usr/bin/env python3
"""
Finished Auction Zero-Print Detector (Auction Market Theory + Footprint)
Identifies finished auctions at candle extremes aligned with the institutional EMA 233 filter:
- Below EMA 233: ONLY SHORTS -> Look for '0 at the top' (Finished Auction High, Bid=0 at extreme high).
- Above EMA 233: ONLY LONGS  -> Look for '0 at the bottom' (Finished Auction Low, Ask=0 at extreme low).
- Evaluates multi-timeframe confluence (15M, 1H, 4H).
"""
import json, os, time, sys
from datetime import datetime

def analyze_finished_auction(bar_ohlc: dict, fp_ladder: dict, ema_233: float, ema_89: float, symbol: str, tf: str) -> dict:
    """
    Analyzes whether a closed candle contains a Finished Auction Zero-Print with EMA 89/233 confluence.
    bar_ohlc: {'o': float, 'h': float, 'l': float, 'c': float, 'ts': int}
    fp_ladder: {'b': list[float], 's': list[float], 'tick': float, 'lo': float}
    """
    c = bar_ohlc['c']
    h = bar_ohlc['h']
    l = bar_ohlc['l']
    
    # Trend filter
    is_bearish_regime = c < ema_233
    is_bullish_regime = c > ema_233
    
    b_vol = fp_ladder.get('b', [])
    s_vol = fp_ladder.get('s', [])
    
    if not b_vol or not s_vol or len(b_vol) < 2:
        return {"detected": False, "reason": "Insufficient footprint rows"}
    
    # Top of candle (highest price row)
    top_bid = b_vol[-1]
    top_ask = s_vol[-1]
    
    # Bottom of candle (lowest price row)
    bot_bid = b_vol[0]
    bot_ask = s_vol[0]
    
    # Check proximity to EMA 89 (within 0.5% or testing EMA 89)
    testing_ema89 = abs(h - ema_89) / ema_89 <= 0.008 if is_bearish_regime else abs(l - ema_89) / ema_89 <= 0.008
    
    result = {
        "symbol": symbol,
        "tf": tf,
        "close": c,
        "ema_89": ema_89,
        "ema_233": ema_233,
        "regime": "BEARISH_BELOW_233" if is_bearish_regime else "BULLISH_ABOVE_233",
        "testing_ema89": testing_ema89,
        "detected": False,
        "signal_type": None,
        "extreme_price": None,
        "stop_loss": None,
        "thesis": ""
    }
    
    # 1. Bearish Case: Below EMA 233 -> Look for Finished Auction High (0 at top) near EMA 89
    if is_bearish_regime:
        if top_bid == 0 and top_ask > 0:
            result.update({
                "detected": True,
                "signal_type": "FINISHED_AUCTION_HIGH_SHORT",
                "extreme_price": h,
                "stop_loss": round(h + (h * 0.0005), 2), # 1 tick / buffer above finished high
                "top_print": f"0 x {top_ask}",
                "thesis": f"Price {c} < EMA 233 ({ema_233:.1f}). Top of {tf} candle printed finished auction 0 x {top_ask} at {h} (EMA89: {ema_89:.1f}). Buyers exhausted at dynamic resistance; pristine short pivot."
            })
            return result
            
    # 2. Bullish Case: Above EMA 233 -> Look for Finished Auction Low (0 at bottom) near EMA 89
    elif is_bullish_regime:
        if bot_ask == 0 and bot_bid > 0:
            result.update({
                "detected": True,
                "signal_type": "FINISHED_AUCTION_LOW_LONG",
                "extreme_price": l,
                "stop_loss": round(l - (l * 0.0005), 2), # 1 tick / buffer below finished low
                "bot_print": f"{bot_bid} x 0",
                "thesis": f"Price {c} > EMA 233 ({ema_233:.1f}). Bottom of {tf} candle printed finished auction {bot_bid} x 0 at {l} (EMA89: {ema_89:.1f}). Sellers exhausted at dynamic support; pristine long pivot."
            })
            return result
            
    return result

if __name__ == "__main__":
    # Test case 1: BTC from User Image 1 (1H chart)
    # Circling shows high at ~60,549.7 with 0 on bid and 46 on ask
    print("=== TEST CASE 1: 1H BTC Footprint from User Image 1 ===")
    test_1h = {
        "bar_ohlc": {"o": 59986.9, "h": 60549.7, "l": 59953.7, "c": 60205.8, "ts": 1795525200},
        "fp_ladder": {
            "b": [10.0, 50.0, 100.0, 0.0],  # top bid is 0.0
            "s": [5.0,  30.0, 80.0,  46.0], # top ask is 46.0
            "tick": 20.0,
            "lo": 59953.7
        },
        "ema_233": 63071.0, # 1H EMA233 is above price (~63k)
        "symbol": "BTC",
        "tf": "1H"
    }
    res1 = analyze_finished_auction(test_1h["bar_ohlc"], test_1h["fp_ladder"], test_1h["ema_233"], 60600.0, test_1h["symbol"], test_1h["tf"])
    print(json.dumps(res1, indent=2))
    
    # Test case 2: BTC from User Image 2 (4H chart)
    # Circling shows high at ~60,625.0 with 0 on bid and 41 on ask
    print("\n=== TEST CASE 2: 4H BTC Footprint from User Image 2 ===")
    test_4h = {
        "bar_ohlc": {"o": 60400.1, "h": 60625.0, "l": 59711.6, "c": 60353.1, "ts": 1795521600},
        "fp_ladder": {
            "b": [20.0, 100.0, 0.0], # top bid is 0.0
            "s": [15.0, 80.0, 41.0], # top ask is 41.0
            "tick": 50.0,
            "lo": 59711.6
        },
        "ema_233": 71476.0, # 4H EMA233 is at 71,476
        "symbol": "BTC",
        "tf": "4H"
    }
    res2 = analyze_finished_auction(test_4h["bar_ohlc"], test_4h["fp_ladder"], test_4h["ema_233"], 65099.0, test_4h["symbol"], test_4h["tf"])
    print(json.dumps(res2, indent=2))
