import MetaTrader5 as mt5
import time
import json

def punch_basket():
    if not mt5.initialize():
        print("MT5 initialization failed")
        return
        
    acc = mt5.account_info()
    print(f"=== MT5 Account #{acc.login} Pre-Execution ===")
    print(f"Equity: ${acc.equity:.2f} | Balance: ${acc.balance:.2f} | Free Margin: ${acc.margin_free:.2f}")
    
    # 1. Gold (XAUUSD.pi) Long
    sym_xau = "XAUUSD.pi"
    mt5.symbol_select(sym_xau, True)
    info_xau = mt5.symbol_info(sym_xau)
    if info_xau:
        ask_xau = info_xau.ask
        sl_xau = 3647.00
        tp_xau = 3664.00
        vol_xau = 0.01
        risk_xau = (ask_xau - sl_xau) * 100 * vol_xau
        reward_xau = (tp_xau - ask_xau) * 100 * vol_xau
        r_xau = reward_xau / risk_xau if risk_xau > 0 else 0
        print(f"\n[Trade 1 - XAUUSD.pi]: Ask={ask_xau:.2f} SL={sl_xau:.2f} TP={tp_xau:.2f} Risk=${risk_xau:.2f} Reward=${reward_xau:.2f} Asymmetry={r_xau:.2f}R")
        
        req_xau = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym_xau,
            "volume": vol_xau,
            "type": mt5.ORDER_TYPE_BUY,
            "price": ask_xau,
            "sl": sl_xau,
            "tp": tp_xau,
            "deviation": 20,
            "magic": 42001,
            "comment": "ATG-XAU-SWEEP-2R",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res_xau = mt5.order_send(req_xau)
        print(f"  Result: retcode={res_xau.retcode} comment={res_xau.comment} deal={res_xau.deal} order={res_xau.order}")
        if res_xau.retcode not in (mt5.TRADE_RETCODE_DONE, 10009):
            # Try with RETURN filling
            req_xau["type_filling"] = mt5.ORDER_FILLING_RETURN
            res_xau = mt5.order_send(req_xau)
            print(f"  Retry with RETURN: retcode={res_xau.retcode} order={res_xau.order}")
    else:
        print(f"Symbol {sym_xau} not available")

    # 2. S&P 500 (SP500.p) Long
    sym_sp = "SP500.p"
    mt5.symbol_select(sym_sp, True)
    info_sp = mt5.symbol_info(sym_sp)
    if info_sp:
        ask_sp = info_sp.ask
        sl_sp = 7208.00
        tp_sp = 7315.00
        vol_sp = 0.01
        risk_sp = (ask_sp - sl_sp) * 10 * vol_sp
        reward_sp = (tp_sp - ask_sp) * 10 * vol_sp
        r_sp = reward_sp / risk_sp if risk_sp > 0 else 0
        print(f"\n[Trade 2 - SP500.p]: Ask={ask_sp:.2f} SL={sl_sp:.2f} TP={tp_sp:.2f} Risk=${risk_sp:.2f} Reward=${reward_sp:.2f} Asymmetry={r_sp:.2f}R")
        
        req_sp = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym_sp,
            "volume": vol_sp,
            "type": mt5.ORDER_TYPE_BUY,
            "price": ask_sp,
            "sl": sl_sp,
            "tp": tp_sp,
            "deviation": 20,
            "magic": 42001,
            "comment": "ATG-SPX-REBOUND-2R",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res_sp = mt5.order_send(req_sp)
        print(f"  Result: retcode={res_sp.retcode} comment={res_sp.comment} deal={res_sp.deal} order={res_sp.order}")
        if res_sp.retcode not in (mt5.TRADE_RETCODE_DONE, 10009):
            req_sp["type_filling"] = mt5.ORDER_FILLING_RETURN
            res_sp = mt5.order_send(req_sp)
            print(f"  Retry with RETURN: retcode={res_sp.retcode} order={res_sp.order}")
    else:
        print(f"Symbol {sym_sp} not available")

    time.sleep(1)
    
    # Readback all positions
    print("\n=== Post-Execution Live Positions ===")
    positions = mt5.positions_get()
    if positions:
        total_risk = 0
        for p in positions:
            print(f"  Ticket #{p.ticket}: {p.symbol} {'BUY' if p.type==0 else 'SELL'} {p.volume} lots @ {p.price_open:.2f} -> {p.price_current:.2f} | SL={p.sl:.2f} TP={p.tp:.2f} | PnL=${p.profit:.2f}")
    
    acc_post = mt5.account_info()
    print(f"\nAccount #{acc_post.login} Equity: ${acc_post.equity:.2f} | Balance: ${acc_post.balance:.2f} | Free Margin: ${acc_post.margin_free:.2f}")

if __name__ == "__main__":
    punch_basket()
    mt5.shutdown()
