import MetaTrader5 as mt5

if not mt5.initialize():
    print("MT5 init failed")
    exit(1)

symbol = "BTCUSD.pi"
entry_price = 61650.0
sl_price = 62210.0
tp_price = 60325.0
volume = 0.01

tick = mt5.symbol_info_tick(symbol)
info = mt5.symbol_info(symbol)
print(f"{symbol} Ask: {tick.ask} Bid: {tick.bid} MinLot: {info.volume_min}")

request = {
    "action": mt5.TRADE_ACTION_PENDING,
    "symbol": symbol,
    "volume": volume,
    "type": mt5.ORDER_TYPE_SELL_LIMIT,
    "price": entry_price,
    "sl": sl_price,
    "tp": tp_price,
    "deviation": 20,
    "magic": 20261123,
    "comment": "R854 BTC Short 2.37R",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_RETURN,
}

check_res = mt5.order_check(request)
print("Order check result:", check_res)

if check_res.retcode == 0:
    res = mt5.order_send(request)
    print("Order send result:", res)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"SUCCESS: Placed {symbol} SELL_LIMIT {volume} @ {entry_price} (SL: {sl_price}, TP: {tp_price}) Ticket: #{res.order}")
    else:
        print(f"FAILED to place order: {res.comment}")
else:
    print(f"Check failed: {check_res.comment}")

mt5.shutdown()
