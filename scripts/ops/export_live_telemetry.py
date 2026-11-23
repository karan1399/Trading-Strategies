import urllib.request
import json
import datetime

symbols = ["BTC", "ETH", "DOGE", "LINK", "ADA", "LTC", "SOL"]
telemetry = {
    "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "host": "antigravity-local-terminal",
    "symbols": {}
}

base_url = "http://127.0.0.1:3000"

# 1. Fetch assets
try:
    req = urllib.request.urlopen(f"{base_url}/api/cg/assets", timeout=10)
    telemetry["assets"] = json.loads(req.read().decode())
except Exception as e:
    telemetry["assets_err"] = str(e)

# 2. Fetch screener
try:
    req = urllib.request.urlopen(f"{base_url}/api/cg/screener", timeout=10)
    telemetry["screener"] = json.loads(req.read().decode())
except Exception as e:
    telemetry["screener_err"] = str(e)

# 3. Fetch liqlevels and live ticker per symbol
for sym in symbols:
    sym_data = {}
    try:
        req = urllib.request.urlopen(f"{base_url}/api/cg/liqlevels?sym={sym}", timeout=10)
        sym_data["liqlevels"] = json.loads(req.read().decode())
    except Exception as e:
        sym_data["liqlevels_err"] = str(e)

    try:
        req = urllib.request.urlopen(f"{base_url}/api/cg/ticker?sym={sym}", timeout=10)
        sym_data["ticker"] = json.loads(req.read().decode())
    except Exception as e:
        sym_data["ticker_err"] = str(e)
    
    try:
        req = urllib.request.urlopen(f"{base_url}/api/cg/live", timeout=10)
        sym_data["live"] = json.loads(req.read().decode())
    except Exception as e:
        sym_data["live_err"] = str(e)

    telemetry["symbols"][sym] = sym_data

with open("desk/inbox/LIVE_TELEMETRY.json", "w", encoding="utf-8") as f:
    json.dump(telemetry, f, indent=2)

print("Saved live telemetry snapshot to desk/inbox/LIVE_TELEMETRY.json")
