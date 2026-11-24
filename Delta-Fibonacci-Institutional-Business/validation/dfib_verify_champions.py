#!/usr/bin/env python3
"""Verify their exact champion setups (audit/expanded_multidim_champions.json)
under the corrected engine: real spreads vs no spread, vs their recorded numbers.
"""
import json
import os
import re
import sys

import numpy as np
import pandas as pd

from pathlib import Path
validation_dir = Path(__file__).resolve().parent
REPO = str(validation_dir.parent)
repo_root = str(validation_dir.parent.parent)
OUT = str(validation_dir / "results")
os.makedirs(OUT, exist_ok=True)

sys.path.insert(0, str(validation_dir))
sys.path.insert(0, REPO)
sys.path.insert(0, repo_root)
from dfib_engine import load_symbol, precompute_signals, simulate, SimCfg, metrics, split_is_oos

champs = json.load(open(os.path.join(REPO, "audit", "expanded_multidim_champions.json")))
print("symbols in their champions file:", len(champs))

rows = []
for symbol, payload in champs.items():
    tfs = payload.get("timeframes", payload)
    if not isinstance(tfs, dict):
        continue
    for tf, cfg in tfs.items():
        if not isinstance(cfg, dict) or "model" not in cfg:
            continue
        model = cfg["model"]                       # EMA | VWAP
        param = cfg.get("param", "")               # EMA_50_MULTIVARIANT | SD_BANDS_MULTIVARIANT
        exit_mode = cfg.get("exit_mode", "fixed_2.0r")
        m = re.match(r"EMA_(\d+)(_MULTIVARIANT)?", param)
        if model == "EMA" and m:
            period, mv = int(m.group(1)), bool(m.group(2))
        elif model == "VWAP":
            period, mv = 0, "MULTIVARIANT" in param
        else:
            continue
        try:
            df = load_symbol(tf, symbol)
            sigs, setups, rds = precompute_signals(df, symbol, tf, model,
                                                   ema_period=period, multivariant=mv)
            res = {}
            for spread_on in (False, True):
                tr = simulate(df, sigs, setups, rds, symbol, tf,
                              SimCfg(exit_mode=exit_mode, spread_on=spread_on))
                res[spread_on] = metrics(tr)
            theirs = {"n": cfg.get("total_trades"), "net_r": cfg.get("net_r"),
                      "pf": cfg.get("profit_factor")}
            rows.append({
                "symbol": symbol, "tf": tf, "model": model,
                "param": param, "exit": exit_mode,
                "their_n": theirs["n"], "their_net_r": theirs["net_r"], "their_pf": theirs["pf"],
                "ours_n_nospread": res[False]["n"], "ours_r_nospread": res[False]["total_r"],
                "ours_pf_nospread": res[False]["pf"],
                "ours_n_spread": res[True]["n"], "ours_r_spread": res[True]["total_r"],
                "ours_pf_spread": res[True]["pf"],
                "ours_dd_spread": res[True]["max_dd_r"],
            })
        except Exception as e:  # noqa: BLE001
            rows.append({"symbol": symbol, "tf": tf, "error": str(e)})

df = pd.DataFrame(rows)
ok = df[~df.get("error", pd.Series(index=df.index, dtype=bool)).notna()] if "error" in df else df
df.to_csv(os.path.join(OUT, "champions_verification.csv"), index=False)
print(f"verified {len(ok)} champion setups (of their file)")

print("\n== aggregate: their claim vs corrected engine ==")
print(f"their total net R       : {ok['their_net_r'].sum():+.1f}R over {int(ok['their_n'].sum())} trades")
print(f"ours, no spread         : {ok['ours_r_nospread'].sum():+.1f}R over {int(ok['ours_n_nospread'].sum())} trades")
print(f"ours, real spreads      : {ok['ours_r_spread'].sum():+.1f}R over {int(ok['ours_n_spread'].sum())} trades")
for col in ["their_pf", "ours_pf_nospread", "ours_pf_spread"]:
    print(f"median {col:18}: {ok[col].median():.2f}")
print(f"their setups positive (their numbers): {(ok['their_net_r'] > 0).sum()}/{len(ok)}")
print(f"ours positive with spreads          : {(ok['ours_r_spread'] > 0).sum()}/{len(ok)}")

print("\n== worst 10 champion setups under real spreads ==")
cols = ["symbol", "tf", "param", "exit", "their_net_r", "ours_r_nospread", "ours_r_spread", "ours_pf_spread"]
print(ok.sort_values("ours_r_spread").head(10)[cols].round(1).to_string(index=False))
print("\n== best 10 under real spreads ==")
print(ok.sort_values("ours_r_spread", ascending=False).head(10)[cols].round(1).to_string(index=False))
