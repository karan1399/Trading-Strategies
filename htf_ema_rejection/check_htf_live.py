#!/usr/bin/env python3
"""Check live HTF EMA rejection signals across the 15 basket members on MT5."""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

if not mt5.initialize():
    print("Failed to initialize MT5")
    exit(1)

basket = [
    {"sym": "XAUAUD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "XAUCNH.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "XAUUSD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "XAUEUR.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "XAUSGD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "XAUGBP.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "GAUUSD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "GAUCNH.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "BTCUSD.pi", "tf": mt5.TIMEFRAME_H4, "period": 89, "pat": "P4_cluster"},
    {"sym": "CHFJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 89, "pat": "P4_cluster"},
    {"sym": "GBPJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P2_two_bar"},
    {"sym": "USDJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P2_two_bar"},
    {"sym": "EURJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster"},
    {"sym": "CADJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P1_wick"},
    {"sym": "DJ30.p", "tf": mt5.TIMEFRAME_H1, "period": 233, "pat": "P2_two_bar"}
]

print("=== Scanning HTF EMA Rejection Basket on Live MT5 ===")
triggers = []

for item in basket:
    sym = item["sym"]
    tf = item["tf"]
    period = item["period"]
    pat = item["pat"]
    tf_str = "H4" if tf == mt5.TIMEFRAME_H4 else "H1"
    
    rates = mt5.copy_rates_from_pos(sym, tf, 0, 300)
    if rates is None or len(rates) < period + 10:
        print(f"{sym:10s} [TF={tf_str}] no rates available")
        continue
    df = pd.DataFrame(rates)
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    o = df["open"].values
    
    # EMA calculation
    alpha = 2.0 / (period + 1.0)
    ema = np.zeros(len(c))
    ema[0] = c[0]
    for i in range(1, len(c)):
        ema[i] = alpha * c[i] + (1 - alpha) * ema[i - 1]
        
    # ATR 14
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr = np.zeros(len(c))
    atr[13] = np.mean(tr[:14])
    for i in range(14, len(c)):
        atr[i] = (atr[i - 1] * 13 + tr[i]) / 14
        
    last_c = c[-2]
    last_h = h[-2]
    last_l = l[-2]
    last_o = o[-2]
    last_ema = ema[-2]
    last_atr = atr[-2]
    dist_pct = (last_c - last_ema) / last_ema * 100.0
    tol = 0.20 * last_atr
    
    # Check touch
    touched = (last_l <= last_ema + tol) and (last_h >= last_ema - tol)
    
    # Check slope
    rising = ema[-2] > ema[-7]
    falling = ema[-2] < ema[-7]
    
    status = "FAR"
    if abs(dist_pct) < 1.0 or touched:
        status = "NEAR_EMA"
        
    # Pattern checks
    sig = "NONE"
    if pat == "P1_wick":
        rng = max(1e-6, last_h - last_l)
        body_top, body_bot = max(last_o, last_c), min(last_o, last_c)
        lw, uw = (body_bot - last_l) / rng, (last_h - body_top) / rng
        if rising and last_l <= last_ema + tol and last_c >= last_ema - tol and lw >= 0.35 and last_c > last_o:
            sig = "BUY_REJECT"
        elif falling and last_h >= last_ema - tol and last_c <= last_ema + tol and uw >= 0.35 and last_c < last_o:
            sig = "SELL_REJECT"
    elif pat == "P2_two_bar":
        mid1 = (o[-3] + c[-3]) / 2.0
        if rising and l[-3] <= ema[-3] + tol and last_c > last_ema and last_c > last_o and last_c > mid1:
            sig = "BUY_REJECT"
        elif falling and h[-3] >= ema[-3] - tol and last_c < last_ema and last_c < last_o and last_c < mid1:
            sig = "SELL_REJECT"
    elif pat == "P4_cluster":
        tol_c = max(1e-5, 0.20 * last_atr)
        ch, cl, cc = h[-5:-2], l[-5:-2], c[-5:-2]
        if rising:
            touches = int((cl <= last_ema + tol_c).sum())
            if touches >= 2 and np.min(cc) >= last_ema - 0.15 * last_atr and last_c > np.max(ch):
                sig = "BUY_REJECT"
        elif falling:
            touches = int((ch >= last_ema - tol_c).sum())
            if touches >= 2 and np.max(cc) <= last_ema + 0.15 * last_atr and last_c < np.min(cl):
                sig = "SELL_REJECT"
                
    if sig != "NONE":
        triggers.append({"sym": sym, "sig": sig, "close": last_c, "ema": last_ema, "atr": last_atr})
        
    print(f"{sym:10s} TF={tf_str} EMA{period}={last_ema:.2f} Close={last_c:.2f} Dist={dist_pct:+.2f}% Status={status} Signal={sig}")

print(f"\nActive HTF EMA Rejection Triggers Right Now: {len(triggers)}")
