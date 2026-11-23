import MetaTrader5 as mt5
import pandas as pd
import numpy as np

def analyze_symbol(sym):
    rates_m15 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 100)
    rates_h1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 100)
    if rates_m15 is None or len(rates_m15) < 50 or rates_h1 is None or len(rates_h1) < 50:
        return None
    
    df15 = pd.DataFrame(rates_m15)
    df15['time'] = pd.to_datetime(df15['time'], unit='s')
    df_h1 = pd.DataFrame(rates_h1)
    
    # EMAs
    df15['ema13'] = df15['close'].ewm(span=13, adjust=False).mean()
    df15['ema89'] = df15['close'].ewm(span=89, adjust=False).mean()
    df_h1['ema13'] = df_h1['close'].ewm(span=13, adjust=False).mean()
    df_h1['ema89'] = df_h1['close'].ewm(span=89, adjust=False).mean()
    
    # RSI 14
    delta = df15['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    df15['rsi'] = 100 - (100 / (1 + rs))
    
    # ATR 14
    tr1 = df15['high'] - df15['low']
    tr2 = (df15['high'] - df15['close'].shift()).abs()
    tr3 = (df15['low'] - df15['close'].shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df15['atr'] = tr.rolling(14).mean()
    
    # Bollinger Bands 20, 2.0
    df15['sma20'] = df15['close'].rolling(20).mean()
    df15['std20'] = df15['close'].rolling(20).std()
    df15['upper_bb'] = df15['sma20'] + 2.0 * df15['std20']
    df15['lower_bb'] = df15['sma20'] - 2.0 * df15['std20']
    
    # Recent high/low
    h20 = df15['high'].iloc[-20:].max()
    l20 = df15['low'].iloc[-20:].min()
    
    curr = df15.iloc[-1]
    prev = df15.iloc[-2]
    curr_h1 = df_h1.iloc[-1]
    
    h1_trend = "BULL" if curr_h1['close'] > curr_h1['ema89'] and curr_h1['ema13'] > curr_h1['ema89'] else ("BEAR" if curr_h1['close'] < curr_h1['ema89'] and curr_h1['ema13'] < curr_h1['ema89'] else "CHOP")
    m15_trend = "BULL" if curr['close'] > curr['ema89'] and curr['ema13'] > curr['ema89'] else ("BEAR" if curr['close'] < curr['ema89'] and curr['ema13'] < curr['ema89'] else "CHOP")
    
    return {
        'symbol': sym,
        'close': curr['close'],
        'rsi': curr['rsi'],
        'atr': curr['atr'],
        'h1_trend': h1_trend,
        'm15_trend': m15_trend,
        'upper_bb': curr['upper_bb'],
        'lower_bb': curr['lower_bb'],
        'sma20': curr['sma20'],
        'h20': h20,
        'l20': l20,
        'std': curr['std20']
    }

def scan_all():
    if not mt5.initialize():
        print("MT5 init failed")
        return
    
    symbols = [
        'XAUUSD.pi', 'XAGUSD.pi', 'BTCUSD.pi', 'ETHUSD.pi', 'SOLUSD.p',
        'GBPUSD.pi', 'USDJPY.pi', 'AUDUSD.pi', 'USDCAD.pi', 'NZDUSD.pi', 'USDCHF.pi',
        'SP500.p', 'NAS100.p', 'DJ30.p'
    ]
    
    results = []
    for s in symbols:
        info = analyze_symbol(s)
        if info:
            results.append(info)
            
    print(f"{'Symbol':<12} {'Price':<10} {'RSI':<6} {'ATR':<8} {'H1':<6} {'M15':<6} {'BB Upper':<10} {'BB Lower':<10} {'Status':<15}")
    print("-" * 85)
    for r in results:
        status = "NEUTRAL"
        if r['close'] >= r['upper_bb']:
            status = "PIERCED UPPER"
        elif r['close'] <= r['lower_bb']:
            status = "PIERCED LOWER"
        elif r['rsi'] < 30:
            status = "OVERSOLD"
        elif r['rsi'] > 70:
            status = "OVERBOUGHT"
            
        print(f"{r['symbol']:<12} {r['close']:<10.3f} {r['rsi']:<6.1f} {r['atr']:<8.3f} {r['h1_trend']:<6} {r['m15_trend']:<6} {r['upper_bb']:<10.3f} {r['lower_bb']:<10.3f} {status:<15}")

if __name__ == "__main__":
    scan_all()
    mt5.shutdown()
