import json
import os

with open("data/telemetry/full_universe_live_telemetry.json", "r", encoding="utf-8") as f:
    data = json.load(f)

screener_rows = data.get("screener", {}).get("rows", [])
deep_scans = data.get("symbols", {})

print(f"Total screener assets: {len(screener_rows)}")
print(f"Total deep scan assets: {len(deep_scans)}")

# --- 1. FULL UNIVERSE SCREENER RANKING ---
# Sort into:
# A) Overextended Short Candidates (High vwapZ, Overbought RSI, Bearish 4H trend, Negative CVD/Absorption)
# B) Oversold Long Candidates (Low vwapZ, Oversold RSI, Bullish divergence)
# C) High Volatility & RVOL Momentum

short_candidates = []
long_candidates = []

for r in screener_rows:
    sym = r.get("sym")
    if not sym:
        continue
    px = r.get("price") or 0
    vwapZ = r.get("vwapZ") or 0
    rvol = r.get("rvol") or 0
    rsi15m = r.get("rsi15m") or 50
    rsi1h = r.get("rsi1h") or 50
    ema4h = r.get("ema4h", "neutral")
    ema1h = r.get("ema1h", "neutral")
    funding = (r.get("funding8hPct") or 0) * 100
    liq_magnet = r.get("liqMagnet") or {}
    confluence = r.get("confluence") or {}
    m1m2 = r.get("m1m2") or {}
    
    item = {
        "sym": sym,
        "name": r.get("name", sym),
        "kind": r.get("kind", "crypto-perp"),
        "price": px,
        "vwapZ": vwapZ,
        "rvol": rvol,
        "rsi15m": rsi15m,
        "rsi1h": rsi1h,
        "ema4h": ema4h,
        "ema1h": ema1h,
        "funding": funding,
        "liq_magnet": liq_magnet,
        "confluence_score": confluence.get("score", 0),
        "bias": confluence.get("bias", "NEUTRAL"),
        "m1m2_regime": m1m2.get("regime", "UNKNOWN"),
        "m1m2_model": m1m2.get("activeModel", "NONE"),
        "m1m2_grade": m1m2.get("grade", "C")
    }
    
    # Overextended upside (Short setups)
    if vwapZ >= 1.2:
        short_candidates.append(item)
    # Overextended downside (Long setups)
    elif vwapZ <= -1.2:
        long_candidates.append(item)

short_candidates.sort(key=lambda x: x["vwapZ"], reverse=True)
long_candidates.sort(key=lambda x: x["vwapZ"])

# --- 2. DEEP SCAN SYNTHESIS FOR THE 13 PRIME ASSETS ---
deep_results = {}

for sym, asset_obj in deep_scans.items():
    tabs = asset_obj.get("tabs", {})
    overview = tabs.get("overview", {})
    mkt = tabs.get("market_snapshot", {})
    liq = tabs.get("liqlevels", {})
    bw = tabs.get("bookwalls", {})
    cvd1 = tabs.get("cvd_1h", {})
    cvd4 = tabs.get("cvd_4h", {})
    sigs = tabs.get("signals", {})
    
    px = overview.get("price") or mkt.get("price") or 0
    
    # Liquidation levels stats
    l_stats = liq.get("stats") or {}
    l_totals = l_stats.get("totals") or {}
    s_liq = l_totals.get("shorts", 0)
    l_liq = l_totals.get("longs", 0)
    tot_liq = s_liq + l_liq
    l_pct = (l_liq / tot_liq * 100) if tot_liq > 0 else 0
    l_bands = l_stats.get("bands") or []
    
    # Book walls
    bw_imbal = bw.get("imbalance", 0) or 0
    bw_imbal5m = bw.get("imbalance5mAgo", 0) or 0
    bw_bids = bw.get("bidDepth", 0) or 0
    bw_asks = bw.get("askDepth", 0) or 0
    largest_bid = bw.get("largestBid") or {}
    largest_ask = bw.get("largestAsk") or {}
    walls = bw.get("walls") or []
    
    # CVD 1h
    cvd_kpis = cvd1.get("kpis") or {}
    quadrant = cvd_kpis.get("quadrant", "unknown")
    taker_buy_share = cvd_kpis.get("buyRatio", 50) or 50
    perp_cvd = cvd_kpis.get("perpCvd", 0) or 0
    spot_cvd = cvd_kpis.get("spotCvd", 0) or 0
    divs = cvd1.get("divergences") or []
    
    # Signals
    sig_counts = sigs.get("counts") or {}
    sig_sets = sigs.get("sets") or []
    
    # Auction VAL / POC / VAH
    auc_session = mkt.get("auction", {}).get("session8h", {}) if mkt.get("auction") else {}
    auc_day = mkt.get("auction", {}).get("utcDay", {}) if mkt.get("auction") else {}
    
    # EMAs
    emas = mkt.get("emas", {}) if mkt.get("emas") else {}
    
    deep_results[sym] = {
        "symbol": sym,
        "price": px,
        "liq_3d": {
            "total_usd": tot_liq,
            "long_usd": l_liq,
            "short_usd": s_liq,
            "long_pct": l_pct,
            "bands": l_bands
        },
        "book_walls": {
            "imbalance": bw_imbal,
            "imbalance_5m_ago": bw_imbal5m,
            "bid_depth_usd": bw_bids,
            "ask_depth_usd": bw_asks,
            "largest_bid": largest_bid,
            "largest_ask": largest_ask,
            "walls": walls[:5]
        },
        "cvd": {
            "quadrant": quadrant,
            "taker_buy_share": taker_buy_share,
            "perp_cvd": perp_cvd,
            "spot_cvd": spot_cvd,
            "divergences": divs
        },
        "signals": {
            "counts": sig_counts,
            "sets": sig_sets[:5]
        },
        "auction": {
            "session": auc_session,
            "day": auc_day
        },
        "emas": emas
    }

out_data = {
    "short_candidates_top": short_candidates[:15],
    "long_candidates_top": long_candidates[:15],
    "deep_results": deep_results
}

with open("data/telemetry/quant_report_structured.json", "w", encoding="utf-8") as f:
    json.dump(out_data, f, indent=2)

print("Saved structured quant report to data/telemetry/quant_report_structured.json")

# Print console summary of candidates
print("\n" + "="*80)
print("TOP SHORT CONFLUENCE CANDIDATES (OVERBOUGHT STRETCH + LIQUIDATION MAGNET)")
print("="*80)
for c in short_candidates[:8]:
    mag = c["liq_magnet"]
    mag_str = f"Mag: {mag.get('side')} @ {mag.get('price')} ({mag.get('distPct'):+.2f}%)" if mag else "No Mag"
    print(f"{c['sym']:8s} | Px: {c['price']:<9.4f} | vwapZ: {c['vwapZ']:>+5.2f}σ | RVOL: {c['rvol']:>4.2f}x | RSI15: {c['rsi15m']:>4.1f} | 4H:{c['ema4h']:<4s} | {mag_str}")

print("\n" + "="*80)
print("DEEP 13 PRIME ORDERFLOW SNAPSHOT")
print("="*80)
for sym, r in deep_results.items():
    l3 = r["liq_3d"]
    bw = r["book_walls"]
    cv = r["cvd"]
    sg = r["signals"]["counts"]
    print(f"[{sym:5s}] Px: {r['price']:<9.4f} | Liq 3D: Longs {l3['long_pct']:.1f}% (${l3['long_usd']/1e6:.1f}M) / Shorts ${l3['short_usd']/1e6:.1f}M | Imbal: {bw['imbalance']:>+.3f} | CVD: {cv['quadrant']} (Perp: {cv['perp_cvd']}) | Sigs: Bull {sg.get('bullish',0)} / Bear {sg.get('bearish',0)}")
