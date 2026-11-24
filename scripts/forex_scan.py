import MetaTrader5 as mt5
import pandas as pd
import numpy as np

if not mt5.initialize():
    print("Failed to initialize MT5")
    exit(1)

pairs = ["EURUSD.pi", "GBPUSD.pi", "USDJPY.pi", "AUDUSD.pi", "USDCAD.pi", "USDCHF.pi", "NZDUSD.pi", "EURGBP.pi", "EURJPY.pi", "GBPJPY.pi"]

print(f"{'Pair':<10} | {'Bid':<8} | {'Ask':<8} | {'Spread (pts)':<12} | {'ATR M15 (pips)':<15} | {'RSI M15':<8} | {'H1 EMA Trend':<12}")
print("-" * 80)

for p in pairs:
    info = mt5.symbol_info(p)
    if not info:
        continue
    rates_m15 = mt5.copy_rates_from_pos(p, mt5.TIMEFRAME_M15, 0, 50)
    rates_h1 = mt5.copy_rates_from_pos(p, mt5.TIMEFRAME_H1, 0, 50)
    if rates_m15 is None or rates_h1 is None or len(rates_m15) < 30 or len(rates_h1) < 30:
        continue
    
    df15 = pd.DataFrame(rates_m15)
    df1 = pd.DataFrame(rates_h1)
    
    # ATR M15
    tr = np.maximum(df15['high'] - df15['low'], np.maximum(abs(df15['high'] - df15['close'].shift(1)), abs(df15['low'] - df15['close'].shift(1))))
    atr15 = tr.rolling(14).mean().iloc[-1]
    
    # RSI M15
    delta = df15['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    rsi15 = 100 - (100 / (1 + rs)).iloc[-1]
    
    # EMA 13/89 H1
    ema13 = df1['close'].ewm(span=13, adjust=False).mean().iloc[-1]
    ema89 = df1['close'].ewm(span=89, adjust=False).mean().iloc[-1]
    trend = "BULL" if ema13 > ema89 else "BEAR"
    
    pip_mult = 100 if "JPY" in p else 10000
    atr_pips = atr15 * pip_mult
    
    print(f"{p:<10} | {info.bid:<8.5f} | {info.ask:<8.5f} | {info.spread:<12} | {atr_pips:<15.1f} | {rsi15:<8.1f} | {trend:<12}")

mt5.shutdown()
