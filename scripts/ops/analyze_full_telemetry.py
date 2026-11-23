import json
import os

with open("data/telemetry/full_universe_live_telemetry.json", "r", encoding="utf-8") as f:
    data = json.load(f)

screener_rows = data.get("screener", {}).get("rows", [])
deep_scans = data.get("symbols", {})

print(f"Loaded {len(screener_rows)} screener rows and {len(deep_scans)} deep scan assets.")

# 1. SCREENER CROSS-SECTIONAL RANKING
ranked = []
for row in screener_rows:
    sym = row.get("symbol")
    px = row.get("price") or row.get("lastPrice") or 0
    vwapZ = row.get("vwapZ") or 0
    rvol = row.get("rvol") or 0
    rsi15m = row.get("rsi15m") or 50
    rsi1h = row.get("rsi1h") or 50
    funding = row.get("funding") or 0
    cvd1h = row.get("cvd1h") or 0
    cvd4h = row.get("cvd4h") or 0
    ranked.append({
        "symbol": sym,
        "price": px,
        "vwapZ": vwapZ,
        "rvol": rvol,
        "rsi15m": rsi15m,
        "rsi1h": rsi1h,
        "funding": funding,
        "cvd1h": cvd1h,
        "cvd4h": cvd4h,
        "stretch": abs(vwapZ)
    })

# Sort by vwapZ stretch (mean reversion potential)
ranked.sort(key=lambda x: x["stretch"], reverse=True)

print("\n--- TOP 12 VWAP STRETCHED ASSETS (POTENTIAL EXHAUSTION / REVERSION) ---")
for r in ranked[:12]:
    px_str = f"{r['price']:<10.4f}" if isinstance(r['price'], (int, float)) else f"{str(r['price']):<10}"
    vwap_str = f"{r['vwapZ']:>+6.2f}σ" if isinstance(r['vwapZ'], (int, float)) else "   N/A"
    rvol_str = f"{r['rvol']:>5.2f}x" if isinstance(r['rvol'], (int, float)) else " N/A "
    rsi15_str = f"{r['rsi15m']:>5.1f}" if isinstance(r['rsi15m'], (int, float)) else " N/A "
    rsi1_str = f"{r['rsi1h']:>5.1f}" if isinstance(r['rsi1h'], (int, float)) else " N/A "
    fund_str = f"{r['funding']*100:>+7.4f}%" if isinstance(r['funding'], (int, float)) else "  N/A  "
    print(f"{str(r['symbol']):10s} | Px: {px_str} | vwapZ: {vwap_str} | RVOL: {rvol_str} | RSI15m: {rsi15_str} | RSI1h: {rsi1_str} | Funding: {fund_str}")

# 2. DEEP SCAN ORDERFLOW SYNTHESIS
print("\n" + "="*80)
print("DEEP SCAN ORDERFLOW SYNTHESIS (ALL 13 PRIME ASSETS)")
print("="*80)

deep_summary = {}

for sym, d in deep_scans.items():
    ticker = d.get("ticker", {})
    px = ticker.get("price") or d.get("market", {}).get("price") or 0
    
    # Liq levels
    liq = d.get("liqlevels", {})
    stats = liq.get("stats") or {}
    totals = stats.get("totals") or {}
    short_liq = totals.get("shorts", 0)
    long_liq = totals.get("longs", 0)
    tot_liq = short_liq + long_liq
    long_pct = (long_liq / tot_liq * 100) if tot_liq > 0 else 0
    
    # Book walls
    bw = d.get("bookwalls", {})
    imbal = bw.get("imbalance", 0) or 0
    imbal5m = bw.get("imbalance5mAgo", 0) or 0
    bid_depth = bw.get("bidDepth", 0) or 0
    ask_depth = bw.get("askDepth", 0) or 0
    largest_bid = bw.get("largestBid") or {}
    largest_ask = bw.get("largestAsk") or {}
    walls = bw.get("walls", []) or []
    
    # CVD
    cvd = d.get("cvd_1h") or d.get("cvd_4h") or d.get("cvd", {})
    kpis = cvd.get("kpis") or {}
    quadrant = kpis.get("quadrant", "unknown")
    buy_ratio = kpis.get("buyRatio", 50) or 50
    perp_cvd = kpis.get("perpCvd", 0) or 0
    spot_cvd = kpis.get("spotCvd", 0) or 0
    divergences = cvd.get("divergences", []) or []
    
    # Signals
    sigs = d.get("signals", {})
    counts = sigs.get("counts") or {}
    sig_sets = sigs.get("sets", []) or []
    active_sigs = []
    for s in sig_sets:
        s_name = s.get("name")
        s_side = s.get("side")
        s_itv = s.get("itv")
        active_sigs.append(f"{s_name}({s_side},{s_itv})")
        
    deep_summary[sym] = {
        "price": px,
        "liq_total": tot_liq,
        "long_liq_pct": long_pct,
        "imbalance": imbal,
        "imbalance_5m_ago": imbal5m,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "largest_bid": largest_bid,
        "largest_ask": largest_ask,
        "wall_count": len(walls),
        "quadrant": quadrant,
        "buy_ratio": buy_ratio,
        "perp_cvd": perp_cvd,
        "spot_cvd": spot_cvd,
        "divergences_count": len(divergences),
        "signals_bullish": counts.get("bullish", 0),
        "signals_bearish": counts.get("bearish", 0),
        "active_signals": active_sigs[:3]
    }
    
    print(f"\n[{sym}] Price: {px}")
    print(f"  Liq 3D: Total ${tot_liq/1e6:.1f}M | Longs: {long_pct:.1f}% (${long_liq/1e6:.1f}M) vs Shorts: {100-long_pct:.1f}% (${short_liq/1e6:.1f}M)")
    print(f"  Book Depth: Imbalance {imbal:>+.4f} (5m ago: {imbal5m:>+.4f}) | Bids: ${bid_depth/1e6:.1f}M vs Asks: ${ask_depth/1e6:.1f}M")
    if largest_bid:
        print(f"  Largest Bid Wall: ${largest_bid.get('usd', 0)/1e6:.2f}M @ {largest_bid.get('price')} (dist: {largest_bid.get('distPct', 0):+.2f}%)")
    if largest_ask:
        print(f"  Largest Ask Wall: ${largest_ask.get('usd', 0)/1e6:.2f}M @ {largest_ask.get('price')} (dist: {largest_ask.get('distPct', 0):+.2f}%)")
    print(f"  CVD Flow: Quadrant '{quadrant}' | Taker Buy Share: {buy_ratio:.1f}% | Perp CVD: {perp_cvd} | Spot CVD: {spot_cvd} | Divs: {len(divergences)}")
    print(f"  Signals: Bullish: {counts.get('bullish', 0)} | Bearish: {counts.get('bearish', 0)} | Active: {', '.join(active_sigs[:3]) if active_sigs else 'None'}")

with open("data/telemetry/deep_summary_processed.json", "w", encoding="utf-8") as f:
    json.dump(deep_summary, f, indent=2)

print("\nProcessed deep summary written to data/telemetry/deep_summary_processed.json")
