import MetaTrader5 as mt5

if not mt5.initialize():
    print("Failed to initialize MT5")
    exit(1)

ticket = 20386324
positions = mt5.positions_get(ticket=ticket)
if not positions:
    print(f"Position {ticket} not found")
    mt5.shutdown()
    exit(1)

pos = positions[0]
print(f"Current Position: {pos.symbol} {pos.volume} lots | Open: {pos.price_open} | Current: {pos.price_current} | SL: {pos.sl} | TP: {pos.tp}")

new_sl = 1732.00 # safely above current price 1721.3 and below old 1755.00
request = {
    "action": mt5.TRADE_ACTION_SLTP,
    "position": ticket,
    "symbol": pos.symbol,
    "sl": new_sl,
    "tp": pos.tp
}

check_res = mt5.order_check(request)
print("Order check result:", check_res)

if check_res.retcode == 0:
    res = mt5.order_send(request)
    print("Order send result:", res)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"SUCCESS: Trailed SL on {pos.symbol} #{ticket} to {new_sl}!")
    else:
        print(f"FAILED to trail SL: {res.comment}")
else:
    print(f"Check failed: {check_res.comment}")

mt5.shutdown()
