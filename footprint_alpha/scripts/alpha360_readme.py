#!/usr/bin/env python3
"""Generate the alpha360_ema_rejection/README.md from alpha360_summary.json."""
import json
import os

DL = "/home/z/my-project/download/alpha360"
S = json.load(open(os.path.join(DL, "alpha360_summary.json")))
D, C, BL, G = S["diversified"], S["concentrated"], S["baseline"], S["grid"]
E = D["equity"]


def f1(x):
    return "-" if x is None else f"{float(x):.1f}"


def f2(x):
    return "-" if x is None else f"{float(x):.2f}"


div_members = S["div_members"]
fam_count = {}
for m in div_members:
    fam_count[m["family"]] = fam_count.get(m["family"], 0) + 1
fam_str = ", ".join(f"{k} x{v}" for k, v in sorted(fam_count.items(), key=lambda kv: -kv[1]))

md = f"""# Alpha360 — 360° Exploration of the HTF EMA Rejection Concept

The validated 89/233 baseline (`../htf_ema_rejection/`) re-opened across its full
design space: **11 EMA pairs** (Fibonacci ladder 8/21→144/377 incl. the baseline
89/233, plus classics 20/50, 50/100, 50/200, 100/200) × **3 reject modes**
(fast / slow / compressed band) × **6 patterns** (P1 wick, P2 two-bar, P3 pierce,
P4 cluster + new P5 inside-bar, P6 engulfing) × **4 gate combos** × **9 exits**,
then a confluence stage (RSI zone, ATR regime, volume surge, session, XAUUSD/
USDJPY cross-asset confirmation) on every surviving plateau cell.

**Trials: {S['n_trials']:,} configurations** across 151 symbols at 1H/4H, real
broker spreads, strict 70/30 IS/OOS discipline, DSR deflated by the full trial
count. Two baskets from the same IS-only plateau selection:

| Basket | IS | OOS (single-shot) |
| --- | --- | --- |
| Baseline 89/233 (prior study) | +{f1(BL['is']['total_r'])}R PF {f2(BL['is']['pf'])} (n={BL['is']['n']}) | +{f1(BL['oos']['total_r'])}R PF {f2(BL['oos']['pf'])} (n={BL['oos']['n']}) |
| Alpha360 CONCENTRATED | +{f1(C['is']['total_r'])}R PF {f2(C['is']['pf'])} (n={C['is']['n']}) | +{f1(C['oos']['total_r'])}R PF {f2(C['oos']['pf'])} (n={C['oos']['n']}) |
| **Alpha360 DIVERSIFIED** (≤3 per family) | +{f1(D['is']['total_r'])}R PF {f2(D['is']['pf'])} (n={D['is']['n']}) | **+{f1(D['oos']['total_r'])}R PF {f2(D['oos']['pf'])} (n={D['oos']['n']})** |

Diversified family mix: {fam_str}. OOS t-stat {f2(D.get('oos_t_stat'))};
MC p-profit {round(D['mc']['p_profit'], 3)}; block bootstrap {round(D['bb']['p_profit'], 3)};
DSR {round(D['dsr']['dsr_prob'], 3)} across {S['n_trials']:,} trials (mixed stream —
the OOS row is the gate). $5,000 equity sim at concurrency-sized risk
({round(E['per_trade_risk_frac'] * 100, 2)}%/trade, max {E['max_concurrent']} open):
**${E['start_usd']:,.0f} → ${E['end_usd']:,.0f}**, max DD {f1(E['max_dd_pct'])}%.

Full report: [`Alpha360_EMA_Rejection_Report.pdf`](Alpha360_EMA_Rejection_Report.pdf) ·
workbook (incl. MT5 calibration): [`Alpha360_EMA_Rejection_Basket.xlsx`](Alpha360_EMA_Rejection_Basket.xlsx) ·
numbers: [`alpha360_summary.json`](alpha360_summary.json)

## Pipeline

- `alpha360_engine.py` — pair grid, reject modes, P1–P6 patterns, gate masks
- `alpha360_common.py` — families, RSI/ATR/volume/session masks, cross-asset drivers
- `alpha360_sweep_a.py` — stage 1 full-universe sweep (resumable JSONL)
- `alpha360_sweep_b.py` — stage 2 confluence overlays on plateau cells
- `alpha360_stage_c.py` — full-exit plateau, dual baskets, single OOS, battery,
  baseline comparison
- `alpha360_summarize.py` — equity sim, charts, workbook, MT5 calibration

Re-run from the repo root (~40 min on 2 cores):

```bash
pip install pandas pyarrow numpy scipy matplotlib openpyxl
cd alpha360_ema_rejection
python3 alpha360_sweep_a.py && python3 alpha360_sweep_b.py && python3 alpha360_stage_c.py
python3 alpha360_summarize.py   # reads ../historical_parquet/ by default
```

## Pre-registered selection rules (IS only)

qualify `is_n ≥ 15, is_pf ≥ 1.15, is_r > 0` · plateau cell `(symbol, tf, pair,
reject, pattern, overlay)` needs `n_qual ≥ 3` · pool = top 90 cells by aggregate
IS R, ≤3 cells per symbol · pick = median-IS-R config of the cell's full
4-gate × 9-exit grid, `pick_is_n ≥ 20` · basket = one cell per symbol, cap 15 ·
DIVERSIFIED adds ≤3 per family (gold, JPY, USD-majors, crypto, indices, energy,
metals, FX-crosses). The OOS column is read once per basket at the end.

## Honest notes

- The pipeline was dry-run on partial sweep data (75 early symbols, pre-gold)
  to shake out mechanics; two IS-only rule refinements (plateau ≥3, pick n≥20)
  were adopted at that point for consistency with the baseline study's
  registered rules. No OOS-based rule changes.
- Members with tiny samples, 1H OOS windows of only ~2 months, and the
  family-cluster structure (gold crosses = one bet) all still apply — read the
  report's concentration section before sizing up.
"""

with open(os.path.join("/home/z/my-project/repos/karan-ts/alpha360_ema_rejection", "README.md"), "w") as f:
    f.write(md)
print("README.md written", len(md), "chars")
