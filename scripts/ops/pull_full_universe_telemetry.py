#!/usr/bin/env python3
"""
Comprehensive Live Order-Flow Telemetry Collector & Quantitative Reviewer
Pulls across all data points, all tabs, and all timeframes from the local CG Terminal.
"""

import urllib.request
import urllib.parse
import json
import time
import os
import sys
from datetime import datetime, timezone

BASE_URL = os.environ.get("CG_TERMINAL_URL", "http://127.0.0.1:3000")

# Primary evaluation universe: majors + candidates
DEEP_SYMBOLS = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "LTC", "XRP", "BNB", "AVAX", "SUI", "NEAR", "BCH"
]

def fetch_json(endpoint, timeout=10):
    url = f"{BASE_URL}{endpoint}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AntigravityTelemetry/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if res.status == 200:
                return json.loads(res.read().decode("utf-8"))
            return {"error": f"HTTP {res.status}"}
    except Exception as e:
        return {"error": str(e)}

def pull_telemetry():
    start_time = time.time()
    iso_now = datetime.now(timezone.utc).isoformat()
    print(f"[{iso_now}] Starting comprehensive telemetry harvest from {BASE_URL}...")

    telemetry = {
        "metadata": {
            "timestamp_utc": iso_now,
            "harvest_duration_sec": 0,
            "base_url": BASE_URL,
            "version": "1.0.0"
        },
        "universe": {},
        "screener": None,
        "assets_registry": None,
        "stopmap_btc": None,
        "symbols": {}
    }

    # 1. Assets Registry
    print("  -> Pulling /api/cg/assets...")
    assets = fetch_json("/api/cg/assets")
    telemetry["assets_registry"] = assets
    if isinstance(assets, dict) and "curated" in assets:
        telemetry["universe"]["curated_count"] = len(assets["curated"])
        telemetry["universe"]["curated_symbols"] = [x.get("sym") for x in assets["curated"] if isinstance(x, dict)]

    # 2. Screener Matrix (All 228+ assets with 31 metrics)
    print("  -> Pulling /api/cg/screener...")
    screener = fetch_json("/api/cg/screener")
    telemetry["screener"] = screener

    # 3. BTC Stopmap / Heatmap
    print("  -> Pulling /api/cg/stopmap?sym=BTC...")
    telemetry["stopmap_btc"] = fetch_json("/api/cg/stopmap?sym=BTC")

    # 4. Deep Telemetry Per Symbol (Across all 7 tabs)
    for sym in DEEP_SYMBOLS:
        print(f"  -> Pulling all order-flow tabs for {sym}...")
        sym_bundle = {
            "symbol": sym,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "tabs": {}
        }

        # Tab 1: Overview & Live Price Ticker
        sym_bundle["tabs"]["overview"] = fetch_json(f"/api/cg/ticker?sym={sym}")
        
        # Tab 2: Footprint (Taker Buy/Sell USD, Delta %, POC)
        sym_bundle["tabs"]["footprint_15m"] = fetch_json(f"/api/cg/fp?sym={sym}&interval=15m&limit=12")
        sym_bundle["tabs"]["footprint_1h"] = fetch_json(f"/api/cg/fp?sym={sym}&interval=1h&limit=12")

        # Tab 3: Large Orders / Whale Flow
        sym_bundle["tabs"]["large_orders"] = fetch_json(f"/api/cg/largeorder?symbol={sym}")

        # Tab 4: Liquidation Levels (Key levels above/below, strength 0-100, USD notional)
        sym_bundle["tabs"]["liqlevels"] = fetch_json(f"/api/cg/liqlevels?sym={sym}")

        # Tab 5: Orderbook Walls & Imbalance (Binance & Aggregate Depth)
        sym_bundle["tabs"]["bookwalls"] = fetch_json(f"/api/cg/bookwalls?market={sym}USDT")

        # Tab 6: CVD (Spot & Perp Cumulative Volume Delta, Divergences)
        sym_bundle["tabs"]["cvd_1h"] = fetch_json(f"/api/cg/cvd?sym={sym}&itv=1h")
        sym_bundle["tabs"]["cvd_4h"] = fetch_json(f"/api/cg/cvd?sym={sym}&itv=4h")

        # Tab 7: Order-flow Signals (Absorption, Exhaustion, Trapped Traders)
        sym_bundle["tabs"]["signals"] = fetch_json(f"/api/cg/signals?sym={sym}")

        # Additional Microstructure: Market Snapshot, Funding & Open Interest
        sym_bundle["tabs"]["market_snapshot"] = fetch_json(f"/api/cg/market?sym={sym}")
        sym_bundle["tabs"]["funding"] = fetch_json(f"/api/cg/funding?sym={sym}")
        sym_bundle["tabs"]["oi"] = fetch_json(f"/api/cg/oi?sym={sym}")

        telemetry["symbols"][sym] = sym_bundle
        time.sleep(0.15)  # respectful pacing

    telemetry["metadata"]["harvest_duration_sec"] = round(time.time() - start_time, 2)
    print(f"[Done] Harvested full telemetry in {telemetry['metadata']['harvest_duration_sec']}s")

    # Persist file
    os.makedirs("data/telemetry", exist_ok=True)
    outpath = "data/telemetry/full_universe_live_telemetry.json"
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(telemetry, f, indent=2)
    print(f"Saved complete telemetry dataset to: {outpath}")
    return telemetry, outpath

if __name__ == "__main__":
    pull_telemetry()
