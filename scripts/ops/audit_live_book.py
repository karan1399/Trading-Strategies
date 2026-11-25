#!/usr/bin/env python3
"""Audit the live R18 MT5 book against the canonical gates.

Reads the ticket facts from desk/audit_data/live_book_r18.json and the
telemetry from data/telemetry/assets/*.json, then evaluates every ticket with
its ACTUAL geometry (entry -> SL -> TP) rather than the candidate geometry
used by build_setup_matrix.py.

Writes desk/audit_data/live_book_audit.csv
"""
import json, os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pullback_backtest as pb

ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, 'desk', 'audit_data')
ASSETS = os.path.join(ROOT, 'data', 'telemetry', 'assets')

G2_STRENGTH = 60.0
G2_USD = 2_000_000.0
G2_STRICT = 40.0


def load_asset(name):
    p = os.path.join(ASSETS, name + '.json')
    return json.load(open(p)) if os.path.exists(p) else None


def union_levels(d, side):
    cl = ((d.get('liquidations') or {}).get('clusters') or {})
    seen = {}
    for w in ('3d', '7d', '30d'):
        st = ((cl.get(w) or {}).get('stats') or {}); k = st.get('keyLevels') or {}
        for x in (k.get(side) or []):
            if x.get('price') is None:
                continue
            key = round(float(x['price']), 10)
            u = float(x.get('usd') or 0)
            if key not in seen or u > seen[key][2]:
                seen[key] = (float(x['price']), float(x.get('strength') or 0), u, w)
    return sorted(seen.values(), key=lambda x: x[0])


def main():
    book = json.load(open(os.path.join(OUT, 'live_book_r18.json')))
    cap = book['stressed_risk_cap_usd_per_trade']; mult = book['stress_multiplier']
    rows = []
    for t in book['tickets']:
        sp = pb.SPECS.get(t['symbol'])
        a = load_asset(t['asset'])
        r = dict(ticket=t['ticket'], asset=t['asset'], symbol=t['symbol'], state=t['state'],
                 side=t['side'], lots=t['lots'], entry=t['entry'], sl=t['sl'], tp=t['tp'])
        if sp is None or a is None:
            rows.append(r); continue
        ratio = (a['mt5']['mid'] / a['market_structure']['price']['last'])
        entry = t['entry']; sl = t['sl']; tp = t['tp']
        # GATE-01/02/04, PIT-16/18/22 are ENTRY-TIME admission gates. If the
        # stop was ratcheted after the fill, the ORIGINAL stop is the geometry
        # that was admitted; the ratcheted level is reported separately.
        sl_entry = t.get('sl_original', t['sl'])
        r['sl_current'] = sl; r['sl_entry'] = sl_entry
        r['sl_ratcheted'] = bool(t.get('sl_original') is not None)
        stop_dist = abs(sl_entry - entry)
        risk = stop_dist * t['lots'] * sp['trade_contract_size']
        reward = abs(tp - entry) * t['lots'] * sp['trade_contract_size']
        spread = sp['spread'] * sp['point']
        r.update(contract=sp['trade_contract_size'], digits=sp['digits'], point=sp['point'],
                 spread_pts=spread, spread_pct=round(spread / entry * 100, 4),
                 stop_dist_pts=round(stop_dist, 6), risk_usd=round(risk, 2),
                 reward_usd=round(reward, 2), rr=round(reward / risk, 3) if risk else None,
                 risk_usd_stressed=round(risk * mult, 2))
        sl = sl_entry
        # ---- GATE-07: stressed risk cap $6.00 per trade ----
        r['GATE07'] = 'PASS' if risk * mult <= cap + 1e-9 else 'FAIL'
        # ---- GATE-04: spread < 0.15% and < 20% of stop distance ----
        r['PIT18_spread_over_stop'] = round(spread / stop_dist, 4) if stop_dist else None
        r['GATE04'] = 'PASS' if (r['spread_pct'] < 0.15 and spread / stop_dist <= 0.20) else 'FAIL'
        # ---- PIT-22: stop >= 5x spread (stricter PIT-18) and >= 1.5% on crypto CFD ----
        is_crypto = 'crypto' in str(a['mt5'].get('category') or '').lower()
        r['is_crypto_cfd'] = is_crypto
        r['PIT22'] = 'PASS' if (stop_dist >= 5 * spread and
                                (not is_crypto or stop_dist / entry * 100 >= 1.5)) else 'FAIL'
        # ---- GATE-01: SL separated from Day POC and Day VWAP by >= 0.5% ----
        ms = a['market_structure']
        poc = ms['auction']['day']['poc'] * ratio; vw = ms['vwap']['vwap'] * ratio
        r['day_poc'] = round(poc, 6); r['day_vwap'] = round(vw, 6)
        r['GATE01_poc_cush_pct'] = round(abs(sl - poc) / poc * 100, 3)
        r['GATE01_vwap_cush_pct'] = round(abs(sl - vw) / vw * 100, 3)
        r['GATE01'] = 'PASS' if (r['GATE01_poc_cush_pct'] >= 0.5 and r['GATE01_vwap_cush_pct'] >= 0.5) else 'FAIL'
        # ---- GATE-02 / PIT-16: Vector B hazards + cold-zone stop ----
        side_ab = 'above' if t['side'].startswith('SELL') else 'below'
        lv = union_levels(a, side_ab)
        haz = []
        for p, s, u, w in lv:
            inside = (entry < p * ratio < sl) if t['side'].startswith('SELL') else (sl < p * ratio < entry)
            if inside:
                haz.append(dict(window=w, price=round(p * ratio, 6), strength=s, usd=u))
        r['vector_b_levels'] = len(lv)
        r['haz_n'] = len(haz)
        r['haz_max_strength'] = max([h['strength'] for h in haz], default=0.0)
        r['haz_total_usd'] = round(sum(h['usd'] for h in haz), 2)
        r['hazards'] = json.dumps(haz)
        if not lv:
            r['GATE02_STRICT'] = 'BLOCKED'; r['GATE02'] = 'BLOCKED'
        else:
            r['GATE02_STRICT'] = 'PASS' if not [h for h in haz if h['strength'] >= G2_STRICT] else 'FAIL'
            r['GATE02'] = 'PASS' if not [h for h in haz if (h['strength'] >= G2_STRENGTH or h['usd'] >= G2_USD)] else 'FAIL'
        if lv:
            outer = lv[-1] if t['side'].startswith('SELL') else lv[0]
            r['shelf_outer_price'] = round(outer[0] * ratio, 6)
            r['PIT16'] = 'PASS' if (sl >= outer[0] * ratio * 1.0015 if t['side'].startswith('SELL')
                                    else sl <= outer[0] * ratio * 0.9985) else 'FAIL'
        else:
            r['PIT16'] = 'BLOCKED'
        # ---- GATE-03: LSR >= 2.5 bans passive limit entries into the magnet path ----
        cl = ((a.get('liquidations') or {}).get('clusters') or {})
        st7 = ((cl.get('7d') or {}).get('stats') or {}); tot7 = st7.get('totals') or {}
        lsr = (tot7.get('shorts') / tot7.get('longs')) if (tot7.get('shorts') and tot7.get('longs')) else None
        r['lsr'] = round(lsr, 2) if lsr else None
        if not t['side'].startswith('SELL'):
            r['GATE03'] = 'PASS'
        elif lsr is None:
            r['GATE03'] = 'BLOCKED'
        else:
            r['GATE03'] = 'BLOCKED' if lsr >= 2.5 else 'PASS'
        r['GATE10_btc_beta'] = None
        rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, 'live_book_audit.csv'), index=False)
    pd.set_option('display.width', 250)
    print(df[['ticket', 'asset', 'state', 'side', 'lots', 'entry', 'sl', 'tp',
              'risk_usd', 'risk_usd_stressed', 'rr', 'GATE01', 'GATE02', 'GATE02_STRICT',
              'GATE03', 'GATE04', 'GATE07', 'PIT16', 'PIT22', 'lsr', 'haz_n',
              'haz_max_strength', 'haz_total_usd']].to_string(index=False))
    return df


if __name__ == '__main__':
    main()
