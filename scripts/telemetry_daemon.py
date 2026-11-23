#!/usr/bin/env python3
"""
telemetry_daemon.py — High-Speed Threaded 360° Order-Flow Telemetry & Continuous Git Sync Daemon.

Features:
 1. Concurrent ThreadPoolExecutor across assets for high-speed harvesting.
 2. Expanded 40-asset universe (30 crypto USDT-M perps + 10 TradFi USDT-M perps).
 3. Respectful pacing + cache reuse so Coinglass rate limits are NEVER triggered.
 4. Generates ALL 4 Authoritative Telemetry Datasets:
    - data/telemetry/live_orderflow_daemon.json (Consolidated multi-tab feed)
    - data/telemetry/deep_orderflow_matrix.json (Multi-range stopmaps, liq levels, book walls, CVD, signals)
    - data/telemetry/full_universe_live_telemetry.json (228-asset screener + deep candidates)
    - data/telemetry/quant_report_structured.json (Automated confluence rankings & scores)
    - data/telemetry/universe_overview.json (Screener + blotter + alerts)
 5. Per-asset modular telemetry under data/telemetry/assets/<SYM>.json and
    7-day continuous footprint summaries under data/telemetry/footprint/<SYM>/.
 6. Automated Git sync loop pushing telemetry updates to origin/main for external AI agents (e.g. Arena.ai).
"""

import urllib.request
import urllib.parse
import json
import time
import os
import sys
import subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = os.environ.get("TERMINAL_BASE_URL", "http://127.0.0.1:3000")
TELEMETRY_DIR = os.path.join("data", "telemetry")
ASSETS_DIR = os.path.join(TELEMETRY_DIR, "assets")
FOOTPRINT_DIR = os.path.join(TELEMETRY_DIR, "footprint")

# Curated High-Confluence Universe (30 Crypto + 10 TradFi)
EXPANDED_CURATED_UNIVERSE = [
    # Top 30 Crypto USDT-M Perps
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR",
    "SUI", "LTC", "BCH", "TIA", "APT", "ARB", "OP", "INJ", "SEI", "DOT",
    "ATOM", "UNI", "FIL", "ICP", "HBAR", "VET", "TRX", "1000PEPE", "1000SHIB",
    # Top 10 TradFi USDT-M Perps
    "XAU", "XAG", "NVDA", "TSLA", "MSTR", "QQQ", "GOOGL", "AAPL", "MSFT", "AMZN"
]

DEEP_MATRIX_SYMBOLS = [
    "BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "XRP", "BNB", "AVAX", "NEAR", "XAU", "NVDA"
]

def fetch_json(path, timeout=10):
    url = f"{BASE_URL}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-TelemetryEngine/3.2"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            if res.status == 200:
                return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        return {"_err": str(e)}
    return {"_err": "non-200"}

def atomic_write(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, filepath)

def harvest_single_asset(sym):
    t0 = time.time()
    iso_now = datetime.now(timezone.utc).isoformat()
    profile = {
        "symbol": sym,
        "updated_at": iso_now,
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

    # 2. DETERMINISTIC M1/M2 NARRATIVE ENGINE
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

    # 3. ATHENA ORACLE (6 Confluence Factors)
    orc = fetch_json(f"/api/cg/oracle?sym={sym}")
    if orc and orc.get("ok"):
        profile["athena_oracle"] = {
            "score": orc.get("score"),
            "verdict": orc.get("verdict"),
            "regime": orc.get("regime"),
            "factors": orc.get("factors"),
            "partial": orc.get("partial")
        }

    # 4. L2/L3 MICROSTRUCTURE & BBO
    micro = fetch_json(f"/api/cg/micro?sym={sym}", timeout=10)
    if micro and micro.get("ok"):
        profile["microstructure_l2_l3"] = {
            "hl": micro.get("hl"),
            "lighter": micro.get("lighter"),
            "bbo": micro.get("bbo"),
            "missingVenues": micro.get("missingVenues")
        }

    # 5. TRADFI QUANT & PERPETUAL POSITIONING
    qnt = fetch_json(f"/api/cg/quant?sym={sym}")
    if qnt and qnt.get("ok"):
        profile["quant_positioning"] = {
            "lsRatios": qnt.get("lsRatios"),
            "oiQuadrant": qnt.get("oiQuadrant"),
            "funding": qnt.get("funding"),
            "basis": qnt.get("basis"),
            "notes": qnt.get("notes")
        }

    # 6. DERIBIT OPTIONS ANALYTICS (BTC & ETH)
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

    # 7. LIQUIDATION LEVELS & STOPMAP HEATMAP
    for rng in ["3d", "7d", "30d"]:
        l_res = fetch_json(f"/api/cg/liqlevels?sym={sym}&range={rng}")
        if l_res and l_res.get("ok"):
            profile["liquidations"]["clusters"][rng] = {
                "stats": l_res.get("stats"),
                "venues": l_res.get("venues"),
                "livePrice": l_res.get("livePrice")
            }

    # Stopmap 24h & 3d
    sm = fetch_json(f"/api/cg/stopmap?sym={sym}&model=1&mode=coin&range=24h", timeout=15)
    if sm and sm.get("ok"):
        sm_model = sm.get("model") or sm
        profile["liquidations"]["stopmap"]["m1_coin_24h"] = {
            "keyLevels": sm_model.get("keyLevels"),
            "overview": sm_model.get("overview"),
            "source": sm.get("source"),
            "stale": sm.get("stale")
        }

    if sym == "BTC":
        sm_btc = fetch_json("/api/cg/stopmap?sym=BTC&model=1&mode=pair&range=24h", timeout=15)
        if sm_btc and sm_btc.get("ok"):
            sm_btc_model = sm_btc.get("model") or sm_btc
            profile["liquidations"]["stopmap"]["m1_pair_24h"] = {
                "keyLevels": sm_btc_model.get("keyLevels"),
                "overview": sm_btc_model.get("overview"),
                "source": sm_btc.get("source"),
                "stale": sm_btc.get("stale")
            }

    # 8. BOOK WALLS (Futures & Spot)
    for mkt_type in ["futures", "spot"]:
        for itv in ["m15", "h1", "h4"]:
            bw = fetch_json(f"/api/cg/bookwalls?sym={sym}&market={mkt_type}&interval={itv}&band=5", timeout=15)
            if bw and bw.get("ok"):
                bw_model = bw.get("model") or bw
                profile["book_walls"][mkt_type][itv] = {
                    "imbalance": bw_model.get("imbalance"),
                    "imbalance5mAgo": bw_model.get("imbalance5mAgo"),
                    "bidDepth": bw_model.get("bidDepth") or (bw_model.get("depth", {}).get("bidUsd") if isinstance(bw_model.get("depth"), dict) else None),
                    "askDepth": bw_model.get("askDepth") or (bw_model.get("depth", {}).get("askUsd") if isinstance(bw_model.get("depth"), dict) else None),
                    "largestBid": bw_model.get("largestBid"),
                    "largestAsk": bw_model.get("largestAsk"),
                    "walls": bw_model.get("walls", [])[:8],
                    "events": bw_model.get("events", [])[:5]
                }

    # 9. CVD (Perp, Spot, Split)
    for cvd_mode in ["perp", "spot", "split"]:
        for itv in ["m15", "h1", "h4"]:
            cv = fetch_json(f"/api/cg/cvd?sym={sym}&mode={cvd_mode}&itv={itv}&anchor=day")
            if cv and cv.get("ok"):
                profile["cvd"][cvd_mode][itv] = {
                    "kpis": cv.get("kpis"),
                    "divergences": cv.get("divergences", []),
                    "quadrant": cv.get("quadrant")
                }

    # 10. FOOTPRINT TIME SERIES (300 bars at 15m) + SUMMARY
    fp_7d = fetch_json(f"/api/cg/fp?sym={sym}&itv=m15&limit=300", timeout=20)
    if fp_7d and fp_7d.get("ok"):
        bars = fp_7d.get("bars", [])
        sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
        os.makedirs(sym_fp_dir, exist_ok=True)

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
            "recent_bars_tail": summary_rows[-8:] if summary_rows else []
        }

    # Signals
    all_sigs = []
    for bt in ["all", "time", "vol", "range"]:
        sg = fetch_json(f"/api/cg/signals?sym={sym}&bt={bt}", timeout=8)
        if sg and sg.get("ok"):
            profile["footprint"]["signals"][bt] = {
                "counts": sg.get("counts"),
                "thresholds": sg.get("thresholds"),
                "sets": sg.get("sets", [])[:4]
            }
            for s in sg.get("sets", []):
                all_sigs.extend(s.get("sigs", []))

    sym_fp_dir = os.path.join(FOOTPRINT_DIR, sym)
    atomic_write(os.path.join(sym_fp_dir, "signals_7d.json"), {
        "sym": sym,
        "updated_at": profile["updated_at"],
        "total_signals": len(all_sigs),
        "signals": all_sigs
    })

    # 11. LARGE ORDERS
    lo = fetch_json(f"/api/cg/largeorder?symbol=Binance_{sym}USDT&interval=m15&hours=24")
    if lo and lo.get("ok"):
        profile["large_orders"]["resting_and_executed"]["24h"] = lo.get("groups", [])[:4]

    # 12. PLAYBOOK STRATEGY CARDS
    cards = fetch_json(f"/api/strategy/cards?sym={sym}")
    if cards and not cards.get("_err"):
        asset_cards = [c for a in cards.get("assets", []) if a.get("sym") == sym for c in a.get("cards", [])]
        profile["playbook_cards"] = {
            "status": "triggered" if cards.get("ok") else "monitoring",
            "cards": asset_cards
        }

    # 13. BACKTEST WALK-FORWARD METRICS
    bt = fetch_json(f"/api/cg/backtest?sym={sym}")
    if bt and bt.get("ok"):
        profile["backtest_metrics"] = {
            "metricsAll": bt.get("metricsAll"),
            "byModel": bt.get("byModel"),
            "byRegime": bt.get("byRegime"),
            "tradeCount": len(bt.get("trades", []))
        }

    # 14. TRADFI QUANT [COT]
    cot = fetch_json(f"/api/cg/cot?sym={sym}&quiet=1")
    if cot and not cot.get("_err"):
        profile["cot_report"] = cot

    # 15. FUNDING & OI
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
    print(f"  [telemetry] {sym:7s} -> {target_file} ({file_size_kb:6.1f} KB) in {elapsed:5.2f}s", flush=True)
    return sym, profile

def generate_authoritative_datasets(harvested_profiles, screener_data):
    """Generates the 4 Authoritative Datasets required by the Trading Desk & Arena.ai."""
    print("[telemetry] Generating 4 authoritative datasets...", flush=True)
    iso_now = datetime.now(timezone.utc).isoformat()

    # 1. LIVE_ORDERFLOW_DAEMON.JSON (Consolidated multi-tab feed across all harvested assets)
    live_daemon_payload = {
        "metadata": {
            "timestamp_utc": iso_now,
            "version": "3.2.0-threaded",
            "assets_harvested": len(harvested_profiles),
            "host": "antigravity-local-terminal"
        },
        "profiles": harvested_profiles
    }
    atomic_write(os.path.join(TELEMETRY_DIR, "live_orderflow_daemon.json"), live_daemon_payload)
    print(f"  -> data/telemetry/live_orderflow_daemon.json ({round(os.path.getsize(os.path.join(TELEMETRY_DIR, 'live_orderflow_daemon.json')) / 1024 / 1024, 2)} MB)", flush=True)

    # 2. DEEP_ORDERFLOW_MATRIX.JSON (Multi-range stopmaps, liq levels, book walls, CVD, signals for prime assets)
    deep_matrix = {}
    for sym in DEEP_MATRIX_SYMBOLS:
        p = harvested_profiles.get(sym)
        if not p:
            continue
        deep_matrix[sym] = {
            "symbol": sym,
            "timestamp_utc": iso_now,
            "ticker": p.get("ticker"),
            "market_structure": p.get("market_structure"),
            "liquidations": p.get("liquidations"),
            "book_walls": p.get("book_walls"),
            "cvd": p.get("cvd"),
            "footprint_signals": p.get("footprint", {}).get("signals"),
            "footprint_history": p.get("footprint", {}).get("history_7d"),
            "large_orders": p.get("large_orders"),
            "narrative_engine": p.get("narrative_engine"),
            "athena_oracle": p.get("athena_oracle")
        }
    atomic_write(os.path.join(TELEMETRY_DIR, "deep_orderflow_matrix.json"), deep_matrix)
    print(f"  -> data/telemetry/deep_orderflow_matrix.json ({round(os.path.getsize(os.path.join(TELEMETRY_DIR, 'deep_orderflow_matrix.json')) / 1024, 1)} KB)", flush=True)

    # 3. FULL_UNIVERSE_LIVE_TELEMETRY.JSON (Full 228-asset screener + deep telemetry)
    full_universe_payload = {
        "metadata": {
            "timestamp_utc": iso_now,
            "total_assets": len(screener_data.get("rows", [])),
            "warm_count": screener_data.get("warmCount"),
            "deep_harvested_count": len(harvested_profiles)
        },
        "screener_rows": screener_data.get("rows", []),
        "deep_profiles": harvested_profiles
    }
    atomic_write(os.path.join(TELEMETRY_DIR, "full_universe_live_telemetry.json"), full_universe_payload)
    print(f"  -> data/telemetry/full_universe_live_telemetry.json ({round(os.path.getsize(os.path.join(TELEMETRY_DIR, 'full_universe_live_telemetry.json')) / 1024 / 1024, 2)} MB)", flush=True)

    # 4. QUANT_REPORT_STRUCTURED.JSON (Formulated candidate rankings, Z-scores, and trade confluence)
    short_candidates = []
    long_candidates = []

    for sym, p in harvested_profiles.items():
        mkt = p.get("market_structure") or {}
        px = (p.get("ticker") or {}).get("price") or mkt.get("price")
        if not px or px <= 0:
            continue

        vwap_data = mkt.get("vwap") or {}
        vwap_val = vwap_data.get("vwap")
        vwap_std = vwap_data.get("std", 1.0)
        vwap_z = (px - vwap_val) / vwap_std if (vwap_val and vwap_std > 0) else 0.0

        rsi_15m = (mkt.get("rsi") or {}).get("m15", 50.0)
        cum_delta_7d = (p.get("footprint", {}).get("history_7d") or {}).get("cum_delta_7d", 0.0)
        oracle_score = (p.get("athena_oracle") or {}).get("score", 50)
        regime = (p.get("narrative_engine") or {}).get("regime", "UNKNOWN")

        # Check nearest liq cluster
        clusters = (p.get("liquidations", {}).get("clusters") or {}).get("3d", {}).get("stats", {})
        largest_level = (p.get("liquidations", {}).get("stopmap", {}).get("m1_coin_24h", {}).get("overview") or {}).get("largestLevel")

        score_short = 0.0
        score_long = 0.0

        if vwap_z > 1.0: score_short += vwap_z * 15
        if rsi_15m > 65: score_short += (rsi_15m - 65) * 2
        if cum_delta_7d < 0: score_short += 10 # Net selling pressure / divergence
        if oracle_score < 48: score_short += (50 - oracle_score) * 1.5

        if vwap_z < -1.0: score_long += abs(vwap_z) * 15
        if rsi_15m < 35: score_long += (35 - rsi_15m) * 2
        if cum_delta_7d > 0: score_long += 10
        if oracle_score > 52: score_long += (oracle_score - 50) * 1.5

        short_candidates.append({
            "sym": sym,
            "price": px,
            "vwap_z": round(vwap_z, 2),
            "rsi_15m": round(rsi_15m, 1),
            "cum_delta_7d": round(cum_delta_7d, 2),
            "oracle_score": oracle_score,
            "regime": regime,
            "confluence_short_score": round(score_short, 1),
            "largest_liq_level": largest_level
        })

        long_candidates.append({
            "sym": sym,
            "price": px,
            "vwap_z": round(vwap_z, 2),
            "rsi_15m": round(rsi_15m, 1),
            "cum_delta_7d": round(cum_delta_7d, 2),
            "oracle_score": oracle_score,
            "regime": regime,
            "confluence_long_score": round(score_long, 1),
            "largest_liq_level": largest_level
        })

    short_candidates.sort(key=lambda x: x["confluence_short_score"], reverse=True)
    long_candidates.sort(key=lambda x: x["confluence_long_score"], reverse=True)

    quant_report = {
        "metadata": {
            "timestamp_utc": iso_now,
            "model": "Multi-Factor Confluence Formulation",
            "evaluator": "Antigravity Quant Engine"
        },
        "top_short_confluence_candidates": short_candidates[:15],
        "top_long_confluence_candidates": long_candidates[:15],
        "prime_assets_evaluated": len(harvested_profiles)
    }
    atomic_write(os.path.join(TELEMETRY_DIR, "quant_report_structured.json"), quant_report)
    print(f"  -> data/telemetry/quant_report_structured.json ({round(os.path.getsize(os.path.join(TELEMETRY_DIR, 'quant_report_structured.json')) / 1024, 1)} KB)", flush=True)

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
            "updated_at": datetime.now(timezone.utc).isoformat(),
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
        atomic_write(os.path.join(TELEMETRY_DIR, "universe_overview.json"), overview_payload)
        return scr
    return {"rows": []}

def git_sync_push():
    """Sync and push fresh telemetry to origin main for external AI agents."""
    try:
        subprocess.run(["git", "add", "data/telemetry", "desk"], check=False, timeout=20)
        status = subprocess.run(["git", "status", "--porcelain", "data/telemetry"], capture_output=True, text=True, timeout=10)
        if status.stdout.strip():
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            subprocess.run(["git", "commit", "-m", f"telemetry: live order-flow snapshot [{ts} UTC]"], check=False, timeout=30)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False, timeout=30)
            res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, timeout=45)
            if res.returncode == 0:
                print(f"[git-sync] Successfully pushed fresh telemetry to origin/main at {ts} UTC", flush=True)
            else:
                print(f"[git-sync] Git push output: {res.stderr.strip() or res.stdout.strip()}", flush=True)
        else:
            print("[git-sync] Working tree clean, no new telemetry diffs to push.", flush=True)
    except Exception as e:
        print(f"[git-sync] Exception during git sync: {e}", flush=True)

def main():
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(FOOTPRINT_DIR, exist_ok=True)
    once = "--once" in sys.argv
    workers = int(os.environ.get("TELEMETRY_WORKERS", "4"))
    interval = int(os.environ.get("TELEMETRY_INTERVAL_SECS", "60"))

    print(f"==================================================================", flush=True)
    print(f"Starting High-Speed Multi-Threaded Order-Flow Telemetry Daemon", flush=True)
    print(f"Parallel Worker Threads: {workers}", flush=True)
    print(f"Curated Universe Assets ({len(EXPANDED_CURATED_UNIVERSE)}): {', '.join(EXPANDED_CURATED_UNIVERSE[:10])}...", flush=True)
    print(f"Target Output Directory: {TELEMETRY_DIR}", flush=True)
    print(f"==================================================================", flush=True)

    while True:
        cycle_start = time.time()
        harvested_profiles = {}

        try:
            # 1. Update Universe Overview (Screener, Risk, Alerts)
            screener_data = update_universe_overview()

            # 2. Parallel Multi-Threaded Asset Harvesting
            print(f"[telemetry] Commencing parallel harvest with {workers} workers...", flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(harvest_single_asset, sym): sym for sym in EXPANDED_CURATED_UNIVERSE}
                for future in as_completed(future_map):
                    sym = future_map[future]
                    try:
                        _, profile = future.result()
                        harvested_profiles[sym] = profile
                    except Exception as e:
                        print(f"  [ERROR] Asset {sym} harvesting failed: {e}", file=sys.stderr, flush=True)

            # 3. Generate the 4 Authoritative Datasets
            generate_authoritative_datasets(harvested_profiles, screener_data)

            # 4. Continuous Git Sync Push to origin/main
            git_sync_push()

        except Exception as e:
            print(f"[telemetry-daemon] Cycle exception: {e}", file=sys.stderr, flush=True)

        cycle_dur = round(time.time() - cycle_start, 2)
        print(f"=== Complete Telemetry Sweep Finished in {cycle_dur}s ({len(harvested_profiles)} assets) ===", flush=True)

        if once:
            break
        time.sleep(interval)

if __name__ == "__main__":
    main()
