#!/usr/bin/env python3
"""Stage B: FX-scoped 2SD wall-fade refinement grid."""
import itertools
import multiprocessing as mp
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivwall_engine import Config, run_config, list_symbols, metrics
from ivwall_runner import classify

OUT = "/home/z/my-project/ivwall_results"
os.makedirs(OUT, exist_ok=True)
TF = sys.argv[1] if len(sys.argv) > 1 else "15M"
SYMS = [s for s in list_symbols(TF) if classify(s) == "fx"]
print(f"FX symbols on {TF}: {len(SYMS)}")


def work(args):
    name, kw = args
    cfg = Config(name=name, **kw)
    tr = run_config(TF, SYMS, cfg)
    if len(tr) == 0:
        return {"name": name, **kw, "all_n": 0}
    q = tr["entry_time"].quantile(0.70)
    is_tr, oos_tr = tr[tr.entry_time <= q], tr[tr.entry_time > q]
    row = {"name": name, **kw, **{f"all_{k}": v for k, v in metrics(tr).items()},
           **{f"is_{k}": v for k, v in metrics(is_tr).items()},
           **{f"oos_{k}": v for k, v in metrics(oos_tr).items()}}
    if row["is_n"] >= 80:
        tr.to_parquet(os.path.join(OUT, f"trades_{name}.parquet"))
    return row


def main():
    grid = []
    for sd, vol, m, buf, trend in itertools.product(
        [1.5, 2.0, 2.5], ["ewma_cc", "atr"], [0.0, 0.5, 1.0], [0.5, 1.0], ["none", "fade_flat"],
    ):
        grid.append((f"fadeB_sd{sd:g}_{vol}_m{m:g}_b{buf:g}_{trend}", dict(
            strategy="fade_touch", sd_wall=sd, vol_estimator=vol, target_mode="wall_m",
            target_m=m, buffer_atr=buf, trend_filter=trend, min_rr=0.3,
        )))
    with mp.Pool(min(8, mp.cpu_count())) as pool:
        rows = pool.map(work, grid)
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(OUT, f"stage_b_{TF}.csv"), index=False)
    cols = ["name", "all_n", "all_pf", "is_n", "is_pf", "is_total_r", "oos_n", "oos_pf", "oos_total_r", "oos_max_dd_r"]
    res = res[res["is_n"] >= 80]
    print("== top 15 by IS PF ==")
    print(res[cols].sort_values("is_pf", ascending=False).head(15).round(3).to_string(index=False))
    print("\n== top 15 by OOS PF ==")
    print(res[cols].sort_values("oos_pf", ascending=False).head(15).round(3).to_string(index=False))


if __name__ == "__main__":
    main()
