import MetaTrader5 as mt5
from datetime import datetime

mt5.initialize()

print("Checking specific tickets:")
for t in [20411123, 20412983, 20418616, 20386324]:
    ord_info = mt5.history_orders_get(ticket=t)
    print(f"Ticket {t}: {ord_info}")

print("\nHistory orders total:", mt5.history_orders_total(datetime(2026, 11, 20), datetime(2026, 11, 24)))
all_recent = mt5.history_orders_get(datetime(2026, 11, 23, 0, 0), datetime(2026, 11, 24, 0, 0))
if all_recent:
    print(f"\nFound {len(all_recent)} orders today:")
    for o in all_recent[-10:]:
        print(f"Ticket #{o.ticket} | {o.symbol} | state={o.state} | type={o.type} | vol={o.volume_initial} | px={o.price_open} | sl={o.sl} | tp={o.tp}")

all_deals_today = mt5.history_deals_get(datetime(2026, 11, 23, 0, 0), datetime(2026, 11, 24, 0, 0))
if all_deals_today:
    print(f"\nFound {len(all_deals_today)} deals today:")
    for d in all_deals_today[-10:]:
        print(f"Deal #{d.ticket} | Order #{d.order} | {d.symbol} | profit=${d.profit} | comment={d.comment}")

mt5.shutdown()
