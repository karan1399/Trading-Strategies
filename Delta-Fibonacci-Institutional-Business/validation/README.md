# Validation Layer — Independent Backtest, Optimization & Robustness Results

**Verdict: DO NOT LAUNCH.** No configuration of the multi-dimensional VWAP/EMA
rejection system survives real-cost, out-of-sample validation on the
`historical_parquet/` store. Full report:
[`Delta_Fibonacci_Validation_Report.docx`](Delta_Fibonacci_Validation_Report.docx)
(+ `.pdf`), workbook: [`Delta_Fibonacci_Validation.xlsx`](Delta_Fibonacci_Validation.xlsx),
machine summary: [`dfib_summary.json`](dfib_summary.json).

## The verdict ladder (every layer under real broker spreads, 70/30 IS/OOS)

| Layer | Result |
| --- | --- |
| Their 362 champion setups, as recorded | **+30,938R**, median PF 1.17 (zero spread, full-sample selection) |
| Same setups, corrected engine, spreads OFF | +5,105R, median PF 1.14 (engine fidelity confirmed) |
| Same setups, corrected engine, real spreads | **−38,762R**, median PF 0.84, only 126/362 positive |
| Pooled same-config systems (240 variants: 2 universes × cost gates × stop floors) | **0 pass** IS gates (best IS PF 1.04 → OOS 0.85) |
| Best disciplined variant: 15-symbol plateau portfolio (IS-only selection) | IS +327R PF 1.58 → **OOS −8.1R PF 0.96** |
| $5,000 equity sim of best variant (concurrency-sized risk, 0.17%/trade) | $5,000 → $8,605 (+72%), all of it in-sample; OOS flat; max DD 6.3% |

Why the claim failed: (1) at 5M/15M frequency the round-trip spread exceeds the
per-trade edge (e.g. HK50 15M: 55-pt spread vs 34-pt ATR ≈ 2R donated per trade);
(2) picking the best of thousands of configs on the full sample guarantees
inflated numbers — with 27,090 configs evaluated, the extreme order statistics
of noise are large. The registry's 100% positive share is the signature.

## Files

- `dfib_engine.py` — faithful port of `engine/` semantics (VWAP bands, 5 rejection
  patterns, structural stops, fixed-RR/trailing exits, cooldowns) **with real
  spread costs on both sides**, cost-gate and stop-floor parameters, IS/OOS split.
- `dfib_sweep.py` — Stage 1: 23 champion assets, 10,350 configs.
- `dfib_stage3.py` / `dfib_stage3b.py` — pooled same-config portfolios + refinements.
- `dfib_verify_champions.py` — re-simulates all 362 registry champions (spreads on/off).
- `dfib_stage4a.py` / `dfib_stage4b.py` — full-universe 151-symbol 1H/4H sweep and
  the pre-registered plateau-selection portfolio + battery.
- `dfib_robustness.py` — Monte Carlo (10k), stationary block bootstrap, Deflated
  Sharpe (27,090 trials), rolling thirds, cost stress.
- `dfib_make_charts.py` / `dfib_make_summary_xlsx.py` — figures and workbook.
- `results/` — verification tables, sweep outputs, portfolio picks and ledgers.

## Re-run from the repo root

```bash
pip install pandas pyarrow numpy scipy matplotlib openpyxl
cd Delta-Fibonacci-Institutional-Business/validation
python3 dfib_verify_champions.py     # ~1 min — reproduces Table 1 of the report
python3 dfib_sweep.py                # ~3 min — Stage 1
python3 dfib_stage3b.py              # ~4 min — pooled portfolios
python3 dfib_stage4a.py && python3 dfib_stage4b.py   # ~6 min — portfolio + battery
python3 dfib_make_charts.py && python3 dfib_make_summary_xlsx.py
```

Total runtime ≈ 15 minutes on 4 cores. The engine reads
`../../historical_parquet/` by default; adjust `PARQUET_ROOT` to point elsewhere.

## When to revisit this verdict

Re-run the pipeline monthly as bars accumulate. If a re-run shows **OOS PF > 1.2
on ≥ 100 untouched trades** for any pre-registered selection (never full-sample),
the verdict can be revisited. The only corner with measurable signal: wide-stop,
low-spread instruments (gold micro-crosses, JPY crosses) at 4H with a cost gate
(skip entries when round-trip spread > ⅓ of stop distance) — the gate lifted
pooled IS PF by +0.14, the only refinement that did anything.
