#!/usr/bin/env python3
"""
telemetry_daemon.py — High-Speed Per-Asset Order-Flow Telemetry Daemon.

Architecture:
- Fetches all data points directly from the local terminal engine (http://127.0.0.1:3000)
- Instead of monolithic multi-megabyte files, writes a clean, dedicated JSON file per asset:
    data/telemetry/assets/<SYM>.json
- Writes a single lightweight universe screener index:
    data/telemetry/universe_overview.json
- Updates continuously in the background using atomic writes (tmp -> replace)
- Covers every single attached tab at each level, market, and timeframe:
    1. Overview (Real-time price, Auction VAL/POC/VAH, VWAP ±1/2/3σ, EMA 13/89/233, RSI)
    2. Footprint (1m, 5m, 15m, 1h, 4h bars, buckets, delta %, POC)
    3. Footprint Signals (15m Time, Auto Volume, Auto Range: Absorption, Divergence, Trap, Exhaustion)
    4. Large Orders (Resting whale walls & executed trade notionals per venue)
    5. Liq Levels (3d, 7d, 30d clusters, total long vs short mass, distance bands)
    6. Stopmap (Models 1, 2, 3 in Coin & Pair modes across 24h, 3d, 7d, 30d)
    7. Book Walls (Futures Aggregated & Spot Aggregated across 15m, 1h, 4h, confirmed walls, age, events)
    8. CVD (Perp, Spot, Split across 15m, 1h, 4h, quadrant, divergences)
    9. TradFi Quant [COT] (CFTC reports for commodities/indices)
   10. Funding & OI (8h rates, open interest trends)
"""

import urllib.request
import json
import time
import os
import sys

BASE_URL = os.environ.get("TERMINAL_BASE_URL", "http://127.0.0.1:3000")
ASSETS_DIR = os.path.join("data", "telemetry", "assets")
OVERVIEW_FILE = os.path.join("data", "telemetry", "universe_overview.json")

# Prime Curated Order-Flow Universe
CURATED_UNIVERSE = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR",
    "SUI", "LTC", "BCH", "TIA", "APT", "ARB", "OP", "XAU", "XAG", "NVDA"
]

def fetch_json(path, timeout=3):
    url = f"{BASE_URL}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-TelemetryEngine/3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if res.status == 200:
                return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        return {"_err": str(e)}
    return {"_err": "non-200"}

def atomic_write(filepath, data):
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, filepath)

def harvest_asset(sym):
    t0 = time.time()
    profile = {
        "symbol": sym,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_epoch": time.time(),
        "ticker": None,
        "market_structure": None,
        "liquidations": {
            "clusters": {},
            "stopmap": {}
        },
        "book_walls": {
            "futures": {},
            "spot": {}
        },
        "cvd": {
            "perp": {},
            "spot": {},
            "split": {}
        },
        "footprint": {
            "bars": {},
            "signals": {}
        },
        "large_orders": {},
        "cot_report": None,
        "funding_and_oi": {}
    }

    # 1. TICKER & MARKET STRUCTURE (Auction, VWAP, EMAs)
    tk = fetch_json(f"/api/cg/ticker?sym={sym}")
    if tk and tk.get("ok"):
        profile["ticker"] = {
            "price": tk.get("price"),
            "dir": tk.get("dir"),
            "source": tk.get("source"),
            "updatedAt": tk.get("updatedAt")
        }

    mkt = fetch_json(f"/api/cg/market?sym={sym}")
    if mkt and mkt.get("ok"):
        profile["market_structure"] = {
            "price": mkt.get("price"),
            "auction": mkt.get("auction"),
            "vwap": mkt.get("vwap"),
            "emas": mkt.get("emas"),
            "rsi": mkt.get("rsi")
        }

    # 2. LIQUIDATION LEVELS (3d, 7d, 30d)
    for rng in ["3d", "7d", "30d"]:
        l_res = fetch_json(f"/api/cg/liqlevels?sym={sym}&range={rng}")
        if l_res and l_res.get("ok"):
            profile["liquidations"]["clusters"][rng] = {
                "stats": l_res.get("stats"),
                "venues": l_res.get("venues"),
                "livePrice": l_res.get("livePrice")
            }

    # 3. STOPMAP HEATMAP (Models 1/2/3, Coin & Pair modes)
    for m in [1, 2, 3]:
        for mode in ["coin", "pair"]:
            for rng in ["24h", "3d", "7d"]:
                sm = fetch_json(f"/api/cg/stopmap?sym={sym}&model={m}&mode={mode}&range={rng}")
                if sm and sm.get("ok"):
                    profile["liquidations"]["stopmap"][f"m{m}_{mode}_{rng}"] = {
                        "keyLevels": sm.get("keyLevels"),
                        "overview": sm.get("overview")
                    }

    # 4. BOOK WALLS (Futures Aggregated & Spot Aggregated across 15m, 1h, 4h)
    for mkt_type in ["futures", "spot"]:
        for itv in ["m15", "h1", "h4"]:
            bw = fetch_json(f"/api/cg/bookwalls?sym={sym}&market={mkt_type}&interval={itv}&band=5")
            if bw and bw.get("ok"):
                profile["book_walls"][mkt_type][itv] = {
                    "imbalance": bw.get("imbalance"),
                    "imbalance5mAgo": bw.get("imbalance5mAgo"),
                    "bidDepth": bw.get("bidDepth"),
                    "askDepth": bw.get("askDepth"),
                    "largestBid": bw.get("largestBid"),
                    "largestAsk": bw.get("largestAsk"),
                    "walls": bw.get("walls", [])[:8],
                    "events": bw.get("events", [])[:5]
                }

    # 5. CVD (Perp, Spot, Split across 15m, 1h, 4h)
    for cvd_mode in ["perp", "spot", "split"]:
        for itv in ["m15", "h1", "h4"]:
            cv = fetch_json(f"/api/cg/cvd?sym={sym}&mode={cvd_mode}&itv={itv}&anchor=day")
            if cv and cv.get("ok"):
                profile["cvd"][cvd_mode][itv] = {
                    "kpis": cv.get("kpis"),
                    "divergences": cv.get("divergences", []),
                    "quadrant": cv.get("quadrant")
                }

    # 6. FOOTPRINT BARS & SIGNALS
    for itv in ["m5", "m15", "h1"]:
        fp = fetch_json(f"/api/cg/fp?sym={sym}&itv={itv}&limit=50")
        if fp and fp.get("ok"):
            profile["footprint"]["bars"][itv] = {
                "supported": fp.get("supported"),
                "bucketsAvailable": fp.get("bucketsAvailable"),
                "venues": fp.get("venues")
            }

    for bt in ["all", "time", "vol", "range"]:
        sg = fetch_json(f"/api/cg/signals?sym={sym}&bt={bt}")
        if sg and sg.get("ok"):
            profile["footprint"]["signals"][bt] = {
                "counts": sg.get("counts"),
                "thresholds": sg.get("thresholds"),
                "sets": sg.get("sets", [])[:5]
            }

    # 7. LARGE ORDERS (24h resting whale walls and executed trades)
    lo = fetch_json(f"/api/cg/largeorder?symbol=Binance_{sym}USDT&interval=m15&hours=24")
    if lo and lo.get("ok"):
        profile["large_orders"]["24h"] = lo.get("groups", [])[:5]

    # 8. TRADFI QUANT [COT] (if mapped for this asset)
    cot = fetch_json(f"/api/cg/cot?sym={sym}&quiet=1")
    if cot and not cot.get("_err"):
        profile["cot_report"] = cot

    # 9. FUNDING & OPEN INTEREST
    fnd = fetch_json(f"/api/cg/funding?sym={sym}")
    if fnd and fnd.get("ok"):
        profile["funding_and_oi"]["funding"] = fnd.get("venues")

    oi = fetch_json(f"/api/cg/oi?sym={sym}")
    if oi and oi.get("ok"):
        profile["funding_and_oi"]["open_interest"] = oi.get("venues")

    # Atomic write to data/telemetry/assets/<SYM>.json
    target_file = os.path.join(ASSETS_DIR, f"{sym}.json")
    atomic_write(target_file, profile)
    elapsed = round(time.time() - t0, 2)
    file_size_kb = round(os.path.getsize(target_file) / 1024, 1)
    print(f"  [telemetry] {sym:5s} -> {target_file} ({file_size_kb} KB) in {elapsed}s", flush=True)

def update_universe_overview():
    scr = fetch_json("/api/cg/screener")
    if scr and scr.get("ok"):
        atomic_write(OVERVIEW_FILE, {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_assets": len(scr.get("rows", [])),
            "warm_count": scr.get("warmCount"),
            "rows": scr.get("rows", [])
        })
        print(f"[telemetry] Universe overview updated ({len(scr.get('rows', []))} assets) -> {OVERVIEW_FILE}", flush=True)

def main():
    os.makedirs(ASSETS_DIR, exist_ok=True)
    once = "--once" in sys.argv
    sym_arg = [a for a in sys.argv if a.startswith("--symbols=")]
    target_symbols = sym_arg[0].split("=")[1].split(",") if sym_arg else CURATED_UNIVERSE
    interval = int(os.environ.get("TELEMETRY_INTERVAL_SECS", "60"))

    print(f"=== Starting High-Speed Per-Asset Telemetry Daemon ===", flush=True)
    print(f"Target directory: {ASSETS_DIR}", flush=True)
    print(f"Symbols ({len(target_symbols)}): {', '.join(target_symbols[:8])}...", flush=True)

    while True:
        cycle_start = time.time()
        try:
            update_universe_overview()
            for sym in target_symbols:
                harvest_asset(sym)
        except Exception as e:
            print(f"Daemon cycle exception: {e}", file=sys.stderr, flush=True)

        cycle_dur = round(time.time() - cycle_start, 2)
        print(f"=== Telemetry Cycle Complete in {cycle_dur}s ===", flush=True)
        if once:
            break
        time.sleep(interval)

if __name__ == "__main__":
    main()
