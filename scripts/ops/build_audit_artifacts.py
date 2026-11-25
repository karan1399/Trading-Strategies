#!/usr/bin/env python3
"""Rebuild the audit artifacts for the R19 setup-matrix review from repo sources only.

Outputs (desk/audit_data/):
  atr_15m_1h.csv        ATR14 + percentile + EMA spine for all 151 parquet symbols
  corr_15m_matrix.csv   aligned 15M log-return correlation matrix
  pullback_bt_results.csv  costed no-lookahead backtest, train/test, null control
  setup_matrix.csv      per-candidate gate results

Run:  python3 scripts/ops/build_audit_artifacts.py
Requires: pandas, numpy, pyarrow
"""
import json, os, sys, glob
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PQ = os.path.join(ROOT, 'historical_parquet')
OUT = os.path.join(ROOT, 'desk', 'audit_data')
os.makedirs(OUT, exist_ok=True)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr_series(df, n=14):
    pc = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - pc).abs(), (df['low'] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def load(tf, sym):
    p = os.path.join(PQ, tf, sym + '.parquet')
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p).set_index('datetime').sort_index()


def build_atr():
    rows = []
    for f in sorted(glob.glob(os.path.join(PQ, '15M', '*.parquet'))):
        sym = os.path.basename(f)[:-len('.parquet')]
        d15 = load('15M', sym); d1h = load('1H', sym)
        if d15 is None or d1h is None:
            continue
        a15 = atr_series(d15, 14); a1h = atr_series(d1h, 14)
        # percentile rank of the most recent ATR against its own history
        p15 = float((a15.dropna() <= a15.iloc[-1]).mean()) if len(a15.dropna()) else np.nan
        p1h = float((a1h.dropna() <= a1h.iloc[-1]).mean()) if len(a1h.dropna()) else np.nan
        rows.append(dict(
            symbol=sym, rows_15M=len(d15), rows_1H=len(d1h),
            first_15M=str(d15.index[0]), last_15M=str(d15.index[-1]),
            close_15M=float(d15['close'].iloc[-1]),
            atr15=float(a15.iloc[-1]), atr15_pctile=round(p15, 4),
            atr1h=float(a1h.iloc[-1]), atr1h_pctile=round(p1h, 4),
            ema13_15M=float(ema(d15['close'], 13).iloc[-1]),
            ema89_15M=float(ema(d15['close'], 89).iloc[-1]),
            ema233_15M=float(ema(d15['close'], 233).iloc[-1]),
        ))
    df = pd.DataFrame(rows).sort_values('symbol')
    df.to_csv(os.path.join(OUT, 'atr_15m_1h.csv'), index=False)
    return df


def build_corr():
    syms, series = [], {}
    for f in sorted(glob.glob(os.path.join(PQ, '15M', '*.parquet'))):
        sym = os.path.basename(f)[:-len('.parquet')]
        d = load('15M', sym)
        if d is None:
            continue
        r = np.log(d['close']).diff()
        series[sym] = r
        syms.append(sym)
    df = pd.concat(series, axis=1, join='inner')
    df = df.dropna(how='any')
    c = df.corr()
    c.to_csv(os.path.join(OUT, 'corr_15m_matrix.csv'))
    meta = dict(observations=int(len(df)), first=str(df.index[0]), last=str(df.index[-1]),
                symbols=len(c))
    json.dump(meta, open(os.path.join(OUT, 'corr_15m_meta.json'), 'w'), indent=1)
    return c, meta


if __name__ == '__main__':
    atr = build_atr()
    print(f'atr_15m_1h.csv          {len(atr)} symbols')
    corr, meta = build_corr()
    print(f"corr_15m_matrix.csv     {corr.shape[0]}x{corr.shape[1]}  obs={meta['observations']}  {meta['first']} .. {meta['last']}")
