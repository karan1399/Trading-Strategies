import MetaTrader5 as mt5
from datetime import datetime, timezone
import pandas as pd

mt5.initialize()

symbols = ["BTCUSD.pi", "ETHUSD.pi", "SOLUSD.p", "XAUUSD.pi"]

print("=== CURRENT PRICES & SPREADS ===")
for s in symbols:
    tick = mt5.symbol_info_tick(s)
    if tick:
        print(f"{s:10s} | Bid: {tick.bid:9.2f} | Ask: {tick.ask:9.2f} | Spread: {tick.ask - tick.bid:5.2f}")

print("\n=== RECENT 15M CANDLES (LAST 5 BARS) ===")
for s in symbols:
    rates = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_M15, 0, 5)
    if rates is not None and len(rates) > 0:
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        print(f"\n--- {s} M15 ---")
        for _, r in df.iterrows():
            print(f"{r['time'].strftime('%H:%M')} | O: {r['open']:8.2f} | H: {r['high']:8.2f} | L: {r['low']:8.2f} | C: {r['close']:8.2f} | Vol: {r['tick_volume']}")

mt5.shutdown()
