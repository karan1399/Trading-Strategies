import MetaTrader5 as mt5
import json
import sys

if not mt5.initialize():
    print(json.dumps({"ok": False, "error": f"MT5 initialize failed: {mt5.last_error()}"}))
    sys.exit(1)

symbol = "SOLUSD.p"
s = mt5.symbol_info(symbol)
if not s:
    print(json.dumps({"ok": False, "error": f"Symbol {symbol} not found on broker"}))
    mt5.shutdown()
    sys.exit(1)

entry_price = 73.00
sl_price = 74.40
tp_price = 69.50
volume = 0.01

req = {
    "action": mt5.TRADE_ACTION_PENDING,
    "symbol": symbol,
    "volume": volume,
    "type": mt5.ORDER_TYPE_SELL_LIMIT,
    "price": entry_price,
    "sl": sl_price,
    "tp": tp_price,
    "deviation": 20,
    "magic": 100854,
    "comment": "ATG-SOL-R854",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
}

res = mt5.order_send(req)
if res is None:
    print(json.dumps({"ok": False, "error": f"order_send failed: {mt5.last_error()}"}))
else:
    print(json.dumps({
        "ok": res.retcode == mt5.TRADE_RETCODE_DONE or res.retcode == 10009,
        "retcode": res.retcode,
        "comment": res.comment,
        "order_ticket": res.order,
        "volume": res.volume,
        "price": res.price
    }))

mt5.shutdown()
