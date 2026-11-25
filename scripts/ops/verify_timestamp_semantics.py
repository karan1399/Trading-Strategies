#!/usr/bin/env python3
"""Prove the bar-timestamp semantics of historical_parquet and show that the
H1 ATR used by a 15M decision must be the LAST COMPLETED H1 bar.

Findings (reproduced by this script):
  1. The 1H parquet is START-LABELLED: the 1H bar labelled T is the exact
     aggregation of the 15M bars labelled T, T+15m, T+30m, T+45m.
     max |dHigh| = max |dClose| = 0.0 over every overlapping bar.
  2. Therefore the last COMPLETED H1 bar knowable at a 15M bar labelled T is
     the one labelled floor(T) - 1h (it closes exactly at floor(T)).
  3. The historical `join + ffill + shift(1)` construction leaks the IN-PROGRESS
     H1 bar into 73.5% of 15M rows (every :15/:30/:45 row). Measured maximum
     ATR error 122%. That is a look-ahead and is now fixed in
     scripts/ops/pullback_backtest.py::prep().

Usage: /tmp/audvenv/bin/python scripts/ops/verify_timestamp_semantics.py [SYMBOL]
"""
import os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pullback_backtest as pb

ROOT = os.path.dirname(os.path.dirname(HERE))
PQ = os.path.join(ROOT, 'historical_parquet')


def check(sym):
    m = pd.read_parquet(os.path.join(PQ, '15M', sym + '.parquet'))
    h = pd.read_parquet(os.path.join(PQ, '1H', sym + '.parquet'))
    m['dt'] = pd.to_datetime(m['datetime'], utc=True); h['dt'] = pd.to_datetime(h['datetime'], utc=True)
    mi = m.set_index('dt').sort_index(); hi = h.set_index('dt').sort_index()

    start = mi.resample('1h', label='left', closed='left').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    close = mi.resample('1h', label='right', closed='right').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    common = hi.index.intersection(start.index)
    out = {}
    for name, agg in (('START-label', start), ('CLOSE-label', close)):
        cm = hi.index.intersection(agg.index)
        a = agg.loc[cm]; b = hi.loc[cm]
        out[name] = dict(
            n=len(cm),
            dHigh=float((a['high'] - b['high']).abs().max()),
            dClose=float((a['close'] - b['close']).abs().max()),
            dLow=float((a['low'] - b['low']).abs().max()))
    print(f'[{sym}] overlapping 1H bars: {len(common)}')
    for name, v in out.items():
        print(f'   {name:12s} n={v["n"]:5d}  max|dHigh|={v["dHigh"]:.6f}  '
              f'max|dLow|={v["dLow"]:.6f}  max|dClose|={v["dClose"]:.6f}')
    verdict = ('START-label' if out['START-label']['dHigh'] <= out['CLOSE-label']['dHigh']
               else 'CLOSE-label')
    print(f'   => 1H bars are {verdict} labelled (1H bar T covers [T, T+1h))')
    cm = hi.index.intersection(start.index)
    mism = (start.loc[cm, 'low'] != hi.loc[cm, 'low']).sum()
    mismh = (start.loc[cm, 'high'] != hi.loc[cm, 'high']).sum()
    print(f'   note: 1H low differs from the 15M-aggregated low in {mism}/{len(cm)} hours; '
          f'high differs in {mismh}/{len(cm)} hours (1H feed is finer-grained than 15M)')

    # leak quantification on the corrected vs the naive join
    h2 = hi.copy(); h2['atrH'] = pb.atr_series(h2, 14); h2.index = h2.index.floor('h')
    d = pb.prep(sym)
    if d is not None and len(d):
        need = d.index.floor('h') - pd.Timedelta('1h')
        correct = h2['atrH'].reindex(need).values
        naive = h2[['atrH']].join(pd.DataFrame(index=d.index), how='right')['atrH'].ffill().shift(1).values
        ok = np.isfinite(correct) & np.isfinite(naive) & (correct > 0)
        rel = np.abs(naive[ok] - correct[ok]) / correct[ok]
        leak_rows = int((rel > 1e-4).sum())
        print(f'   naive join vs corrected: {leak_rows}/{int(ok.sum())} rows differ '
              f'({100*leak_rows/ok.sum():.1f}%), max relative ATR error {100*rel.max():.1f}%, '
              f'median {100*np.median(rel):.1f}%')
        mins = sorted(set(d.index[ok][rel > 1e-4].minute))
        print(f'   minutes-of-hour affected: {mins}  (0 = hour-aligned rows are correct)')


if __name__ == '__main__':
    syms = sys.argv[1:] or ['ETHUSD.pi', 'BNBUSD.p', 'USWTI.p', 'EURUSD.pi', 'XAUUSD.pi']
    for s in syms:
        if os.path.exists(os.path.join(PQ, '15M', s + '.parquet')):
            check(s); print()
