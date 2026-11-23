import urllib.request
import json
import time
import os

BASE = "http://localhost:3000"

def fetch_json(endpoint):
    url = f"{BASE}{endpoint}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-DeepQuant/1.0"})
        with urllib.request.urlopen(req, timeout=12) as res:
            if res.status == 200:
                return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        return {"_err": str(e)}
    return {"_err": "non-200"}

# Assets to deeply analyze
SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR"]

results = {}

for sym in SYMBOLS:
    print(f"--> Harvesting deep matrix for {sym}...")
    asset_data = {
        "symbol": sym,
        "timestamp": time.time(),
        "stopmap": {},
        "liqlevels": {},
        "bookwalls": {},
        "cvd": {},
        "signals": {},
        "large_orders": {},
        "market": {}
    }

    # 1. STOPMAP (Liquidation Heatmap - Models 1/2/3, coin & pair, ranges)
    for model in [1, 2, 3]:
        for mode in ["coin", "pair"]:
            for rng in ["24h", "3d", "7d", "30d"]:
                res = fetch_json(f"/api/cg/stopmap?sym={sym}&model={model}&mode={mode}&range={rng}")
                if res and res.get("ok"):
                    key = f"m{model}_{mode}_{rng}"
                    asset_data["stopmap"][key] = {
                        "keyLevels": res.get("keyLevels"),
                        "overview": res.get("overview"),
                        "authTier": res.get("authTier"),
                        "mode": res.get("mode")
                    }

    # 2. LIQLEVELS (3d, 7d, 30d)
    for rng in ["3d", "7d", "30d"]:
        res = fetch_json(f"/api/cg/liqlevels?sym={sym}&range={rng}")
        if res and res.get("ok"):
            asset_data["liqlevels"][rng] = {
                "stats": res.get("stats"),
                "venues": res.get("venues"),
                "perVenue": res.get("perVenue"),
                "livePrice": res.get("livePrice")
            }

    # 3. BOOK WALLS (futures vs spot, intervals 1m, 5m, 15m, 1h, 4h, bands 2%, 5%)
    for market in ["futures", "spot"]:
        asset_data["bookwalls"][market] = {}
        for itv in ["15m", "1h", "4h"]:
            res = fetch_json(f"/api/cg/bookwalls?sym={sym}&market={market}&interval={itv}&band=5")
            if res and res.get("ok"):
                asset_data["bookwalls"][market][itv] = {
                    "imbalance": res.get("imbalance"),
                    "imbalance5mAgo": res.get("imbalance5mAgo"),
                    "bidDepth": res.get("bidDepth"),
                    "askDepth": res.get("askDepth"),
                    "largestBid": res.get("largestBid"),
                    "largestAsk": res.get("largestAsk"),
                    "walls": res.get("walls", [])[:8], # top 8 confirmed walls
                    "events": res.get("events", [])[:5]  # recent add/fill/pull events
                }

    # 4. CVD (perp vs spot vs split, across intervals 5m, 15m, 1h, 4h, 1d)
    for mode in ["perp", "spot", "split"]:
        asset_data["cvd"][mode] = {}
        for itv in ["15m", "1h", "4h"]:
            res = fetch_json(f"/api/cg/cvd?sym={sym}&mode={mode}&itv={itv}&anchor=day")
            if res and res.get("ok"):
                asset_data["cvd"][mode][itv] = {
                    "kpis": res.get("kpis"),
                    "divergences": res.get("divergences", []),
                    "takerBuyShare": res.get("takerBuyShare"),
                    "quadrant": res.get("quadrant")
                }

    # 5. SIGNALS (15m time, auto volume, auto range)
    for bt in ["all", "time", "vol", "range"]:
        res = fetch_json(f"/api/cg/signals?sym={sym}&bt={bt}")
        if res and res.get("ok"):
            asset_data["signals"][bt] = {
                "counts": res.get("counts"),
                "thresholds": res.get("thresholds"),
                "sets": res.get("sets", [])
            }

    # 6. LARGE ORDERS (24h, 72h)
    bin_pair = f"Binance_{sym}USDT"
    res = fetch_json(f"/api/cg/largeorder?symbol={bin_pair}&interval=15m&hours=24")
    if res and res.get("ok"):
        asset_data["large_orders"]["24h"] = res.get("groups", [])[:5]

    # 7. MARKET OVERVIEW & AUCTION
    res = fetch_json(f"/api/cg/market?sym={sym}")
    if res and res.get("ok"):
        asset_data["market"] = res

    results[sym] = asset_data

out_path = "data/telemetry/deep_orderflow_matrix.json"
os.makedirs("data/telemetry", exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(f"COMPLETE! Deep matrix written to {out_path} ({os.path.getsize(out_path)} bytes)")
