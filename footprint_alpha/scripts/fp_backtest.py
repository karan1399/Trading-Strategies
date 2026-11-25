#!/usr/bin/env python3
"""Fee-aware event-loop backtest: raw auction-complete rule vs ML-filtered.

Both strategies run through the SAME engine on out-of-fold events:
  - one position at a time (realistic for manual order punching)
  - fixed $ risk per trade (default $25, env FP_RISK_USD)
  - pessimistic barrier outcomes from labeling (SL-first on ambiguity)
  - taker or maker fees per entry mode (in R units)
  - bootstrap 95% CI + probability of profit (5000 resamples)
  - deflated Sharpe: PSR tested against sqrt(2 ln N_trials)
Outputs: footprint/results/fp_backtest.json, equity_curve.png, trades csv.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fp_core import BASE  # noqa: E402

RES = os.path.join(BASE, "results")
MODEL_DIR = os.path.join(BASE, "model")
RISK_USD = float(os.environ.get("FP_RISK_USD", 25.0))
BAR_MIN = 5


def simulate(ev, label):
    """One-position-at-a-time walk over events sorted by signal time."""
    ev = ev.sort_values("ts").reset_index(drop=True)
    taken, free_after = [], pd.Timestamp("1970-01-01", tz="UTC")
    for _, t in ev.iterrows():
        start = t.ts
        if start <= free_after:
            continue
        taken.append(t)
        free_after = start + pd.Timedelta(minutes=BAR_MIN * (int(t.n_bars) + 2))
    tr = pd.DataFrame(taken)
    if len(tr) == 0:
        return {"label": label, "n": 0}, tr
    r = tr.exit_r.to_numpy()
    adj = tr.exit_r.to_numpy() - tr.fee_r.to_numpy()
    wins, losses = r[r > 0], r[r <= 0]
    eq = np.cumsum(adj * RISK_USD)
    dd = float((np.maximum.accumulate(eq) - eq).max()) if len(eq) else 0.0
    return {
        "label": label, "n": int(len(tr)), "risk_usd": RISK_USD,
        "win_rate": float((r > 0).mean()), "mean_r": float(r.mean()),
        "pf": float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() < 0 else float("inf"),
        "fee_adj_mean_r": float(adj.mean()),
        "total_usd": float(adj.sum() * RISK_USD),
        "max_dd_usd": dd,
        "sharpe_trade": float(adj.mean() / adj.std()) if len(adj) > 2 else 0.0,
        "n_days": float((tr.ts.max() - tr.ts.min()).total_seconds() / 86400),
    }, tr


def bootstrap(adj_r, n=5000, seed=13):
    rng = np.random.default_rng(seed)
    sums = np.array([adj_r[rng.integers(0, len(adj_r), len(adj_r))].sum()
                    for _ in range(n)])
    return {"ci5_usd": float(np.quantile(sums, 0.05) * RISK_USD),
            "ci95_usd": float(np.quantile(sums, 0.95) * RISK_USD),
            "p_profit": float((sums > 0).mean())}


def deflated_sharpe(adj, n_trials):
    """PSR evaluated against the expected max Sharpe of n_trials configs."""
    from math import erf, sqrt
    from scipy.stats import skew
    n = len(adj)
    sr = adj.mean() / adj.std() if n > 2 else 0.0
    sk = float(skew(adj))
    sr0 = sqrt(2 * np.log(max(n_trials, 2))) / sqrt(max(n - 1, 1))
    z = (sr - sr0) * sqrt(max(n - 1, 1)) / sqrt(max(1 - sk * sr + sr * sr / 4, 1e-9))
    return {"sr": float(sr), "sr0": float(sr0), "dsr_prob": float(0.5 * (1 + erf(z / sqrt(2))))}


def main():
    oof = pd.read_parquet(os.path.join(RES, "oof.parquet"))
    meta = json.load(open(os.path.join(MODEL_DIR, "meta.json")))
    thr = meta["threshold"]
    evj = {}
    if os.path.exists(os.path.join(RES, "fp_events.json")):
        evj = json.load(open(os.path.join(RES, "fp_events.json")))

    oof["adj_r"] = oof.exit_r - oof.fee_r
    n_sel = int((oof.p >= thr).sum())
    print(f"OOF events={len(oof)}  threshold={thr:.2f}  taken by ML={n_sel} "
          f"({n_sel/len(oof)*100:.0f}%)\n")

    m_raw, tr_raw = simulate(oof, "raw rule (all events)")
    m_ml, tr_ml = simulate(oof[oof.p >= thr], f"ML filtered (p>={thr:.2f})")
    for m in (m_raw, m_ml):
        print(f"{m['label']:<26} n={m.get('n',0):>4} win={m.get('win_rate',0)*100:5.1f}% "
              f"meanR={m.get('mean_r',0):+.3f} feeAdj={m.get('fee_adj_mean_r',0):+.3f} "
              f"PF={m.get('pf',0):.2f} total=${m.get('total_usd',0):+,.0f} "
              f"maxDD=${m.get('max_dd_usd',0):,.0f}")

    out = {"risk_usd": RISK_USD, "threshold": thr, "raw": m_raw, "ml": m_ml}
    if n_sel > 10:
        adj = tr_ml.exit_r.to_numpy() - tr_ml.fee_r.to_numpy()
        bs = bootstrap(adj)
        n_trials = int(evj.get("trials", 48)) + 20  # events grid + train configs
        dsr = deflated_sharpe(adj, n_trials)
        out["ml_bootstrap"] = bs
        out["ml_deflated_sharpe"] = dsr
        out["n_trials"] = n_trials
        print(f"\nML bootstrap: 95% CI total ${bs['ci5_usd']:+,.0f} .. ${bs['ci95_usd']:+,.0f}, "
              f"P(profit)={bs['p_profit']*100:.0f}%")
        print(f"deflated Sharpe (vs {n_trials} trials): SR={dsr['sr']:.3f} "
              f"SR*={dsr['sr0']:.3f} DSR prob={dsr['dsr_prob']:.3f}")

    # monthly breakdown of the ML strategy
    if n_sel > 0:
        tr_ml2 = tr_ml.copy()
        tr_ml2["month"] = tr_ml2.ts.dt.strftime("%Y-%m")
        adj2 = tr_ml2.exit_r - tr_ml2.fee_r
        mon = pd.DataFrame({"month": tr_ml2.month, "adj_r": adj2}).groupby("month").agg(
            ["count", "sum"])
        mon.columns = ["n", "sum_r"]
        mon["usd"] = mon.sum_r * RISK_USD
        print("\nmonthly (ML strategy, fee-adj):")
        print(mon.to_string())
        out["monthly"] = {k: [int(v.n), float(v.sum_r), float(v.usd)]
                          for k, v in mon.iterrows()}
        tr_ml.to_csv(os.path.join(RES, "trades_ml.csv"), index=False)

    # equity curve chart (English labels; fees included)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.font_manager as fm
    fm.fontManager.addfont('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
    for tr, name, col in ((tr_raw, "raw auction-complete rule", "#888c93"),
                         (tr_ml, "ML-filtered", "#0b7285")):
        if len(tr):
            eq = np.cumsum((tr.exit_r.to_numpy() - tr.fee_r.to_numpy()) * RISK_USD)
            ax.plot(tr.ts.to_numpy(), eq, label=f"{name} (n={len(tr)})", color=col, lw=1.6)
    ax.axhline(0, color="#adb5bd", lw=0.8)
    ax.set_title(f"BTCUSDT 5m auction-complete strategies - fee-adjusted, ${RISK_USD:.0f} risk/trade")
    ax.set_ylabel("cumulative $")
    ax.legend(loc="upper left", bbox_to_anchor=(0.01, 0.99))
    fig.savefig(os.path.join(RES, "equity_curve.png"), dpi=110)
    with open(os.path.join(RES, "fp_backtest.json"), "w") as f:
        json.dump(out, f, indent=1, default=float)
    print("\nsaved fp_backtest.json, equity_curve.png, trades_ml.csv")


if __name__ == "__main__":
    main()
