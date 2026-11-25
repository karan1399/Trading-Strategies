#!/usr/bin/env python3
"""No-lookahead backtest of the S10 EMA89 pullback template (MT5, 15M).

Model (matches desk order style P5-03 passive MT5 limits):
  1. Signal at CLOSE of bar i: downtrend (close < EMA89 < EMA233) and price wicked
     up into EMA89 and closed back below it.
  2. Place a passive LIMIT at EMA89(i). Known only at bar i close -> no lookahead.
  3. Fill at the first LATER bar j>i whose range touches EMA89(i); fill = EMA89(i).
     If price never returns within `expiry` bars the order never fills (no trade).
  4. Stop = fill + risk_mult * ATR, Target = fill - rr * ATR.
     Intrabar ambiguity resolved CONSERVATIVELY: if stop and target are both
     touched in the same bar, the STOP is assumed to fill first.
  5. Time stop: exit at close after `time_stop` bars if +0.5R was never reached
     (desk P5-05 / GATE-05). A cumulative "reached" flag is used.
  6. Costs: full broker spread per round trip, plus optional adverse slippage
     expressed in ATR units (`slip_atr`). Crypto CFDs carry $0 commission
     (calibrated against desk fills: UNI #20475921 gross $10.30 == desk $10.30;
      BNB #20432336 risk $4.75 vs desk $4.78).

NULL MODELS (both share the identical exit engine and regime filter):
  * null_mode='touch' : the entry bar is drawn at random from the bars that
    actually touch the EMA89 limit after the signal -> "touch-conditioned".
  * null_mode='date'  : the entry bar is drawn at random from ANY bar on the
    SAME UTC DATE as the signal (regime filter still applied), and the limit is
    placed at that bar's EMA89 -> "date-conditioned". This removes the day's
    directional drift and isolates the pullback-timing edge.

TIMESTAMP SEMANTICS (verified empirically against the raw parquet, see
scripts/ops/verify_timestamp_semantics.py):
  * 15M bar labelled T covers [T, T+15m); H1 bar labelled T covers [T, T+1h).
    The 1H parquet high/close reproduce the 15M aggregation EXACTLY under
    start-labelling (max |dHigh| = 0.0 over 1251 overlapping ETH bars).
  * Therefore the last COMPLETED H1 bar available at 15M bar T is the one
    labelled floor(T) - 1h (it closes at floor(T)). `prep()` uses exactly that.
    A naive `join + ffill + shift(1)` leaks the IN-PROGRESS H1 bar into 73.5%
    of 15M rows (measured max ATR error 122%); that bug is fixed here.
"""
import json, os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PQ = os.path.join(ROOT, 'historical_parquet')
CATALOG = os.path.join(ROOT, 'desk', 'mt5_symbols_universe.json')


def load_specs():
    cat = json.load(open(CATALOG))
    return {s['name']: s for c, lst in cat.items() for s in lst}


SPECS = load_specs()


def load(tf, sym):
    p = os.path.join(PQ, tf, sym + '.parquet')
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p).set_index('datetime').sort_index()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr_series(df, n=14):
    pc = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - pc).abs(), (df['low'] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def prep(sym, atr_tf='1H'):
    """15M bars with M15/H1 ATR and the EMA spine, all shifted to avoid lookahead.

    Availability rule: an H1 bar labelled H covers [H, H+1h) and only becomes
    knowable at H+1h. A 15M bar labelled T (covering [T, T+15m)) may therefore
    only use H1 bars with H + 1h <= T, i.e. H <= T - 1h. The H1 grid is hourly
    and the 15M grid is quarter-hourly, so the only admissible H is
    floor(T) - 1h.
    """
    d = load('15M', sym); h = load(atr_tf, sym)
    if d is None or h is None or len(d) < 900:
        return None
    d = d.copy(); h = h.copy()
    d['ema13'] = ema(d['close'], 13)
    d['ema89'] = ema(d['close'], 89)
    d['ema233'] = ema(d['close'], 233)
    d['atr15'] = atr_series(d, 14)
    h['atrH'] = atr_series(h, 14)
    h.index = h.index.floor('h')
    src = h['atrH'].sort_index()
    need = d.index.floor('h') - pd.Timedelta('1h')     # last COMPLETED H1 bar
    d['atrH'] = src.reindex(need).ffill().values
    return d.dropna(subset=['ema233', 'atr15', 'atrH'])


def _outcome(O, H, L, C, j, fill, risk, side, time_stop, max_bars, spread, slip, rr=2.0):
    """Shared exit engine. Returns (R, reason, held) or None if still open."""
    if side == 'short':
        stop = fill + risk; tgt = fill - rr * risk
    else:
        stop = fill - risk; tgt = fill + rr * risk
    n = len(C)
    held = 0; reached = False; k = j
    while k < min(n, j + max_bars):
        held += 1
        if side == 'short':
            stop_hit = H[k] >= stop; tgt_hit = L[k] <= tgt
            if L[k] <= fill - 0.5 * risk: reached = True
        else:
            stop_hit = L[k] <= stop; tgt_hit = H[k] >= tgt
            if H[k] >= fill + 0.5 * risk: reached = True
        if stop_hit: exitp, reason, xb = stop, 'stop', k; break
        if tgt_hit: exitp, reason, xb = tgt, 'target', k; break
        if time_stop and held >= time_stop and not reached:
            exitp, reason, xb = C[k], 'time', k; break
        k += 1
    else:
        xb = min(n - 1, j + max_bars)
        exitp, reason = C[xb], 'eod'
    # adverse slippage applied to the exit price (a short exits by BUYING, so a
    # worse exit is a HIGHER price; a long exits by SELLING, so a worse exit is
    # a LOWER price).
    exitp = exitp + slip if side == 'short' else exitp - slip
    R = ((fill - exitp) / risk) if side == 'short' else ((exitp - fill) / risk)
    if spread:
        R -= spread / risk
    return R, reason, held, xb


def _stats(t, sym, side):
    w = t[t['R'] > 0]; l = t[t['R'] <= 0]
    cum = t['R'].cumsum(); dd = float((cum.cummin() - cum).min())
    gp = float(w['R'].sum()); gl = float(-l['R'].sum())
    return dict(symbol=sym, side=side, n=len(t), wr=round(100 * len(w) / len(t), 1),
                expR=round(float(t['R'].mean()), 4), totR=round(float(t['R'].sum()), 2),
                pf=round(gp / gl, 3) if gl > 0 else None, ddR=round(dd, 2),
                stop_pct=round(100 * (t['reason'] == 'stop').mean(), 1),
                tgt_pct=round(100 * (t['reason'] == 'target').mean(), 1),
                time_pct=round(100 * (t['reason'] == 'time').mean(), 1))


def _arrays(d, atr_col):
    return (d['high'].values, d['low'].values, d['close'].values,
            d['ema89'].values, d['ema233'].values, d[atr_col].values)


def _signal_idx(C, H, L, e89, e233, side):
    """Indices of bars that close the pullback signal."""
    if side == 'short':
        m = (C < e89) & (e89 < e233) & (H >= e89)
    else:
        m = (C > e89) & (e89 > e233) & (L <= e89)
    return np.where(m)[0]


def _touches(H, L, limit, side, lo, hi):
    if side == 'short':
        return np.where(H[lo:hi] >= limit)[0] + lo
    return np.where(L[lo:hi] <= limit)[0] + lo


def run(d, sym, side='short', risk_mult=1.5, rr=2.0, time_stop=8, expiry=8,
        max_bars=48, atr_col='atrH', apply_spread=True, random_entry=False,
        null_mode=None, rng=None, slip_atr=0.0, return_trades=False,
        overlap=False):
    """Backtest one symbol with the desk's execution model.

    overlap=False (default): strictly non-overlapping positions, one per
    symbol at a time -- matches the desk's single-position-per-symbol style.
    """
    sp = SPECS.get(sym)
    if sp is None:
        return None
    spread = sp['spread'] * sp['point'] if apply_spread else 0.0
    H, L, C, e89, e233, AV = _arrays(d, atr_col)
    n = len(C); idx = d.index
    sig = _signal_idx(C, H, L, e89, e233, side)
    trades = []
    si = 0
    while si < len(sig):
        i = int(sig[si])
        if not np.isfinite(AV[i]) or AV[i] <= 0:
            si += 1; continue
        limit = e89[i]; av = AV[i]
        hits = _touches(H, L, limit, side, i + 1, min(n, i + 1 + expiry))
        if len(hits) == 0:
            si += 1; continue
        j = int(hits[0])
        risk = risk_mult * av; slip = slip_atr * av
        R, reason, held, xb = _outcome(None, H, L, C, j, limit, risk, side,
                                       time_stop, max_bars, spread, slip, rr)
        trades.append(dict(R=R, reason=reason, held=held, t=idx[i], exit_t=idx[xb]))
        if not overlap:
            # skip every later signal that would still be open at the exit bar
            si = int(np.searchsorted(sig, xb, side='right'))
        else:
            si += 1
    if not trades:
        return None
    t = pd.DataFrame(trades); t['t'] = pd.to_datetime(t['t'])
    out = _stats(t, sym, side)
    if return_trades:
        out['trades'] = t
    return out


def run_paired(d, sym, side='short', risk_mult=1.5, rr=2.0, time_stop=8, expiry=8,
               max_bars=48, atr_col='atrH', apply_spread=True, null_mode='date',
               rng=None, slip_atr=0.0):
    """Paired signal-vs-null comparison, one null trade per signal event.

    null_mode='touch' : the fill bar is drawn at random from the bars that
                        actually touch the EMA89 limit (touch-conditioned).
    null_mode='date'  : the entry bar is drawn at random from any bar on the
                        SAME UTC DATE (regime filter still applied) and the
                        limit is placed at that bar's EMA89 (date-conditioned).

    Returns a DataFrame with one row per paired event: sig_R and null_R.
    """
    sp = SPECS.get(sym)
    if sp is None:
        return None
    spread = sp['spread'] * sp['point'] if apply_spread else 0.0
    H, L, C, e89, e233, AV = _arrays(d, atr_col)
    n = len(C); idx = d.index
    dates = idx.normalize()
    sig = _signal_idx(C, H, L, e89, e233, side)
    if rng is None:
        rng = np.random.default_rng(0)
    rows = []
    for i in sig:
        if not np.isfinite(AV[i]) or AV[i] <= 0:
            continue
        limit = e89[i]; av = AV[i]
        hits = _touches(H, L, limit, side, i + 1, min(n, i + 1 + expiry))
        if len(hits) == 0:
            continue
        j_sig = int(hits[0])
        risk = risk_mult * av; slip = slip_atr * av
        sR, sreason, sheld, sxb = _outcome(None, H, L, C, j_sig, limit, risk, side,
                                            time_stop, max_bars, spread, slip, rr)
        sig_exit = sxb
        # ---- null ----
        if null_mode == 'touch':
            cand = hits
        else:  # date-conditioned
            cand = np.where(dates == dates[i])[0]
            cand = cand[(cand != i) & (cand > 0)]
            cand = cand[np.array([(C[k] < e89[k] and e89[k] < e233[k]) if side == 'short'
                                  else (C[k] > e89[k] and e89[k] > e233[k]) for k in cand])] \
                if len(cand) else cand
        if len(cand) == 0:
            continue
        k0 = int(cand[rng.integers(len(cand))])
        if null_mode == 'touch':
            nlimit, nav, j_null = limit, av, k0
        else:
            nlimit, nav = e89[k0], AV[k0]
            hh = _touches(H, L, nlimit, side, k0 + 1, min(n, k0 + 1 + expiry))
            if len(hh) == 0:
                continue
            j_null = int(hh[0])
        if not np.isfinite(nav) or nav <= 0:
            continue
        nrisk = risk_mult * nav; nslip = slip_atr * nav
        nR, nreason, nheld, nxb = _outcome(None, H, L, C, j_null, nlimit, nrisk, side,
                                            time_stop, max_bars, spread, nslip, rr)
        rows.append(dict(t=idx[i], sig_R=sR, sig_reason=sreason, null_R=nR,
                         null_reason=nreason, null_entry=idx[k0], sig_exit=idx[sig_exit]))
    if not rows:
        return None
    return pd.DataFrame(rows)


def apply_nonoverlap(p):
    """Keep only paired events whose signal was not still open at the next
    signal bar -- i.e. reproduce the single-position-per-symbol rule so the
    signal arm and the null arm are evaluated on an identical event set."""
    if p is None or len(p) == 0:
        return p
    keep = []
    prev_exit = None
    for i in range(len(p)):
        if prev_exit is None or p['t'].iloc[i] > prev_exit:
            keep.append(i); prev_exit = p['sig_exit'].iloc[i]
    return p.iloc[keep].reset_index(drop=True)
