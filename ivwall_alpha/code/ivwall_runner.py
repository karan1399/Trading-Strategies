#!/usr/bin/env python3
"""Stage A screening: 24 IV-wall configs x 151 symbols x 15M, IS/OOS ranked."""
import os
import sys
import itertools
import multiprocessing as mp

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivwall_engine import Config, run_config, list_symbols, metrics

OUT = "/home/z/my-project/ivwall_results"
os.makedirs(OUT, exist_ok=True)
TF = sys.argv[1] if len(sys.argv) > 1 else "15M"
SYMS = list_symbols(TF)


def classify(sym: str) -> str:
    fx = {"EUR","GBP","USD","JPY","CHF","AUD","NZD","CAD","NOK","SEK","SGD","HKD","CNH","HUF","MXN","ZAR","CZK","PLN","TRY","DKK"}
    base = sym.split(".")[0]
    if sym in ("XAUUSD.pi", "XAGUSD.pi", "XPTUSD.pi", "XPDUSD.pi"):
        return "metal"
    if sym in ("USOIL.pi", "UKOIL.pi", "NATGAS.pi", "COPPER.p", "ALUMINIUM.p", "GAS.p", "GASOLINE.p", "HEATOIL.p", "CORN.p", "WHEAT.p", "SOYBEAN.p", "SUGAR.p", "COFFEE.p", "COCOA.p", "COTTON.p"):
        return "commodity"
    if base in ("SPX500", "UKO", "NDX100", "GER40", "US30", "US500", "USTEC", "JP225", "HK50", "AU200", "EU50", "CHINA50", "CHINAH", "US2000", "CA60", "ES35", "MIB40", "MEXBOL", "NETH25", "GER75", "SMI20", "AUS200"):
        return "index"
    if len(base) == 6 and base[:3] in fx and base[3:] in fx:
        return "fx"
    return "crypto"


def work(args):
    name, kw = args
    cfg = Config(name=name, **kw)
    tr = run_config(TF, SYMS, cfg)
    if len(tr) == 0:
        return {"name": name, **kw, "all_n": 0}
    tr["iclass"] = tr["symbol"].map(classify)
    q = tr["entry_time"].quantile(0.70)
    is_tr, oos_tr = tr[tr.entry_time <= q], tr[tr.entry_time > q]
    row = {"name": name, **kw, **{f"all_{k}": v for k, v in metrics(tr).items()},
           **{f"is_{k}": v for k, v in metrics(is_tr).items()},
           **{f"oos_{k}": v for k, v in metrics(oos_tr).items()}}
    tr.to_parquet(os.path.join(OUT, f"trades_{name}.parquet"))
    return row


def main():
    grid = []
    for strat, k, vol in itertools.product(
        ["fade_touch", "rejection", "breakout", "exhaustion"],
        [1.0, 2.0],
        ["ewma_cc", "parkinson", "atr"],
    ):
        tgt = {"fade_touch": "open", "rejection": "open", "exhaustion": "open",
               "breakout": "next_wall"}[strat]
        grid.append((f"{strat}_sd{k:g}_{vol}", dict(
            strategy=strat, sd_wall=k, vol_estimator=vol, target_mode=tgt,
            buffer_atr=0.5, min_rr=0.5 if strat in ("fade_touch", "rejection", "exhaustion") else 0.0,
        )))
    with mp.Pool(min(8, mp.cpu_count())) as pool:
        rows = pool.map(work, grid)
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(OUT, f"stage_a_{TF}.csv"), index=False)
    cols = ["name", "all_n", "all_total_r", "all_pf", "all_win_rate", "is_total_r", "is_pf",
            "oos_total_r", "oos_pf", "oos_max_dd_r", "oos_n"]
    res = res.sort_values("is_total_r", ascending=False)
    print(res[cols].head(15).round(3).to_string(index=False))
    print("\n--- by OOS total_r ---")
    print(res[cols].sort_values("oos_total_r", ascending=False).head(15).round(3).to_string(index=False))


if __name__ == "__main__":
    main()
