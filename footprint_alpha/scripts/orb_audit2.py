#!/usr/bin/env python3
"""ORB baseline audit - Part 2: causality, session alignment, stop-hunt stats.

D. Causality: 4H bar timestamp convention (open-stamp) via forward-4h aggregation
   of 15M bars; merge_asof shift(1) correctness + the .bfill() edge.
E. Session alignment: where is the true NYSE open in the data (30-min volume step)?
F. Midpoint vs opposite stop: stop-hunt statistics on 15M sessions.
G. Baseline reproduction: run their engine on SP500/NAS100, compare to CSV rows.
"""
import os
import sys

import numpy as np
import pandas as pd

REPO = "/home/z/my-project/repos/karan-ts"
ORB = os.path.join(REPO, "ORB")
sys.path.insert(0, ORB)

TARGETS = ["SP500.p", "NAS100.p", "GER40.p", "BTCUSD.pi", "XAUUSD.pi"]


def load(sym, tf):
    df = pd.read_parquet(os.path.join(REPO, "historical_parquet", tf, f"{sym}.parquet"))
    df["dt"] = pd.to_datetime(df["datetime"], utc=True)
    return df.sort_values("dt").reset_index(drop=True)


def infer_digits(df):
    closes = df["close"].to_numpy()
    for k in range(0, 9):
        scaled = closes * 10.0 ** k
        if float(np.abs(scaled - np.round(scaled)).max()) < 1e-6:
            return k
    return 2


def section_d():
    print("=" * 90)
    print("D. CAUSALITY: 4H timestamp convention + merge_asof shift(1)")
    print("=" * 90)
    for sym in ["SP500.p", "BTCUSD.pi"]:
        d15, d4 = load(sym, "15M"), load(sym, "4H")
        d15 = d15.set_index("dt")
        tested = match_f = match_b = 0
        for _, row in d4.iterrows():
            t0, t1 = row["dt"], row["dt"] + pd.Timedelta(hours=4)
            win = d15[(d15.index >= t0) & (d15.index < t1)]
            if len(win) < 10:  # need a dense 15M window to test
                continue
            tested += 1
            if np.isclose(win.high.max(), row.high, atol=1e-9) and \
               np.isclose(win.low.min(), row.low, atol=1e-9):
                match_f += 1
            win_b = d15[(d15.index > t1 - pd.Timedelta(hours=4)) & (d15.index <= t1)]
            if len(win_b) >= 10 and np.isclose(win_b.high.max(), row.high, atol=1e-9) and \
               np.isclose(win_b.low.min(), row.low, atol=1e-9):
                match_b += 1
        print(f"{sym}: 4H bars tested={tested}  forward(open-stamp) match={match_f}  "
              f"backward(close-stamp) match={match_b}")
    print("-> forward match => 4H dt is the bar OPEN; shift(1)+merge_asof(backward)")
    print("   is causal: a 15M bar at T sees the 4H bar that CLOSED at or before T.")
    print("-> residual defect: htf_ema200 shift(1).bfill() lets the first 4H bar see")
    print("   the second bar's EMA (lookahead window = 1 HTF bar at dataset start;")
    print("   harmless for 15M data starting 2026, latent for full-history runs).")


def section_e():
    print("\n" + "=" * 90)
    print("E. SESSION ALIGNMENT: where is the true NYSE open?")
    print("=" * 90)
    for sym in ["SP500.p", "NAS100.p"]:
        df = load(sym, "15M")
        dg = infer_digits(df)
        hhmm = df.dt.dt.strftime("%H:%M")
        vol = df.groupby(hhmm)["tick_volume"].median()
        seg = vol[[f"{h:02d}:{m:02d}" for h in (13, 14) for m in (0, 15, 30, 45)]]
        print(f"{sym} 15M median tick_volume by 15m slot (UTC):")
        print("   " + "  ".join(f"{k}:{v:.0f}" for k, v in seg.items()))
        # true range by slot too
        rng = (df.high - df.low).groupby(hhmm).median()
        seg2 = rng[[f"{h:02d}:{m:02d}" for h in (13, 14) for m in (0, 15, 30, 45)]]
        print("   " + "  ".join(f"{k}:{v:.2f}" for k, v in seg2.items()))
    print("-> If the volume/range step lands on 14:30, NYSE open = 14:30 UTC (winter)")
    print("   and the baseline's session_start_hour=14 builds ORs on PRE-open bars.")


def section_f():
    print("\n" + "=" * 90)
    print("F. MIDPOINT STOP-HUNT STATISTICS (15M sessions, NY window)")
    print("=" * 90)
    res = []
    for sym in TARGETS:
        df = load(sym, "15M")
        dg = infer_digits(df)
        point = 10.0 ** (-dg)
        h, l, c, o = df.high.values, df.low.values, df.close.values, df.open.values
        n = len(df)
        tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
        atr = np.concatenate([[h[0] - l[0]], pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()])
        dates = df.dt.dt.date.values
        hours = df.dt.dt.hour.values
        mins = df.dt.dt.minute.values
        or_mask = (hours == 14) & (mins < 30)          # 14:00-14:30 OR window
        sess_mask = ((hours == 14) & (mins >= 30)) | ((hours > 14) & (hours < 20))
        stats = {"orvr_le_0.8": [0, 0], "orvr_0.8_1.2": [0, 0], "orvr_gt_1.2": [0, 0]}
        for d in np.unique(dates):
            di = np.where(dates == d)[0]
            orb = di[or_mask[di]]
            if len(orb) < 2:
                continue
            orb = orb[:2]
            or_h, or_l = h[orb].max(), l[orb].min()
            if or_h <= or_l:
                continue
            rest = di[sess_mask[di]]
            if len(rest) < 4:
                continue
            orvr = (or_h - or_l) / atr[orb[-1]]
            buf = 0.05 * atr[orb[-1]]
            mid = 0.5 * (or_h + or_l)
            broke_i = None
            side = None
            for i in rest:
                if c[i] > or_h + buf and c[i] > o[i]:
                    broke_i, side = i, "L"
                    break
                if c[i] < or_l - buf and c[i] < o[i]:
                    broke_i, side = i, "S"
                    break
            if broke_i is None:
                continue
            j0 = broke_i + 1
            if j0 >= len(df) or dates[j0] != d:
                continue
            entry = o[j0]
            risk = abs(entry - mid)
            if risk <= 0:
                continue
            tgt = entry + 2.0 * risk if side == "L" else entry - 2.0 * risk
            outcome = "neither"
            for j in rest[rest > broke_i]:
                if side == "L":
                    if l[j] <= mid:
                        outcome = "mid_first"
                        break
                    if h[j] >= tgt:
                        outcome = "target_first"
                        break
                else:
                    if h[j] >= mid:
                        outcome = "mid_first"
                        break
                    if l[j] <= tgt:
                        outcome = "target_first"
                        break
            if outcome == "neither":
                continue
            key = "orvr_le_0.8" if orvr <= 0.8 else ("orvr_0.8_1.2" if orvr <= 1.2 else "orvr_gt_1.2")
            stats[key][0] += 1
            stats[key][1] += int(outcome == "mid_first")
        line = f"{sym:<10}"
        for k, (tot, hnt) in stats.items():
            line += f"  {k}: n={tot} mid-first-before-2R={hnt / max(tot, 1) * 100:.0f}%"
        print(line)
        res.append({"sym": sym, "stats": {k: {"n": v[0], "hunted_pct": v[1] / max(v[0], 1)} for k, v in stats.items()}})
    print("-> Interpretation: high ORVR opens (over-expanded) get the midpoint stop")
    print("   revisited most often -> the baseline's max_orvr<=1.2 filter is doing the")
    print("   real work; the midpoint stop itself is only safe post-compression.")


def o_open(df, i):
    return df.open.values[i]


def section_g():
    print("\n" + "=" * 90)
    print("G. BASELINE ENGINE REPRODUCTION (their code, their data)")
    print("=" * 90)
    from orb_engine import simulate_orb, OrbConfig, evaluate_orb_metrics
    for sym, session in [("SP500.p", 14), ("NAS100.p", 14), ("GER40.p", 8)]:
        p = os.path.join(REPO, "historical_parquet", "15M", f"{sym}.parquet")
        df = pd.read_parquet(p)
        df["dt"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.set_index("dt").sort_index()
        cfg = OrbConfig(session_start_hour=session, session_duration_hours=8,
                        or_bars=1, sl_mode="midpoint", tp_mode="eod", trend_filter=False)
        trades = simulate_orb(df, "15M", sym, cfg)
        n = len(trades)
        n_is = int(n * 0.7)
        m_oos = evaluate_orb_metrics(trades[n_is:])
        print(f"{sym} 15m_OR midpoint/eod: total_n={n} oos_n={m_oos['n']} "
              f"oos_r={m_oos['total_r']} oos_pf={m_oos['pf']} oos_t={m_oos['t_stat']}")
    print("-> compare with orb_qualified.csv row1: SP500 oos_r=48.23 oos_pf=7.45 oos_t=1.16")


if __name__ == "__main__":
    section_d()
    section_e()
    section_f()
    section_g()
