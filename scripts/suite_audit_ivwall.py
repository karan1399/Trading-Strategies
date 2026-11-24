#!/usr/bin/env python3
"""Red-team audit of ivwall_alpha (20-pair M15 wall-fade) — their engine, my costs.

Protocol:
  - THEIR deployable config (fade_touch / ewma_cc / sd_wall 2.0 / wall_m 0.5 /
    buffer_atr 1.0 / min_rr 0.3 / 2 trades-per-session / 6-bar cooldown), run via
    their own backtest_symbol (causal: session sigma from bar before open,
    EM fixed for the day, next-bar-open entry, stop-first sequencing).
  - MY cost model: zero-spread bars replaced by the pair's median NON-ZERO 15M
    spread; charged both sides by their engine. Stress at x1.5 / x2.
  - Chronological day split 70/30 over the union window (2026-09-07 .. 11-20,
    ~52 trading days -> IS ~36 / OOS ~16). Selection integrity test: per-pair
    IS screen (PF>=1.3, n>=8) -> one OOS evaluation of that subset.
  - Pooled OOS daily-R t, deflation vs 96 trials; MC iid + block bootstrap
    (5-day blocks) on the OOS R stream.
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/home/z/my-project"
PQ = os.path.join(ROOT, "historical_parquet", "15M")
sys.path.insert(0, os.path.join(ROOT, "ivwall_alpha", "code"))
from ivwall_engine import Config, backtest_symbol  # noqa: E402

OUT = os.path.join(ROOT, "scripts", "suite_audit_out")
os.makedirs(OUT, exist_ok=True)

BASKET = ["CADJPY.pi", "EURGBP.pi", "EURCAD.pi", "USDSGD.pi", "NZDCHF.pi",
          "EURUSD.pi", "EURHKD.pi", "USDZAR.pi", "AUDSGD.pi", "GBPNZD.pi",
          "CNHJPY.pi", "NZDSGD.pi", "NZDCAD.pi", "GBPAUD.pi", "AUDNZD.pi",
          "EURNZD.pi", "GBPUSD.pi", "USDNOK.pi", "CADCHF.pi", "USDCHF.pi"]

CFG = Config(name="audit", strategy="fade_touch", vol_estimator="ewma_cc",
             target_mode="wall_m", sd_wall=2.0, target_m=0.5, buffer_atr=1.0,
             min_rr=0.3)


def infer_digits(close: pd.Series) -> int:
    s = close.astype(str).str.extract(r"\.(\d+)")[0].fillna("")
    s = s.str.rstrip("0")
    dd = s.str.len()
    if not len(dd):
        return 2
    return int(min(int(dd.mode().iloc[0]), 8))


def load_enforced(symbol: str, mult: float = 1.0):
    df = pd.read_parquet(os.path.join(PQ, symbol + ".parquet"))[
        ["datetime", "open", "high", "low", "close", "tick_volume", "spread"]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.sort_values("datetime").reset_index(drop=True)
    dg = infer_digits(df["close"])
    px = 10.0 ** -dg
    sp = df["spread"].to_numpy(float) * px
    nz = sp[sp > 0]
    med_nz = float(np.median(nz)) if len(nz) else 2.0 * px
    en = np.where(sp > 0, sp, med_nz) * mult
    df["spread"] = en / px            # engine expects POINTS
    df["digits"] = dg
    df = df.set_index("datetime")
    df.attrs["symbol"] = symbol
    stats = {"digits": dg, "zero_pct": 100.0 * float((sp <= 0).mean()),
             "med_nz_pip": med_nz / px, "p90_pip": float(np.quantile(sp[sp > 0], 0.90)) / px if len(nz) else 0.0}
    return df, stats


def metrics(tr: pd.DataFrame):
    if not len(tr):
        return {"n": 0, "r": 0.0, "pf": 0.0, "t": 0.0}
    r = tr["r_net"].to_numpy(float)
    w, lo = r[r > 0], r[r < 0]
    return {"n": int(len(r)), "r": float(r.sum()),
            "pf": float(w.sum() / abs(lo.sum())) if lo.sum() < 0 else 99.0,
            "t": float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 2 else 0.0}


def run_all(mult: float):
    trs, stats = [], {}
    for sym in BASKET:
        df, st = load_enforced(sym, mult)
        st["symbol"] = sym
        stats[sym] = st
        try:
            t = backtest_symbol(df, CFG)
        except Exception as e:
            print(f"  {sym}: engine error {e}")
            continue
        if len(t):
            trs.append(t)
    return pd.concat(trs, ignore_index=True).sort_values("entry_time").reset_index(drop=True) if trs else pd.DataFrame(), stats


def mc_iid(r, n=10000, seed=7):
    rng = np.random.default_rng(seed)
    sums = np.array([rng.choice(r, len(r), replace=True).sum() for _ in range(n)])
    dds = []
    for _ in range(2000):
        s = rng.choice(r, len(r), replace=True)
        eq = np.cumsum(s)
        dds.append(float((np.maximum.accumulate(eq) - eq).max()))
    return {"p_profit": float((sums > 0).mean()), "ci5": float(np.quantile(sums, 0.05)),
            "dd95": float(np.quantile(dds, 0.95))}


def block_boot(r_days: np.ndarray, n=5000, seed=11, block=5):
    rng = np.random.default_rng(seed)
    k = len(r_days)
    nb = math.ceil(k / block)
    outs = []
    for _ in range(n):
        idx = rng.integers(0, k - block + 1, nb)
        sample = np.concatenate([r_days[i:i + block] for i in idx])[:k]
        outs.append(sample.sum())
    outs = np.array(outs)
    return {"p_profit": float((outs > 0).mean()), "ci5": float(np.quantile(outs, 0.05))}


def main():
    print("=" * 100)
    print("IV-WALL RED-TEAM AUDIT — their engine, enforced non-zero spreads, day-split IS/OOS")
    print("=" * 100)
    tr, stats = run_all(1.0)
    st = pd.DataFrame(stats).T
    print(st.round(2).to_string())
    all_days = sorted({str(d.date()) for sym in BASKET
                       for d in pd.to_datetime(pd.read_parquet(os.path.join(PQ, sym + '.parquet')).datetime, utc=True)})
    k = int(len(all_days) * 0.70)
    is_days, oos_days = set(all_days[:k]), set(all_days[k:])
    print(f"\nwindow: {all_days[0]} .. {all_days[-1]}  ({len(all_days)} days) -> IS {len(is_days)} / OOS {len(oos_days)}")
    print(f"OOS starts: {all_days[k]}")

    tr["day"] = tr["entry_time"].map(lambda d: str(pd.Timestamp(d).date()))
    tr["leg"] = np.where(tr["day"].isin(is_days), "IS", "OOS")
    for leg in ("IS", "OOS"):
        print(f"{leg}: {metrics(tr[tr['leg'] == leg])}")

    rows = []
    for sym, g in tr.groupby("symbol"):
        m_is, m_oos = metrics(g[g["leg"] == "IS"]), metrics(g[g["leg"] == "OOS"])
        rows.append({"symbol": sym, "is_n": m_is["n"], "is_r": round(m_is["r"], 1), "is_pf": round(m_is["pf"], 2),
                     "oos_n": m_oos["n"], "oos_r": round(m_oos["r"], 1), "oos_pf": round(m_oos["pf"], 2),
                     "spread_med_pip": round(stats[sym]["med_nz_pip"], 2)})
    tbl = pd.DataFrame(rows).sort_values("oos_r", ascending=False)
    print("\nper-pair (spread-enforced):")
    print(tbl.to_string(index=False))
    tbl.to_csv(os.path.join(OUT, "ivwall_audit_pairs.csv"), index=False)

    # selection integrity: IS screen then single OOS eval
    sel = tbl[(tbl["is_pf"] >= 1.3) & (tbl["is_n"] >= 8)]["symbol"].tolist()
    sub = tr[(tr["leg"] == "OOS") & (tr["symbol"].isin(sel))]
    print(f"\nIS-screen subset ({len(sel)} pairs: {', '.join(sel)}): OOS {metrics(sub)}")

    # pooled OOS daily-R + deflation
    oos = tr[tr["leg"] == "OOS"]
    daily = oos.groupby("day")["r_net"].sum()
    v = daily.to_numpy()
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v)))
    hair96 = math.sqrt(2 * math.log(96))
    print(f"\npooled OOS daily-R: n_days={len(v)} sum={v.sum():+.1f}R t={t:+.2f} "
          f"deflated(96 trials)={t - hair96:+.2f} [haircut {hair96:.2f}]")
    mc = mc_iid(oos["r_net"].to_numpy(float))
    bb = block_boot(v)
    print(f"MC iid: P(profit)={mc['p_profit']:.4f} ci5={mc['ci5']:+.1f}R dd95={mc['dd95']:.1f}R | "
          f"block boot(5d): P(profit)={bb['p_profit']:.4f} ci5={bb['ci5']:+.1f}R")

    # spread stress
    for mult in (1.5, 2.0):
        tr2, _ = run_all(mult)
        tr2["day"] = tr2["entry_time"].map(lambda d: str(pd.Timestamp(d).date()))
        tr2["leg"] = np.where(tr2["day"].isin(is_days), "IS", "OOS")
        o2 = tr2[tr2["leg"] == "OOS"]
        d2 = o2.groupby("day")["r_net"].sum()
        v2 = d2.to_numpy()
        t2 = v2.mean() / (v2.std(ddof=1) / math.sqrt(len(v2))) if len(v2) > 2 else 0.0
        print(f"spread x{mult}: OOS n={len(o2)} sum={v2.sum():+.1f}R t={t2:+.2f} pf={metrics(o2)['pf']:.2f}")

    with open(os.path.join(OUT, "ivwall_audit_summary.json"), "w") as f:
        json.dump({"is": metrics(tr[tr['leg'] == 'IS']), "oos": metrics(oos),
                   "mc": mc, "bb": bb, "pooled_t": t, "hair96": hair96,
                   "deflated_t_96": t - hair96,
                   "is_screen_subset": sel,
                   "window": {"start": all_days[0], "end": all_days[-1], "oos_start": all_days[k]}}, f, indent=1)


if __name__ == "__main__":
    main()
