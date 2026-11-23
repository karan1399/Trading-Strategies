import MetaTrader5 as mt5
import json

if not mt5.initialize():
    print("MT5 init failed:", mt5.last_error())
    exit(1)

all_symbols = mt5.symbols_get()
crypto_syms = [s.name for s in all_symbols if "USD" in s.name and any(c in s.name for c in ["BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "LTC", "BNB", "AVAX", "NEAR", "SUI", "BCH"])]

print(f"Discovered {len(crypto_syms)} crypto symbols on Blueberry MT5:")
catalogue = []
for sym in crypto_syms:
    info = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)
    if info and tick:
        catalogue.append({
            "name": sym,
            "path": info.path,
            "contract_size": info.trade_contract_size,
            "volume_min": info.volume_min,
            "volume_step": info.volume_step,
            "volume_max": info.volume_max,
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": tick.ask - tick.bid
        })

mt5.shutdown()

print(json.dumps(catalogue, indent=2))
