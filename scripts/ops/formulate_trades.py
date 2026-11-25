#!/usr/bin/env python3
"""Formulate desk trades from the freshest telemetry JSON, per
desk/LLM_TRADE_FORMULATION_PLAYBOOK.md, then apply the canonical desk gates
from desk/CANONICAL_MASTER_SPECIFICATION.md.

Two layers are applied, in this order:

  LAYER 1 - PLAYBOOK (LLM_TRADE_FORMULATION_PLAYBOOK.md)
      §2 hard trend gate, §3 setups A-E, §4 confluence 0-10, §5 risk,
      §6 exact JSON verdict shape, §8 honesty rules.
      The playbook is an LLM-facing document and does NOT contain the desk's
      canonical risk gates.

  LAYER 2 - CANONICAL GATES (CANONICAL_MASTER_SPECIFICATION.md)
      GATE-01 auction clearance, GATE-02 Vector B, GATE-03 LSR magnet,
      GATE-04 spread drag, GATE-07 $6.00 stressed per-trade cap,
      PIT-16 cold-zone SL, PIT-18/22 spread-to-stop, K-4/P5-02 portfolio.

A formulation is only actionable if it survives BOTH layers.

DATA SOURCE: data/telemetry/assets/<ASSET>.json (persisted terminal telemetry)
plus data/telemetry/footprint/<ASSET>/summary_7d.json for the 15m bar series.

KNOWN DATA GAPS (handled explicitly, never silently):
  * The m5 EMA-233 that the playbook's tracker gate is built on is NOT
    persisted. The gate is computed from the m15 EMA-233 and cross-checked
    against narrative_engine.regime/direction; disagreements are recorded.
  * The per-price footprint grid (fp.b / fp.s) is not persisted and the
    persisted bars carry buy_vol/sell_vol = 0, so top-3-row dominance_ratio
    is unavailable for 237 of 252 assets. The +3 confluence credit of §4 is
    therefore NOT awarded unless dominance is actually computable.
  * "finished auction" strikes use the bar close-back-beyond-level test plus
    the persisted finished_high/finished_low flag when present (10 assets).

Writes:
  desk/audit_data/trade_formulations.json   per-asset verdicts (§6 shape)
  desk/audit_data/trade_formulations.csv    flat table for triage
"""
import csv, glob, json, os, sys, time
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pullback_backtest as pb

ROOT = os.path.dirname(os.path.dirname(HERE))
ASSETS = os.path.join(ROOT, 'data', 'telemetry', 'assets')
FP = os.path.join(ROOT, 'data', 'telemetry', 'footprint')
OUT = os.path.join(ROOT, 'desk', 'audit_data')
BOOK = os.path.join(ROOT, 'desk', 'audit_data', 'live_book_r24.json')

# ---- canonical constants (CANONICAL_MASTER_SPECIFICATION.md) ----
G1_CUSHION_PCT = 0.5          # GATE-01: SL clearance from Day POC and Day VWAP
G2_STRENGTH = 60.0            # desk-operative Vector B hazard threshold
G2_USD = 2_000_000.0
G2_STRICT = 40.0              # PIT-16 high-density definition
G3_LSR = 2.5                  # GATE-03 LSR magnet threshold
G4_SPREAD_PCT = 0.15          # GATE-04 spread as % of price
G4_SPREAD_OVER_STOP = 0.20    # GATE-04 / PIT-18
STRESS = 1.20                 # K-4 1.20x adverse stop scenario
CAP_PER_TRADE = 6.00          # K-4 binding cap (bridge enforces $6.00, erratum A-24)
PIT22_SPREAD_X = 5.0          # PIT-22 stricter 5x PIT-18
PIT22_CRYPTO_MIN_PCT = 1.5    # PIT-22 crypto stop >= 1.5% of entry
PLAYBOOK_MAX_STOP_PCT = 1.0   # §5 structural stop must fit inside 1.0%
PLAYBOOK_MIN_RR = 2.0         # §5 minimum R at T2
GRADE_A_MIN = 7
GRADE_B_MIN = 5


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ts_to_ms(ts):
    """Normalise a persisted epoch to milliseconds."""
    if ts is None:
        return None
    return ts * 1000 if ts < 1e11 else ts


def epoch_to_iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------- loaders
def load_asset(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def liq_levels(d):
    """Union of the 3d/7d/30d key levels. The windows are cumulative snapshots
    (measured: 53/77 assets have nested 3d subset 7d subset 30d price sets)."""
    cl = ((d.get('liquidations') or {}).get('clusters') or {})
    out = {}
    for side in ('above', 'below'):
        seen = {}
        for w in ('3d', '7d', '30d'):
            st = ((cl.get(w) or {}).get('stats') or {}); k = st.get('keyLevels') or {}
            for x in (k.get(side) or []):
                if x.get('price') is None:
                    continue
                key = round(float(x['price']), 10)
                u = float(x.get('usd') or 0)
                if key not in seen or u > seen[key]['usd']:
                    seen[key] = dict(price=float(x['price']), strength=float(x.get('strength') or 0),
                                     usd=u, lev=x.get('lev'), window=w)
        out[side] = sorted(seen.values(), key=lambda z: z['price'])
    tot = {}
    for w in ('3d', '7d', '30d'):
        st = ((cl.get(w) or {}).get('stats') or {})
        tot[w] = (st.get('totals') or {})
    return out, tot


def bars_15m(d, asset):
    """Full persisted 15m OHLC series.

    `footprint.history_7d.recent_bars_tail` is only the last 8 bars; the full
    300-bar (75h) series lives in data/telemetry/footprint/<ASSET>/summary_7d.json.
    The tail is used only to locate the newest bar timestamp."""
    rows = []
    p = os.path.join(FP, asset, 'summary_7d.json')
    if os.path.exists(p):
        try:
            rows = json.load(open(p)).get('rows') or []
        except Exception:
            rows = []
    if not rows:
        rows = (d.get('footprint') or {}).get('history_7d', {}).get('recent_bars_tail') or []
    # 16 of the persisted summary_7d.json files store `ts` in SECONDS while the
    # rest use milliseconds (AMZN, BLESS, COPPER, CRCL, FIL, INTC, KSM, MU,
    # PENGU, QTUM, SKHY, SOL, SOXL, SPCX, TSM, XLM). Normalise to ms.
    rows = [dict(b, ts=(b['ts'] * 1000 if b['ts'] < 1e11 else b['ts'])) for b in rows]
    return sorted(rows, key=lambda b: b['ts'])


def all_signals(d):
    sigs = []
    for bt in ('time', 'vol', 'range', 'all'):
        for s in ((d.get('footprint') or {}).get('signals') or {}).get(bt, {}).get('sets', []) or []:
            for sg in (s.get('sigs') or []):
                sigs.append(dict(sg, bar_type=bt, group=s.get('label') or bt))
    return sigs


def dom_ratio(bar, side):
    """top-3-row dominance ratio from a persisted bar, or None if the volume
    grid is absent (buy_vol/sell_vol are 0 in the persisted summaries)."""
    bv = float(bar.get('buy_vol') or 0); sv = float(bar.get('sell_vol') or 0)
    if bv <= 0 and sv <= 0:
        return None
    if side == 'short':
        return sv / bv if bv > 0 else float('inf')
    return bv / sv if sv > 0 else float('inf')


def atr_from_bars(bars, tf='h1', n=14):
    """ATR(n) from the persisted 15m bar series. tf='h1' resamples to hourly
    bars first (the desk's S10 convention uses the H1 ATR)."""
    if not bars:
        return None
    rows = sorted(bars, key=lambda b: b['ts'])
    if tf == 'h1':
        agg = {}
        for b in rows:
            hr = b['ts'] // 3600000 * 3600000
            a = agg.get(hr)
            if a is None:
                agg[hr] = dict(ts=hr, o=b['o'], h=b['h'], l=b['l'], c=b['c'])
            else:
                a['h'] = max(a['h'], b['h']); a['l'] = min(a['l'], b['l']); a['c'] = b['c']
        rows = [agg[k] for k in sorted(agg)]
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(1, len(rows)):
        pc = rows[i - 1]['c']
        trs.append(max(rows[i]['h'] - rows[i]['l'], abs(rows[i]['h'] - pc), abs(rows[i]['l'] - pc)))
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n



# ------------------------------------------------------- playbook layer
def evaluate_playbook(d, asset):
    mt5 = d.get('mt5') or {}
    ms = d.get('market_structure') or {}
    if not mt5.get('mid') or not ms:
        return None
    price = float(mt5['mid'])
    mkt_last = float((ms.get('price') or {}).get('last') or price)
    # Data-integrity guard: the MT5 quote and the persisted footprint/auction
    # telemetry must live on the same price scale. COPPER.p quotes ~11818 while
    # its footprint auction sits at ~5.4 (a 2178x mismatch), so every level,
    # stop and target derived from it would be nonsense.
    for _lbl, _v in (('market_structure.price.last', mkt_last),
                     ('market_structure.emas.m15.e233', (ms.get('emas') or {}).get('m15', {}).get('e233'))):
        if _v:
            try:
                _r = price / float(_v)
            except (TypeError, ValueError):
                continue
            if _r < 0.5 or _r > 2.0:
                return dict(gate=None, setup=None, direction=None, score=0,
                            reason=f"data-integrity: mt5.mid={price:.6g} and {_lbl}={float(_v):.6g} "
                                   f"differ by {_r:.0f}x - the quote and the persisted telemetry are on "
                                   f"different price scales, so no level/stop/target can be derived",
                            price=price, mkt_last=mkt_last, ne=d.get('narrative_engine') or {},
                            evidence=[f"data-integrity SKIP: mt5.mid={price:.6g} vs {_lbl}={float(_v):.6g} "
                                      f"({_r:.0f}x scale mismatch)"],
                            scale_mismatch=True)
    emas = ms.get('emas') or {}
    m15 = emas.get('m15') or {}
    h1 = emas.get('h1') or {}
    if not m15.get('e233'):
        return None
    ev = []

    # --- §2 hard trend gate (m15 EMA-233 proxy; m5 not persisted) ---
    if price < float(m15['e233']):
        gate = 'short_only'
    elif price > float(m15['e233']):
        gate = 'long_only'
    else:
        gate = 'neutral'
    ne = d.get('narrative_engine') or {}
    ne_regime = ne.get('regime'); ne_dir = ne.get('direction')
    ev.append(f"market_structure.emas.m15.e233={float(m15['e233']):.6g} vs mt5.mid={price:.6g} "
              f"-> gate {gate} (m15 EMA-233 proxy for the playbook's m5 EMA-233)")
    if ne_regime or ne_dir:
        ev.append(f"narrative_engine.regime={ne_regime} direction={ne_dir} grade={ne.get('grade')} "
                  f"confidence={ne.get('confidence')}")
    gate_conflict = None
    if gate == 'short_only' and ne_dir in ('LONG',):
        gate_conflict = f"narrative_engine.direction={ne_dir} contradicts the {gate} gate"
    if gate == 'long_only' and ne_dir in ('SHORT',):
        gate_conflict = f"narrative_engine.direction={ne_dir} contradicts the {gate} gate"

    if gate == 'neutral':
        return dict(gate=gate, setup=None, direction=None, score=0, evidence=ev,
                    gate_conflict=gate_conflict, price=price, mkt_last=mkt_last,
                    ne=ne, reason='price sits on the m15 EMA-233 - no trade, wait for separation')

    direction = 'short' if gate == 'short_only' else 'long'
    lv, tot = liq_levels(d)
    side_ab = 'above' if direction == 'short' else 'below'
    levels = lv[side_ab]
    bars = bars_15m(d, asset)
    sigs = all_signals(d)
    bw = (d.get('book_walls') or {}).get('futures') or {}
    bwm15 = bw.get('m15') or {}
    walls = bwm15.get('walls') or []
    imb = bwm15.get('imbalance'); imb5 = bwm15.get('imbalance5mAgo')
    auc = (ms.get('auction') or {}).get('day') or {}
    vwap = (ms.get('vwap') or {}).get('vwap')
    last_bar_ts = max([b['ts'] for b in bars], default=0)

    # --- candidate setups ---
    setups = []

    # Setup A / E: key-level strike with the auction finishing against it.
    # short: bar high pierces a level ABOVE price and closes back BELOW it.
    # long : bar low pierces a level BELOW price and closes back ABOVE it.
    # §3 Setup A: a liquidation key level was STRUCK (price traded into it) and
    # the auction at the level finished against the strike. The per-price grid
    # is not persisted, so "the auction finished against" is proxied by the bar
    # CLOSE back on the trend side of the zone -- which the playbook lists as a
    # quality marker, not a precondition. The strike therefore requires the
    # level to have been traded into; close-back is recorded as a marker.
    # Search the last 24 bars (6h) so the most recent strike can always be
    # reported with its age. Per §3 a strike older than 3 bars is CONTEXT, not
    # an entry - it is still surfaced, but it does not qualify as Setup A.
    STRIKE_LOOKBACK = 24
    last_strike = None
    for i in range(len(bars) - 1, max(len(bars) - 1 - STRIKE_LOOKBACK, -1), -1):
        b = bars[i]
        age = len(bars) - 1 - i
        for L in levels:
            p = L['price']
            if direction == 'short':
                pierced = b['h'] >= p
                closed_back = b['c'] < p
            else:
                pierced = b['l'] <= p
                closed_back = b['c'] > p
            if pierced:
                last_strike = dict(bar=b, level=L, age=age, index=i, closed_back=closed_back)
                break
        if last_strike:
            break
    strike = last_strike if (last_strike and last_strike['age'] <= 3) else None
    if last_strike and not strike:
        ev.append(f"most recent key-level strike {last_strike['level']['price']:.6g} was "
                  f"{last_strike['age']} bars old (>3) - CONTEXT per §3, not an entry")
    if strike:
        dr = dom_ratio(strike['bar'], direction)
        fuel_top = sorted([L['usd'] for L in levels], reverse=True)
        top_tercile = strike['level']['usd'] >= (fuel_top[max(0, len(fuel_top) // 3 - 1)] if fuel_top else 0)
        setups.append(('A' if dr is None or dr < 3 else 'A', dict(
            kind='A', zone=strike['level']['price'], zone_usd=strike['level']['usd'],
            strength=strike['level']['strength'], age_bars=strike['age'],
            dominance=dr, bar_ts=strike['bar']['ts'],
            bar_high=strike['bar']['h'], bar_low=strike['bar']['l'], bar_close=strike['bar']['c'],
            closed_back=bool(strike['closed_back']),
            top_tercile=top_tercile,
            finished_flag=bool(strike['bar'].get('finished_high') or strike['bar'].get('finished_low')))))
        ev.append(f"Setup A: 15m bar {epoch_to_iso(strike['bar']['ts'])} (age {strike['age']} bars) "
                  f"pierced liq level {strike['level']['price']:.6g} and closed "
                  f"{'below' if direction=='short' else 'above'} it at {strike['bar']['c']:.6g} "
                  f"(strength {strike['level']['strength']}, est. ${strike['level']['usd']:,.0f}, "
                  f"window {strike['level']['window']})")
        if dr is None:
            ev.append("Setup A: dominance_ratio NOT computable - persisted bars carry buy_vol/sell_vol=0, "
                      "so the +3 §4 credit is not awarded")
        else:
            ev.append(f"Setup A: dominance_ratio={dr:.2f}")

    # Setup B: absorption / exhaustion signal near a key level or confirmed wall.
    zone = strike['level']['price'] if strike else None
    b_setup = None
    for sg in sigs:
        if sg.get('t') not in ('abs', 'exh'):
            continue
        if direction == 'short' and sg.get('side') != 'sell':
            continue
        if direction == 'long' and sg.get('side') != 'buy':
            continue
        p = sg.get('p')
        if p is None:
            continue
        ts = sg.get('ts')
        ts_ms = ts_to_ms(ts)
        age_min = (last_bar_ts - ts_ms) / 60000.0 if ts_ms else 999
        near_lvl = any(abs(p - L['price']) / L['price'] <= 0.003 for L in levels)
        near_wall = any(abs(p - w['price']) / p <= 0.003 for w in walls if w.get('price'))
        if (near_lvl or near_wall) and age_min <= 60:
            b_setup = dict(kind='B', price=p, ts=ts, age_min=age_min, t=sg['t'],
                           near_level=near_lvl, near_wall=near_wall, ratio=sg.get('r'))
            break
    if b_setup:
        setups.append(('B', b_setup))
        ev.append(f"Setup B: {b_setup['t']} signal @ {b_setup['price']:.6g} "
                  f"({b_setup['age_min']:.0f} min old, near_liq_level={b_setup['near_level']}, "
                  f"near_wall={b_setup['near_wall']}, r={b_setup['ratio']})")

    # Setup C: CVD divergence in the trade direction.
    c_setup = None
    for venue in ('perp', 'spot'):
        for tf in ('m15', 'h1'):
            for dv in (((d.get('cvd') or {}).get(venue) or {}).get(tf) or {}).get('divergences') or []:
                want = 'bear' if direction == 'short' else 'bull'
                if dv.get('type') != want:
                    continue
                ts = dv.get('ts')
                ts_ms = ts_to_ms(ts)
                age_min = (last_bar_ts - ts_ms) / 60000.0 if ts_ms else 999
                if age_min <= 240:
                    c_setup = dict(kind='C', venue=venue, tf=tf, age_min=age_min,
                                   type=dv.get('type'), ret12=dv.get('ret12'), hit=dv.get('hit'))
                    break
            if c_setup:
                break
        if c_setup:
            break
    if c_setup:
        setups.append(('C', c_setup))
        ev.append(f"Setup C: {c_setup['type']} CVD divergence on {c_setup['venue']}/{c_setup['tf']} "
                  f"({c_setup['age_min']:.0f} min old, ret12={c_setup['ret12']}, hit={c_setup['hit']})")

    # Setup D: confirmed wall within 0.5% of price, age > 30 min, touched.
    d_setup = None
    for w in walls:
        wp = w.get('price')
        if not wp:
            continue
        if abs(wp - price) / price > 0.005:
            continue
        if (w.get('ageMs') or 0) < 30 * 60000:
            continue
        if not w.get('touched'):
            continue
        side = w.get('side')            # 'a' = ask (overhead), 'b' = bid (below)
        if direction == 'short' and side != 'a':
            continue
        if direction == 'long' and side != 'b':
            continue
        d_setup = dict(kind='D', price=wp, usd=w.get('usd'), age_min=(w.get('ageMs') or 0) / 60000.0,
                       dist_pct=w.get('distPct'), side=side)
        break
    if d_setup:
        setups.append(('D', d_setup))
        ev.append(f"Setup D: confirmed touched wall @ {d_setup['price']:.6g} "
                  f"(age {d_setup['age_min']:.0f} min, est. ${(d_setup['usd'] or 0):,.0f}, "
                  f"dist {d_setup['dist_pct']}%)")

    if not setups:
        return dict(gate=gate, setup=None, direction=direction, score=0, evidence=ev,
                    gate_conflict=gate_conflict, price=price, mkt_last=mkt_last, ne=ne,
                    reason='no Setup A-E trigger detected in the fresh telemetry')

    # pick the primary setup by kind priority A > E > B > C > D
    order = {'A': 0, 'E': 0, 'B': 1, 'C': 2, 'D': 3}
    setups.sort(key=lambda s: order.get(s[0], 9))
    primary_kind, primary = setups[0]

    # --- §4 confluence scoring ---
    score = 0
    credits = []
    dominance_credit = 0
    if primary_kind == 'A':
        if primary.get('dominance') is not None and primary['dominance'] >= 5 \
                and (primary['zone_usd'] >= 1_000_000 or primary['top_tercile']):
            score += 3
            dominance_credit = 3
            credits.append(f"+3 Setup A dominance_ratio={primary['dominance']:.2f}>=5 with fuel "
                           f"(zone est. ${primary['zone_usd']:,.0f})")
        else:
            ev.append("§4 +3 Setup-A credit NOT awarded: dominance_ratio is not computable from the "
                      "persisted telemetry (buy_vol/sell_vol are 0), so the §3 auction-quality leg "
                      "is UNVERIFIED")
        if primary.get('closed_back'):
            ev.append("§3 quality marker: strike bar CLOSED back on the trend side of the zone")
        else:
            ev.append("§3 quality marker MISSING: strike bar did not close back on the trend side "
                      "of the zone - the auction may still be running")
    # +2 zone coincides with day auction level
    zone_p = primary.get('zone') or primary.get('price')
    if zone_p and auc:
        for nm in ('poc', 'vah', 'val'):
            lv_ = auc.get(nm)
            if lv_ and abs(zone_p - lv_) / lv_ <= 0.0015:
                score += 2
                credits.append(f"+2 zone {zone_p:.6g} within 0.15% of market_structure.auction.day.{nm}={lv_:.6g}")
                break
    # +2 CVD divergence in the trade direction
    if any(k == 'C' for k, _ in setups):
        score += 2
        credits.append("+2 CVD divergence active in the trade direction")
    # +1 wall/whale beyond the zone (invalidation-side protection)
    prot = None
    if zone_p:
        for w in walls:
            wp = w.get('price')
            if not wp:
                continue
            if direction == 'short' and wp > zone_p and abs(wp - zone_p) / zone_p < 0.03:
                prot = w; break
            if direction == 'long' and wp < zone_p and abs(wp - zone_p) / zone_p < 0.03:
                prot = w; break
    if prot:
        score += 1
        credits.append(f"+1 resting wall @ {prot['price']:.6g} (est. ${(prot.get('usd') or 0):,.0f}) "
                       f"beyond the zone protects the stop")
    # +1 book imbalance agrees and moved
    if imb is not None and imb5 is not None:
        agrees = (imb < -0.05) if direction == 'short' else (imb > 0.05)
        moved = (imb < imb5) if direction == 'short' else (imb > imb5)
        if agrees and moved:
            score += 1
            credits.append(f"+1 book_walls.futures.m15.imbalance={imb:.4f} agrees with the trade "
                           f"and moved from imbalance5mAgo={imb5:.4f}")
        else:
            ev.append(f"§4 imbalance credit not awarded: imbalance={imb:.4f}, 5mAgo={imb5:.4f}")
    # +1 absorption/exhaustion signal within 3 bars, direction agrees
    if any(k == 'B' for k, _ in setups):
        score += 1
        credits.append("+1 absorption/exhaustion signal within the 3-bar window, direction agrees")

    # EMA chop -> require >= 8
    e13 = float(m15.get('e13') or 0); e89 = float(m15.get('e89') or 0); e233 = float(m15['e233'])
    chop = not (min(e13, e89, e233) < price < max(e13, e89, e233)) or abs(e13 - e89) / e89 > 0.01
    need = 8 if chop else GRADE_A_MIN
    if chop:
        ev.append(f"§4 EMA chop detected on m15 (e13 {e13:.6g} / e89 {e89:.6g} / e233 {e233:.6g} "
                  f"vs price {price:.6g}) -> A-grade threshold raised to 8")

    # --- §5 risk ---
    entry = zone_p if zone_p else price
    struct = None
    if primary_kind == 'A':
        struct = primary['bar_high'] if direction == 'short' else primary['bar_low']
    elif primary_kind == 'B':
        struct = primary['price']
    elif primary_kind == 'D':
        struct = primary['price']
    if struct is None:
        struct = price
    # §5 stop buffer: "+ 0.5 * tick * 10" == 5 ticks beyond the structure.
    # (The strike bar pierced the zone, so for a short bar_high >= zone_price
    #  and for a long bar_low <= zone_price: using bar_high/bar_low is
    #  therefore identical to the playbook's max(zone_price, strike-bar high)
    #  / min(zone_price, strike-bar low).)
    tick = float(mt5.get('point') or 0) or 0.001
    buffer = 0.5 * tick * 10                    # = 5 ticks (§5)
    stop_struct = struct + buffer if direction == 'short' else struct - buffer
    # PIT-16: the stop must also sit strictly beyond the ENTIRE adverse-side
    # liquidation shelf. The protective stop is the further of the two.
    cold = None
    all_lv = lv['above'] + lv['below']
    # PIT-16 cold zone: clear the whole HIGH-DENSITY adverse band, i.e. every
    # adverse-side level at or above the 40% strength threshold, not merely the
    # outermost +/-4% key level. This is the rule the desk applies in practice
    # (desk/log.md R22: the ETH stop 1725.50 sits beyond the 1713.89-1715.39
    # 3d/7d/30d peak band, not beyond the outermost key level).
    if direction == 'short':
        adverse = [L for L in lv['above'] if L['price'] > entry]
        dense = [L['price'] for L in adverse if L['strength'] >= G2_STRICT]
        if dense:
            cold = max(dense) * 1.0015
        elif adverse:
            cold = max(L['price'] for L in adverse) * 1.0015
    else:
        adverse = [L for L in lv['below'] if L['price'] < entry]
        dense = [L['price'] for L in adverse if L['strength'] >= G2_STRICT]
        if dense:
            cold = min(dense) * 0.9985
        elif adverse:
            cold = min(L['price'] for L in adverse) * 0.9985
    stop = stop_struct if cold is None else (max(stop_struct, cold) if direction == 'short'
                                             else min(stop_struct, cold))
    stop_source = 'structure' if (cold is None or
                                   (direction == 'short' and stop_struct >= cold) or
                                   (direction == 'long' and stop_struct <= cold)) else 'cold-zone'
    stop_pct = abs(stop - entry) / entry * 100
    # §5 distinguishes two stops:
    #   * the STRUCTURAL stop (structure + 5 ticks) - "If that is further than
    #     1.0% away, the setup is invalid - structural stop does not fit."
    #   * the PIT-16 COLD-ZONE stop, which PIT-21 may require to sit wider
    #     still; that is a protective requirement, and PIT-21 makes it a veto
    #     unless the account can accommodate it (handled by GATE-07/portfolio).
    struct_pct = abs(stop_struct - entry) / entry * 100
    over_stop_limit = stop_pct > PLAYBOOK_MAX_STOP_PCT
    struct_stop_invalid = struct_pct > PLAYBOOK_MAX_STOP_PCT
    atr_h1 = atr_from_bars(bars, 'h1')
    atr_15 = atr_from_bars(bars, '15m')

    # targets: T1 = nearest same-side liq cluster in the trade direction;
    #          T2 = day POC/VAH/VAL extreme
    tgt_side = 'below' if direction == 'short' else 'above'
    t1 = None
    for L in lv[tgt_side]:
        if direction == 'short' and L['price'] < entry:
            t1 = L['price']; break
        if direction == 'long' and L['price'] > entry:
            t1 = L['price']; break
    t2 = None
    if auc:
        cands = [auc.get('val'), auc.get('poc'), auc.get('vah')]
        cands = [c for c in cands if c is not None]
        if direction == 'short':
            cands = [c for c in cands if c < entry]
            t2 = min(cands) if cands else None
        else:
            cands = [c for c in cands if c > entry]
            t2 = max(cands) if cands else None
    risk_pts = abs(stop - entry)
    if t1 is None:
        t1 = entry - PLAYBOOK_MIN_RR * risk_pts if direction == 'short' else entry + PLAYBOOK_MIN_RR * risk_pts
    if t2 is None:
        t2 = entry - PLAYBOOK_MIN_RR * risk_pts if direction == 'short' else entry + PLAYBOOK_MIN_RR * risk_pts
        t2_is_fallback = True
    else:
        t2_is_fallback = False
    rr = abs(t2 - entry) / risk_pts if risk_pts else 0
    rr_ok = rr >= PLAYBOOK_MIN_RR
    rr_note = (f"§5 minimum R at T2 is {PLAYBOOK_MIN_RR}; this formulation offers only {rr:.2f}R"
               if not rr_ok else f"§5 R at T2 = {rr:.2f}R (minimum {PLAYBOOK_MIN_RR})")
    if t2_is_fallback:
        rr_note += " | no day-auction level in the trade direction: T2 is the mechanical 2R fallback"
    ev.append(rr_note)

    return dict(gate=gate, setup=primary_kind, direction=direction, score=score,
                evidence=ev + credits, gate_conflict=gate_conflict, price=price,
                mkt_last=mkt_last, ne=ne, entry=entry, stop=stop, t1=t1, t2=t2,
                rr=rr, stop_pct=stop_pct, risk_pts=risk_pts, need=need, chop=chop,
                zone=zone_p, levels=levels, tot=tot, auc=auc, vwap=vwap,
                atr_h1=atr_h1, atr_15=atr_15, stop_source=stop_source,
                stop_struct=stop_struct, stop_cold=cold,
                over_stop_limit=over_stop_limit, struct_stop_invalid=struct_stop_invalid,
                struct_stop_pct=struct_pct, dominance_credit=dominance_credit,
                last_strike=last_strike, rr_ok=rr_ok, t2_is_fallback=t2_is_fallback,
                walls=walls, imb=imb, imb5=imb5, primary=primary, setups=setups)


# -------------------------------------------------- canonical gate layer
def apply_canonical_gates(form, d, asset, book, equity):
    """Layer 2: the desk's canonical risk gates from
    CANONICAL_MASTER_SPECIFICATION.md, on the formulated geometry."""
    g = dict(GATE01=None, GATE02=None, GATE03=None, GATE04=None, GATE07=None,
             PIT16=None, PIT22=None, notes=[], size_lots=None, risk_usd=None)
    mt5 = d.get('mt5') or {}
    sym = mt5.get('symbol')
    sp = pb.SPECS.get(sym)
    if sp is None:
        g['notes'].append(f"no MT5 contract spec for {sym} - cannot size")
        return g, None
    entry = form['entry']; stop = form['stop']
    # Spread source: the LIVE bid/ask carried in the asset JSON is fresher and
    # authoritative; the static pb.SPECS spread is a fallback for assets whose
    # quote is missing. (COPPER's static spread is 9x its live quote.)
    live_spread = None
    if mt5.get('bid') is not None and mt5.get('ask') is not None:
        live_spread = abs(mt5['ask'] - mt5['bid'])
    spread = live_spread if live_spread else sp['spread'] * sp['point']
    g['spread_source'] = 'live bid/ask' if live_spread else 'static SPEC'
    contract = sp['trade_contract_size']
    risk_pts = abs(stop - entry)
    if risk_pts <= 0:
        g['notes'].append('zero stop distance')
        return g, None

    # GATE-01: SL separated from Session/Day POC and Day VWAP by >= 0.5%
    # cushion OR 1.0x ATR (canonical: ">= 0.5% cushion (or 1.0x ATR)").
    # Each reference level independently satisfies (percentage cushion OR the
    # ATR alternative); both must clear.
    auc = form.get('auc') or {}; vwap = form.get('vwap')
    poc = auc.get('poc'); vw = vwap
    atr = form.get('atr_h1') or form.get('atr_15')
    if poc and vw:
        c1 = abs(stop - poc) / poc * 100
        c2 = abs(stop - vw) / vw * 100
        alt = None
        if atr:
            alt = (abs(stop - poc) >= atr, abs(stop - vw) >= atr)
        ok1 = (c1 >= G1_CUSHION_PCT) or (alt is not None and alt[0])
        ok2 = (c2 >= G1_CUSHION_PCT) or (alt is not None and alt[1])
        g['GATE01'] = 'PASS' if (ok1 and ok2) else 'FAIL'
        g['GATE01_cush_poc'] = round(c1, 3); g['GATE01_cush_vwap'] = round(c2, 3)
        g['GATE01_leg_poc'] = 'pct' if c1 >= G1_CUSHION_PCT else ('atr' if (alt and alt[0]) else 'none')
        g['GATE01_leg_vwap'] = 'pct' if c2 >= G1_CUSHION_PCT else ('atr' if (alt and alt[1]) else 'none')
        g['notes'].append(f"GATE-01: SL vs day POC {c1:.2f}%, vs day VWAP {c2:.2f}% "
                          f"(need >= {G1_CUSHION_PCT}% or 1.0x ATR; legs poc={g['GATE01_leg_poc']} "
                          f"vwap={g['GATE01_leg_vwap']})"
                          + (f"; 1.0x ATR alt (ATR {atr:.6g}): poc {alt[0]} vwap {alt[1]}"
                             if alt else "; no ATR available for the 1.0x-ATR alternative"))
    else:
        g['GATE01'] = 'BLOCKED'

    # GATE-02 / PIT-16: Vector B hazards and cold-zone SL
    lv = form.get('levels') or []
    levels_all = form.get('levels_all') or []
    haz = []
    for L in levels_all:
        p = L['price']
        inside = (entry < p < stop) if form['direction'] == 'short' else (stop < p < entry)
        if inside:
            haz.append(L)
    g['GATE02'] = 'PASS' if not [h for h in haz if (h['strength'] >= G2_STRENGTH or h['usd'] >= G2_USD)] else 'FAIL'
    g['GATE02_strict'] = 'PASS' if not [h for h in haz if h['strength'] >= G2_STRICT] else 'FAIL'
    g['hazards'] = haz
    hard_haz = [h for h in haz if (h['strength'] >= G2_STRENGTH or h['usd'] >= G2_USD)]
    if hard_haz:
        g['notes'].append("GATE-02: " + "; ".join(
            f"liq hazard pool {h['price']:.6g} (strength {h.get('strength')}, est. ${h.get('usd', 0):,.0f}) "
            f"inside the risk path" for h in hard_haz))
    elif haz:
        g['notes'].append("GATE-02: no hazard pool above the strength/fuel threshold inside the risk path "
                          "(weak pools present: " +
                          ", ".join(f"{h['price']:.6g} (str {h.get('strength')})" for h in haz) + ")")
    if levels_all:
        if form['direction'] == 'short':
            adverse = [L for L in levels_all if L['price'] > entry]
            dense = [L['price'] for L in adverse if L['strength'] >= G2_STRICT]
            outer = max(dense) if dense else (max(L['price'] for L in adverse) if adverse else None)
            g['PIT16'] = 'PASS' if (outer is not None and stop >= outer * 1.0015) else 'FAIL'
        else:
            adverse = [L for L in levels_all if L['price'] < entry]
            dense = [L['price'] for L in adverse if L['strength'] >= G2_STRICT]
            outer = min(dense) if dense else (min(L['price'] for L in adverse) if adverse else None)
            g['PIT16'] = 'PASS' if (outer is not None and stop <= outer * 0.9985) else 'FAIL'
        g['notes'].append(f"PIT-16: SL {stop:.6g} vs cold-zone boundary {outer:.6g} "
                          f"(high-density band edge)")
    else:
        g['PIT16'] = 'BLOCKED'

    # GATE-03: LSR magnet
    tot = form.get('tot') or {}
    s7 = (tot.get('7d') or {}).get('shorts'); l7 = (tot.get('7d') or {}).get('longs')
    lsr = (s7 / l7) if (s7 and l7) else None
    g['lsr'] = round(lsr, 2) if lsr else None
    if form['direction'] == 'long':
        g['GATE03'] = 'PASS'
    elif lsr is None:
        g['GATE03'] = 'BLOCKED'
        g['notes'].append("GATE-03: BLOCKED - no 7d long/short liquidation totals, so the LSR magnet "
                          "ratio cannot be computed from the persisted telemetry")
    else:
        g['GATE03'] = 'BLOCKED' if lsr >= G3_LSR else 'PASS'
        g['notes'].append(f"GATE-03: 7d LSR = {lsr:.2f} vs magnet threshold {G3_LSR} - "
                          + ("passive limit entries into the magnet path are banned"
                             if lsr >= G3_LSR else "no LSR magnet in the path"))

    # GATE-04 / PIT-18 / PIT-22: spread drag
    sp_pct = spread / entry * 100
    over_stop = spread / risk_pts
    g['spread_pts'] = spread; g['spread_pct'] = round(sp_pct, 4)
    g['spread_over_stop'] = round(over_stop, 4)
    g['GATE04'] = 'PASS' if (sp_pct < G4_SPREAD_PCT and over_stop <= G4_SPREAD_OVER_STOP) else 'FAIL'
    g['notes'].append(f"GATE-04: spread {spread:.6g} ({g['spread_source']}) = {sp_pct:.4f}% of entry "
                      f"(need < {G4_SPREAD_PCT}%), {over_stop:.2f}x the stop distance "
                      f"(need <= {G4_SPREAD_OVER_STOP}x)")
    is_crypto = 'crypto' in str(mt5.get('category') or '').lower()
    g['is_crypto'] = is_crypto
    g['PIT22'] = 'PASS' if (risk_pts >= PIT22_SPREAD_X * spread and
                            (not is_crypto or risk_pts / entry * 100 >= PIT22_CRYPTO_MIN_PCT)) else 'FAIL'
    g['notes'].append(f"PIT-22: stop distance {risk_pts:.6g} vs {PIT22_SPREAD_X}x spread "
                      f"({PIT22_SPREAD_X*spread:.6g}) and "
                      f"{'crypto' if is_crypto else 'non-crypto'} minimum stop "
                      f"{PIT22_CRYPTO_MIN_PCT if is_crypto else 0}% of entry")

    # GATE-07 + K-4 sizing: nominal risk <= $5.00 so that 1.20x stressed <= $6.00
    max_nominal = CAP_PER_TRADE / STRESS
    lot_cap = max_nominal / (contract * risk_pts)
    step = sp['volume_step']; vmin = sp['volume_min']
    lots = (int(lot_cap / step) * step) if step else lot_cap
    if lots < vmin - 1e-12:
        g['GATE07'] = 'FAIL'
        g['notes'].append(f"GATE-07: minimum quantity {vmin} lot exceeds the cap "
                          f"(lot cap {lot_cap:.4f}) - veto")
        lots = vmin
    else:
        risk_usd = lots * contract * risk_pts
        g['size_lots'] = round(lots, 4)
        g['risk_usd'] = round(risk_usd, 2)
        g['risk_usd_stressed'] = round(risk_usd * STRESS, 2)
        g['GATE07'] = 'PASS' if risk_usd * STRESS <= CAP_PER_TRADE + 1e-9 else 'FAIL'
        g['notes'].append(f"GATE-07: {lots} lots, nominal ${risk_usd:.2f}, stressed 1.20x ${risk_usd*STRESS:.2f} "
                          f"(cap ${CAP_PER_TRADE:.2f})")
    return g, sp


def portfolio_check(form, sp, book, equity, g):
    """K-4 / P5-02 portfolio capacity: count pending orders, not just fills."""
    p = dict(ok=True, reasons=[], owned=0, owned_crypto=0, pending=0, headroom=None)
    opens = [t for t in book['tickets'] if t['state'] == 'OPEN']
    pends = [t for t in book['tickets'] if t['state'] == 'PENDING']
    p['owned'] = len(opens); p['owned_crypto'] = sum(1 for t in opens if t.get('asset_class') == 'crypto')
    p['pending'] = len(pends)
    sym = None
    if sp is not None:
        sym = sp['name']
    p['own_symbol'] = any(t['symbol'] == sym for t in opens)
    if p['own_symbol']:
        p['ok'] = False
        p['reasons'].append(f"desk already owns {sym} - K-4 forbids a second position in the same symbol")
    if p['owned'] + 1 > book['k4_max_owned_positions']:
        p['ok'] = False
        p['reasons'].append(f"owned positions would go {p['owned']}->{p['owned']+1}, "
                            f"K-4 cap {book['k4_max_owned_positions']}")
    is_crypto = g.get('is_crypto')
    if is_crypto and p['owned_crypto'] + 1 > book['k4_max_owned_crypto']:
        p['ok'] = False
        p['reasons'].append(f"owned crypto positions would go {p['owned_crypto']}->{p['owned_crypto']+1}, "
                            f"K-4 cap {book['k4_max_owned_crypto']}")
    # heat: open + pending (incl. this one) stressed
    # For an OPEN position the remaining risk is measured from the CURRENT stop.
    # A ratcheted stop that is already on the profit side (BNB: short 560.90
    # with the stop at 554.50) leaves $0 of downside risk -- the $1.22 is LOCKED
    # PROFIT, not risk.
    heat = 0.0
    for t in opens:
        r = t.get('remaining_risk_usd')
        if r is None:
            r = t.get('desk_risk_usd') or 0.0
            if t.get('sl_original'):
                r = t.get('desk_risk_usd_original') or r
        heat += r * STRESS
    for t in pends:
        heat += (t.get('desk_risk_usd') or 0.0) * STRESS
    heat += (g.get('risk_usd') or 0.0) * STRESS
    p['heat_stressed'] = round(heat, 2)
    p['heat_pct'] = round(100 * heat / equity, 3)
    p['headroom'] = round(book['risk_cap_usd'] - heat, 2)
    if heat > book['risk_cap_usd']:
        p['ok'] = False
        p['reasons'].append(f"stressed heat ${heat:.2f} exceeds the 1.00% cap ${book['risk_cap_usd']:.2f}")
    return p


# ---------------------------------------------------------------- main
def main():
    book = json.load(open(BOOK))
    equity = book['equity_usd']
    results = []
    files = sorted(glob.glob(os.path.join(ASSETS, '*.json')))
    for path in files:
        d = load_asset(path)
        if not d:
            continue
        asset = os.path.basename(path)[:-len('.json')]
        form = evaluate_playbook(d, asset)
        if form is None:
            continue
        ev = form['evidence']
        gate_conflict = form.get('gate_conflict')
        # the playbook gate is a hard filter: a conflicting narrative direction
        # does not override it (§8 rule 4)
        if form['setup'] is None:
            results.append(dict(sym=asset, symbol=(d.get('mt5') or {}).get('symbol'),
                                action='no-trade', direction=None, grade=None, score=form['score'],
                                setup=None, reason=form.get('reason'), form=form, gates=None, pf=None))
            continue
        grade = 'A' if form['score'] >= form['need'] else 'B'
        # The canonical gates are evaluated for EVERY asset that has a setup, so
        # the triage artifacts show which gates would also block a watch-tier
        # name (not just the score>=5 shortlist).
        lv, tot = liq_levels(d)
        form['levels_all'] = lv['above'] + lv['below']
        form['levels'] = lv['above'] if form['direction'] == 'short' else lv['below']
        g, sp = apply_canonical_gates(form, d, asset, book, equity)
        pf = portfolio_check(form, sp, book, equity, g) if sp else None
        if form['score'] < GRADE_B_MIN:
            results.append(dict(sym=asset, symbol=(d.get('mt5') or {}).get('symbol'),
                                action='no-trade', direction=None, grade=None, score=form['score'],
                                setup=form['setup'],
                                reason=f"confluence {form['score']} < 5 (grade threshold) - report as watch",
                                form=form, gates=g, pf=pf))
            continue
        fails = [k for k in ('GATE01', 'GATE02', 'GATE03', 'GATE04', 'GATE07', 'PIT16', 'PIT22')
                 if g.get(k) in ('FAIL', 'BLOCKED')]
        # a blocked gate is a coverage gap, a fail is a veto
        veto = [k for k in fails if g.get(k) == 'FAIL']
        blocked = [k for k in fails if g.get(k) == 'BLOCKED']
        action = 'trade'
        reason = None
        if not form.get('rr_ok', True):
            action = 'no-trade'
            reason = f"§5 R at T2 is {form['rr']:.2f}R, below the {PLAYBOOK_MIN_RR} minimum"
        elif veto:
            action = 'no-trade'
            reason = f"canonical gate veto: {', '.join(veto)}"
        elif blocked:
            action = 'watch'
            reason = f"unresolved coverage gap: {', '.join(blocked)}"
        elif pf and not pf['ok']:
            action = 'no-trade'
            reason = "K-4/P5-02 portfolio: " + "; ".join(pf['reasons'])
        elif form.get('struct_stop_invalid'):
            action = 'no-trade'
            reason = (f"§5 structural stop {form['struct_stop_pct']:.2f}% from entry exceeds the "
                      f"playbook 1.0% limit - structural stop does not fit, setup is invalid")
        elif form.get('over_stop_limit'):
            action = 'watch'
            reason = (f"PIT-16 cold-zone stop {form['stop_pct']:.2f}% from entry exceeds the playbook "
                      f"1.0% limit (structural stop was {form.get('struct_stop_pct', 0):.2f}%) - "
                      f"PIT-21 requires explicit operator acceptance of the wider cold-zone stop")
        else:
            action = 'trade'
        if action == 'trade':
            size_pct = 100 * (g.get('risk_usd') or 0) / equity
            if grade == 'B' and size_pct > 0.5:
                action = 'watch'
                reason = "B-grade requires <= 0.5% of equity at risk"
        results.append(dict(sym=asset, symbol=(d.get('mt5') or {}).get('symbol'),
                            action=action, direction=form['direction'], grade=grade,
                            score=form['score'], setup=form['setup'], reason=reason,
                            form=form, gates=g, pf=pf))

    # write artifacts
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'trade_formulations.csv'), 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['sym', 'symbol', 'action', 'direction', 'grade', 'confluence', 'setup',
                    'regime_gate', 'entry', 'stop', 't1', 't2', 'rr', 'stop_pct',
                    'lots', 'risk_usd', 'risk_usd_stressed', 'size_pct',
                    'GATE01', 'GATE02', 'GATE03', 'GATE04', 'GATE07', 'PIT16', 'PIT22',
                    'lsr', 'spread_pct', 'reason'])
        for r in sorted(results, key=lambda x: (-(x['score'] or 0), x['sym'])):
            f = r['form']; g = r.get('gates') or {}; pf = r.get('pf') or {}
            w.writerow([r['sym'], r['symbol'], r['action'], r['direction'], r['grade'], r['score'],
                        r['setup'], f.get('gate'), f.get('entry'), f.get('stop'), f.get('t1'),
                        f.get('t2'), round(f['rr'], 3) if f.get('rr') else None,
                        round(f['stop_pct'], 3) if f.get('stop_pct') else None,
                        g.get('size_lots'), g.get('risk_usd'), g.get('risk_usd_stressed'),
                        round(100 * (g.get('risk_usd') or 0) / equity, 3) if g.get('risk_usd') else None,
                        g.get('GATE01'), g.get('GATE02'), g.get('GATE03'), g.get('GATE04'),
                        g.get('GATE07'), g.get('PIT16'), g.get('PIT22'), g.get('lsr'),
                        g.get('spread_pct'), r.get('reason') or ''])

    payload = []
    for r in results:
        f = r['form']; g = r.get('gates') or {}
        # evidence: the scored lines plus the canonical gate diagnostics
        ev = list(f.get('evidence') or [])
        for n in (g.get('notes') or []):
            if n not in ev:
                ev.append(n)
        payload.append(dict(
            sym=r['sym'], asOf=f.get('as_of') or now_iso(),
            regime=f.get('gate'), setup=r.get('setup'), action=r['action'],
            direction=r['direction'], grade=r['grade'], confluence=r['score'],
            entry=f.get('entry'), stop=f.get('stop'),
            targets=[f.get('t1'), f.get('t2')] if f.get('t1') else None,
            rr=round(f['rr'], 3) if f.get('rr') else None,
            sizePct=round(100 * (g.get('risk_usd') or 0) / equity, 3) if g.get('risk_usd') else 0,
            evidence=ev,
            invalidation=('opposite gate flip, zone fully traversed with closes beyond it, '
                          'or the protecting wall is pulled' if r['action'] == 'trade'
                          else (r.get('reason') or 'n/a - no position')),
            stale=[],
            canonical_gates={k: g.get(k) for k in ('GATE01', 'GATE02', 'GATE03', 'GATE04',
                                                  'GATE07', 'PIT16', 'PIT22', 'lsr')},
            risk_detail={**{k: g.get(k) for k in ('size_lots', 'risk_usd', 'risk_usd_stressed',
                                                  'spread_pts', 'spread_pct', 'spread_over_stop',
                                                  'spread_source')
                            if g.get(k) is not None},
                         **{k: f.get(k) for k in ('stop_pct', 'stop_source', 'atr_h1', 'atr_15',
                                                 't2_is_fallback')
                            if f.get(k) is not None}},
            portfolio=({'owned': pf['owned'], 'owned_crypto': pf['owned_crypto'],
                        'pending': pf['pending'], 'heat_stressed': pf.get('heat_stressed'),
                        'heat_pct': pf.get('heat_pct'), 'ok': pf.get('ok'),
                        'reasons': pf.get('reasons')} if (pf := r.get('pf')) else None),
        ))
    json.dump(payload, open(os.path.join(OUT, 'trade_formulations.json'), 'w'), indent=1)

    # console summary
    import collections
    print(f"assets evaluated      : {len(results)}")
    print(f"action counts         : {dict(collections.Counter(r['action'] for r in results))}")
    print(f"setup counts          : {dict(collections.Counter(r['setup'] for r in results if r['setup']))}")
    print(f"gate counts           : {dict(collections.Counter(r['form'].get('gate') for r in results))}")
    print()
    ranked = sorted([r for r in results if r['action'] in ('trade', 'watch')],
                    key=lambda x: (-(x['score'] or 0), x['sym']))
    print(f"{'sym':8s} {'act':7s} {'dir':6s} {'gr':2s} {'sc':3s} {'setup':5s} {'entry':>12s} "
          f"{'stop':>12s} {'rr':>5s} {'risk$':>6s} gates")
    for r in ranked:
        f = r['form']; g = r.get('gates') or {}
        gates = ",".join(f"{k.replace('GATE','G').replace('PIT','P')}:{g.get(k)}" for k in
                         ('GATE01', 'GATE02', 'GATE03', 'GATE04', 'GATE07', 'PIT16', 'PIT22'))
        print(f"{r['sym']:8s} {r['action']:7s} {str(r['direction']):6s} {str(r['grade']):2s} "
              f"{r['score']:3d} {str(r['setup']):5s} "
              f"{(f.get('entry') or 0):12.6g} {(f.get('stop') or 0):12.6g} "
              f"{(f.get('rr') or 0):5.2f} {(g.get('risk_usd') or 0):6.2f} {gates}")
    print()
    n_trade = sum(1 for r in results if r['action'] == 'trade')
    print(f"ACTIONABLE FORMULATIONS: {n_trade}")
    for r in results:
        if r['action'] in ('trade', 'watch'):
            print(f"  {r['sym']}: {r['action']} - {r.get('reason') or 'all gates pass'}")


if __name__ == '__main__':
    main()
