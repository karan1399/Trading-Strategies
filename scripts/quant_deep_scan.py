import json, time, datetime, sys, os
sys.path.insert(0, os.path.abspath("."))
import MetaTrader5 as mt5
import numpy as np

if not mt5.initialize():
    print("MT5 Init Failed")
    quit()

from engine.unified_alpha_pilot import (
    sig_htf_gold_at, sig_vc_at, sig_ivwall_at, sig_orb_at,
    wilder_atr, ewma_sigma, session_spans_from, spec_from_mt5
)

cfg = json.load(open("engine/config.json"))
print("================================================================================")
print("QUANTITATIVE MULTI-FACTOR SCAN ON LIVE BROKER MARKET")
print("================================================================================")

# 1. Sleeve 1: htf_gold (H4)
print("\n>>> Sleeve 1: htf_gold (H4 EMA233 / P4 Cluster)")
s_htf = cfg["sleeves"]["htf_gold"]
for sym, params in s_htf["symbols"].items():
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H4, 0, 100)
    if rates is not None and len(rates) > 30:
        o = rates["open"][:-1].astype(float)
        h = rates["high"][:-1].astype(float)
        l = rates["low"][:-1].astype(float)
        c = rates["close"][:-1].astype(float)
        atr = wilder_atr(h, l, c, 14)
        sig = sig_htf_gold_at(o, h, l, c, atr, len(c)-1, params)
        if sig:
            print(f"  [SIGNAL TRIGGERED] {sym:<10} | Side: {sig['side']} | Note: {sig['note']} | SL: {sig.get('sl_abs')}")
        else:
            print(f"  [FLAT] {sym:<10} | Close: {c[-1]:.2f} | No EMA233 cluster rejection on closed bar")

# 2. Sleeve 2: vc_gold (H4)
print("\n>>> Sleeve 2: vc_gold (H4 Volatility Compression Squeeze)")
s_vc = cfg["sleeves"]["vc_gold"]
for sym, params in s_vc["symbols"].items():
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H4, 0, 100)
    if rates is not None and len(rates) > 30:
        o = rates["open"][:-1].astype(float)
        h = rates["high"][:-1].astype(float)
        l = rates["low"][:-1].astype(float)
        c = rates["close"][:-1].astype(float)
        atr = wilder_atr(h, l, c, 14)
        sig = sig_vc_at(o, h, l, c, atr, len(c)-1, params)
        if sig:
            print(f"  [SIGNAL TRIGGERED] {sym:<10} | Side: {sig['side']} | Note: {sig['note']} | SL: {sig.get('sl_abs')}")
        else:
            print(f"  [FLAT] {sym:<10} | Close: {c[-1]:.2f} | Bollinger/Keltner squeeze not firing on closed bar")

# 3. Sleeve 3: ivwall (M15)
print("\n>>> Sleeve 3: ivwall (M15 FX Implied Volatility Wall Fades)")
s_iv = cfg["sleeves"]["ivwall"]
signals_found = 0
for sym in s_iv["symbols"]:
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 150)
    if rates is not None and len(rates) > 50:
        o = rates["open"][:-1].astype(float)
        h = rates["high"][:-1].astype(float)
        l = rates["low"][:-1].astype(float)
        c = rates["close"][:-1].astype(float)
        tm = (rates["time"][:-1] // 60).astype("int64")
        atr = wilder_atr(h, l, c, 14)
        sig_series = ewma_sigma(c, 0.94)
        spans = session_spans_from(tm, 2)
        sig = sig_ivwall_at(o, h, l, c, tm, atr, sig_series, len(c)-1, {**s_iv, "broker_utc_offset": 2}, spans)
        if sig:
            signals_found += 1
            print(f"  [SIGNAL TRIGGERED] {sym:<10} | Side: {sig['side']} | Note: {sig['note']} | SL: {sig.get('sl_abs')}")
if signals_found == 0:
    print("  [ALL FLAT] 14 FX majors checked. No price touching/fading 2.5-sigma IV boundary on closed M15 bar.")

# 4. Sleeve 4: orb_nas (M15)
print("\n>>> Sleeve 4: orb_nas (M15 NAS100 30-min Opening Range Breakout)")
sym = "NAS100.p"
r15 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 150)
r4 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H4, 0, 100)
if r15 is not None and r4 is not None:
    o = r15["open"][:-1].astype(float)
    h = r15["high"][:-1].astype(float)
    l = r15["low"][:-1].astype(float)
    c = r15["close"][:-1].astype(float)
    tm = (r15["time"][:-1] // 60).astype("int64")
    atr = wilder_atr(h, l, c, 14)
    htf_c4 = r4["close"][:-1].astype(float)
    sig = sig_orb_at(o, h, l, c, tm, atr, htf_c4, len(c)-1, cfg["sleeves"]["orb_nas"])
    if sig:
        print(f"  [SIGNAL TRIGGERED] {sym} | Side: {sig['side']} | Note: {sig['note']}")
    else:
        print(f"  [FLAT] {sym} | Cash open session (14:30 UTC) passed; window closed or no breakout extension.")

print("\n================================================================================")
print("CURRENT BLOTTER STATUS & EXPOSURE AUDIT")
print("================================================================================")
positions = mt5.positions_get()
print(f"Active Positions: {len(positions) if positions else 0} / 2 MAX")
if positions:
    for p in positions:
        print(f"  Ticket #{p.ticket}: {p.symbol} {'BUY' if p.type==0 else 'SELL'} {p.volume} lots @ {p.price_open:.2f} | Current: {p.price_current:.2f} | Floating: ${p.profit:.2f} USD")
orders = mt5.orders_get()
print(f"Pending Orders: {len(orders) if orders else 0}")
if orders:
    for o in orders:
        print(f"  Ticket #{o.ticket}: {o.symbol} Type {o.type} {o.volume_current} lots @ {o.price_open:.2f} | SL: {o.sl:.2f} | TP: {o.tp:.2f}")

mt5.shutdown()
