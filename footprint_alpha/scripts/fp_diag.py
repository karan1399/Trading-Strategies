#!/usr/bin/env python3
"""Diagnose aggTrades vs klines taker-buy volume mismatch: per-day corr in
both directions (is our side interpretation inverted?), worst bars, and
whether errors cluster (maintenance days) or are uniform."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fp_core import BASE, load_all  # noqa: E402

KL = os.path.join(BASE, "klines")


def main():
    bars, _, _, days = load_all()
    kl = pd.concat([pd.read_parquet(os.path.join(KL, f))
                    for f in sorted(os.listdir(KL))[:len(days)]], ignore_index=True)
    m = bars.merge(kl, left_on="ts", right_on="open_ts", how="inner")
    m["day"] = m.ts.dt.date.astype(str)
    m["bv"] = m.buy_v + m.sell_v
    rows = []
    for d, g in m.groupby("day"):
        rows.append({
            "day": d,
            "n": len(g),
            "corr_buy_tbv": np.corrcoef(g.buy_v, g.tbv)[0, 1],
            "corr_sell_tbv": np.corrcoef(g.sell_v, g.tbv)[0, 1],
            "p50_reldiff": (abs(g.buy_v - g.tbv) /
                            np.maximum(g.bv, 1e-9)).median(),
            "p999_reldiff": (abs(g.buy_v - g.tbv) /
                             np.maximum(g.bv, 1e-9)).quantile(0.999),
        })
    t = pd.DataFrame(rows).sort_values("corr_buy_tbv")
    print("worst 8 days by corr(buy_v, kline_taker_buy):")
    print(t.head(8).to_string(index=False))
    print("\nbest 3 days:")
    print(t.tail(3).to_string(index=False))
    bad = m.assign(rd=abs(m.buy_v - m.tbv) / np.maximum(m.bv, 1e-9))
    bad = bad.sort_values("rd", ascending=False).head(10)
    print("\nworst 10 bars:")
    print(bad[["ts", "day", "buy_v", "sell_v", "vol", "tbv", "cnt", "rd"]]
          .to_string(index=False))
    print("\noverall corr(sell_v, tbv) =",
          round(np.corrcoef(m.sell_v, m.tbv)[0, 1], 6),
          " vs corr(buy_v, tbv) =",
          round(np.corrcoef(m.buy_v, m.tbv)[0, 1], 6))


if __name__ == "__main__":
    main()
