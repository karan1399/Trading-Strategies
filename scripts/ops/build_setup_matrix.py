#!/usr/bin/env python3
"""Build the R19 setup matrix: costed backtest + per-candidate gate results.

Writes
  desk/audit_data/pullback_bt_results.csv
  desk/audit_data/setup_matrix.csv

GATE-02 is evaluated in both readings because the canonical specification and
the desk's operating log disagree:

  * Spec literal  [GATE-02] "Zero liquidation hazard pools permitted inside the
    risk path (P_entry -> P_SL)", where a hazard pool is a high-density cluster
    (>= 40% strength per [PIT-16]). Strict reading = zero pools of strength
    >= 40 inside Vector B.
  * Desk operative: desk/log.md records GATE-02 passing with pools present --
    e.g. "Vector B Risk Zone [9.45, 9.68]: 2 small clusters $160k ... zero
    clusters >= $5M", "Vector B Hazard = $1,596,072 USD (< $2.0M threshold)",
    "1 minor cluster at 9.589 ($173.6k USD, strength 59.8 < 60 threshold)". The
    operative thresholds are strength >= 60 OR notional >= $2,000,000.

Both are reported. The desks' own executed tickets (e.g. ETH #20496681,
entry 1695.50 / SL 1724.50 beyond the 1713.89-1720.76 shelf) only satisfy the
operative reading, which is why [PIT-16] ("SL strictly beyond the ENTIRE
3D/7D/30D shelf") and a literal zero-pool GATE-02 are mutually exclusive: any
stop placed beyond the whole shelf necessarily contains that shelf in
P_entry -> P_SL.
"""
import json, os, sys, glob
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pullback_backtest as pb

ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, 'desk', 'audit_data')
ASSETS = os.path.join(ROOT, 'data', 'telemetry', 'assets')

CFG = dict(rr=2.0, risk_mult=1.5, time_stop=8, atr_col='atrH')
SLIP = 0.10          # adverse slippage stress, in ATR units
G2_STRENGTH = 60.0   # desk operative threshold
G2_USD = 2_000_000.0 # desk operative threshold
G2_STRICT = 40.0     # [PIT-16] high-density definition


def asset(name):
    p = os.path.join(ASSETS, name + '.json')
    if not os.path.exists(p):
        return None
    return json.load(open(p))


def clusters(d):
    cl = ((d.get('liquidations') or {}).get('clusters') or {})
    out = {}
    for w in ('3d', '7d', '30d'):
        st = ((cl.get(w) or {}).get('stats') or {}); k = st.get('keyLevels') or {}
        nz = lambda l: [(float(x['price']), float(x.get('strength') or 0),
                         float(x.get('usd') or 0)) for x in (l or []) if x.get('price') is not None]
        out[w] = dict(above=nz(k.get('above')), below=nz(k.get('below')), last=st.get('last'),
                      shorts=(st.get('totals') or {}).get('shorts'),
                      longs=(st.get('totals') or {}).get('longs'))
    return out


def basis_ratio(d):
    ms = d['market_structure']; last = ms['price']['last']; mid = d['mt5']['mid']
    return (mid / last) if last else 1.0


def union_levels(cl, side):
    """Union of the 3d/7d/30d key levels on one side, de-duplicated by price.

    The windows are cumulative snapshots (measured: 53/77 assets have nested
    3d subset 7d subset 30d price sets and 67/77 have monotonically growing
    notionals), so the union is the operative 'entire multi-exchange shelf'.
    """
    seen = {}
    for w in ('3d', '7d', '30d'):
        for p, s, u in cl[w][side]:
            k = round(p, 10)
            if k not in seen or u > seen[k][2]:
                seen[k] = (p, s, u, w)
    return sorted(seen.values(), key=lambda x: x[0])


def gates(asset_name, side='short'):
    d = asset(asset_name)
    if d is None:
        return None
    if not d.get('mt5') or not d.get('market_structure'):
        return None
    ms = d['market_structure']; cl = clusters(d); ratio = basis_ratio(d)
    sp = pb.SPECS.get(d['mt5']['symbol'])
    if sp is None:
        return None
    last = d['mt5']['mid']
    atr15 = None; atr1h = None
    cpath = os.path.join(ROOT, 'historical_parquet', '15M', d['mt5']['symbol'] + '.parquet')
    if os.path.exists(cpath):
        dd = pb.load('15M', d['mt5']['symbol'])
        if dd is not None:
            atr15 = float(pb.atr_series(dd, 14).iloc[-1])
        hpath = os.path.join(ROOT, 'historical_parquet', '1H', d['mt5']['symbol'] + '.parquet')
        if os.path.exists(hpath):
            hh = pb.load('1H', d['mt5']['symbol'])
            if hh is not None:
                atr1h = float(pb.atr_series(hh, 14).iloc[-1])
    entry = ms['emas']['m15']['e89']
    side_ab = 'above' if side == 'short' else 'below'
    lv = union_levels(cl, side_ab)
    res = dict(asset=asset_name, symbol=d['mt5']['symbol'], side=side, last=last,
               entry_ema89=entry, atr15=atr15, atr1h=atr1h,
               basis_ratio=round(ratio, 5), spread_pts=sp['spread'] * sp['point'],
               contract=sp['trade_contract_size'], digits=sp['digits'])
    if not lv:
        res.update(PIT16='BLOCKED', GATE02='BLOCKED', GATE02_STRICT='BLOCKED',
                   terminal_cluster=None, hazards=[])
        return res
    # PIT-16 / cold zone: SL strictly beyond the ENTIRE shelf (outermost level)
    outer = lv[-1] if side == 'short' else lv[0]
    sl_buf = outer[0] * 1.0015
    atr_sl = (entry + 1.5 * atr1h) if (side == 'short' and atr1h) else None
    sl = max(sl_buf, atr_sl) if (side == 'short' and atr_sl) else sl_buf
    risk = abs(sl - entry)
    res['sl_coldzone'] = round(sl, res['digits']); res['risk_pts'] = round(risk, 6)
    res['terminal_cluster'] = (round(outer[0], 5), outer[1], outer[2])
    res['shelf_levels'] = len(lv)
    res['PIT16'] = 'PASS' if (sl >= outer[0] * 1.0015 if side == 'short' else sl <= outer[0] * 0.9985) else 'FAIL'
    # GATE-02 Vector B = (entry -> SL). Adverse-side pools only:
    # short -> short-liquidation pools above; long -> long-liquidation pools below.
    haz = []
    for p, s, u, w in lv:
        inside = (entry < p < sl) if side == 'short' else (sl < p < entry)
        if inside:
            haz.append((w, round(p * ratio, 6), s, u))
    res['hazards'] = haz
    res['haz_n'] = len(haz)
    res['haz_max_strength'] = max([h[2] for h in haz], default=0.0)
    res['haz_total_usd'] = round(sum(h[3] for h in haz), 2)
    strict_haz = [h for h in haz if h[2] >= G2_STRICT]
    res['GATE02_STRICT'] = 'PASS' if not strict_haz else 'FAIL'
    op_haz = [h for h in haz if (h[2] >= G2_STRENGTH or h[3] >= G2_USD)]
    res['GATE02'] = 'PASS' if not op_haz else 'FAIL'
    s7 = cl['7d']['shorts']; l7 = cl['7d']['longs']
    lsr = (s7 / l7) if (s7 and l7) else None
    res['lsr'] = round(lsr, 2) if lsr else None
    res['GATE03'] = ('BLOCKED' if (lsr and lsr >= 2.5) else 'PASS') if side == 'short' else 'PASS'
    poc = ms['auction']['day']['poc'] * ratio; vw = ms['vwap']['vwap'] * ratio
    res['GATE01_poc_cush_pct'] = round(abs(sl - poc) / poc * 100, 3)
    res['GATE01_vwap_cush_pct'] = round(abs(sl - vw) / vw * 100, 3)
    res['GATE01'] = 'PASS' if (res['GATE01_poc_cush_pct'] >= 0.5 and res['GATE01_vwap_cush_pct'] >= 0.5) else 'FAIL'
    spread = sp['spread'] * sp['point']
    res['spread_pct'] = round(spread / entry * 100, 4)
    res['PIT18_spread_over_stop'] = round(spread / risk, 4) if risk else None
    res['GATE04'] = 'PASS' if (res['spread_pct'] < 0.15 and spread / risk <= 0.20) else 'FAIL'
    # PIT-22: MT5 banned if planned stop < 3.0x spread "subject to the stricter
    # 5x PIT-18" (i.e. spread/stop <= 0.20 -> stop >= 5x spread), or if the
    # stop distance is < 1.5% of entry on an enabled crypto CFD.
    is_crypto_cfd = 'crypto' in str(d['mt5'].get('category') or '').lower()
    res['is_crypto_cfd'] = is_crypto_cfd
    res['PIT22_spread_x3_ok'] = bool(risk >= 3 * spread)
    res['PIT22_spread_x5_ok'] = bool(risk >= 5 * spread)
    res['PIT22_crypto_1p5_ok'] = (not is_crypto_cfd) or (risk / entry * 100 >= 1.5)
    res['PIT22'] = 'PASS' if (res['PIT22_spread_x5_ok'] and res['PIT22_crypto_1p5_ok']) else 'FAIL'
    # GATE-07: MT5 stressed risk is capped at $6.00; 1.20x stress on the nominal
    # risk must fit inside it, i.e. nominal risk <= $5.00. The position is
    # floored to the broker volume step and vetoed if the MINIMUM quantity
    # cannot fit inside the cap.
    max_risk_usd = 6.00 / 1.20
    if risk > 0:
        lot_cap = max_risk_usd / (sp['trade_contract_size'] * risk)
        step = sp['volume_step']; vmin = sp['volume_min']
        lots = (int(lot_cap / step) * step) if step else lot_cap
        res['lot_cap'] = round(lot_cap, 4)
        res['min_lot_fits'] = bool(lots >= vmin - 1e-12)
        if not res['min_lot_fits']:
            lots = vmin
        res['sized_lots'] = round(lots, 4)
        res['risk_usd'] = round(lots * sp['trade_contract_size'] * risk, 2)
        res['risk_usd_stressed'] = round(res['risk_usd'] * 1.2, 2)
        res['GATE07'] = 'PASS' if (res['min_lot_fits'] and res['risk_usd'] * 1.2 <= 6.00 + 1e-9) else 'FAIL'
    else:
        res['GATE07'] = 'FAIL'
    return res


def main():
    os.makedirs(OUT, exist_ok=True)
    syms = sorted(os.path.basename(f)[:-len('.parquet')]
                  for f in glob.glob(os.path.join(ROOT, 'historical_parquet', '15M', '*.parquet')))
    syms = [s for s in syms if s in pb.SPECS]
    rows = []
    for si, s in enumerate(syms):
        d = pb.prep(s)
        if d is None:
            continue
        sp = pb.SPECS[s]; spread = sp['spread'] * sp['point']
        atrH = float(d['atrH'].iloc[-1]); risk = 1.5 * atrH
        eligible = (spread / risk <= 0.20) and (risk >= 3.0 * spread)
        n = len(d); cut = int(n * 0.70)
        rec = dict(symbol=s, spread_over_stop=round(spread / risk, 4), eligible=eligible,
                   atrH_last=round(atrH, 6), bars=n, train_cut=cut)
        for lab, dd in (('train', d.iloc[:cut]), ('test', d.iloc[cut:])):
            r = pb.run(dd, s, 'short', **CFG)
            rs = pb.run(dd, s, 'short', slip_atr=SLIP, **CFG)
            pt = pb.apply_nonoverlap(pb.run_paired(dd, s, 'short', null_mode='touch',
                                                   rng=np.random.default_rng(101), **CFG))
            pdn = pb.apply_nonoverlap(pb.run_paired(dd, s, 'short', null_mode='date',
                                                     rng=np.random.default_rng(202), **CFG))
            rec[f'{lab}_n'] = r['n'] if r else 0
            rec[f'{lab}_expR'] = r['expR'] if r else None
            rec[f'{lab}_pf'] = r['pf'] if r else None
            rec[f'{lab}_ddR'] = r['ddR'] if r else None
            rec[f'{lab}_wr'] = r['wr'] if r else None
            rec[f'{lab}_tgt_pct'] = r['tgt_pct'] if r else None
            rec[f'{lab}_time_pct'] = r['time_pct'] if r else None
            rec[f'{lab}_expR_slip'] = rs['expR'] if rs else None
            rec[f'{lab}_pf_slip'] = rs['pf'] if rs else None
            # paired nulls on the identical non-overlapping event set
            rec[f'{lab}_null_touch_n'] = len(pt) if pt is not None else 0
            rec[f'{lab}_null_touch_expR'] = round(float(pt['null_R'].mean()), 4) if pt is not None and len(pt) else None
            rec[f'{lab}_edge_touch'] = round(float((pt['sig_R'] - pt['null_R']).mean()), 4) if pt is not None and len(pt) else None
            rec[f'{lab}_null_date_n'] = len(pdn) if pdn is not None else 0
            rec[f'{lab}_null_date_expR'] = round(float(pdn['null_R'].mean()), 4) if pdn is not None and len(pdn) else None
            rec[f'{lab}_edge_date'] = round(float((pdn['sig_R'] - pdn['null_R']).mean()), 4) if pdn is not None and len(pdn) else None
        rows.append(rec)
    bt = pd.DataFrame(rows).sort_values('symbol')
    bt.to_csv(os.path.join(OUT, 'pullback_bt_results.csv'), index=False)
    print(f'pullback_bt_results.csv  {len(bt)} symbols  eligible={int(bt["eligible"].sum())}')

    names = sorted(os.path.basename(f)[:-len('.json')] for f in glob.glob(os.path.join(ASSETS, '*.json')))
    mrows = []
    for nm in names:
        g = gates(nm)
        if g:
            mrows.append(g)
    m = pd.DataFrame(mrows)
    m.to_csv(os.path.join(OUT, 'setup_matrix.csv'), index=False)
    print(f'setup_matrix.csv         {len(m)} telemetry assets evaluated')
    for c in ('PIT16', 'GATE01', 'GATE02', 'GATE02_STRICT', 'GATE03', 'GATE04', 'GATE07', 'PIT22'):
        if c in m.columns:
            print(f'   {c:14s} {dict(m[c].value_counts())}')


if __name__ == '__main__':
    main()
