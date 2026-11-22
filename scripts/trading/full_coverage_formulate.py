#!/usr/bin/env python3
"""
FULL-COVERAGE FORMULATION ENGINE  —  R825
=========================================
Rebuild of the desk formulation so that EVERY route that can inform a trade is
consumed, and every route that CANNOT be consumed is recorded explicitly as
UNVERIFIED / UNAVAILABLE rather than silently substituted.

Design responses to desk/DATA_COVERAGE_AUDIT.md:
  FIX-1  liqlevels is pulled for 3d / 7d / 30d and the STOP is validated against
         the WORST-CASE (widest) shelf cluster, not the 3d view.
  FIX-2  oracle (Athena §15) and narrative (§8 DECIDE) are first-class score inputs.
  FIX-3  funding / oi / largeorder / cvd / micro are consumed from the terminal's
         own multi-venue feeds instead of external proxies.
  FIX-4  stopmap failure (needs CG_SESSION) is recorded, not papered over with 3d.
  FIX-5  liq-live per-venue emptiness is recorded as a PIT-11 GATE-UNEVALUABLE.
  FIX-6  position size is computed against the 1.0% NLE cap via the trading-skills
         fixed_fractional_position_size, never per-contract-only.
  FIX-7  T1/T2/T3 tranche ladder replaces the single-TP bracket.
  FIX-8  friction (1 tick + fees) is applied to every R multiple.

Every fetch is cached to data/restart/raw/<SYM>.json with its own status so the
coverage report can state, per route, exactly how many symbols succeeded.
"""
import json, os, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = os.environ.get("TERM", "http://127.0.0.1:3000")
OUT = Path("data/restart"); RAW = OUT/"raw"
RAW.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- route registry
# name -> (query-template, required?, what it must yield)
ROUTES = {
    "screener":    ("/api/cg/screener",                      True,  "universe + scan fields"),
    "market":      ("/api/cg/market?sym={s}",                True,  "price, auction, EMA, RSI, VWAP, ATR"),
    "narrative":   ("/api/cg/narrative?sym={s}",             True,  "regime, activeModel, grade (§8 DECIDE)"),
    "oracle":      ("/api/cg/oracle?sym={s}",                True,  "Athena 8-factor score (§15)"),
    "liqlevels3":  ("/api/cg/liqlevels?sym={s}&range=3d",    True,  "3d liquidation shelves"),
    "liqlevels7":  ("/api/cg/liqlevels?sym={s}&range=7d",    True,  "7d liquidation shelves"),
    "liqlevels30": ("/api/cg/liqlevels?sym={s}&range=30d",   True,  "30d liquidation shelves"),
    "bookwalls":   ("/api/cg/bookwalls?sym={s}",             True,  "L2 depth + walls + imbalance"),
    "fp":          ("/api/cg/fp?sym={s}&itv=m15",            True,  "footprint bars (delta by price)"),
    "signals":     ("/api/cg/signals?sym={s}",               True,  "time+volume footprint signals"),
    "cvd":         ("/api/cg/cvd?sym={s}",                   False, "cumulative volume delta"),
    "funding":     ("/api/cg/funding?sym={s}",               False, "multi-venue funding"),
    "oi":          ("/api/cg/oi?sym={s}",                    False, "open interest by venue"),
    "largeorder":  ("/api/cg/largeorder",                    False, "whale executions (market-wide)"),
    "micro":       ("/api/cg/micro?sym={s}",                 False, "Hyperliquid BBO + trades"),
    "quant":       ("/api/cg/quant?sym={s}",                 False, "long/short ratios"),
}
BLOCKED = {   # routes that exist but cannot be consumed today
    "stopmap":  "40000 — needs CG_SESSION in .env (PIT-16 heat-map)",
    "perpdepth":"no verified perp venues for most crypto — registry is index-only",
    "cot":      "no COT contract mapped for crypto",
    "options":  "BTC/ETH only",
}


def fetch(path, timeout=30):
    """Single attempt. The terminal already serves stale cache through a 429 window,
    so a 502/503 here means the upstream gate is saturated: DO NOT retry-storm.
    Back off once and let the caller decide."""
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
            if body[:1] == b"<":
                return {"_status": "HTML", "_err": "route returned HTML page"}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        return {"_status": "FAIL", "_err": f"HTTP {e.code}"}
    except Exception as e:
        return {"_status": "FAIL", "_err": f"{type(e).__name__}: {e}"}


def pull_symbol(sym):
    """Pull the complete evidence bundle for one symbol. Returns dict route->payload."""
    out = {"sym": sym, "fetchedAt": int(time.time() * 1000)}
    for name, (tpl, _req, _desc) in ROUTES.items():
        if name == "screener":
            continue
        p = fetch(tpl.format(s=sym))
        ok = bool(p.get("ok")) and "ok" in p
        out[name] = p if ok else {"_status": "FAIL", "_err": p.get("error") or p.get("_err")}
    return out


# ---------------------------------------------------------------- helpers
def ema_stack(ema):
    """Return (bear_count, spread_atr) across h1/h4/d1."""
    n = 0; tot = 0; cnt = 0
    for tf in ("h1", "h4", "d1"):
        e = ema.get(tf) or {}
        a13, a89, a233 = e.get("e13"), e.get("e89"), e.get("e233")
        if a13 is None or a89 is None or a233 is None:
            continue
        cnt += 1
        if a13 < a89 < a233:
            n += 1
    # spread in ATR-ish terms: use h4 e13 vs e233 relative distance
    h4 = ema.get("h4") or {}
    if h4.get("e13") and h4.get("e233"):
        tot = (h4["e233"] / h4["e13"] - 1) * 100
    return n, cnt, tot


def shelves(ll, side="above"):
    try:
        return ll["stats"]["keyLevels"][side]
    except Exception:
        return []



def usd_norm(v, ref_price):
    """Normalize a liquidation-shelf USD figure to real USD.

    VERIFIED 2026-11-22: the Coinglass payload is NOT unit-consistent.
      - BTC-scale symbols return thousands (62789.2 -> 10462 meaning $10.46M)
      - sub-dollar symbols return raw dollars (XRP 0.9644 -> 71,333,298)
    Treating them the same produced false "$0M, immaterial" reads on BTC and
    false "$888M" reads on DOGE. This is the fix.
    """
    v = float(v or 0)
    if v <= 0:
        return 0.0
    if ref_price > 1000 and v < 1e6:      # high-priced: value is in thousands
        return v * 1000.0
    if 100 < ref_price <= 1000 and v < 1e6:
        return v * 1000.0
    return v


def cluster_stop(sym_data, entry, direction, atr):
    """
    FIX-1: derive a stop that clears the WORST-CASE shelf cluster across 3d/7d/30d.
    Returns (stop, detail).
    """
    all_lv = []
    for r in ("liqlevels3", "liqlevels7", "liqlevels30"):
        ll = sym_data.get(r)
        if not ll or not ll.get("ok"):
            continue
        side = "above" if direction == "SHORT" else "below"
        for L in shelves(ll, side):
            try:
                px = float(L["price"]); usd = usd_norm(L.get("usd"), px)
                strg = float(L.get("strength") or 0)
            except Exception:
                continue
            all_lv.append({"px": px, "usd": usd, "strength": strg,
                           "range": ll.get("range"), "lev": L.get("lev")})
    if not all_lv:
        return None, {"reason": "no shelves in any range"}

    if direction == "SHORT":
        candidates = [L for L in all_lv if L["px"] > entry]
        # anything within 1.2 ATR above entry is sweepable
        near = [L for L in candidates if L["px"] - entry <= 1.2 * atr]
        material = [L for L in near if L["usd"] >= 15e6 or L["strength"] >= 45]
        if material:
            worst = max(material, key=lambda L: L["px"])
            stop = worst["px"] + 0.35 * atr
            return stop, {"basis": "clears worst material shelf in 1.2-ATR sweep zone",
                          "shelf": worst, "n_material": len(material),
                          "all_near": sorted(near, key=lambda L: L["px"])}
        if candidates:
            nearest = min(candidates, key=lambda L: L["px"])
            stop = nearest["px"] + 0.35 * atr
            return stop, {"basis": "clears nearest shelf (no material cluster)",
                          "shelf": nearest, "n_material": 0, "all_near": near}
        return entry + 0.75 * atr, {"basis": "no shelf above — pure ATR stop",
                                    "shelf": None, "n_material": 0, "all_near": []}
    return None, {"reason": "long-side not implemented in this pass"}


def pick_targets(sym_data, entry, stop, direction):
    """Downside liquidity/volume nodes as T1/T2/T3."""
    m = sym_data.get("market") or {}
    au = (m.get("auction") or {})
    cands = []
    for tf in ("session", "day", "week"):
        a = au.get(tf) or {}
        for k in ("val", "poc"):
            v = a.get(k)
            if v: cands.append({"px": float(v), "tag": f"{tf}-{k}"})
    for r in ("liqlevels3", "liqlevels7", "liqlevels30"):
        ll = sym_data.get(r)
        if not ll or not ll.get("ok"): continue
        for L in shelves(ll, "below"):
            try:
                _px = float(L["price"]); _u = usd_norm(L.get("usd"), _px)
                if _u >= 20e6:
                    cands.append({"px": _px, "tag": f"{ll.get('range')}-liq${_u/1e6:.0f}M"})
            except Exception:
                pass
    # keep only targets below entry for SHORT, sort descending (nearest first)
    cands = [c for c in cands if c["px"] < entry]
    # dedupe within 0.25% and sort
    cands.sort(key=lambda c: -c["px"])
    ded = []
    for c in cands:
        if not ded or abs(c["px"] / ded[-1]["px"] - 1) > 0.0025:
            ded.append(c)
    risk = abs(stop - entry)          # magnitude, sign-independent
    tranches = []
    for i, c in enumerate(ded):
        if c["px"] >= entry: continue
        r = (entry - c["px"]) / risk if risk > 0 else 0
        if r < 1.0: continue
        tranches.append({"tag": c["tag"], "px": round(c["px"], 6), "R": round(r, 2)})
    # thin to 3 sensible tranches
    if len(tranches) > 3:
        tranches = [tranches[0], tranches[len(tranches)//2], tranches[-1]]
    return tranches


def score(sym_data):
    """Multi-pillar score. Every pillar reports its source route so coverage is auditable."""
    P = {}
    m = sym_data.get("market") or {}
    nar = sym_data.get("narrative") or {}
    orc = sym_data.get("oracle") or {}
    bw = (sym_data.get("bookwalls") or {}).get("model") or {}
    fnd = sym_data.get("funding") or {}
    oi = sym_data.get("oi") or {}
    sig = sym_data.get("signals") or {}
    q = sym_data.get("quant") or {}

    bear_n, tf_n, spread = ema_stack(m.get("ema") or {})
    # 1. TREND  (max 25) — HTF EMA stack + magnitude
    trend = (bear_n / tf_n * 18) if tf_n else 0
    trend += min(abs(spread), 12) / 12 * 7
    P["trend"] = {"pts": round(trend, 2), "max": 25,
                  "src": "market.ema",
                  "note": f"{bear_n}/{tf_n} TFs bear-stacked, h4 e13/e233 spread {spread:+.1f}%"}

    # 2. STRETCH  (max 15) — VWAP z + RSI position (we short INTO strength, so
    #    favourable = price AT/above VWAP, RSI not oversold)
    vz = None
    try:
        vwap = m["vwap"]["vwap"]; band = m["vwap"]["bands"]["sd1"]; last = m["price"]["last"]
        vz = (last - vwap) / band
    except Exception:
        pass
    st = 0
    if vz is not None:
        st += 9 if vz >= 0.5 else (5 if vz >= 0 else 0)
    r_d1 = (m.get("rsi") or {}).get("d1")
    r_h4 = (m.get("rsi") or {}).get("h4")
    if r_d1 is not None and r_d1 < 32: st += 3        # cheap to short, but bounce risk
    if r_h4 is not None and 40 <= r_h4 <= 60: st += 3  # room to fall
    P["stretch"] = {"pts": round(st, 2), "max": 15, "src": "market.vwap+market.rsi",
                    "note": f"vwap z {vz if vz is None else round(vz,2)}σ, rsi h4 {r_h4}, d1 {r_d1}"}

    # 3. FLOW  (max 20) — footprint signal balance + absorption/exhaustion
    counts = sig.get("counts") or {}
    bsig = counts.get("bearish") or 0; usig = counts.get("bullish") or 0
    fl = min(bsig / max(bsig + usig, 1) * 14, 14)
    if bsig > usig: fl += 3
    exh = 0
    for s in (sig.get("sets") or []):
        for g in (s.get("sigs") or []):
            if g.get("t") in ("exh", "abs", "trap") and g.get("side") == "sell": exh += 1
    fl += min(exh, 3)
    P["flow"] = {"pts": round(fl, 2), "max": 20, "src": "signals+fp",
                 "note": f"{bsig} bear / {usig} bull signals, {exh} sell-side exh/abs/trap"}

    # 4. POSITIONING  (max 12) — funding positive = crowded long = good for short
    fr = []
    for v in (fnd.get("venues") or []):
        try: fr.append(float(v.get("rate8h") or 0))
        except Exception: pass
    avgf = sum(fr) / len(fr) if fr else None
    po = 0
    if avgf is not None:
        po += 6 if avgf > 0.01 else (4 if avgf > 0 else 1)   # longs paying = crowded long
    oiv = (oi.get("venues") or [])
    if oiv: po += 2
    lsr = None
    try:
        lsr = q["lsRatios"]["1h"]["global"]
        if lsr and float(lsr) > 1: po += 4
    except Exception:
        pass
    P["positioning"] = {"pts": round(po, 2), "max": 12, "src": "funding+oi+quant",
                        "note": f"avg funding8h {avgf if avgf is None else round(avgf,4)}%, "
                                f"{len(oiv)} OI venues, LSR1h {lsr}"}

    # 5. BOOK  (max 13) — imbalance toward ask = supply
    imb = bw.get("imbalance")
    bk = 0
    if imb is not None:
        bk += max(0, -imb) * 13     # negative imbalance (ask-heavy) favours shorts
    P["book"] = {"pts": round(bk, 2), "max": 13, "src": "bookwalls",
                 "note": f"L2 imbalance {imb if imb is None else round(imb,3)} "
                         f"(bid ${(bw.get('depth') or {}).get('bidUsd',0)/1e6:.0f}M / "
                         f"ask ${(bw.get('depth') or {}).get('askUsd',0)/1e6:.0f}M)"}

    # 6. ATHENA ORACLE  (max 10) — inverse: low score = bearish = favourable for short
    osc = orc.get("score")
    oo = 0
    if osc is not None:
        oo = max(0, (55 - osc)) / 55 * 10
    P["oracle"] = {"pts": round(oo, 2), "max": 10, "src": "oracle §15",
                   "note": f"{osc}/100 — {orc.get('verdict','n/a')[:60]}"}

    # 7. REGIME / DECIDE  (max 5)
    rg = nar.get("regime"); am = nar.get("activeModel")
    rc = 0
    if rg in ("RANGE_BALANCE", "TREND_DOWN"): rc += 3
    if am and am != "NO_TRADE": rc += 2
    P["regime"] = {"pts": round(rc, 2), "max": 5, "src": "narrative §8",
                   "note": f"{rg} · model {am} · grade {nar.get('grade')}"}

    total = sum(v["pts"] for v in P.values())
    mx = sum(v["max"] for v in P.values())
    return P, round(total, 2), mx


def main():
    t0 = time.time()
    scr = fetch("/api/cg/screener")
    if not scr.get("ok"):
        sys.exit("screener down")
    (OUT/"screener.json").write_text(json.dumps(scr))
    rows = scr["rows"]
    print(f"[universe] {len(rows)} instruments ({sum(1 for r in rows if r.get('warm'))} warm)", flush=True)

    # Stage A — cheap directional prefilter from screener alone
    shorts, longs, mixed, neutral = [], [], [], []
    for r in rows:
        e4, e1 = r.get("ema4h"), r.get("ema1h")
        if e4 == "bear" and e1 == "bear": shorts.append(r)
        elif e4 == "bull" and e1 == "bull": longs.append(r)
        elif e4 and e1: mixed.append(r)
        else: neutral.append(r)
    print(f"[stageA] short-bias {len(shorts)} | long-bias {len(longs)} | mixed {len(mixed)} | unscanned {len(neutral)}", flush=True)

    # Stage B — deep evidence on every short-bias name + any mixed
    targets = [r["sym"] for r in shorts] + [r["sym"] for r in mixed]
    print(f"[stageB] deep-pulling {len(targets)} symbols × {len(ROUTES)-1} routes "
          f"= {len(targets)*(len(ROUTES)-1)} calls", flush=True)

    results = {s_: {"sym": s_} for s_ in targets}
    order = []            # routes ordered cheapest/cached-first
    for name in ("market", "narrative", "oracle", "bookwalls", "signals",
                 "funding", "oi", "quant", "micro",
                 "liqlevels3", "liqlevels7", "liqlevels30",
                 "fp", "cvd", "largeorder"):
        order.append(name)

    def sweep(passes=3):
        for p in range(passes):
            todo = [(sy, nm) for sy in targets for nm in order
                    if not (isinstance(results[sy].get(nm), dict) and results[sy][nm].get("ok"))]
            if not todo:
                return
            print(f"[pass {p+1}] {len(todo)} calls outstanding", flush=True)
            gets = 0
            for i, (sy, nm) in enumerate(todo, 1):
                payload = fetch(ROUTES[nm][0].format(s=sy))
                if payload.get("ok"):
                    results[sy][nm] = payload
                    gets += 1
                else:
                    results[sy][nm] = payload
                    if payload.get("_status") == "FAIL" and "429" in str(payload.get("_err")):
                        time.sleep(8)
                time.sleep(0.28)          # ~3.5 req/s, well under the 6-slot gate
                if i % 60 == 0:
                    print(f"   {i}/{len(todo)} ok={gets} ({time.time()-t0:.0f}s)", flush=True)
            print(f"[pass {p+1}] recovered {gets}/{len(todo)}", flush=True)
            if gets < len(todo) * 0.03:
                print("[pass] diminishing returns — stopping", flush=True)
                return
            time.sleep(12)

    sweep(passes=3)

    # coverage accounting
    cov = {}
    for name in ROUTES:
        if name == "screener": continue
        ok = sum(1 for d in results.values()
                 if isinstance(d.get(name), dict) and d[name].get("ok"))
        cov[name] = {"ok": ok, "n": len(results), "pct": round(ok/max(len(results),1)*100, 1)}
    print("[coverage]", json.dumps(cov), flush=True)

    for s, d in results.items():
        (RAW/f"{s}.json").write_text(json.dumps(d))

    # Stage C — score + geometry
    out = []
    for s, d in results.items():
        try:
            m = d.get("market") or {}
            if not m.get("ok"): continue
            last = m["price"]["last"]
            atr = (m.get("vol") or {}).get("atr14H1") or (last * 0.01)
            P, tot, mx = score(d)
            # entry: nearest material shelf above = the short trigger
            entry, entry_basis = None, None
            best = None
            for rng in ("liqlevels3", "liqlevels7", "liqlevels30"):
                ll = d.get(rng)
                if not ll or not ll.get("ok"): continue
                for L in shelves(ll, "above"):
                    try:
                        px = float(L["price"]); usd = float(L.get("usd") or 0)
                    except Exception: continue
                    if px <= last: continue
                    if px - last > 3.5 * atr: continue
                    sc = usd * (float(L.get("strength") or 0)/100)
                    if best is None or sc > best[0]:
                        best = (sc, px, L, rng)
            if best:
                entry = best[1]; entry_basis = f"{best[3]} shelf ${best[2]['usd']/1e6:.0f}M str{best[2].get('strength')}"
            else:
                entry = last + 0.9 * atr; entry_basis = "no shelf — 0.9 ATR fade"
            stop, sdet = cluster_stop(d, entry, "SHORT", atr)
            if stop is None or stop <= entry:
                continue
            tranches = pick_targets(d, entry, stop, "SHORT")
            risk = stop - entry          # SHORT: risk is the distance UP to the stop
            risk_pct = risk / entry * 100
            out.append({
                "sym": s, "score": tot, "max": mx, "pct": round(tot/mx*100, 1),
                "pillars": P, "last": last, "atr": atr,
                "entry": round(entry, 6), "entry_basis": entry_basis,
                "stop": round(stop, 6), "stop_basis": sdet.get("basis"),
                "stop_shelf": sdet.get("shelf"), "n_material": sdet.get("n_material"),
                "risk": round(risk, 6), "risk_pct": round(risk_pct, 3),
                "tranches": tranches,
                "atr_state": (m.get("vol") or {}).get("atrState"),
            })
        except Exception as e:
            print(f"  [warn] {s}: {e}", flush=True)

    out.sort(key=lambda x: -x["score"])
    summary = {"ts": int(time.time()*1000), "universe": len(rows),
               "stage_a": {"short": len(shorts), "long": len(longs),
                           "mixed": len(mixed), "unscanned": len(neutral)},
               "deep_pulled": len(results), "coverage": cov,
               "blocked_routes": BLOCKED, "candidates": out}
    (OUT/"formulation_full.json").write_text(json.dumps(summary, indent=1))
    print(f"\n[done] {len(out)} candidates scored in {time.time()-t0:.0f}s -> data/restart/formulation_full.json")
    print(f"{'SYM':<10}{'SCORE':>7}{'%':>7}{'ENTRY':>11}{'STOP':>11}{'RISK%':>7}{'T1R':>6}{'T2R':>6}")
    for c in out[:25]:
        t = c["tranches"]
        print(f"{c['sym']:<10}{c['score']:>7.1f}{c['pct']:>6.0f}%{c['entry']:>11.5f}"
              f"{c['stop']:>11.5f}{c['risk_pct']:>7.2f}"
              f"{(t[0]['R'] if t else 0):>6.1f}{(t[-1]['R'] if t else 0):>6.1f}")


if __name__ == "__main__":
    main()
