import MetaTrader5 as mt5
import json
import urllib.request
from datetime import datetime, timezone

def read_mt5():
    print("=" * 60)
    print("MT5 LIVE VENUE READBACK")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    if not mt5.initialize():
        print("[ERROR] Could not initialize MT5")
        return None
    acc = mt5.account_info()
    print(f"Account: {acc.login} | Server: {acc.server}")
    print(f"Balance: ${acc.balance:.2f} | Equity: ${acc.equity:.2f} | Margin: ${acc.margin:.2f}")
    gap = 2700.0 - acc.equity
    print(f"Blueberry FundedNext Phase 1 Goal: $2,700.00 | Gap: ${gap:.2f}")

    pos = mt5.positions_get() or []
    print(f"\nOpen Positions ({len(pos)}):")
    crypto_count = 0
    non_crypto_count = 0
    for p in pos:
        is_c = any(c in p.symbol.upper() for c in ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA'])
        tag = "[CRYPTO]" if is_c else "[NON-CRYPTO]"
        if is_c:
            crypto_count += 1
        else:
            non_crypto_count += 1
        side = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
        print(f"  {tag} #{p.ticket} {p.symbol} {side} {p.volume} lots @ {p.price_open:.5f} -> {p.price_current:.5f} | PnL: ${p.profit:+.2f} | SL: {p.sl} | TP: {p.tp}")

    orders = mt5.orders_get() or []
    print(f"\nPending Orders ({len(orders)}):")
    for o in orders:
        is_c = any(c in o.symbol.upper() for c in ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA'])
        tag = "[CRYPTO]" if is_c else "[NON-CRYPTO]"
        print(f"  {tag} #{o.ticket} {o.symbol} type={o.type} {o.volume_current} lots @ {o.price_open}")

    mt5.shutdown()
    return {
        "balance": acc.balance,
        "equity": acc.equity,
        "gap": gap,
        "positions_total": len(pos),
        "crypto_positions": crypto_count,
        "non_crypto_positions": non_crypto_count,
        "orders_total": len(orders)
    }

if __name__ == "__main__":
    res = read_mt5()
