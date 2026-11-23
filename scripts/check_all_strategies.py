import json
import requests
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

def check_lh_signals():
    print("--- 1. LH-Trend Hub 8898 Signals ---")
    try:
        r = requests.get('http://127.0.0.1:8898/signals', timeout=3)
        if r.status_code == 200:
            data = r.json()
            signals = data.get('signals', [])
            print(f"Total monitored: {len(signals)}")
            non_wait = [s for s in signals if s.get('signal') != 'WAIT']
            if non_wait:
                for s in non_wait:
                    print(f"  ACTIVE SIGNAL: {s.get('symbol')} -> {s.get('signal')} ({s.get('regime')}) score={s.get('confluence', {}).get('score')} grade={s.get('confluence', {}).get('grade')}")
            else:
                print("  All 21 instruments currently in WAIT regime.")
                # Show top 5 by confluence score
                sorted_s = sorted(signals, key=lambda x: x.get('confluence', {}).get('score', 0), reverse=True)
                print("  Top 5 by confluence score:")
                for s in sorted_s[:5]:
                    c = s.get('confluence', {})
                    print(f"    {s.get('symbol'):<10} score={c.get('score'):.1f} grade={c.get('grade')} regime={s.get('regime')} price={s.get('price')}")
    except Exception as e:
        print(f"  Error querying LH Hub: {e}")

def check_iv_walls():
    print("\n--- 2. IV-Wall Alpha 20 FX Pairs (M15) ---")
    if not mt5.initialize():
        print("  MT5 initialize failed")
        return
    
    # Read lot table
    try:
        df = pd.read_csv("ivwall_alpha/mt5_lot_table.csv")
        pairs = df['symbol'].tolist()
    except Exception as e:
        print(f"  Could not load lot table: {e}")
        return

    print(f"Scanning {len(pairs)} Alpha pairs for 2.0 SD wall touches...")
    wall_candidates = []
    for sym in pairs:
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 50)
        if rates is None or len(rates) < 30:
            continue
        closes = pd.Series([r[4] for r in rates])
        highs = pd.Series([r[2] for r in rates])
        lows = pd.Series([r[3] for r in rates])
        
        # 20-period rolling mean and std
        sma = closes.rolling(20).mean().iloc[-1]
        std = closes.rolling(20).std().iloc[-1]
        upper_wall = sma + 2.0 * std
        lower_wall = sma - 2.0 * std
        
        curr_price = closes.iloc[-1]
        prev_price = closes.iloc[-2]
        curr_high = highs.iloc[-1]
        curr_low = lows.iloc[-1]
        
        sigma_dist = (curr_price - sma) / std if std > 0 else 0
        
        # Check if pierced or touched upper wall
        if curr_high >= upper_wall:
            # Rejection / fade check: if current close is back below upper wall
            rejection = curr_price < upper_wall
            wall_candidates.append({
                'symbol': sym,
                'type': 'FADE_SHORT',
                'sigma': sigma_dist,
                'price': curr_price,
                'wall': upper_wall,
                'rejection': rejection,
                'sl': curr_high + 0.5 * std,
                'tp': sma
            })
        elif curr_low <= lower_wall:
            rejection = curr_price > lower_wall
            wall_candidates.append({
                'symbol': sym,
                'type': 'FADE_LONG',
                'sigma': sigma_dist,
                'price': curr_price,
                'wall': lower_wall,
                'rejection': rejection,
                'sl': curr_low - 0.5 * std,
                'tp': sma
            })
            
    if wall_candidates:
        print(f"Found {len(wall_candidates)} wall-touch candidates:")
        for c in wall_candidates:
            print(f"  {c['symbol']:<12} {c['type']:<10} sigma={c['sigma']:+.2f} price={c['price']:.5f} wall={c['wall']:.5f} rejected={c['rejection']}")
    else:
        print("  Zero pairs currently touching 2.0 SD boundary.")

def check_open_positions():
    print("\n--- 3. MT5 Open Positions & Balances ---")
    acc = mt5.account_info()
    if acc:
        print(f"Equity: ${acc.equity:.2f} | Balance: ${acc.balance:.2f} | Free Margin: ${acc.margin_free:.2f}")
    positions = mt5.positions_get()
    print(f"Open Positions: {len(positions) if positions else 0}")
    if positions:
        for p in positions:
            print(f"  Ticket #{p.ticket}: {p.symbol} {'BUY' if p.type==0 else 'SELL'} {p.volume} lots @ {p.price_open:.5f} -> {p.price_current:.5f} | SL: {p.sl:.5f} TP: {p.tp:.5f} | PnL: ${p.profit:.2f}")

if __name__ == "__main__":
    check_lh_signals()
    check_iv_walls()
    check_open_positions()
    mt5.shutdown()
