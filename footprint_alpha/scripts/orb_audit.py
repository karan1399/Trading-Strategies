#!/usr/bin/env python3
"""ORB baseline audit - Part 1: data forensics + baseline claim verification.

A. Data inventory: date coverage, session alignment (DST), spread-column
   reality (constant vs dynamic, open-window vs rest-of-day).
B. Baseline verification: recompute sweep CSV aggregates, champion portfolio
   metrics, and the selection-inflation adjustment (deflated Sharpe across
   the 2,560-configuration search).
C. Regime: monthly trend stats over the 3-year 4H window; where the 15M OOS
   windows sit in the regime timeline.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = "/home/z/my-project/repos/karan-ts"
ORB = os.path.join(REPO, "ORB")
RES = os.path.join(ORB, "results")
OUT = "/home/z/my-project/footprint/results"
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, ORB)

TARGETS = ["SP500.p", "NAS100.p", "GER40.p", "BTCUSD.pi", "XAUUSD.pi"]
TFS = ["5M", "15M", "1H", "4H"]


def infer_digits(df):
    """Infer true tick decimals from OHLC price precision (parquet has no digits col).

    atol-only comparison (no rtol): for large prices (BTC ~80k) the default
    rtol=1e-5 swallows the fractional part and mis-infers digits=0.
    """
    closes = df["close"].to_numpy()
    for k in range(0, 9):
        scaled = closes * 10.0 ** k
        if float(np.abs(scaled - np.round(scaled)).max()) < 1e-6:
            return k
    return 2


def load(sym, tf):
    p = os.path.join(REPO, "historical_parquet", tf, f"{sym}.parquet")
    df = pd.read_parquet(p)
    df["dt"] = pd.to_datetime(df["datetime"], utc=True)
    return df.sort_values("dt").reset_index(drop=True)


def section_a():
    print("=" * 90)
    print("A. DATA FORENSICS")
    print("=" * 90)
    inv = []
    for sym in TARGETS:
        for tf in TFS:
            df = load(sym, tf)
            dg = infer_digits(df)
            sp = df["spread"].values.astype(float)
            pt = 10.0 ** (-dg)
            c = df["close"].values
            sp_pct = sp * pt / c * 1e4  # spread in bps of price
            inv.append({"sym": sym, "tf": tf, "n": len(df), "digits": dg,
                        "start": str(df.dt.iloc[0].date()),
                        "end": str(df.dt.iloc[-1].date()),
                        "spread_bps_med": round(float(np.median(sp_pct)), 2),
                        "spread_bps_p95": round(float(np.quantile(sp_pct, 0.95)), 2),
                        "spread_unique": int(len(np.unique(sp)))})
    t = pd.DataFrame(inv)
    print(t.to_string(index=False))
    # FX overcharge check (baseline hard-codes digits=2)
    print("\n-- FX spread overcharge under baseline digits=2 vs inferred digits --")
    for sym in ["EURUSD.pi", "GBPUSD.pi", "USDJPY.pi", "GBPJPY.pi"]:
        df = load(sym, "15M")
        dg = infer_digits(df)
        sp_med = float(np.median(df["spread"].values))
        wrong = sp_med * 10.0 ** (-2)
        right = sp_med * 10.0 ** (-dg)
        bar_range = float(np.median(df["high"].values - df["low"].values))
        print(f"{sym:<10} inferred_digits={dg}  spread_pts_med={sp_med:.0f}  "
              f"baseline_charge={wrong:.5f}  correct={right:.5f}  ratio={wrong/right:.0f}x  "
              f"wrong/median_bar_range={wrong/bar_range:.1f}x")
    # session alignment: first bars of the day on 15M for SP500 (NYSE DST check)
    print("\n-- SP500.p 15M: first-bar-of-day hour distribution --")
    df = load("SP500.p", "15M")
    firsts = df.groupby(df.dt.dt.date).dt.min().dt.hour
    print(firsts.value_counts().to_string())
    print("-- SP500.p 15M: bar volume by hour (tick_volume median) --")
    vol_by_h = df.groupby(df.dt.dt.hour)["tick_volume"].median()
    print(vol_by_h.to_string())
    # open-window spread vs rest (indices, 15M)
    print("\n-- 15M stored spread (bps) by hour bucket --")
    for sym in ["SP500.p", "NAS100.p", "BTCUSD.pi", "XAUUSD.pi"]:
        df = load(sym, "15M")
        dg = infer_digits(df); pt = 10.0 ** (-dg)
        sp = df["spread"].values.astype(float) * pt / df["close"].values * 1e4
        hr = df.dt.dt.hour.values
        seg = {"open_win(14-16)": sp[(hr >= 14) & (hr < 16)],
               "rest_of_day": sp[(hr < 14) | (hr >= 16)]}
        line = f"{sym:<10}"
        for k, v in seg.items():
            line += f"  {k}: med={np.median(v):.2f} p90={np.quantile(v,0.9):.2f}"
        print(line)
    return t


def section_b():
    print("\n" + "=" * 90)
    print("B. BASELINE CLAIM VERIFICATION")
    print("=" * 90)
    sweep = pd.read_csv(os.path.join(RES, "orb_sweep_full.csv"))
    mtf = pd.read_csv(os.path.join(RES, "orb_mtf_sweep_full.csv"))
    qual = pd.read_csv(os.path.join(RES, "orb_qualified.csv"))
    mqual = pd.read_csv(os.path.join(RES, "orb_mtf_qualified.csv"))
    champ = pd.read_csv(os.path.join(RES, "champion_portfolio_trades.csv"))

    n_single = len(sweep)
    pos_oos = int((sweep.oos_r > 0).sum())
    print(f"single-TF sweep rows={n_single} (report claims 1,060 evaluated)")
    idx = sweep[sweep.cluster == "Indices"]
    print(f"Indices 15M rows={len(idx)} (report claims 200 tested)  "
          f"positive OOS={(idx.oos_r > 0).sum()}  avg OOS PF={idx.oos_pf.mean():.2f}  "
          f"total OOS R={idx.oos_r.sum():.2f}")
    fx = sweep[sweep.cluster == "Forex"]
    print(f"Forex rows={len(fx)} total OOS R={fx.oos_r.sum():.1f} (report: -11,460R aggregate)")
    # the fatal flaw: how many sweep rows pass the qualification filter?
    q = sweep[(sweep.is_pf >= 1.25) & (sweep.oos_pf >= 1.20) & (sweep.oos_n >= 8) & (sweep.oos_r > 0)]
    print(f"rows passing OOS-qualification filter: {len(q)} -> these define 'validated' setups")
    # NOTE: the qualification filter itself uses oos_pf/oos_n/oos_r — i.e. selection ON OOS.
    # selection inflation: E[max OOS R | no config-specific edge] via recentered bootstrap.
    # H0: no config is better than its siblings; shared window drift removed by recentering.
    print("\n-- selection inflation test (SP500.p, 15M, 15m_OR rows) --")
    sp = sweep[(sweep.symbol == "SP500.p") & (sweep.tf == "15M") & (sweep.or_window == "15m_OR")]
    print(f"configs={len(sp)}  best OOS R={sp.oos_r.max():.1f}  best OOS PF={sp.oos_pf.max():.2f}  "
          f"median OOS R={sp.oos_r.median():.2f}  mean OOS R={sp.oos_r.mean():.2f}")
    # per-trade R proxy: SP500 champion trades (all 32), recentered to zero mean
    champ = pd.read_csv(os.path.join(RES, "champion_portfolio_trades.csv"))
    sp_tr = champ[champ.symbol == "SP500.p"].r_net.to_numpy()
    tr0 = sp_tr - sp_tr.mean()
    rng = np.random.default_rng(11)
    n_cfg, n_oos = len(sp), 10
    nulls = []
    for _ in range(4000):
        sums = [tr0[rng.integers(0, len(tr0), n_oos)].sum() for _ in range(n_cfg)]
        nulls.append(max(sums))
    print(f"bootstrap null E[max OOS R | no config edge, n=10/config] = {np.mean(nulls):.1f} "
          f"(p90={np.quantile(nulls, 0.9):.1f}) vs observed best {sp.oos_r.max():.1f} "
          f"-> observed max {'within' if sp.oos_r.max() <= np.quantile(nulls, 0.95) else 'OUTSIDE'} "
          f"the no-edge 95% band; plus all 20 configs share the same OOS crash window")

    # champion portfolio recompute
    print("\n-- champion portfolio recompute from trades CSV --")
    r = champ.r_net.to_numpy()
    n = len(r)
    wins, losses = r[r > 0], r[r < 0]
    pf = wins.sum() / abs(losses.sum())
    wr = (r > 0).mean()
    t = r.mean() / (r.std(ddof=1) / np.sqrt(n))
    cum = np.cumsum(r)
    dd = (np.maximum.accumulate(cum) - cum).max()
    print(f"n={n} totalR={r.sum():.2f} PF={pf:.2f} WR={wr*100:.1f}% maxDD={dd:.2f}R t={t:.2f}")
    print(f"report claims: 221 trades, +168.40R, PF 2.31, WR 39.8%, DD -24.39R, t 2.91")
    syms = champ.symbol.value_counts()
    print("trades per champion:", dict(syms))
    # deflated sharpe for the whole search (2,560 configs)
    from math import erf, sqrt
    sr = r.mean() / r.std(ddof=1)
    sk = float(pd.Series(r).skew())
    ntr = n_single + len(mtf)
    sr0 = sqrt(2 * np.log(ntr)) / sqrt(n - 1)
    z = (sr - sr0) * sqrt(n - 1) / sqrt(max(1 - sk * sr + sr * sr / 4, 1e-9))
    print(f"deflated-Sharpe vs {ntr} trials: SR={sr:.3f} SR*={sr0:.3f} "
          f"DSR prob={0.5*(1+erf(z/sqrt(2))):.3f}")
    # per-champion OOS t-stats with tiny n
    print("\n-- qualified setups: OOS sample sizes --")
    for _, row in qual.head(8).iterrows():
        print(f"  {row.symbol} {row.sl_mode}/{row.tp_mode}: oos_n={row.oos_n} "
              f"oos_pf={row.oos_pf} oos_t={row.oos_t}")
    return {"champ": {"n": int(n), "r": float(r.sum()), "pf": float(pf),
                      "wr": float(wr), "dd": float(dd), "t": float(t)}}


def section_c():
    print("\n" + "=" * 90)
    print("C. REGIME CONTEXT (where the OOS windows sit)")
    print("=" * 90)
    out = {}
    for sym in ["SP500.p", "NAS100.p", "BTCUSD.pi", "XAUUSD.pi"]:
        df = load(sym, "4H")
        df["month"] = df.dt.dt.strftime("%Y-%m")
        m = df.groupby("month").apply(
            lambda g: (g.close.iloc[-1] / g.close.iloc[0] - 1) * 100,
            include_groups=False)
        out[sym] = m
        last3 = m.tail(4)
        print(f"{sym}: last 4 monthly 4H-close returns: "
              + ", ".join(f"{k}:{v:+.1f}%" for k, v in last3.items())
              + f"  | full-window trend {float(m.sum()):+.1f}%")
        # 15M OOS window dates
        d15 = load(sym, "15M")
        oos_start = d15.dt.iloc[int(len(d15) * 0.7)]
        print(f"   15M OOS window starts {oos_start.date()} "
              f"(data {d15.dt.iloc[0].date()} .. {d15.dt.iloc[-1].date()})")
    return out


if __name__ == "__main__":
    a = section_a()
    b = section_b()
    c = section_c()
    with open(os.path.join(OUT, "orb_audit_part1.json"), "w") as f:
        json.dump({"inventory": a.to_dict("records"), "baseline": b,
                   "regime": {k: {str(kk): float(vv) for kk, vv in v.items()}
                              for k, v in c.items()}}, f, indent=1, default=str)
    print("\nsaved orb_audit_part1.json")
