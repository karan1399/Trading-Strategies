import MetaTrader5 as mt5

def check_specs():
    if not mt5.initialize():
        print("MT5 failed")
        return
    symbols = ['XAUUSD.pi', 'XAGUSD.pi', 'SP500.p', 'NAS100.p', 'DJ30.p', 'BTCUSD.pi', 'SOLUSD.p', 'USDCAD.pi', 'USDJPY.pi']
    print(f"{'Symbol':<12} {'Bid':<10} {'Ask':<10} {'Contract':<10} {'TickVal':<10} {'MinLot':<8} {'LotStep':<8}")
    print("-" * 75)
    for s in symbols:
        info = mt5.symbol_info(s)
        if info:
            print(f"{s:<12} {info.bid:<10.3f} {info.ask:<10.3f} {info.trade_contract_size:<10.2f} {info.trade_tick_value:<10.4f} {info.volume_min:<8.2f} {info.volume_step:<8.2f}")
        else:
            print(f"{s:<12} NOT FOUND")
            
if __name__ == "__main__":
    check_specs()
    mt5.shutdown()
