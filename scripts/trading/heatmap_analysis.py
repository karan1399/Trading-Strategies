#!/usr/bin/env python3
"""
heatmap_analysis.py — mine the Coinglass liquidation-heatmap payload for trade
formulation.

WHY THIS EXISTS
---------------
The Liquidation Heat Map *tab* draws ~24 wall bars from the top-24 clusters.
The underlying `/api/cg/stopmap` payload carries far more:

  liqRows    ~14,500  (price, time) cells of raw liquidation notional
  columns     ~286    5-minute time slices, each with its own cell ladder
  cumulative  ~139    at every price: how much liq sits ABOVE vs BELOW it
  profile      ~140    time-integrated heat per price level
  candles      ~288    5-minute OHLC, aligned to the same grid

`cumulative` is the important one and the UI never plots it. It answers the
only question that matters for positioning:

    at price P, how much fuel is there above me vs below me?

That is a directional magnet map. This script turns it into a decision.

WHAT IT COMPUTES
----------------
  fuel_above / fuel_below   total liq notional resting above / below last price
  skew                      (above-below)/(above+below) — positive = squeeze risk
                            for shorts, i.e. upward magnet
  walls_above / walls_below the top clusters each side, with distance in ATR
  vacuum                    the largest gap with almost NO liq — price moves
                            fast through it, so it is not support/resistance
  cascade                   consecutive clusters that chain together (each
                            within 0.6x of the previous) — once price enters,
                            it accelerates all the way through
  magnet                    the single largest cluster, weighted by how close it
                            is — the price most likely to be visited
  asym_score                0-100 composite ranking of directional bias

The output is a ranked table plus per-symbol detail. Nothing here places,
amends or cancels an order. Arena proposes; Antigravity executes.

USAGE
  python3 scripts/trading/heatmap_analysis.py                      # majors, 24h
  python3 scripts/trading/heatmap_analysis.py --range 3d --sym BTC ETH
  python3 scripts/trading/heatmap_analysis.py --json               # machine read

NOTE ON RATE LIMITS
  Coinglass returns code 50001 ("upstream rate-limited") under burst. Space
  calls >=10s apart. This script enforces that by default (--pace).
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
DEFAULT_SYMS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "LINK"]


def fetch(sym: str, rng: str, model: str = "1", mode: str = "coin",
          timeout: int = 60) -> dict:
    url = (f"{BASE}/api/cg/stopmap?sym={urllib.parse.quote(sym)}"
           f"&model={model}&mode={mode}&range={rng}")
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


def atr_from_candles(candles: list[dict], n: int = 14) -> tuple[float | None, float | None]:
    """ATR from the heatmap's own 5-minute candles.

    The payload ships ONLY 5-minute OHLC, so the raw ATR is a 5-minute ATR —
    tiny (0.17 % on BTC) and useless for daily-scale distance work because a
    $16 M wall 1.4 % away reads as "8 ATR". Scale it to an hourly equivalent
    (sqrt(12) x 5m, the standard random-walk rescale) and quote THAT, so a
    reader comparing levels to ATR gets a number that means something.
    Returns (atr_5m, atr_1h).
    """
    if len(candles) < n + 1:
        return None, None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    if not trs:
        return None, None
    a5 = sum(trs[-n:]) / n
    return a5, a5 * math.sqrt(12)


def analyse(payload: dict, sym: str) -> dict | None:
    m = payload.get("model")
    if not payload.get("ok") or not m:
        return None

    ov = m.get("overview") or {}
    last = ov.get("lastPrice")
    if not last:
        return None

    atr5, atr = atr_from_candles(m.get("candles") or [])
    atr_pct = (atr / last * 100) if atr else None

    kl = m.get("keyLevels") or {}
    above = [w for w in (kl.get("above") or []) if w.get("price")]
    below = [w for w in (kl.get("below") or []) if w.get("price")]

    # ---- fuel above/below ----
    # DO NOT use `cumulative`: its `below` column is identically zero and its
    # maxima happen to equal overview.shorts / overview.longs, i.e. it is a
    # degenerate series, not a price-ordered running sum. Building a skew on it
    # produced +0.00 for every symbol (verified on BTC/ETH/SOL).
    # Use keyLevels, whose `usd` field IS notional and is what the UI prints.
    fuel_above = sum(float(w.get("usd") or 0) for w in
                     (kl.get("above") or []) if w.get("price"))
    fuel_below = sum(float(w.get("usd") or 0) for w in
                     (kl.get("below") or []) if w.get("price"))
    tot = fuel_above + fuel_below
    skew = ((fuel_above - fuel_below) / tot) if tot else 0.0

    # Corroborating dimensionless check: raw cells are leverage-weighted HEAT
    # units (they sum to ~186x overview.total, so they are NOT dollars). The
    # above/below RATIO is still unit-consistent and worth quoting separately.
    heat_above = heat_below = 0.0
    yax = m.get("yAxis") or []
    for col in (m.get("columns") or []):
        for cell in (col.get("cells") or []):
            try:
                idx, val = int(cell[0]), float(cell[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (0 <= idx < len(yax)):
                continue
            if yax[idx] > last:
                heat_above += val
            elif yax[idx] < last:
                heat_below += val
    heat_skew = ((heat_above - heat_below) / (heat_above + heat_below)
                 if (heat_above + heat_below) else 0.0)

    # ---- walls with ATR-normalised distance ----
    def decorate(rows, sign):
        out = []
        for w in rows:
            p = float(w["price"])
            dist = (p / last - 1) * 100
            out.append({
                "price": p,
                "usd": float(w.get("usd") or 0),
                "dist_pct": dist,
                "dist_atr": (abs(p - last) / atr) if atr else None,
                "sign": sign,
            })
        return sorted(out, key=lambda x: abs(x["dist_pct"]))

    wa = decorate(above, +1)
    wb = decorate(below, -1)

    # ---- cascade: clusters that chain, each close to the previous ----
    def cascade(rows):
        if len(rows) < 2:
            return rows, 0
        chain, best, n = [rows[0]], [rows[0]], 1
        for prev, cur in zip(rows, rows[1:]):
            gap_atr = (abs(cur["price"] - prev["price"]) / atr) if atr else 99
            if gap_atr <= 0.6:
                chain.append(cur)
                if len(chain) > len(best):
                    best = chain[:]
                n = max(n, len(chain))
            else:
                chain = [cur]
        return best, n

    cas_a, n_a = cascade(wa)
    cas_b, n_b = cascade(wb)

    # ---- vacuum: widest gap between consecutive key levels with low mass ----
    def vacuum(rows):
        if len(rows) < 2:
            return None
        span = sorted(rows, key=lambda x: x["price"])
        best = None
        for prev, cur in zip(span, span[1:]):
            gap = cur["price"] - prev["price"]
            mass = prev["usd"] + cur["usd"]
            score = gap / (mass / 1e6 + 1)  # wide + light = fast travel
            if best is None or score > best["score"]:
                best = {"from": prev["price"], "to": cur["price"],
                        "gap_pct": (cur["price"] / prev["price"] - 1) * 100,
                        "gap_atr": (gap / atr) if atr else None,
                        "score": score}
        return best

    vac = vacuum(wa + wb)

    # ---- magnet: largest cluster weighted by proximity ----
    magnet = None
    for w in wa + wb:
        w_score = w["usd"] / (abs(w["dist_pct"]) + 0.35)
        if magnet is None or w_score > magnet["score"]:
            magnet = {**w, "score": w_score}

    # ---- asym score 0-100, 50 = balanced ----
    parts = []
    if tot:
        parts.append(skew)
    if n_a or n_b:
        parts.append((n_a - n_b) / max(n_a + n_b, 1))
    if wa and wb:
        near_a = wa[0]["dist_atr"] or 9
        near_b = wb[0]["dist_atr"] or 9
        parts.append((near_b - near_a) / max(near_a + near_b, 0.01))
    raw = sum(parts) / len(parts) if parts else 0.0
    asym = max(0, min(100, 50 + raw * 50))

    return {
        "sym": sym,
        "last": last,
        "chg24h": ov.get("chgPct24h"),
        "atr": atr,
        "atr5": atr5,
        "atr_pct": atr_pct,
        "heat_skew": heat_skew,
        "longs": ov.get("longs"),
        "shorts": ov.get("shorts"),
        "long_share": (ov["longs"] / (ov["longs"] + ov["shorts"]) * 100)
                       if ov.get("longs") and ov.get("shorts") else None,
        "liq24h": ov.get("liq24h"),
        "largest": ov.get("largestLevel"),
        "fuel_above": fuel_above,
        "fuel_below": fuel_below,
        "skew": skew,
        "walls_above": wa[:5],
        "walls_below": wb[:5],
        "cascade_above": {"levels": cas_a, "n": n_a,
                          "depth_usd": sum(x["usd"] for x in cas_a)},
        "cascade_below": {"levels": cas_b, "n": n_b,
                          "depth_usd": sum(x["usd"] for x in cas_b)},
        "vacuum": vac,
        "magnet": magnet,
        "asym": asym,
        "cells": m.get("liqRows") or 0,   # an int count, not a list
        "columns": len(m.get("columns") or []),
    }


def usd(v) -> str:
    if not v:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:,.2f}B"
    if a >= 1e6:
        return f"${v/1e6:,.1f}M"
    if a >= 1e3:
        return f"${v/1e3:,.0f}k"
    return f"${v:,.0f}"


def atr_s(v) -> str:
    return f"{v:.2f}" if v is not None else "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", nargs="*", default=DEFAULT_SYMS)
    ap.add_argument("--range", default="24h")
    ap.add_argument("--model", default="1")
    ap.add_argument("--mode", default="coin")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--pace", type=float, default=10.0,
                    help="seconds between calls (Coinglass 50001 under burst)")
    a = ap.parse_args()

    rows = []
    for i, sym in enumerate(a.sym):
        if i and a.pace:
            time.sleep(a.pace)
        p = fetch(sym, a.range, a.model, a.mode)
        if not p.get("ok"):
            print(f"  {sym}: FAIL {str(p.get('error'))[:70]}", file=sys.stderr)
            continue
        r = analyse(p, sym)
        if r:
            rows.append(r)

    if a.json:
        print(json.dumps({"range": a.range, "rows": rows}, indent=2))
        return 0

    if not rows:
        print("no usable payloads", file=sys.stderr)
        return 1

    print(f"\nLIQUIDATION FUEL MAP · range {a.range} · model {a.model} · {a.mode}")
    print("=" * 118)
    print(f"{'SYM':<6}{'LAST':>11}{'ATR%':>7}{'FUEL ABOVE':>13}{'FUEL BELOW':>13}"
          f"{'SKEW':>8}{'NEAR↑':>10}{'NEAR↓':>10}{'ASYM':>7}  BIAS")
    print("-" * 118)
    for r in sorted(rows, key=lambda x: -abs(x["skew"])):
        na = r["walls_above"][0] if r["walls_above"] else None
        nb = r["walls_below"][0] if r["walls_below"] else None
        na_s = f"{na['dist_pct']:+.1f}%" if na else "—"
        nb_s = f"{nb['dist_pct']:+.1f}%" if nb else "—"
        bias = ("^ SQUEEZE" if r["skew"] > 0.10 else
                "v FLUSH" if r["skew"] < -0.10 else "· balanced")
        print(f"{r['sym']:<6}{r['last']:>11,.2f}{(r['atr_pct'] or 0):>7.2f}"
              f"{usd(r['fuel_above']):>13}{usd(r['fuel_below']):>13}"
              f"{r['skew']:>+8.2f}{na_s:>10}{nb_s:>10}"
              f"{r['asym']:>7.0f}  {bias}")
    print("=" * 118)

    for r in rows:
        print(f"\n── {r['sym']} @ {r['last']:,.2f}  ({r['chg24h']:+.2f}% 24h) "
              f"· ATR {atr_s(r['atr'])} ({atr_s(r['atr_pct'])}%)")
        if r["long_share"] is not None:
            print(f"   open liq map: longs {usd(r['longs'])} / shorts {usd(r['shorts'])}"
                  f"  → {r['long_share']:.1f}% long-weighted")
        l24 = r["liq24h"] or {}
        if l24:
            print(f"   realised 24h: longs liq'd {usd(l24.get('long'))}"
                  f" / shorts liq'd {usd(l24.get('short'))}")
        print(f"   fuel (top-24 walls, USD): above {usd(r['fuel_above'])}"
              f" | below {usd(r['fuel_below'])} | skew {r['skew']:+.2f}"
              f" | asym {r['asym']:.0f}/100")
        print(f"   heat (all cells, unitless): skew {r['heat_skew']:+.2f}"
              f"  <-- corroborates direction only, not dollars")

        for label, key in (("ABOVE ↑ (shorts to squeeze)", "walls_above"),
                           ("BELOW ↓ (longs to flush)", "walls_below")):
            print(f"   {label}")
            for w in r[key]:
                print(f"     {w['price']:>11,.2f}  {w['dist_pct']:+6.2f}%"
                      f"  {atr_s(w['dist_atr']):>5} ATR   {usd(w['usd']):>10}")

        ca, cb = r["cascade_above"], r["cascade_below"]
        if ca["n"] >= 2:
            print(f"   ⚡ cascade ABOVE: {ca['n']} chained levels "
                  f"{ca['levels'][0]['price']:,.0f}→{ca['levels'][-1]['price']:,.0f} "
                  f"= {usd(ca['depth_usd'])} of fuel in one run")
        if cb["n"] >= 2:
            print(f"   ⚡ cascade BELOW: {cb['n']} chained levels "
                  f"{cb['levels'][0]['price']:,.0f}→{cb['levels'][-1]['price']:,.0f} "
                  f"= {usd(cb['depth_usd'])} of fuel in one run")
        if r["vacuum"]:
            v = r["vacuum"]
            print(f"   ▢ vacuum {v['from']:,.0f}→{v['to']:,.0f} "
                  f"({v['gap_pct']:+.1f}%, {atr_s(v['gap_atr'])} ATR) — light, price travels fast")
        if r["magnet"]:
            g = r["magnet"]
            print(f"   ◎ magnet {g['price']:,.2f} ({g['dist_pct']:+.2f}%) "
                  f"{usd(g['usd'])} — largest cluster weighted by proximity")
        print(f"   payload: {r['cells']:,} liq cells across {r['columns']} columns")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
