import MetaTrader5 as mt5
import pandas as pd
import numpy as np

if not mt5.initialize():
    print("MT5 failed")
    exit(1)

sym = "XAUUSD.pi"
rates_m15 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 50)
rates_h1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 50)

df_m15 = pd.DataFrame(rates_m15)
df_h1 = pd.DataFrame(rates_h1)

tr = np.maximum(df_m15['high'] - df_m15['low'], np.maximum(abs(df_m15['high'] - df_m15['close'].shift(1)), abs(df_m15['low'] - df_m15['close'].shift(1))))
atr_m15 = tr.rolling(14).mean().iloc[-1]

tr_h1 = np.maximum(df_h1['high'] - df_h1['low'], np.maximum(abs(df_h1['high'] - df_h1['close'].shift(1)), abs(df_h1['low'] - df_h1['close'].shift(1))))
atr_h1 = tr_h1.rolling(14).mean().iloc[-1]

tick = mt5.symbol_info_tick(sym)
spread_usd = tick.ask - tick.bid

print(f"XAUUSD.pi Live: Bid={tick.bid}, Ask={tick.ask}, Spread=${spread_usd:.3f}")
print(f"ATR M15: ${atr_m15:.2f}, ATR H1: ${atr_h1:.2f}")
print(f"Recent M15 Low: {df_m15['low'].min():.2f}, High: {df_m15['high'].max():.2f}")
print(f"Last 5 M15 Closes: {[round(x, 2) for x in df_m15['close'].iloc[-5:]]}")
mt5.shutdown()
