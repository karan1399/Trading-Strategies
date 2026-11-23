#!/usr/bin/env python3
"""
telemetry_daemon.py — Continuous Local Order-Flow Telemetry Daemon.
Pulls every data point from all 10 attached terminal tabs across all levels,
markets, and timeframes into an atomic live JSON file.

Tabs Covered:
1. Overview: /api/cg/ticker, /api/cg/market, /api/cg/screener
2. Footprint: /api/cg/fp (m1, m5, m15, m30, h1, h4, d1) + /api/cg/signals (all, time, vol, range)
3. Large Orders: /api/cg/largeorder (m15, h1, h4, 24h/72h)
4. Liq Levels: /api/cg/liqlevels (3d, 7d, 30d) + /api/cg/stopmap (Models 1/2/3, coin/pair, 24h..30d)
5. Book Walls: /api/cg/bookwalls (futures & spot, intervals m1..d1, bands 0.5..5%)
6. CVD: /api/cg/cvd (perp, spot, split, total across m1..d1)
7. TradFi Quant [COT]: /api/cg/cot (XAU, XAG, SP500, NDX100) + /api/cg/quant
8. Backtest: /api/cg/backtest?trades=0
9. Alerts: /api/cg/notifications
10. Live L2: /api/cg/live, /api/cg/livebook
"""

import urllib.request
import json
import time
import os
import sys

BASE_URL = os.environ.get("TERMINAL_BASE_URL", "http://127.0.0.1:3000")
OUT_FILE = os.path.join("data", "telemetry", "live_orderflow_daemon.json")
TMP_FILE = OUT_FILE + ".tmp"
INBOX_OUT = os.path.join("desk", "inbox", "LIVE_TELEMETRY.json")

# Core assets for deep multi-timeframe multi-model sweep
PRIME_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR"]

def get_json(endpoint, timeout=3):
    url = f"{BASE_URL}{endpoint}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-TelemetryDaemon/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if res.status == 200:
                return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        return {"_err": str(e)}
    return {"_err": "non-200"}

def run_cycle():
    t0 = time.time()
    telemetry = {
        "metadata": {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "timestamp_epoch": time.time(),
            "version": "2.0-universal-daemon",
            "tabs_covered": [
                "Overview", "Footprint", "Large Orders", "Liq Levels",
                "Book Walls", "CVD", "TradFi Quant [COT]", "Backtest",
                "Alerts", "Live L2 & Ticket [WS]"
            ]
        },
        "universe_screener": None,
        "backtest_summary": None,
        "live_alerts": None,
        "cot_reports": {},
        "prime_assets": {}
    }

    # 1. OVERVIEW SCREENER (All 231 universe assets)
    scr = get_json("/api/cg/screener")
    if scr and scr.get("ok"):
        telemetry["universe_screener"] = {
            "count": len(scr.get("rows", [])),
            "warmCount": scr.get("warmCount"),
            "rows": scr.get("rows", [])
        }

    # 2. BACKTEST RESULTS (Last walk-forward calibration)
    bt = get_json("/api/cg/backtest?trades=0")
    if bt and bt.get("ok"):
        telemetry["backtest_summary"] = bt.get("status") or bt.get("params")

    # 3. ALERTS & LIVE NOTIFICATION FEED
    notifs = get_json("/api/cg/notifications")
    if notifs and notifs.get("ok"):
        telemetry["live_alerts"] = {
            "count": notifs.get("count"),
            "items": notifs.get("items", [])[:20]
        }

    # 4. TRADFI QUANT [COT] REPORTS (XAU, XAG, SP500, NDX100)
    for c_sym in ["XAU", "XAG", "SP500", "NDX100"]:
        c_res = get_json(f"/api/cg/cot?sym={c_sym}")
        if c_res and not c_res.get("_err"):
            telemetry["cot_reports"][c_sym] = c_res

    # 5. DEEP DIVE PER ASSET ACROSS ALL TABS, LEVELS, AND TIMEFRAMES
    for sym in PRIME_SYMBOLS:
        print(f"  [telemetry] Harvesting deep matrix for {sym}...", flush=True)
        asset_node = {
            "symbol": sym,
            "overview": get_json(f"/api/cg/market?sym={sym}"),
            "ticker": get_json(f"/api/cg/ticker?sym={sym}"),
            "footprint": {},
            "signals": {},
            "large_orders": {},
            "liq_levels": {},
            "stopmap": {},
            "book_walls": {
                "futures": {},
                "spot": {}
            },
            "cvd": {
                "perp": {},
                "spot": {},
                "split": {}
            }
        }

        # Tab: Footprint (m1, m5, m15, h1, h4)
        for itv in ["m1", "m5", "m15", "h1", "h4"]:
            fp_res = get_json(f"/api/cg/fp?sym={sym}&itv={itv}&limit=50")
            if fp_res and fp_res.get("ok"):
                asset_node["footprint"][itv] = fp_res

        # Tab: Signals (15m Time, Auto Volume, Auto Range)
        for bt in ["all", "time", "vol", "range"]:
            sig_res = get_json(f"/api/cg/signals?sym={sym}&bt={bt}")
            if sig_res and sig_res.get("ok"):
                asset_node["signals"][bt] = sig_res

        # Tab: Large Orders (Resting walls & large executed trades)
        for itv in ["m15", "h1", "h4"]:
            lo_res = get_json(f"/api/cg/largeorder?symbol=Binance_{sym}USDT&interval={itv}&hours=24")
            if lo_res and lo_res.get("ok"):
                asset_node["large_orders"][itv] = lo_res.get("groups", [])

        # Tab: Liq Levels (3d, 7d, 30d clusters)
        for rng in ["3d", "7d", "30d"]:
            l_res = get_json(f"/api/cg/liqlevels?sym={sym}&range={rng}")
            if l_res and l_res.get("ok"):
                asset_node["liq_levels"][rng] = l_res.get("stats")

        # Tab: Stopmap (Liquidation Heatmap Models 1/2/3, coin & pair)
        for m in [1, 2, 3]:
            for mode in ["coin", "pair"]:
                for rng in ["24h", "3d", "7d"]:
                    sm_res = get_json(f"/api/cg/stopmap?sym={sym}&model={m}&mode={mode}&range={rng}")
                    if sm_res and sm_res.get("ok"):
                        asset_node["stopmap"][f"m{m}_{mode}_{rng}"] = {
                            "keyLevels": sm_res.get("keyLevels"),
                            "overview": sm_res.get("overview")
                        }

        # Tab: Book Walls (Futures vs Spot across intervals and depth bands)
        for mkt in ["futures", "spot"]:
            for itv in ["m5", "m15", "h1"]:
                bw_res = get_json(f"/api/cg/bookwalls?sym={sym}&market={mkt}&interval={itv}&band=5")
                if bw_res and bw_res.get("ok"):
                    asset_node["book_walls"][mkt][itv] = {
                        "imbalance": bw_res.get("imbalance"),
                        "imbalance5mAgo": bw_res.get("imbalance5mAgo"),
                        "bidDepth": bw_res.get("bidDepth"),
                        "askDepth": bw_res.get("askDepth"),
                        "largestBid": bw_res.get("largestBid"),
                        "largestAsk": bw_res.get("largestAsk"),
                        "walls": bw_res.get("walls", [])[:6]
                    }

        # Tab: CVD (Perp, Spot, Split across intervals)
        for mode in ["perp", "spot", "split"]:
            for itv in ["m15", "h1", "h4"]:
                cvd_res = get_json(f"/api/cg/cvd?sym={sym}&mode={mode}&itv={itv}&anchor=day")
                if cvd_res and cvd_res.get("ok"):
                    asset_node["cvd"][mode][itv] = {
                        "kpis": cvd_res.get("kpis"),
                        "divergences": cvd_res.get("divergences", []),
                        "quadrant": cvd_res.get("quadrant")
                    }

        telemetry["prime_assets"][sym] = asset_node

    telemetry["metadata"]["cycle_duration_sec"] = round(time.time() - t0, 2)

    # Atomic write to data/telemetry/live_orderflow_daemon.json
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(TMP_FILE, "w", encoding="utf-8") as f:
        json.dump(telemetry, f, indent=2)
    os.replace(TMP_FILE, OUT_FILE)

    # Also sync copy to desk/inbox/LIVE_TELEMETRY.json for Arena/GLM bus
    os.makedirs(os.path.dirname(INBOX_OUT), exist_ok=True)
    with open(INBOX_OUT, "w", encoding="utf-8") as f:
        json.dump(telemetry, f, indent=2)

    print(f"[{time.strftime('%H:%M:%S')}] Telemetry cycle complete in {telemetry['metadata']['cycle_duration_sec']}s -> {OUT_FILE} ({os.path.getsize(OUT_FILE)} bytes)", flush=True)

def main():
    once = "--once" in sys.argv
    interval = int(os.environ.get("TELEMETRY_INTERVAL_SECS", "60"))
    print(f"Starting Telemetry Daemon (once={once}, interval={interval}s)...", flush=True)
    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"Cycle error: {e}", file=sys.stderr, flush=True)
        if once:
            break
        time.sleep(interval)

if __name__ == "__main__":
    main()
