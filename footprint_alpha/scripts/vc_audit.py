#!/usr/bin/env python3
"""VC baseline audit - Part 1: verify report/companion claims against artifacts.

A. CSV verification: cluster aggregates vs report Section 2.
B. Baseline engine spot re-runs (their code, their data) -> exact CSV match.
C. Corrected-digits FX re-runs (companion's central D1 fix, independently).
D. Exit-mode OOS-R correlation across the 4 exits (companion D5).
E. Qualification filter admission rates (companion D4) + deflated t (D6, with
   the companion's misquoted t-stats corrected).
F. Lead-lag cross-correlations (companion Q3) + gold-quad correlation.
G. Gold yearly R (companion Q4b) + FX 4H spread zeros (companion Q4a).
H. Per-asset IS/OOS split dates (coverage disclosure the report omits).
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/home/z/my-project"
VC = os.path.join(ROOT, "Volatility_compression")
RES = os.path.join(VC, "results")
sys.path.insert(0, VC)

CORE = ["XAUUSD.pi", "XAUAUD.pi", "NAS100.p", "SP500.p", "BTCUSD.pi"]
FX = ["EURUSD.pi", "GBPUSD.pi", "USDJPY.pi", "GBPJPY.pi", "AUDUSD.pi", "USDCAD.pi"]


def load(symbol, tf):
    p = os.path.join(ROOT, "historical_parquet", tf, f"{symbol}.parquet")
    df = pd.read_parquet(p)
    df["dt"] = pd.to_datetime(df["datetime"], utc=True)
    return df.sort_values("dt").reset_index(drop=True)


def infer_digits(close):
    c = np.asarray(close, dtype=float)
    for k in range(0, 9):
        scaled = c * 10.0 ** k
        if float(np.abs(scaled - np.round(scaled)).max()) < 1e-6:
            return k
    return 2


def section_a():
    print("=" * 96)
    print("A. SWEEP CSV vs REPORT SECTION 2 (cluster aggregates)")
    print("=" * 96)
    df = pd.read_csv(os.path.join(RES, "volatility_compression_full_sweep.csv"))
    print(f"rows={len(df)} (report claims 208; 26 assets x 2 TF x 4 exits)")
    agg = df.groupby(["cluster", "tf"]).agg(
        setups=("symbol", "count"), pos_oos=("oos_r", lambda x: int((x > 0).sum())),
        avg_is_pf=("is_pf", "mean"), avg_oos_pf=("oos_pf", "mean"),
        total_oos_r=("oos_r", "sum"))
    print(agg.round(2).to_string())
    return df


def section_b(sweep):
    print("\n" + "=" * 96)
    print("B. BASELINE ENGINE SPOT RE-RUNS (their code, their data)")
    print("=" * 96)
    from quant_volatility_compression import simulate_squeeze, evaluate_metrics
    checks = [("XAUUSD.pi", "4H", "fixed_4.0r"), ("XAUAUD.pi", "4H", "fixed_3.0r"),
              ("EURUSD.pi", "1H", "fixed_2.0r"), ("NAS100.p", "1H", "fixed_4.0r")]
    for sym, tf, ex in checks:
        df = load(sym, tf)
        df = df.set_index("dt").sort_index()
        trades = simulate_squeeze(df, tf, sym, exit_mode=ex)
        n = len(trades)
        n_is = int(n * 0.70)
        m_is = evaluate_metrics(trades[:n_is])
        m_oos = evaluate_metrics(trades[n_is:])
        row = sweep[(sweep.symbol == sym) & (sweep.tf == tf) & (sweep.exit == ex)]
        if len(row):
            r = row.iloc[0]
            match = (m_is["total_r"] == r["is_r"] and m_oos["total_r"] == r["oos_r"]
                     and m_oos["pf"] == r["oos_pf"])
            print(f"{sym:<10} {tf} {ex:<12} re-run: is_r={m_is['total_r']} oos_r={m_oos['total_r']} "
                  f"oos_pf={m_oos['pf']} oos_n={m_oos['n']} oos_t={m_oos['t_stat']} | "
                  f"CSV: is_r={r['is_r']} oos_r={r['oos_r']} oos_pf={r['oos_pf']} "
                  f"oos_n={r['oos_n']} oos_t={r['oos_t']} -> {'EXACT MATCH' if match else 'MISMATCH'}")
        else:
            print(f"{sym} {tf} {ex}: not in sweep CSV")


def section_c(sweep):
    print("\n" + "=" * 96)
    print("C. CORRECTED-DIGITS FX RE-RUNS (companion D1 fix, independent)")
    print("=" * 96)
    from quant_volatility_compression import simulate_squeeze, evaluate_metrics
    out = []
    for sym in FX:
        df0 = load(sym, "4H")
        dg = infer_digits(df0["close"].to_numpy())
        for tf in ["4H", "1H"]:
            df = load(sym, tf)
            df["digits"] = dg          # patch: engine reads df["digits"] when present
            df = df.set_index("dt").sort_index()
            for ex in ["fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r"]:
                trades = simulate_squeeze(df, tf, sym, exit_mode=ex)
                if not trades:
                    continue
                n = len(trades)
                n_is = int(n * 0.70)
                m_oos = evaluate_metrics(trades[n_is:])
                m_is = evaluate_metrics(trades[:n_is])
                # baseline (digits=2) row from shipped sweep
                base = sweep[(sweep.symbol == sym) & (sweep.tf == tf) & (sweep.exit == ex)]
                b_oos = float(base.iloc[0]["oos_r"]) if len(base) else np.nan
                out.append({"sym": sym, "tf": tf, "exit": ex, "digits": dg,
                            "base_oos_r": b_oos, "fix_oos_r": m_oos["total_r"],
                            "fix_oos_pf": m_oos["pf"], "fix_oos_n": m_oos["n"],
                            "fix_is_pf": m_is["pf"]})
    t = pd.DataFrame(out)
    print(t.round(2).to_string(index=False))
    pos = int((t.fix_oos_r > 0).sum())
    print(f"corrected-tick FX configs positive OOS: {pos}/{len(t)}")
    return t


def section_d(sweep):
    print("\n" + "=" * 96)
    print("D. EXIT-MODE OOS-R CORRELATION (companion D5: 4 exits = 1 signal series)")
    print("=" * 96)
    exits = ["fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r"]
    piv = sweep.pivot_table(index=["symbol", "tf"], columns="exit", values="oos_r")
    piv = piv.dropna()
    print(f"series with all 4 exits: {len(piv)}")
    import itertools
    pw = []
    for e1, e2 in itertools.combinations(exits, 2):
        pw.append(float(np.corrcoef(piv[e1], piv[e2])[0, 1]))
    print("across-series OOS-R correlations between exit modes:")
    for (e1, e2), r in zip(itertools.combinations(exits, 2), pw):
        print(f"  {e1} vs {e2}: r={r:+.3f}")
    print(f"median across-series exit-pair corr = {np.median(pw):.3f}")


def section_e(sweep):
    print("\n" + "=" * 96)
    print("E. QUALIFICATION FILTER + DEFLATED t (companion D4/D6, corrected)")
    print("=" * 96)
    qual_mask = ((sweep.is_pf >= 1.25) & (sweep.oos_pf >= 1.20) &
                 (sweep.oos_n >= 8) & (sweep.oos_r > 0))
    print(f"qualified rows: {int(qual_mask.sum())}/{len(sweep)}")
    m4 = sweep[(sweep.cluster == "Commodity_Metals") & (sweep.tf == "4H")]
    adm = (m4.is_pf >= 1.25).mean()
    print(f"metals 4H: is_pf>=1.25 admission = {adm*100:.0f}%  (avg IS PF {m4.is_pf.mean():.2f}, "
          f"avg OOS PF {m4.oos_pf.mean():.2f}, positive OOS {int((m4.oos_r>0).sum())}/{len(m4)})")
    import math
    q = sweep[qual_mask]
    for _, r in q.head(8).iterrows():
        t_def = r["oos_t"] - math.sqrt(2 * math.log(208))
        print(f"  {r['symbol']:<10} {r['tf']} {r['exit']:<12} oos_t={r['oos_t']:.2f} "
              f"deflated(208)={t_def:+.2f}  [companion quoted {r['symbol']} t values: verify]")
    # metals pooled OOS t (their engine, all metals 4H configs pooled by unique trade date)
    from quant_volatility_compression import simulate_squeeze
    pool = {}
    for sym in ["XAUUSD.pi", "XAGUSD.pi", "XAUAUD.pi", "XAUGBP.pi", "XAUCNH.pi"]:
        df = load(sym, "4H").set_index("dt").sort_index()
        for ex in ["fixed_3.0r", "fixed_4.0r"]:
            trades = simulate_squeeze(df, "4H", sym, exit_mode=ex)
            n_is = int(len(trades) * 0.70)
            for t in trades[n_is:]:
                key = str(t.entry_time)[:10]
                pool.setdefault(key, {}).setdefault(sym, 0.0)
                pool[key][sym] += t.r_net
    daily = np.array([sum(v.values()) for v in pool.values()])
    t_all = daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))
    t_defl = t_all - math.sqrt(2 * math.log(10))   # 5 symbols x 2 exits = 10 signal series
    print(f"metals 4H pooled OOS daily-R: n_days={len(daily)} sum={daily.sum():.1f}R "
          f"t={t_all:.2f} deflated(10 series)={t_defl:+.2f}")


def section_f():
    print("\n" + "=" * 96)
    print("F. LEAD-LAG CROSS-CORRELATIONS + GOLD-QUAD CORRELATION (companion Q3/Q4)")
    print("=" * 96)
    def load_close(sym):
        return load(sym, "4H").set_index("dt")["close"].sort_index()
    pairs = [("XAUUSD.pi", "XAGUSD.pi"), ("SP500.p", "NAS100.p"), ("BTCUSD.pi", "SOLUSD.p")]
    for lead, lag in pairs:
        a, b = load_close(lead), load_close(lag)
        j = pd.concat([a, b], axis=1, join="inner").dropna()
        ra, rb = np.diff(np.log(j.iloc[:, 0])), np.diff(np.log(j.iloc[:, 1]))
        cc = [float(np.corrcoef(ra[max(k, 0):], rb[:len(rb) - max(k, 0)])[0, 1]) if k > 0
              else float(np.corrcoef(ra, rb)[0, 1]) for k in range(0, 8)]
        print(f"{lead} -> {lag} (inner-join n={len(j)}): " +
              "  ".join(f"lag{k}:{v:+.3f}" for k, v in enumerate(cc)))
    closes = pd.concat([load_close(s) for s in ["XAUUSD.pi", "XAUAUD.pi", "XAUCNH.pi", "XAUGBP.pi"]],
                       axis=1, join="inner").dropna()
    R = np.diff(np.log(closes.to_numpy()), axis=0)
    M = pd.DataFrame(R, columns=closes.columns)
    print("gold-quad 4H return correlations (inner-join):")
    print(M.corr().round(3).to_string())


def section_g():
    print("\n" + "=" * 96)
    print("G. GOLD YEARLY R + FX SPREAD ZEROS (companion Q4)")
    print("=" * 96)
    from quant_volatility_compression import simulate_squeeze
    df = load("XAUUSD.pi", "4H").set_index("dt").sort_index()
    for ex in ["fixed_3.0r", "fixed_4.0r"]:
        trades = simulate_squeeze(df, "4H", "XAUUSD.pi", exit_mode=ex)
        yr = {}
        for t in trades:
            y = str(t.entry_time)[:4]
            yr[y] = yr.get(y, 0.0) + t.r_net
        print(f"XAUUSD 4H {ex}: " + "  ".join(f"{k}:{v:+.1f}" for k, v in sorted(yr.items())))
    for sym in ["EURUSD.pi", "GBPUSD.pi"]:
        df = load(sym, "4H")
        sp = df.spread.to_numpy(float)
        print(f"{sym} 4H spread: zero on {(sp == 0).mean() * 100:.0f}% of bars, "
              f"median={np.median(sp):.0f} pts, p90={np.quantile(sp, 0.9):.0f} pts")
    dfx = load("XAUUSD.pi", "4H")
    spx = dfx.spread.to_numpy(float)
    print(f"XAUUSD 4H spread: zero on {(spx == 0).mean() * 100:.0f}%, median={np.median(spx):.0f} pts")


def section_h():
    print("\n" + "=" * 96)
    print("H. PER-ASSET COVERAGE AND SPLIT DATES (the report's undisclosed caveat)")
    print("=" * 96)
    for sym in CORE:
        for tf in ["4H", "1H"]:
            df = load(sym, tf)
            days = sorted(df.dt.dt.date.unique())
            k = int(len(days) * 0.7)
            print(f"{sym:<10} {tf}: {len(df)} bars {str(days[0])}..{str(days[-1])} "
                  f"({len(days)} days) -> IS ends {days[k - 1]}, OOS starts {days[k]} "
                  f"({len(days) - k} OOS days)")


if __name__ == "__main__":
    sweep = section_a()
    section_b(sweep)
    section_c(sweep)
    section_d(sweep)
    section_e(sweep)
    section_f()
    section_g()
    section_h()
