#!/usr/bin/env python3
"""Live IV Wall Scanner for the 20 Alpha Pairs on MetaTrader 5.

Calculates session Expected Move (EM), Upper & Lower 2.0 SD Walls, ATR(14),
and scans for live Wall Touch + Close Back Inside Fade setups.
"""
import math
from datetime import datetime, timezone
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

if not mt5.initialize():
    print("Failed to initialize MT5")
    exit(1)

LOT_TABLE_PATH = r"c:\Users\SIGMA\Documents\Trading-Strategies\ivwall_alpha\mt5_lot_table.csv"
df_lots = pd.read_csv(LOT_TABLE_PATH)

print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Scanning {len(df_lots)} IV-Wall Alpha Pairs on Live MT5...")

results = []

for _, row in df_lots.iterrows():
    sym = row["symbol"]
    digits = int(row["digits"])
    pip_size = float(row["pip_size"])
    lots_50usd = float(row["lots_at_50usd_risk"])
    
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 150)
    if rates is None or len(rates) < 100:
        continue
    
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    
    # Session open is 22:00 UTC (00:00 broker time)
    # Find the most recent day boundary (time.hour == 22 and time.minute == 0)
    # or the start of the current calendar broker day
    df["date"] = df["time"].dt.date
    today = df["date"].iloc[-1]
    
    # ATR(14)
    tr = np.maximum(df["high"] - df["low"], 
                    np.maximum(abs(df["high"] - df["close"].shift(1)),
                               abs(df["low"] - df["close"].shift(1))))
    atr14 = tr.rolling(14).mean().iloc[-1]
    
    # EWMA(0.94) volatility on log returns over prior bars
    ret = np.log(df["close"] / df["close"].shift(1))
    var = ret.ewm(alpha=1 - 0.94).var().iloc[-1]
    sigma_bar = math.sqrt(var) if var > 0 else 0.001
    
    # Session open
    # Look for the bar at or after 22:00 UTC yesterday
    session_bars = df[df["time"] >= (df["time"].iloc[-1].floor("D"))]
    session_open = session_bars["open"].iloc[0] if len(session_bars) else df["open"].iloc[-96]
    
    # 2.0 SD Expected Move for 96 15m bars in a 24h day
    em = session_open * sigma_bar * math.sqrt(96)
    upper_wall = session_open + 2.0 * em
    lower_wall = session_open - 2.0 * em
    
    current_px = df["close"].iloc[-1]
    last_high = df["high"].iloc[-2]
    last_low = df["low"].iloc[-2]
    last_close = df["close"].iloc[-2]
    
    # Check touch conditions on the completed bar (-2)
    upper_touch = (last_high >= upper_wall) and (last_close < upper_wall)
    lower_touch = (last_low <= lower_wall) and (last_close > lower_wall)
    
    dist_upper_pips = (upper_wall - current_px) / pip_size
    dist_lower_pips = (current_px - lower_wall) / pip_size
    
    signal = "NONE"
    entry, sl, tp = 0.0, 0.0, 0.0
    
    if upper_touch:
        signal = "SELL_FADE"
        entry = current_px
        sl = upper_wall + 1.0 * atr14
        tp = session_open + 0.5 * em
    elif lower_touch:
        signal = "BUY_FADE"
        entry = current_px
        sl = lower_wall - 1.0 * atr14
        tp = session_open - 0.5 * em
        
    results.append({
        "symbol": sym,
        "price": current_px,
        "session_open": session_open,
        "upper_wall": upper_wall,
        "lower_wall": lower_wall,
        "em_pips": em / pip_size,
        "dist_upper_pips": dist_upper_pips,
        "dist_lower_pips": dist_lower_pips,
        "atr_pips": atr14 / pip_size,
        "signal": signal,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lots": lots_50usd,
    })

df_res = pd.DataFrame(results)
print("\n=== LIVE IV WALL SCANNER RESULTS (Top 20 Alpha Pairs) ===")
for _, r in df_res.iterrows():
    sig_str = f"*** {r['signal']} ***" if r['signal'] != "NONE" else "HOLD"
    print(f"{r['symbol']:10} Px={r['price']:<9.5f} Open={r['session_open']:<9.5f} "
          f"Upper={r['upper_wall']:<9.5f} Lower={r['lower_wall']:<9.5f} "
          f"DistU={r['dist_upper_pips']:<6.1f}p DistL={r['dist_lower_pips']:<6.1f}p [{sig_str}]")

active_signals = df_res[df_res["signal"] != "NONE"]
print(f"\nActive Wall Touch Fade Triggers Right Now: {len(active_signals)}")
for _, r in active_signals.iterrows():
    print(f"  --> {r['symbol']}: {r['signal']} @ {r['entry']} | SL: {r['sl']} | TP: {r['tp']} | Lots: {r['lots']}")

mt5.shutdown()
