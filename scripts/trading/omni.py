#!/usr/bin/env python3
"""
omni.py — 360° trade fusion across every data layer the terminal exposes.

WHY THIS EXISTS
---------------
Earlier desk notes used ONE layer at ONE timeframe: liquidation heat, Model 1,
24h window. That is a slice, not a view. The terminal actually exposes 14
independent layers across 6 timeframes and 3 heat-map models:

  LAYER            ROUTE                    WHAT IT ADDS
  heat-1/2/3       cg/stopmap?model=1|2|3   liq fuel under 3 leverage assumptions
  liqlevels        cg/liqlevels             PER-VENUE liq levels + strength/lev tier
  regime           cg/narrative             classifier + M1/M2 plan + 11-pt checklist
  flow-signals     cg/signals               5 types x 3 bar-types (abs/trap/div/exh/sdiv)
  positioning      cg/quant                 LS ratio, OI quadrant, basis, CB premium
  options          cg/options               dvol, GEX regime, gamma flip, max pain
  funding          cg/funding               21 venues, 8h-normalised
  open-interest    cg/oi                    OI + 1h/4h/24h deltas
  delta            cg/cvd                   spot vs perp CVD, divergences
  orderflow        cg/orderflow             price-bucketed buy/sell volume
  book             cg/bookwalls             resting depth, bid/ask walls
  micro            cg/micro                 HL mark-vs-oracle, multi-venue BBO

TIMEFRAMES: m5, m15, m30, h1, h4, d1 for the flow layers; 12h..2y for heat.

HOW IT FUSES
------------
Every layer casts a DIRECTIONAL VOTE in [-1, +1] with a weight and a stated
reason. Votes are NOT averaged blindly:

  1. Each layer is reduced to (direction, strength) at each timeframe it supports.
  2. Strength is normalised to a z-like scale so no layer dominates by unit size.
  3. Agreement is measured as the weighted fraction of layers on one side.
  4. CONFLICTS are reported explicitly, not smoothed away. A 7-5 split with two
     heavy layers opposed is a different animal from a 12-0 sweep and is scored
     differently.
  5. Conviction = agreement x mean strength x (1 - conflict penalty).
  6. A veto list runs AFTER scoring: no trade if regime is COMPRESSION_SQUEEZE
     with M1 OFF, if the proposed stop sits inside liquidation fuel (the exact
     defect found on the live ETH short), or if reward < 1.5x risk.

WHAT IT WILL NOT DO
-------------------
- It will not invent a direction when layers disagree. "NO TRADE" with the
  conflict named is a valid output and an approved outcome.
- It will not treat a unitless heat unit as USD (see HEATMAP_MINING_R827 §2).
- It will not claim coverage it lacks: every layer reports ok/FAIL and every
  derived number carries its source.
- It never places, amends or cancels. Output is proposals for Antigravity.

USAGE
  python3 scripts/trading/omni.py --sym BTC ETH XRP
  python3 scripts/trading/omni.py --sym BTC --tf all --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("DESK_BASE", "http://127.0.0.1:3000")
TFS = ["m5", "m15", "m30", "h1", "h4", "d1"]
HEAT_PACE = 10.0  # Coinglass 50001 under burst


# ───────────────────────────── transport ─────────────────────────────
def get(path: str, timeout: int = 60) -> dict:
    url = f"{BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def q(sym: str) -> str:
    return urllib.parse.quote(sym.upper())


# ───────────────────────────── layer pulls ─────────────────────────────
def pull_all(sym: str) -> dict:
    """Pull every layer once. Never raises; failures are recorded."""
    L: dict = {"sym": sym.upper()}
    S = q(sym)

    L["narrative"] = get(f"/api/cg/narrative?sym={S}")
    L["signals"] = get(f"/api/cg/signals?sym={S}")
    L["quant"] = get(f"/api/cg/quant?sym={S}")
    L["options"] = get(f"/api/cg/options?sym={S}")
    L["funding"] = get(f"/api/cg/funding?sym={S}")
    L["oi"] = get(f"/api/cg/oi?sym={S}")
    L["cvd"] = get(f"/api/cg/cvd?sym={S}")
    L["micro"] = get(f"/api/cg/micro?sym={S}")
    L["liqlevels"] = get(f"/api/cg/liqlevels?sym={S}")
    L["bookwalls"] = get(f"/api/cg/bookwalls?sym={S}")

    # per-timeframe flow layers
    L["cvd_tf"] = {}
    L["of_tf"] = {}
    for tf in TFS:
        L["cvd_tf"][tf] = get(f"/api/cg/cvd?sym={S}&itv={tf}")
        L["of_tf"][tf] = get(f"/api/cg/orderflow?sym={S}&itv={tf}")

    # heat maps: 3 models x 2 ranges, paced
    L["heat"] = {}
    first = True
    for mdl in ("1", "2", "3"):
        for rng in ("24h", "3d"):
            if not first:
                time.sleep(HEAT_PACE)
            first = False
            L["heat"][f"m{mdl}_{rng}"] = get(
                f"/api/cg/stopmap?sym={S}&model={mdl}&mode=coin&range={rng}", timeout=90)
    return L


# ───────────────────────────── helpers ─────────────────────────────
def ok(layer) -> bool:
    return isinstance(layer, dict) and layer.get("ok") is not False


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def slope_vote(series: list, n: int = 40) -> float:
    """Direction of a cumulative series over the last n points, z-normalised."""
    if not series or len(series) < n + 1:
        return 0.0
    tail = [v for v in series[-n:] if isinstance(v, (int, float))]
    if len(tail) < 5:
        return 0.0
    delta = tail[-1] - tail[0]
    span = max(tail) - min(tail)
    if span <= 0:
        return 0.0
    return clamp(delta / span)


def atr_from(candles: list, n: int = 14) -> float | None:
    if len(candles) < n + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        try:
            trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
        except (KeyError, TypeError):
            continue
    return sum(trs[-n:]) / n if trs else None


# ───────────────────────────── votes ─────────────────────────────
def vote_cvd_tf(L: dict) -> list[dict]:
    """Spot vs perp CVD per timeframe. Perp leads on positioning, spot on real flow."""
    out = []
    for tf in TFS:
        d = L["cvd_tf"].get(tf)
        if not ok(d):
            continue
        tot = slope_vote(d.get("total") or [])
        per = slope_vote(d.get("perp") or [])
        spo = slope_vote(d.get("spot") or [])
        # perp flow is the price driver; spot confirms. weight spot lower.
        v = clamp(0.6 * per + 0.4 * spo)
        if abs(v) < 0.05:
            continue
        out.append({
            "layer": f"cvd-{tf}", "dir": v, "weight": 1.0,
            "why": f"perp {per:+.2f} / spot {spo:+.2f} (total {tot:+.2f})",
        })
    return out


def vote_orderflow_tf(L: dict) -> list[dict]:
    """Price-bucketed buy vs sell volume — aggressive takers, not cumulative."""
    out = []
    for tf in TFS:
        d = L["of_tf"].get(tf)
        if not ok(d):
            continue
        bars = d.get("bars") or []
        if not bars:
            continue
        buy = sell = 0.0
        for b in bars[-20:]:
            for cell in (b.get("buckets") or []):
                try:
                    buy += float(cell[4])   # buy notional
                    sell += float(cell[5])  # sell notional
                except (IndexError, TypeError, ValueError):
                    continue
        tot = buy + sell
        if tot <= 0:
            continue
        imb = (buy - sell) / tot
        if abs(imb) < 0.04:
            continue
        out.append({
            "layer": f"of-{tf}", "dir": clamp(imb * 2.0), "weight": 1.0,
            "why": f"taker buy {buy/(tot)*100:.0f}% vs sell {(1-buy/tot)*100:.0f}% "
                   f"({tot/1e6:,.0f}M notional, last 20 bars)",
        })
    return out


def vote_signals(L: dict) -> list[dict]:
    d = L["signals"]
    if not ok(d):
        return []
    out = []
    for s in (d.get("sets") or []):
        sigs = s.get("sigs") or []
        if not sigs:
            continue
        b = sum(1 for x in sigs if x.get("side") == "buy")
        sl = sum(1 for x in sigs if x.get("side") == "sell")
        n = b + sl
        if n == 0:
            continue
        v = (b - sl) / n
        out.append({
            "layer": f"signals-{s.get('label','?')}", "dir": clamp(v), "weight": 0.8,
            "why": f"{n} signals: {b} buy / {sl} sell "
                   f"({','.join(sorted({x.get('t','?') for x in sigs}))})",
        })
    return out


def vote_positioning(L: dict) -> list[dict]:
    """Funding, OI quadrant, basis. Crowded-long = contrarian short fuel."""
    out = []
    d = L["quant"]
    if not ok(d):
        return out
    f = d.get("funding") or {}
    apr = f.get("annualizedAprPct")
    if apr is not None:
        # high positive funding = longs paying = crowded long = bearish tilt
        v = clamp(-apr / 60.0)
        out.append({"layer": "funding", "dir": v, "weight": 0.9,
                    "why": f"OI-weighted {f.get('oiWeightedRate8h')} /8h = {apr:+.1f}% APR "
                           f"across {len(f.get('venues') or [])} venues"})
    b = d.get("basis") or {}
    bp = b.get("basisPct")
    if bp is not None:
        out.append({"layer": "basis", "dir": clamp(-bp / 0.5), "weight": 0.6,
                    "why": f"perp-spot basis {bp:+.3f}% · CB premium "
                           f"{b.get('coinbasePremiumBps')}bps"})
    oq = d.get("oiQuadrant") or {}
    for tf in ("1h", "4h", "24h"):
        row = oq.get(tf)
        if not isinstance(row, dict):
            continue
        dp = row.get("priceDeltaPct")
        doi = row.get("oiDeltaPct")
        if dp is None or doi is None:
            continue
        # price up + OI up = new longs (trend). price up + OI down = short cover.
        if dp > 0 and doi > 0:
            v = 0.5
        elif dp > 0 and doi < 0:
            v = 0.2   # covering rally — weaker
        elif dp < 0 and doi > 0:
            v = -0.5  # new shorts
        else:
            v = -0.2  # long liquidation
        out.append({"layer": f"oi-quad-{tf}", "dir": v, "weight": 0.7,
                    "why": f"price {dp:+.2f}% with OI {doi:+.2f}% "
                           f"=> {'new longs' if v>0 and doi>0 else 'short cover' if v>0 else 'new shorts' if doi>0 else 'long flush'}"})
    return out


def vote_options(L: dict) -> list[dict]:
    """Gamma regime + skew. Negative gamma amplifies moves; dealers chase."""
    out = []
    d = L["options"]
    if not ok(d):
        return out
    g = d.get("gex") or {}
    reg = g.get("regime")
    flip = g.get("gammaFlipPrice")
    spot = g.get("spot")
    if reg and spot and flip:
        # negative gamma: dealers short gamma -> moves amplify, trend persists
        # positive gamma: dealers long gamma -> moves mean-revert, fade extremes
        v = 0.0
        if "NEGATIVE" in str(reg):
            v = 0.15 * (1 if spot > flip else -1) * 2  # mild trend-follow bias
        rr = d.get("riskReversal25d") or {}
        rv = rr.get("value")
        if rv is not None:
            # positive RR = calls bid = bullish skew
            v += clamp(rv / 25.0) * 0.5
        out.append({
            "layer": "options-gex", "dir": clamp(v), "weight": 0.6,
            "why": f"{reg} · flip {flip:,.0f} vs spot {spot:,.0f} · "
                   f"RR25d {rv if rv is not None else 'n/a'}",
        })
    pc = d.get("putCallRatio") or {}
    if pc.get("vol24h"):
        v = clamp((1.0 - pc["vol24h"]) * 2)
        out.append({"layer": "options-pcr", "dir": v, "weight": 0.4,
                    "why": f"put/call vol {pc['vol24h']:.3f} (OI {pc.get('oi')})"})
    return out


def vote_micro(L: dict) -> list[dict]:
    """HL mark vs oracle, and multi-venue BBO skew."""
    out = []
    d = L["micro"]
    if not ok(d):
        return out
    hl = d.get("hl") or {}
    mv = hl.get("markVsOraclePct")
    if mv is not None:
        out.append({"layer": "mark-oracle", "dir": clamp(mv / 0.15), "weight": 0.5,
                    "why": f"HL mark {hl.get('mark')} vs oracle {hl.get('oracle')} = {mv:+.3f}%"})
    bbo = d.get("bbo") or {}
    mids = []
    for v in (bbo.get("venues") or []):
        if v.get("bid") and v.get("ask"):
            try:
                mids.append((float(v["bid"]) + float(v["ask"])) / 2)
            except (TypeError, ValueError):
                pass
    if len(mids) >= 2:
        spread = (max(mids) - min(mids)) / (sum(mids) / len(mids))
        out.append({"layer": "venue-spread", "dir": 0.0, "weight": 0.2,
                    "why": f"{len(mids)} venues, max mid dispersion {spread*100:.3f}% "
                           f"(fragmentation, not direction)"})
    return out


def vote_liquidations(L: dict, last: float) -> list[dict]:
    """Liquidation FUEL skew across 3 models. Above-fuel = short squeeze magnet."""
    out = []
    for key, h in (L["heat"] or {}).items():
        if not ok(h):
            continue
        m = h.get("model") or {}
        kl = m.get("keyLevels") or {}
        a = [w for w in (kl.get("above") or []) if w.get("price")]
        b = [w for w in (kl.get("below") or []) if w.get("price")]
        fa = sum(float(w.get("usd") or 0) for w in a)
        fb = sum(float(w.get("usd") or 0) for w in b)
        tot = fa + fb
        if tot <= 0:
            continue
        skew = (fa - fb) / tot
        out.append({
            "layer": f"heat-{key}", "dir": clamp(skew * 1.2), "weight": 0.9,
            "why": f"fuel above {fa/1e6:,.1f}M vs below {fb/1e6:,.1f}M "
                   f"=> skew {skew:+.2f}",
        })
    return out


# ───────────────────────────── heat extraction ─────────────────────────────
def heat_consensus(L: dict, last: float) -> dict:
    """Walls that survive all 3 models are real; ones that don't are model artefacts."""
    per: dict[str, list[dict]] = {}
    usable = 0
    for key, h in (L["heat"] or {}).items():
        if not ok(h):
            continue
        usable += 1
        m = h.get("model") or {}
        kl = m.get("keyLevels") or {}
        for w in (kl.get("above") or []):
            if w.get("price"):
                per.setdefault("above", []).append({"px": w["price"], "usd": w.get("usd") or 0,
                                                    "src": key})
        for w in (kl.get("below") or []):
            if w.get("price"):
                per.setdefault("below", []).append({"px": w["price"], "usd": w.get("usd") or 0,
                                                    "src": key})

    def cluster(rows: list[dict], tol_pct: float = 0.35) -> list[dict]:
        if not rows:
            return []
        rows = sorted(rows, key=lambda x: x["px"])
        groups, cur = [], [rows[0]]
        for r in rows[1:]:
            if abs(r["px"] - cur[-1]["px"]) / cur[-1]["px"] * 100 <= tol_pct:
                cur.append(r)
            else:
                groups.append(cur)
                cur = [r]
        groups.append(cur)
        out = []
        for g in groups:
            models = {x["src"].split("_")[0] for x in g}
            out.append({
                "px": sorted(x["px"] for x in g)[len(g) // 2],
                "usd_max": max(x["usd"] for x in g),
                "usd_sum": sum(x["usd"] for x in g),
                "n_models": len(models),
                "models": sorted(models),
            })
        return sorted(out, key=lambda x: -x["n_models"] * x["usd_max"])

    return {
        "above": cluster(per.get("above", [])),
        "below": cluster(per.get("below", [])),
        "configs_ok": usable,
    }


# ───────────────────────────── fusion ─────────────────────────────
def fuse(sym: str, L: dict) -> dict:
    nar = L["narrative"] if ok(L["narrative"]) else {}
    ctx = nar.get("context") or {}
    last = ctx.get("price")
    if not last:
        dl = L["micro"].get("hl") or {}
        last = dl.get("mark")
    if not last:
        return {"sym": sym, "error": "no price available"}

    votes: list[dict] = []
    votes += vote_cvd_tf(L)
    votes += vote_orderflow_tf(L)
    votes += vote_signals(L)
    votes += vote_positioning(L)
    votes += vote_options(L)
    votes += vote_micro(L)
    votes += vote_liquidations(L, last)

    # aggregate
    net = sum(v["dir"] * v["weight"] for v in votes)
    wsum = sum(v["weight"] for v in votes) or 1.0
    score = net / wsum                     # -1..+1
    bulls = [v for v in votes if v["dir"] > 0.05]
    bears = [v for v in votes if v["dir"] < -0.05]
    wb = sum(v["weight"] for v in bulls)
    ws = sum(v["weight"] for v in bears)
    agree = max(wb, ws) / (wb + ws) if (wb + ws) else 0.5

    # conflict: heavy layers on the losing side
    losing = bears if wb >= ws else bulls
    conflict_w = sum(v["weight"] for v in losing)
    conflict = conflict_w / (wb + ws) if (wb + ws) else 0.0

    direction = "LONG" if score > 0.08 else "SHORT" if score < -0.08 else "NEUTRAL"

    # ── FAMILY AGGREGATION ────────────────────────────────────────────────
    # A single blended score hides the structure. Six heat configs that disagree
    # with each other can outvote six CVD timeframes that agree perfectly, even
    # though "liquidations are split" and "flow is unanimous" are completely
    # different statements. Group votes into families and report each family's
    # own consensus, then measure how many FAMILIES agree on direction. That is
    # the number an operator can actually act on.
    def family_of(layer: str) -> str:
        if layer.startswith("cvd") or layer.startswith("of-"):
            return "flow"
        if layer.startswith("heat"):
            return "liquidations"
        if layer.startswith("signals"):
            return "microstructure"
        if layer.startswith(("funding", "basis", "oi-quad")):
            return "positioning"
        if layer.startswith("options"):
            return "vol/options"
        return "venue"

    fam: dict[str, list[dict]] = {}
    for v in votes:
        fam.setdefault(family_of(v["layer"]), []).append(v)

    families = {}
    for name, vs in fam.items():
        w = sum(x["weight"] for x in vs) or 1.0
        net_v = sum(x["dir"] * x["weight"] for x in vs) / w
        b = sum(1 for x in vs if x["dir"] > 0.05)
        s_ = sum(1 for x in vs if x["dir"] < -0.05)
        families[name] = {
            "dir": net_v,
            "n": len(vs),
            "bull": b,
            "bear": s_,
            "consensus": (b - s_) / len(vs) if vs else 0,
        }

    # family-level agreement: the honest measure of a 360 view
    fam_bull = [f for f in families.values() if f["dir"] > 0.05]
    fam_bear = [f for f in families.values() if f["dir"] < -0.05]
    fam_total = len(fam_bull) + len(fam_bear)
    fam_agree = (max(len(fam_bull), len(fam_bear)) / fam_total) if fam_total else 0.5

    # conviction blends the raw vote score with FAMILY consensus, so a family
    # that votes 6-0 counts for more than six families that cancel out.
    conviction = abs(score) * agree * (1 - 0.5 * conflict) * 100
    conviction = (conviction * 0.5) + (fam_agree * abs(score) * 100 * 0.5)

    heat = heat_consensus(L, last)

    # risk geometry from real levels
    atr = ctx.get("atrH1")
    plan = geometry(direction, last, atr, heat, L)

    vetoes = []
    regime = nar.get("regime")
    m1 = (nar.get("m1") or {}).get("status")
    m2 = (nar.get("m2") or {}).get("status")
    if regime == "COMPRESSION_SQUEEZE" and m1 == "OFF":
        vetoes.append(f"regime {regime}, M1 OFF — model itself says stand down")
    if plan.get("stop_in_fuel"):
        vetoes.append(f"stop {plan['stop']:,.4f} sits inside liq fuel "
                      f"({plan['stop_fuel']['px']:,.4f} = ${plan['stop_fuel']['usd_max']/1e6:,.1f}M)")
    if plan.get("rr") is not None and plan["rr"] < 1.5:
        vetoes.append(f"R:R {plan['rr']:.2f} < 1.5 minimum")
    if agree < 0.60:
        vetoes.append(f"layer agreement only {agree*100:.0f}% (< 60%)")
    if conviction < 15:
        vetoes.append(f"conviction {conviction:.0f}/100 (< 15)")

    return {
        "sym": sym, "last": last, "direction": direction,
        "score": score, "agreement": agree, "conflict": conflict,
        "conviction": conviction,
        "n_votes": len(votes), "bulls": len(bulls), "bears": len(bears),
        "families": families, "family_agreement": fam_agree,
        "regime": regime, "regime_note": nar.get("regimeNote"),
        "grade": nar.get("grade"), "nar_direction": nar.get("direction"),
        "m1": m1, "m2": m2,
        "atr_h1": atr, "vwap_z": ctx.get("vwapZ"), "rvol": ctx.get("rvol"),
        "plan": plan, "vetoes": vetoes,
        "verdict": "NO TRADE" if vetoes else direction,
        "votes": votes, "heat": heat,
        "layers_ok": {k: ok(v) for k, v in L.items() if k not in ("heat", "cvd_tf", "of_tf")},
        "tf_ok": {"cvd": [t for t in TFS if ok(L["cvd_tf"].get(t))],
                  "of": [t for t in TFS if ok(L["of_tf"].get(t))]},
    }


def geometry(direction: str, last: float, atr: float | None, heat: dict,
             L: dict) -> dict:
    """Entry / stop / target built from REAL levels, with fuel-awareness."""
    if direction == "NEUTRAL" or not atr:
        return {"entry": None, "stop": None, "target": None, "rr": None,
                "stop_fuel": None, "note": "no direction or no ATR"}

    opposing = heat["below"] if direction == "LONG" else heat["above"]
    aligned = heat["above"] if direction == "LONG" else heat["below"]
    opposing = [w for w in opposing if w["n_models"] >= 2]
    aligned = [w for w in aligned if w["n_models"] >= 2]

    entry = last

    # ── TARGET PLACEMENT (fixed) ──────────────────────────────────────────
    # The first aligned wall is FUEL, not a destination. Price moving into a
    # liquidation cluster ACCELERATES as those positions are force-closed, so
    # stopping at the cluster throws away the move it causes. Target the far
    # side of the nearest significant cluster instead: if walls sit at A then
    # B (B further), aim at B. Same logic as "do not put the stop inside fuel",
    # applied to the exit.
    def pick_target(cands):
        if not cands:
            return None
        cands = sorted(cands, key=lambda x: abs(x["px"] - entry))
        if len(cands) >= 2:
            return cands[1]["px"]
        return cands[0]["px"]

    if direction == "LONG":
        cand = [w for w in opposing if w["px"] < entry]
        base = max(cand, key=lambda x: x["px"])["px"] if cand else entry - 1.5 * atr
        stop = min(base - 0.35 * atr, entry - 1.0 * atr)
        tgt_c = [w for w in aligned if w["px"] > entry]
        target = pick_target(tgt_c) if tgt_c else entry + 3 * atr
    else:
        cand = [w for w in opposing if w["px"] > entry]
        base = min(cand, key=lambda x: x["px"])["px"] if cand else entry + 1.5 * atr
        stop = max(base + 0.35 * atr, entry + 1.0 * atr)
        tgt_c = [w for w in aligned if w["px"] < entry]
        target = pick_target(tgt_c) if tgt_c else entry - 3 * atr

    risk = abs(stop - entry)
    reward = abs(target - entry)
    rr = (reward / risk) if risk else None

    # does the stop sit inside a multi-model fuel cluster?
    sf = None
    zone = [w for w in opposing if min(entry, stop) <= w["px"] <= max(entry, stop)]
    if zone:
        sf = max(zone, key=lambda x: x["usd_max"])

    return {"entry": entry, "stop": stop, "target": target, "rr": rr,
            "stop_fuel": sf,
            "stop_atr": risk / atr if atr else None,
            "target_atr": reward / atr if atr else None}


# ───────────────────────────── render ─────────────────────────────
def render(r: dict) -> str:
    if r.get("error"):
        return f"{r['sym']}: ERROR {r['error']}"
    out = []
    out.append(f"\n{'='*96}")
    out.append(f"{r['sym']}  @ {r['last']:,.4f}   verdict: {r['verdict']}"
               f"   conviction {r['conviction']:.0f}/100")
    out.append(f"{'='*96}")
    out.append(f"  score {r['score']:+.3f} | agreement {r['agreement']*100:.0f}%"
               f" | conflict {r['conflict']*100:.0f}% | votes {r['n_votes']}"
               f" ({r['bulls']} bull / {r['bears']} bear)")
    out.append(f"  regime {r['regime']} (grade {r['grade']}) · M1 {r['m1']} · M2 {r['m2']}"
               f" · ATR1h {r['atr_h1']:,.2f}" if r["atr_h1"] else
               f"  regime {r['regime']} · M1 {r['m1']} · M2 {r['m2']}")
    if r.get("regime_note"):
        out.append(f"    {r['regime_note']}")
    out.append(f"  timeframes: cvd {r['tf_ok']['cvd']} | orderflow {r['tf_ok']['of']}")
    out.append(f"  heat configs ok: {r['heat']['configs_ok']}/6 "
               f"(3 models x 2 ranges)")

    out.append("\n  FAMILY CONSENSUS (the 360 view — one line per layer family):")
    for name, f in sorted(r["families"].items(), key=lambda x: -abs(x[1]["dir"])):
        tag = "BULL" if f["dir"] > 0.05 else "BEAR" if f["dir"] < -0.05 else "FLAT"
        out.append(f"    {tag} {f['dir']:+.2f}  {name:<15} {f['bull']}b/{f['bear']}s of {f['n']}"
                   f"  consensus {f['consensus']*100:+.0f}%")
    out.append(f"    => families agreeing: {r['family_agreement']*100:.0f}%")

    out.append("\n  VOTES (direction x weight):")
    for v in sorted(r["votes"], key=lambda x: -abs(x["dir"] * x["weight"])):
        tag = "BULL" if v["dir"] > 0 else "BEAR"
        out.append(f"    {tag} {v['dir']:+.2f} w{v['weight']:.1f}  {v['layer']:<18} {v['why']}")

    out.append("\n  LIQUIDATION FUEL (multi-model consensus, n>=2 models):")
    for side, rows in (("ABOVE", r["heat"]["above"][:6]), ("BELOW", r["heat"]["below"][:6])):
        for w in rows:
            if w["n_models"] >= 2:
                d = (w["px"] / r["last"] - 1) * 100
                out.append(f"    {side} {w['px']:>12,.4f}  {d:+6.2f}%  "
                           f"${w['usd_max']/1e6:>8,.1f}M  {w['n_models']}/3 models {w['models']}")

    p = r["plan"]
    if p.get("entry"):
        out.append(f"\n  PLAN  entry {p['entry']:,.4f}  stop {p['stop']:,.4f} "
                   f"({p['stop_atr']:.2f} ATR)  target {p['target']:,.4f} "
                   f"({p['target_atr']:.2f} ATR)  R:R {p['rr']:.2f}")
        if p.get("stop_fuel"):
            sf = p["stop_fuel"]
            out.append(f"    ⚠ stop sits INSIDE fuel cluster {sf['px']:,.4f} "
                       f"(${sf['usd_max']/1e6:,.1f}M, {sf['n_models']}/3 models)")

    if r["vetoes"]:
        out.append("\n  VETOES:")
        for v in r["vetoes"]:
            out.append(f"    ✗ {v}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", nargs="*", default=["BTC"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    results = []
    for sym in a.sym:
        t0 = time.time()
        L = pull_all(sym)
        r = fuse(sym, L)
        r["elapsed_s"] = round(time.time() - t0, 1)
        results.append(r)
        if not a.quiet and not a.json:
            print(render(r))
        print(f"  [{sym}] pulled in {r['elapsed_s']}s", file=sys.stderr)

    if a.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    # ranked summary
    tradable = [r for r in results if not r.get("error") and r["verdict"] != "NO TRADE"]
    print(f"\n{'#'*96}")
    print("RANKED SUMMARY")
    print(f"{'#'*96}")
    print(f"{'SYM':<6}{'VERDICT':<10}{'CONV':>6}{'AGREE':>7}{'CONFL':>7}{'SCORE':>8}"
          f"{'ENTRY':>12}{'STOP':>12}{'TARGET':>12}{'RR':>6}")
    print("-" * 96)
    for r in sorted(results, key=lambda x: -(x.get("conviction") or 0)):
        if r.get("error"):
            print(f"{r['sym']:<6}ERROR")
            continue
        p = r["plan"]
        print(f"{r['sym']:<6}{r['verdict']:<10}{r['conviction']:>6.0f}"
              f"{r['agreement']*100:>6.0f}%{r['conflict']*100:>6.0f}%{r['score']:>+8.3f}"
              f"{(f'{p[chr(101)+chr(110)+chr(116)+chr(114)+chr(121)]:,.4f}' if p.get('entry') else '—'):>12}"
              f"{(f'{p[chr(115)+chr(116)+chr(111)+chr(112)]:,.4f}' if p.get('stop') else '—'):>12}"
              f"{(f'{p[chr(116)+chr(97)+chr(114)+chr(103)+chr(101)+chr(116)]:,.4f}' if p.get('target') else '—'):>12}"
              f"{(f'{p[chr(114)+chr(114)]:.2f}' if p.get('rr') else '—'):>6}")
    print("-" * 96)
    print(f"tradable after vetoes: {len(tradable)}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
