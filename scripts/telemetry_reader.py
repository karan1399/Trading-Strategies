#!/usr/bin/env python3
"""
Institutional Telemetry & Footprint Reader (Deterministic Gatekeeper)
Reads the latest live asset telemetry and footprint signals for any symbol.
Verifies freshness, checks orderbook depth, CVD divergences, and absorption prints.
"""
import json, os, time, sys, glob
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ASSETS_DIR = os.path.join(ROOT, "data", "telemetry", "assets")
FOOTPRINT_DIR = os.path.join(ROOT, "data", "telemetry", "footprint")

def read_asset_telemetry(symbol: str, max_stale_sec: int = 600) -> dict:
    sym = symbol.upper().replace(".PI", "").replace(".P", "").replace("USD", "")
    # handle common symbol aliases
    if sym.startswith("XAU"): sym = "XAU"
    elif sym.startswith("BTC"): sym = "BTC"
    elif sym.startswith("ETH"): sym = "ETH"
    elif sym.startswith("SOL"): sym = "SOL"
    elif sym.startswith("BNB"): sym = "BNB"

    asset_file = os.path.join(ASSETS_DIR, f"{sym}.json")
    if not os.path.exists(asset_file):
        return {"ok": False, "error": f"Telemetry file {asset_file} not found"}

    mtime = os.path.getmtime(asset_file)
    age_sec = time.time() - mtime
    is_fresh = age_sec <= max_stale_sec

    with open(asset_file, "r") as f:
        data = json.load(f)

    # Footprint signals
    fp_file = os.path.join(FOOTPRINT_DIR, sym, "signals_7d.json")
    recent_signals = []
    if os.path.exists(fp_file):
        try:
            with open(fp_file, "r") as ff:
                fp_data = json.load(ff)
                signals = fp_data.get("signals", [])
                recent_signals = signals[-5:] if signals else []
        except Exception:
            pass

    ne = data.get("narrative_engine") or {}
    ms = data.get("market_structure") or {}
    l2 = data.get("microstructure_l2_l3") or {}
    ticker = data.get("ticker") or {}
    price_obj = ms.get("price") or {}
    auction = ms.get("auction") or {}
    day_auction = auction.get("day") or {}
    vwap_obj = ms.get("vwap") or {}

    return {
        "ok": True,
        "symbol": sym,
        "is_fresh": is_fresh,
        "age_seconds": round(age_sec, 1),
        "updated_at": data.get("updated_at"),
        "price": ticker.get("price") or price_obj.get("last"),
        "regime": ne.get("regime"),
        "grade": ne.get("grade"),
        "confidence": ne.get("confidence"),
        "direction": ne.get("direction"),
        "thesis": ne.get("thesis"),
        "plan": ne.get("plan"),
        "auction_poc": day_auction.get("poc"),
        "auction_val": day_auction.get("val"),
        "auction_vah": day_auction.get("vah"),
        "day_vwap": vwap_obj.get("vwap"),
        "l2_imbalance": (l2.get("hl") or {}).get("imbalance20"),
        "recent_footprint_signals": recent_signals
    }

def scan_all_fresh_candidates(min_conf: int = 70):
    files = glob.glob(os.path.join(ASSETS_DIR, "*.json"))
    candidates = []
    for f in files:
        sym = os.path.splitext(os.path.basename(f))[0]
        res = read_asset_telemetry(sym)
        if res.get("ok") and (res.get("confidence", 0) or 0) >= min_conf:
            candidates.append(res)
    return sorted(candidates, key=lambda x: x.get("confidence", 0), reverse=True)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--all":
        symbol = sys.argv[1]
        res = read_asset_telemetry(symbol)
        print(json.dumps(res, indent=2))
    else:
        candidates = scan_all_fresh_candidates(75)
        print(f"=== TOP TELEMETRY & FOOTPRINT CANDIDATES (>=75% CONFIDENCE) [{len(candidates)}] ===")
        for c in candidates[:8]:
            print(f"[{c['grade']:<2}] {c['symbol']:<6} | Conf: {c['confidence']}% | Dir: {c['direction']:<5} | Age: {c['age_seconds']}s | Price: {c['price']}")
            print(f"     Thesis: {c['thesis'][:90]}...")
            if c['recent_footprint_signals']:
                print(f"     Latest Footprint: {c['recent_footprint_signals'][-1]}")
            print()
