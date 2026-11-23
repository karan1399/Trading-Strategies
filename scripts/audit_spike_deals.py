import MetaTrader5 as mt5
from datetime import datetime, timezone

mt5.initialize()

deal_tickets = [18513226, 18513361, 18513505, 18513608, 18513813, 18514001, 18514035]

print("=== DEALS AUDIT ===")
for dt in deal_tickets:
    d = mt5.history_deals_get(ticket=dt)
    if d:
        deal = d[0]
        side = "BUY" if deal.type == 0 else "SELL"
        t_utc = datetime.fromtimestamp(deal.time_msc / 1000, timezone.utc).isoformat()
        print(f"Deal #{deal.ticket} | Order #{deal.order} | {deal.symbol:9s} | {side} | vol={deal.volume} | px={deal.price:8.2f} | profit=${deal.profit:6.2f} | comm='{deal.comment}' | time={t_utc}")

mt5.shutdown()
