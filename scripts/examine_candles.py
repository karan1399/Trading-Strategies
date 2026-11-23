import MetaTrader5 as mt5
import pandas as pd
import numpy as np

def examine_candles(symbol):
    print(f"\n=================== {symbol} ===================")
    rates_m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 10)
    rates_m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 10)
    if rates_m15 is None or rates_m5 is None:
        print("Failed to fetch rates")
        return
    
    df15 = pd.DataFrame(rates_m15)
    df15['time'] = pd.to_datetime(df15['time'], unit='s')
    df5 = pd.DataFrame(rates_m5)
    df5['time'] = pd.to_datetime(df5['time'], unit='s')
    
    print("M15 Last 5 Candles:")
    for idx, row in df15.tail(5).iterrows():
        print(f"  {row['time']} | O: {row['open']:.2f} H: {row['high']:.2f} L: {row['low']:.2f} C: {row['close']:.2f} | Vol: {row['tick_volume']}")
        
    print("M5 Last 5 Candles:")
    for idx, row in df5.tail(5).iterrows():
        print(f"  {row['time']} | O: {row['open']:.2f} H: {row['high']:.2f} L: {row['low']:.2f} C: {row['close']:.2f} | Vol: {row['tick_volume']}")

if __name__ == "__main__":
    mt5.initialize()
    for s in ['NAS100.p', 'SP500.p', 'BTCUSD.pi', 'XAUUSD.pi']:
        examine_candles(s)
    mt5.shutdown()
