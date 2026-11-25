#!/usr/bin/env python3
"""Final improvement round: frequency bump, vol-regime gate, basket MC."""
import multiprocessing as mp
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivwall_engine import Config, run_config, list_symbols, metrics
from ivwall_robustness import mc_iid, block_bootstrap
from ivwall_runner import classify

OUT = "/home/z/my-project/ivwall_results"
FX = [s for s in list_symbols("15M") if classify(s) == "fx"]


def run_cfg(**kw):
    cfg = Config(name="x", strategy="fade_touch", vol_estimator="ewma_cc",
                 target_mode="wall_m", sd_wall=2.0, target_m=0.5, buffer_atr=1.0,
                 min_rr=0.3, **kw)
    tr = run_config("15M", FX, cfg)
    if len(tr):
        tr["iclass"] = tr["symbol"].map(classify)
    return tr


def mrow(tag, tr):
    m = metrics(tr)
    r = tr["r_net"].values
    mc = mc_iid(r, n=5000)
    return {"tag": tag, "n": m["n"], "total_r": round(m["total_r"], 1), "pf": round(m["pf"], 3),
            "wr": round(m["win_rate"], 3), "dd": round(m["max_dd_r"], 1),
            "mc_p": round(mc["p_profit"], 3), "mc_ci5": round(mc["total_ci5"], 1)}


def main():
    rows = []
    base = run_cfg()
    rows.append(mrow("C1 base", base))
    q = base["entry_time"].quantile(0.70)

    d3 = run_cfg(max_trades_session=3, cooldown_bars=4)
    rows.append(mrow("D: 3 trades/session, cd4", d3))

    for vq in [0.3, 0.5]:
        e = run_cfg(vol_q_min=vq)
        rows.append(mrow(f"E: vol gate q>={vq}", e))

    e2 = run_cfg(vol_q_min=0.3, max_trades_session=3, cooldown_bars=4)
    rows.append(mrow("F: vol gate 0.3 + 3/session", e2))

    res = pd.DataFrame(rows)
    print(res.to_string(index=False))

    # final flagship: best of the above by OOS-aware judgement; recompute IS/OOS for F
    for tag, tr in [("C1", base), ("F", e2)]:
        q = tr["entry_time"].quantile(0.70)
        print(f"\n{tag} IS :", {k: round(v,3) for k,v in metrics(tr[tr.entry_time<=q]).items()})
        print(f"{tag} OOS:", {k: round(v,3) for k,v in metrics(tr[tr.entry_time>q]).items()})

    # basket MC on top of flagship
    # Flagship = pre-registered C1 (sd2/m0.5/b1). F is rejected on OOS evidence
    # (PF 0.51) even though its all-period PF looks higher — never select on the
    # full sample, and never select the variant that failed its own OOS gate.
    best = base
    best.to_parquet(os.path.join(OUT, "trades_flagship.parquet"))
    q = best["entry_time"].quantile(0.70)
    g = best[best.entry_time <= q].groupby("symbol").agg(
        n=("r_net", "count"),
        pf=("r_net", lambda x: x[x > 0].sum() / max(1e-9, abs(x[x <= 0].sum()))),
        tot=("r_net", "sum"))
    top20 = g[(g.n >= 5) & (g.pf > 1)].sort_values("tot", ascending=False).head(20).index.tolist()
    b = best[best.symbol.isin(top20)]
    qb = b["entry_time"].quantile(0.70)
    rb = b["r_net"].values
    print(f"\nFLAGSHIP={best.attrs.get('name','?')} basket top-{len(top20)}: "
          f"IS {metrics(b[b.entry_time<=qb])} \n OOS {metrics(b[b.entry_time>qb])}")
    print(" basket MC:", mc_iid(rb, n=10000))
    print(" basket BB:", block_bootstrap(rb, n=5000))
    b.to_parquet(os.path.join(OUT, "trades_basket.parquet"))
    top20s = pd.Series(top20)
    top20s.to_csv(os.path.join(OUT, "basket_symbols.csv"), index=False)


if __name__ == "__main__":
    main()
