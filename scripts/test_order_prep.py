import MetaTrader5 as mt5

def test_order_prep():
    if not mt5.initialize():
        print("MT5 failed")
        return
        
    acc = mt5.account_info()
    print(f"Account: #{acc.login} | Equity: ${acc.equity:.2f} | Free Margin: ${acc.margin_free:.2f}")
    
    # Check XAUUSD.pi
    info_xau = mt5.symbol_info("XAUUSD.pi")
    margin_xau = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, "XAUUSD.pi", 0.01, info_xau.ask)
    print(f"XAUUSD.pi: Ask={info_xau.ask:.2f} Bid={info_xau.bid:.2f} Spread={info_xau.spread} MarginReq=${margin_xau:.2f}")
    
    # Check SP500.p
    info_sp = mt5.symbol_info("SP500.p")
    margin_sp = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, "SP500.p", 0.01, info_sp.ask)
    print(f"SP500.p: Ask={info_sp.ask:.2f} Bid={info_sp.bid:.2f} Spread={info_sp.spread} MarginReq=${margin_sp:.2f}")
    
    # Check BTCUSD.pi
    info_btc = mt5.symbol_info("BTCUSD.pi")
    margin_btc = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, "BTCUSD.pi", 0.01, info_btc.ask)
    print(f"BTCUSD.pi: Ask={info_btc.ask:.2f} Bid={info_btc.bid:.2f} Spread={info_btc.spread} MarginReq=${margin_btc:.2f}")

if __name__ == "__main__":
    test_order_prep()
    mt5.shutdown()
