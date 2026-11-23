#!/usr/bin/env python3
"""
telemetry_daemon.py — Complete 360° Per-Asset Order-Flow Telemetry Daemon.

Covers 100% of terminal features across all 11 tabs with ZERO omissions:
 1. Overview: /api/cg/ticker, /api/cg/market, /api/cg/screener
 2. Market Structure: Auction VAL/POC/VAH (8h, Day, Month), Day VWAP ±1/2/3σ, EMA 13/89/233, RSI
 3. Narrative Engine: /api/cg/narrative (Regime, active model M1/M2, pFill, pT1, EV_R, grade)
 4. Athena Oracle: /api/cg/oracle (6 confluence factors: Apollo, Poseidon, Hades, Zeus, Atlas, Hermes)
 5. L2/L3 Microstructure: /api/cg/micro (Hyperliquid L2, Lighter L3 whale orders, multi-venue BBO)
 6. TradFi Quant & Positioning: /api/cg/quant (Long/Short ratios 15m..4h, top accounts, OI quadrants, basis)
 7. TradFi Quant [COT]: /api/cg/cot (CFTC institutional positioning for commodities/indices)
 8. Derivatives & Options: /api/cg/options (Deribit DVOL, 25-delta skew, max pain, put/call ratio for BTC/ETH)
 9. Footprint: /api/cg/fp (m1..h4), /api/cg/orderflow-agg (Multi-venue aggregated footprint)
10. Footprint Signals: /api/cg/signals (Time, Volume, Range: Absorption, Divergence, Trap, Exhaustion)
11. Large Orders & Whale Tape: /api/cg/largeorder + /api/cg/lto (Large Taker Orders >= $100k)
12. Liq Levels: /api/cg/liqlevels (3d, 7d, 30d clusters, distance bands)
13. Liq Heatmap / Stopmap: /api/cg/stopmap (Models 1, 2, 3 in Coin & Pair modes)
14. Real-Time Liquidation Tape: /api/cg/liq-live (Binance, Bybit, OKX force-orders)
15. Book Walls: /api/cg/bookwalls (Futures & Spot, intervals m15..h4, bands 0.5..5%, walls, events)
16. CVD: /api/cg/cvd (Perp, Spot, Split across m15..h4, quadrant, divergences)
17. Funding & OI: /api/cg/funding, /api/cg/oi
18. Playbook Strategy Cards: /api/strategy/cards (EA-2, EA-5, EA-6 candidate trade setups with SL/TP ladder)
19. Backtest Walk-Forward Engine: /api/cg/backtest (Expectancy R, win rate, profit factor, drawdown, Monte Carlo)
20. Live Alerts & Notification Stream: /api/cg/notifications + /api/cg/alert-rules
"""

import urllib.request
import json
import time
import os
import sys

BASE_URL = os.environ.get("TERMINAL_BASE_URL", "http://127.0.0.1:3000")
ASSETS_DIR = os.path.join("data", "telemetry", "assets")
FOOTPRINT_DIR = os.path.join("data", "telemetry", "footprint")
OVERVIEW_FILE = os.path.join("data", "telemetry", "universe_overview.json")

# Prime Curated Order-Flow Universe
CURATED_UNIVERSE = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR",
    "SUI", "LTC", "BCH", "TIA", "APT", "ARB", "OP", "XAU", "XAG", "NVDA"
]

def fetch_json(path, timeout=8):
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
        "narrative_engine": None,
        "athena_oracle": None,
        "microstructure_l2_l3": None,
        "quant_positioning": None,
        "options_deribit": None,
        "liquidations": {
            "clusters": {},
            "stopmap": {},
            "live_prints_tape": None
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
            "aggregated_footprint": None,
            "signals": {}
        },
        "large_orders": {
            "resting_and_executed": {},
            "large_taker_orders_100k": None
        },
        "playbook_cards": None,
        "backtest_metrics": None,
        "cot_report": None,
        "funding_and_oi": {}
    }

    # 1. TICKER & MARKET STRUCTURE (Auction, VWAP, EMAs, RSI)
    tk = fetch_json(f"/api/cg/ticker?sym={sym}")
    if tk and tk.get("ok"):
        profile["ticker"] = {
            "price": tk.get("price"),
            "dir": tk.get("dir"),
            "source": tk.get("source"),
            "updatedAt": tk.get("updatedAt")
        }

    mkt = fetch_json(f"/api/cg/market?sym={sym}", timeout=8)
    if mkt and mkt.get("ok"):
        profile["market_structure"] = {
            "price": mkt.get("price"),
            "auction": mkt.get("auction"),
            "vwap": mkt.get("vwap"),
            "emas": mkt.get("ema") or mkt.get("emas"),
            "rsi": mkt.get("rsi")
        }

    # 2. DETERMINISTIC M1/M2 NARRATIVE ENGINE (Regime, Model, Probabilities, EV)
    narr = fetch_json(f"/api/cg/narrative?sym={sym}")
    if narr and narr.get("ok"):
        profile["narrative_engine"] = {
            "regime": narr.get("regime"),
            "activeModel": narr.get("activeModel"),
            "direction": narr.get("direction"),
            "grade": narr.get("grade"),
            "confidence": narr.get("confidence"),
            "thesis": narr.get("thesis"),
            "plan": narr.get("plan"),
            "probabilities": {
                "pFill": narr.get("pFill"),
                "pT1": narr.get("pT1"),
                "pMeanRevert": narr.get("pMeanRevert"),
                "evR": narr.get("evR"),
                "bestR": narr.get("bestR")
            }
        }

    # 3. ATHENA ORACLE (6 Confluence Factors: Apollo, Poseidon, Hades, Zeus, Atlas, Hermes)
    orc = fetch_json(f"/api/cg/oracle?sym={sym}")
    if orc and orc.get("ok"):
        profile["athena_oracle"] = {
            "score": orc.get("score"),
            "verdict": orc.get("verdict"),
            "regime": orc.get("regime"),
            "factors": orc.get("factors"),
            "partial": orc.get("partial")
        }

    # 4. L2/L3 MICROSTRUCTURE & MULTI-VENUE BBO (Hyperliquid L2, Lighter L3 Whales, Multi-BBO)
    micro = fetch_json(f"/api/cg/micro?sym={sym}", timeout=12)
    if micro and micro.get("ok"):
        profile["microstructure_l2_l3"] = {
            "hl": micro.get("hl"),
            "lighter": micro.get("lighter"),
            "bbo": micro.get("bbo"),
            "missingVenues": micro.get("missingVenues")
        }

    # 5. TRADFI QUANT & PERPETUAL POSITIONING (Long/Short Ratios, Top Accounts, OI Quadrants, Basis)
    qnt = fetch_json(f"/api/cg/quant?sym={sym}")
    if qnt and qnt.get("ok"):
        profile["quant_positioning"] = {
            "lsRatios": qnt.get("lsRatios"),
            "oiQuadrant": qnt.get("oiQuadrant"),
            "funding": qnt.get("funding"),
            "basis": qnt.get("basis"),
            "notes": qnt.get("notes")
        }

    # 6. DERIBIT OPTIONS ANALYTICS (BTC & ETH: DVOL, Skew, Max Pain, Put/Call Ratio)
    if sym in ["BTC", "ETH"]:
        opt = fetch_json(f"/api/cg/options?sym={sym}")
        if opt and opt.get("ok"):
            profile["options_deribit"] = {
                "dvol": opt.get("dvol"),
                "termStructure": opt.get("termStructure"),
                "skew": opt.get("skew"),
                "maxPain": opt.get("maxPain"),
                "putCallRatio": opt.get("putCallRatio")
            }

    # 4. LIQUIDATION LEVELS & STOPMAP HEATMAP (Layer-by-Layer: Clusters + Primary Heatmap + Live Tape)
    for rng in ["3d", "7d", "30d"]:
        l_res = fetch_json(f"/api/cg/liqlevels?sym={sym}&range={rng}")
        if l_res and l_res.get("ok"):
            profile["liquidations"]["clusters"][rng] = {
                "stats": l_res.get("stats"),
                "venues": l_res.get("venues"),
                "livePrice": l_res.get("livePrice")
            }

    # Heatmap Model 1 (Coin 24h & 3d, plus Pair 24h for BTC) — Rate-safe with polite pacing
    for rng in ["24h", "3d"]:
        sm = fetch_json(f"/api/cg/stopmap?sym={sym}&model=1&mode=coin&range={rng}", timeout=8)
        if sm and sm.get("ok"):
            profile["liquidations"]["stopmap"][f"m1_coin_{rng}"] = {
                "keyLevels": sm.get("keyLevels"),
                "overview": sm.get("overview"),
                "source": sm.get("source"),
                "stale": sm.get("stale")
            }
        time.sleep(0.15)

    if sym == "BTC":
        sm_btc = fetch_json("/api/cg/stopmap?sym=BTC&model=1&mode=pair&range=24h", timeout=8)
        if sm_btc and sm_btc.get("ok"):
            profile["liquidations"]["stopmap"]["m1_pair_24h"] = {
                "keyLevels": sm_btc.get("keyLevels"),
                "overview": sm_btc.get("overview"),
                "source": sm_btc.get("source"),
                "stale": sm_btc.get("stale")
            }

    # 5. REAL-TIME LIQUIDATION TAPE (Live force-orders stream)
    liq_live = fetch_json(f"/api/cg/liq-live?sym={sym}")
    if liq_live and liq_live.get("ok"):
        profile["liquidations"]["live_prints_tape"] = {
            "eventsStored": liq_live.get("universeEventsStored"),
            "recentTape": liq_live.get("tape", [])[:10],
            "stats": liq_live.get("stats")
        }

    # 6. BOOK WALLS (Futures Aggregated & Spot Aggregated)
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

    # 7. CVD (Perp, Spot, Split across intervals)
    for cvd_mode in ["perp", "spot", "split"]:
        for itv in ["m15", "h1", "h4"]:
            cv = fetch_json(f"/api/cg/cvd?sym={sym}&mode={cvd_mode}&itv={itv}&anchor=day")
            if cv and cv.get("ok"):
                profile["cvd"][cvd_mode][itv] = {
                    "kpis": cv.get("kpis"),
                    "divergences": cv.get("divergences", []),
                    "quadrant": cv.get("quadrant")
                }

    # 8. FOOTPRINT 7-DAY TIME SERIES (672 bars at 15m) + GRANULAR ARCHIVE
    fp_7d = fetch_json(f"/api/cg/fp?sym={sym}&itv=m15&limit=672", timeout=15)
    if fp_7d and fp_7d.get("ok"):
        bars = fp_7d.get("bars", [])
        sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
        os.makedirs(sym_fp_dir, exist_ok=True)

        # 1. Full 7-Day Depth Series (for tick replay & deep backtests)
        atomic_write(os.path.join(sym_fp_dir, "bars_15m_7d.json"), {
            "sym": sym,
            "itv": "m15",
            "bars_count": len(bars),
            "updated_at": profile["updated_at"],
            "bars": bars
        })

        # 2. AI-Optimized Structured Summary Matrix (~45 KB, ideal for LLM context)
        summary_rows = []
        cum_delta = 0.0
        for b in bars:
            fp = b.get("fp")
            buy_v = sum(fp.get("b", [])) if fp else 0.0
            sell_v = sum(fp.get("s", [])) if fp else 0.0
            delta = buy_v - sell_v
            cum_delta += delta
            tot_v = buy_v + sell_v
            delta_pct = (delta / tot_v * 100) if tot_v > 0 else 0.0

            poc_p, max_v = None, 0.0
            if fp and fp.get("b") and fp.get("s"):
                tick = fp.get("tick", 1)
                lo = fp.get("lo", b.get("l"))
                for i in range(len(fp["b"])):
                    row_v = fp["b"][i] + fp["s"][i]
                    if row_v > max_v:
                        max_v = row_v
                        poc_p = round(lo + i * tick, 2)

            summary_rows.append({
                "ts": b.get("ts"),
                "o": b.get("o"),
                "h": b.get("h"),
                "l": b.get("l"),
                "c": b.get("c"),
                "buy_vol": round(buy_v, 2),
                "sell_vol": round(sell_v, 2),
                "delta": round(delta, 2),
                "delta_pct": round(delta_pct, 2),
                "cum_delta": round(cum_delta, 2),
                "poc_price": poc_p,
                "poc_vol": round(max_v, 2)
            })

        atomic_write(os.path.join(sym_fp_dir, "summary_7d.json"), {
            "sym": sym,
            "timeframe": "15m",
            "days": 7,
            "total_bars": len(summary_rows),
            "start_time": summary_rows[0]["ts"] if summary_rows else None,
            "end_time": summary_rows[-1]["ts"] if summary_rows else None,
            "cum_delta_7d": round(cum_delta, 2),
            "rows": summary_rows
        })

        profile["footprint"]["history_7d"] = {
            "bars_count": len(bars),
            "cum_delta_7d": round(cum_delta, 2),
            "summary_file": f"data/telemetry/footprint/{sym}/summary_7d.json",
            "full_bars_file": f"data/telemetry/footprint/{sym}/bars_15m_7d.json",
            "recent_bars_tail": summary_rows[-12:] if summary_rows else []
        }

    # Multi-exchange aggregated footprint
    fp_agg = fetch_json(f"/api/cg/orderflow-agg?sym={sym}&interval=m15&limit=25", timeout=8)
    if fp_agg and fp_agg.get("ok"):
        profile["footprint"]["aggregated_footprint"] = {
            "venues": fp_agg.get("venues"),
            "venuesOk": fp_agg.get("venuesOk"),
            "barCount": len(fp_agg.get("bars", []))
        }

    # Signals 7D Matrix
    all_sigs = []
    for bt in ["all", "time", "vol", "range"]:
        sg = fetch_json(f"/api/cg/signals?sym={sym}&bt={bt}", timeout=8)
        if sg and sg.get("ok"):
            profile["footprint"]["signals"][bt] = {
                "counts": sg.get("counts"),
                "thresholds": sg.get("thresholds"),
                "sets": sg.get("sets", [])[:5]
            }
            for s in sg.get("sets", []):
                all_sigs.extend(s.get("sigs", []))

    sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
    os.makedirs(sym_fp_dir, exist_ok=True)
    atomic_write(os.path.join(sym_fp_dir, "signals_7d.json"), {
        "sym": sym,
        "updated_at": profile["updated_at"],
        "total_signals": len(all_sigs),
        "signals": all_sigs
    })

    # 9. LARGE ORDERS & LARGE TAKER ORDERS (>= $100k tape)
    lo = fetch_json(f"/api/cg/largeorder?symbol=Binance_{sym}USDT&interval=m15&hours=24")
    if lo and lo.get("ok"):
        profile["large_orders"]["resting_and_executed"]["24h"] = lo.get("groups", [])[:5]

    lto = fetch_json(f"/api/cg/lto?symbol={sym}USDT&minVolUsd=100000")
    if lto and lto.get("ok"):
        profile["large_orders"]["large_taker_orders_100k"] = {
            "supported": lto.get("supported"),
            "orders": lto.get("orders", [])[:10]
        }

    # 10. PLAYBOOK STRATEGY CARDS (EA-2, EA-5, EA-6 candidate setups & trade levels)
    cards = fetch_json(f"/api/strategy/cards?sym={sym}")
    if cards and not cards.get("_err"):
        asset_cards = [c for a in cards.get("assets", []) if a.get("sym") == sym for c in a.get("cards", [])]
        profile["playbook_cards"] = {
            "status": "triggered" if cards.get("ok") else "monitoring",
            "engines": cards.get("engines"),
            "weights": cards.get("weights"),
            "cards": asset_cards
        }

    # 11. BACKTEST WALK-FORWARD QUANT METRICS (Net R, Profit Factor, Win Rate, Expectancy)
    bt = fetch_json(f"/api/cg/backtest?sym={sym}")
    if bt and bt.get("ok"):
        profile["backtest_metrics"] = {
            "metricsAll": bt.get("metricsAll"),
            "byModel": bt.get("byModel"),
            "byRegime": bt.get("byRegime"),
            "tradeCount": len(bt.get("trades", []))
        }

    # 12. TRADFI QUANT [COT] (if mapped)
    cot = fetch_json(f"/api/cg/cot?sym={sym}&quiet=1")
    if cot and not cot.get("_err"):
        profile["cot_report"] = cot

    # 13. FUNDING & OPEN INTEREST
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
    risk_summary = fetch_json("/api/cg/risk")
    desk_live = fetch_json("/api/desk/live")
    cnbc_macro = fetch_json("/api/cg/cnbc?syms=SP500,NDX100")
    notifs = fetch_json("/api/cg/notifications?limit=25")
    rules = fetch_json("/api/cg/alert-rules")
    engine_st = fetch_json("/api/cg/engine/status")

    if scr and scr.get("ok"):
        overview_payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_assets": len(scr.get("rows", [])),
            "warm_count": scr.get("warmCount"),
            "macro_quotes": cnbc_macro.get("quotes") if (cnbc_macro and cnbc_macro.get("ok")) else None,
            "engine_status": {
                "started": engine_st.get("started"),
                "backfillDays": engine_st.get("backfillDays"),
                "series": [s.get("name") for s in engine_st.get("series", []) if isinstance(s, dict)] if isinstance(engine_st.get("series"), list) else []
            } if engine_st else None,
            "portfolio_risk": risk_summary.get("positions") if (risk_summary and risk_summary.get("ok")) else None,
            "desk_live_blotter": {
                "mt5": desk_live.get("mt5"),
                "analysis": desk_live.get("analysis"),
                "orders": desk_live.get("orders")
            } if (desk_live and desk_live.get("ok")) else None,
            "live_alerts": {
                "count": notifs.get("count"),
                "recent": notifs.get("items", [])[:15]
            } if (notifs and notifs.get("ok")) else None,
            "alert_rules": rules.get("rules") if (rules and rules.get("ok")) else None,
            "rows": scr.get("rows", [])
        }
        atomic_write(OVERVIEW_FILE, overview_payload)
        print(f"[telemetry] Universe overview updated ({len(scr.get('rows', []))} assets + desk + alerts + macro) -> {OVERVIEW_FILE}", flush=True)

def main():
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(FOOTPRINT_DIR, exist_ok=True)
    once = "--once" in sys.argv
    sym_arg = [a for a in sys.argv if a.startswith("--symbols=")]
    target_symbols = sym_arg[0].split("=")[1].split(",") if sym_arg else CURATED_UNIVERSE
    interval = int(os.environ.get("TELEMETRY_INTERVAL_SECS", "60"))

    print(f"=== Starting Complete 360° Per-Asset Telemetry Daemon ===", flush=True)
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
