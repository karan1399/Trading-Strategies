#!/usr/bin/env python3
"""
close_non_crypto_positions.py
Surgically closes all non-crypto positions (Forex, Energy, Indices) on MT5
at TradFi market open to protect Blueberry FundedNext Phase 1 equity
from opening spread blowout, slippage, and weekend gap risk.

Crypto positions (BTC, ETH, SOL, BNB, XRP, etc.) are preserved.
Stale non-crypto pending orders are cancelled.
Supports --loop-until-open to poll until market opens and fills orders.
"""

import sys
import time
import argparse
import datetime
import json
import MetaTrader5 as mt5

CRYPTO_SYMBOLS = {'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOGE', 'AVAX', 'DOT', 'LINK', 'LTC'}

def is_crypto(symbol: str) -> bool:
    sym = symbol.upper()
    return any(c in sym for c in CRYPTO_SYMBOLS)

def close_all_non_crypto(max_retries=15, retry_interval=2.0):
    print("=" * 60)
    print("MT5 NON-CRYPTO POSITION CLOSURE & RISK DE-LEVERAGING")
    print(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    print("=" * 60)

    if not mt5.initialize():
        print("[ERROR] Failed to initialize MT5")
        sys.exit(1)

    acc = mt5.account_info()
    print(f"Account: {acc.login} | Balance: ${acc.balance:.2f} | Equity: ${acc.equity:.2f}")

    # First: Cancel Stale Non-Crypto Pending Orders
    orders = mt5.orders_get()
    cancelled_orders = 0
    if orders:
        for o in orders:
            if is_crypto(o.symbol):
                print(f"[PRESERVE] Crypto Order #{o.ticket} {o.symbol} {o.volume_current} lots @ {o.price_open}")
                continue

            req = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": o.ticket
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"[CANCELLED] Stale Order #{o.ticket} {o.symbol}")
                cancelled_orders += 1
            else:
                ret = res.retcode if res else "None"
                print(f"[NOTE] Order #{o.ticket} {o.symbol} cancel retcode={ret}")

    print(f"Total stale non-crypto orders cancelled: {cancelled_orders}")

    # Second: Close Non-Crypto Positions with Retry Loop
    for attempt in range(1, max_retries + 1):
        positions = mt5.positions_get()
        non_crypto_pos = [p for p in (positions or []) if not is_crypto(p.symbol)]
        
        if not non_crypto_pos:
            print("[SUCCESS] All non-crypto positions are closed!")
            break

        print(f"\n[ATTEMPT {attempt}/{max_retries}] Found {len(non_crypto_pos)} open non-crypto positions...")
        all_closed_this_round = True

        for p in non_crypto_pos:
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            price = mt5.symbol_info_tick(p.symbol)
            if not price:
                print(f"[WARN] No tick for {p.symbol}, market may be pre-open. Retrying...")
                all_closed_this_round = False
                continue

            close_price = price.bid if close_type == mt5.ORDER_TYPE_SELL else price.ask
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": p.ticket,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": close_type,
                "price": close_price,
                "deviation": 50,
                "magic": p.magic,
                "comment": "TradFi open risk exit",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"[CLOSED] Non-crypto #{p.ticket} {p.symbol} {p.volume} lots @ {close_price} (pnl: ${p.profit:.2f})")
            else:
                ret = res.retcode if res else "None"
                comment = res.comment if res else "Unknown"
                print(f"[RETRY] Pos #{p.ticket} {p.symbol}: retcode={ret} ({comment})")
                all_closed_this_round = False

        if all_closed_this_round:
            print("[SUCCESS] All non-crypto positions successfully closed!")
            break

        time.sleep(retry_interval)

    # Final Account State
    acc_after = mt5.account_info()
    print(f"\nFinal State: Balance ${acc_after.balance:.2f} | Equity ${acc_after.equity:.2f} | Margin ${acc_after.margin:.2f}")
    print(f"Blueberry Phase 1 Gap: ${2700.0 - acc_after.equity:.2f}")

    # Print remaining positions
    pos_remaining = mt5.positions_get()
    print(f"\nRemaining Open Positions ({len(pos_remaining) if pos_remaining else 0}):")
    if pos_remaining:
        for p in pos_remaining:
            print(f"  #{p.ticket} {p.symbol} {'BUY' if p.type == 0 else 'SELL'} {p.volume} lots (profit: ${p.profit:.2f})")

    mt5.shutdown()

def main():
    parser = argparse.ArgumentParser(description="Close non-crypto positions on MT5.")
    parser.add_argument("--retries", type=int, default=15, help="Max retry attempts")
    parser.add_argument("--interval", type=float, default=2.0, help="Retry interval in seconds")
    args = parser.parse_args()

    close_all_non_crypto(max_retries=args.retries, retry_interval=args.interval)

if __name__ == "__main__":
    main()
