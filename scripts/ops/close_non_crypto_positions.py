#!/usr/bin/env python3
"""
close_non_crypto_positions.py
Surgically closes all non-crypto positions (Forex, Energy, Indices) on MT5
at TradFi market open to protect Blueberry FundedNext Phase 1 equity
from opening spread blowout, slippage, and weekend gap risk.

Crypto positions (BTC, ETH, SOL, BNB, XRP, etc.) are preserved.
Stale non-crypto pending orders are cancelled.
"""

import sys
import time
import json
import MetaTrader5 as mt5

CRYPTO_SYMBOLS = {'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOGE', 'AVAX', 'DOT', 'LINK', 'LTC'}

def is_crypto(symbol: str) -> bool:
    sym = symbol.upper()
    return any(c in sym for c in CRYPTO_SYMBOLS)

def main():
    print("=" * 60)
    print("MT5 NON-CRYPTO POSITION CLOSURE & RISK DE-LEVERAGING")
    print("=" * 60)

    if not mt5.initialize():
        print("[ERROR] Failed to initialize MT5")
        sys.exit(1)

    acc = mt5.account_info()
    print(f"Account: {acc.login} | Balance: ${acc.balance:.2f} | Equity: ${acc.equity:.2f}")

    # 1. Close Non-Crypto Positions
    positions = mt5.positions_get()
    closed_count = 0
    closed_pnl = 0.0

    if positions:
        for p in positions:
            if is_crypto(p.symbol):
                print(f"[PRESERVE] Crypto #{p.ticket} {p.symbol} {p.volume} lots (pnl: ${p.profit:.2f})")
                continue

            # Determine closing order type
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            price = mt5.symbol_info_tick(p.symbol)
            if not price:
                print(f"[WARN] No tick for {p.symbol}, skipping #{p.ticket}")
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
                print(f"[CLOSED] Non-crypto #{p.ticket} {p.symbol} {p.volume} lots @ {close_price} (profit: ${p.profit:.2f})")
                closed_count += 1
                closed_pnl += p.profit
            else:
                ret = res.retcode if res else "None"
                comment = res.comment if res else "Unknown"
                print(f"[FAILED] Close #{p.ticket} {p.symbol}: retcode={ret} ({comment})")

    print(f"\nTotal non-crypto positions closed: {closed_count} | Realized PnL: ${closed_pnl:.2f}")

    # 2. Cancel Stale Non-Crypto Pending Orders
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
                print(f"[FAILED] Cancel Order #{o.ticket}: retcode={ret}")

    print(f"Total stale non-crypto orders cancelled: {cancelled_orders}")

    # Final Account State
    acc_after = mt5.account_info()
    print(f"\nFinal State: Balance ${acc_after.balance:.2f} | Equity ${acc_after.equity:.2f} | Margin ${acc_after.margin:.2f}")
    print(f"Blueberry Phase 1 Gap: ${2700.0 - acc_after.equity:.2f}")

    mt5.shutdown()

if __name__ == "__main__":
    main()
