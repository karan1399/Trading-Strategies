import json

with open("data/telemetry/full_universe_live_telemetry.json", "r", encoding="utf-8") as f:
    d = json.load(f)

print("=== TELEMETRY SUMMARY ===")
print("Timestamp:", d["metadata"]["timestamp_utc"])
print("Harvest Duration:", d["metadata"]["harvest_duration_sec"], "seconds")
print("Total symbols deeply harvested:", len(d["symbols"]))

print("\n=== PER-SYMBOL LIVE METRICS ===")
for sym, bundle in d["symbols"].items():
    tabs = bundle["tabs"]
    
    # Overview / ticker
    ticker = tabs.get("overview", {})
    last_price = ticker.get("lastPrice") or ticker.get("price") or "N/A"
    
    # Liq levels
    liq = tabs.get("liqlevels", {})
    stats = liq.get("stats", {})
    key_levels = stats.get("keyLevels", {})
    above_levels = key_levels.get("above", [])
    below_levels = key_levels.get("below", [])
    
    nearest_above = above_levels[0] if above_levels else {}
    nearest_below = below_levels[0] if below_levels else {}
    
    # CVD
    cvd = tabs.get("cvd_1h", {})
    cvd_div = cvd.get("divergence") or "None"
    
    # Bookwalls
    bw = tabs.get("bookwalls", {})
    imb = bw.get("imbalance") or bw.get("imbPct") or "N/A"
    
    # Signals
    sigs = tabs.get("signals", {})
    sig_list = sigs.get("signals", []) if isinstance(sigs, dict) else []
    
    print(f"\n--- {sym} ---")
    print(f"  Live Price: {last_price}")
    print(f"  Nearest Fuel Above: {nearest_above.get('price')} (dist: {nearest_above.get('distPct')}%, str: {nearest_above.get('strength')})")
    print(f"  Nearest Target Below: {nearest_below.get('price')} (dist: {nearest_below.get('distPct')}%, str: {nearest_below.get('strength')})")
    print(f"  Total Key Levels Above: {len(above_levels)}, Below: {len(below_levels)}")
    print(f"  CVD 1H Divergence: {cvd_div}")
    print(f"  Orderbook Imbalance: {imb}")
    print(f"  Signals Count: {len(sig_list)}")
