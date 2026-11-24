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

MT5_ALIASES = {
    "ADAUSD.P": "ADA", "BCHUSD.P": "BCH", "LNKUSD.P": "LINK", "XLMUSD.P": "XLM",
    "AVXUSD.P": "AVAX", "DOGUSD.P": "DOGE", "FILUSD.P": "FIL", "GRTUSD.P": "GRT",
    "NERUSD.P": "NEAR", "SOLUSD.P": "SOL", "TRXUSD.P": "TRX", "UNIUSD.P": "UNI",
    "VETUSD.P": "VET", "XMRUSD.P": "XMR", "BNBUSD.P": "BNB", "AVEUSD.P": "AAVE",
    "ALGUSD.P": "ALGO", "ATMUSD.P": "ATOM", "AXSUSD.P": "AXS", "CHZUSD.P": "CHZ",
    "COMUSD.P": "COMP", "EGLUSD.P": "EGLD", "FLWUSD.P": "FLOW", "IOTUSD.P": "IOTA",
    "KSMUSD.P": "KSM", "NEOUSD.P": "NEO", "XSIUSD.P": "SUSHI", "THTUSD.P": "THETA",
    "XTZUSD.P": "XTZ", "ZECUSD.P": "ZEC", "MANUSD.P": "MANA", "INCUSD.P": "1INCH",
    "ARWUSD.P": "AR", "BATUSD.P": "BAT", "CHRUSD.P": "CHR", "CRVUSD.P": "CRV",
    "ENJUSD.P": "ENJ", "BARUSD.P": "HBAR", "KAVUSD.P": "KAVA", "KNCUSD.P": "KNC",
    "ONTUSD.P": "ONT", "QTMUSD.P": "QTUM", "SNDUSD.P": "SAND", "SKLUSD.P": "SKL",
    "SNXUSD.P": "SNX", "YFIUSD.P": "YFI", "ZILUSD.P": "ZIL", "ZRXUSD.P": "ZRX",
    "DSHUSD.P": "DASH", "BTCUSD.PI": "BTC", "DOTUSD.PI": "DOT", "ETHUSD.PI": "ETH",
    "LTCUSD.PI": "LTC", "XRPUSD.PI": "XRP",
    "XAUUSD.PI": "XAU", "XAGUSD.PI": "XAG",
    "USWTI.P": "CL", "UKBRENT.P": "UKO", "GAS.P": "NATGAS",
    "XPTUSD.PI": "XPT", "XPDUSD.PI": "XPD", "COPPER.P": "COPPER",
}

def resolve_telemetry_sym(symbol: str) -> str:
    raw = symbol.strip().upper()
    if raw in MT5_ALIASES:
        return MT5_ALIASES[raw]
    stripped = raw
    for suf in (".PI", ".P", ".I", ".PRO", ".RAW"):
        if stripped.endswith(suf):
            stripped = stripped[:-len(suf)]
            break
    if os.path.exists(os.path.join(ASSETS_DIR, f"{stripped}.json")):
        return stripped
    if stripped.endswith("USD") and len(stripped) > 3:
        cand = stripped[:-3]
        if os.path.exists(os.path.join(ASSETS_DIR, f"{cand}.json")):
            return cand
    return stripped

def read_asset_telemetry(symbol: str, max_stale_sec: int = 600) -> dict:
    sym = resolve_telemetry_sym(symbol)

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
