import urllib.request
import json
import time
import os

BASE_URL = "http://127.0.0.1:3000"
SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "LTC"]

results = {
    "timestamp": time.time(),
    "screener": None,
    "symbols": {}
}

def fetch(path, timeout=8):
    url = f"{BASE_URL}{path}"
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(req.read().decode())
    except Exception as e:
        return {"error": str(e)}

print("Fetching /api/cg/screener...")
results["screener"] = fetch("/api/cg/screener")

for sym in SYMBOLS:
    print(f"Fetching data for {sym}...")
    sym_bundle = {}
    
    # 1. Ticker
    sym_bundle["ticker"] = fetch(f"/api/cg/ticker?sym={sym}")
    
    # 2. Liq Levels
    sym_bundle["liqlevels"] = fetch(f"/api/cg/liqlevels?sym={sym}")
    
    # 3. Book Walls
    sym_bundle["bookwalls"] = fetch(f"/api/cg/bookwalls?market={sym}USDT")
    
    # 4. CVD
    sym_bundle["cvd"] = fetch(f"/api/cg/cvd?sym={sym}&itv=1h")
    
    # 5. Signals
    sym_bundle["signals"] = fetch(f"/api/cg/signals?sym={sym}")
    
    # 6. Large orders / Whale
    sym_bundle["large_orders"] = fetch(f"/api/cg/largeorder?symbol={sym}")
    
    # 7. Footprint
    sym_bundle["footprint"] = fetch(f"/api/cg/fp?sym={sym}&interval=15m&limit=10")
    
    results["symbols"][sym] = sym_bundle
    time.sleep(0.3)

# Save comprehensive bundle
os.makedirs("data/telemetry", exist_ok=True)
outfile = "data/telemetry/terminal_comprehensive_live.json"
with open(outfile, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(f"Comprehensive live telemetry saved to {outfile}")
