# IV Wall Alpha — FX 2SD Wall-Fade (MT5 Deployment Package)

Deployable research package for the **IV Wall strategy** on the `historical_parquet/`
data store: session-anchored expected-move envelopes, 2-SD wall fade on FX,
**alpha basket only** (20 in-sample-selected pairs), fully charged broker spreads,
validated with Monte Carlo / block bootstrap / walk-forward / Deflated Sharpe.

Full study: `IV_Wall_Strategy_Report.docx` (+ `.pdf`). Numbers: `summary.json`,
`IV_Wall_Results.xlsx`, `IV_Wall_Equity_Simulation.xlsx`.

## The strategy (exactly what to punch in MT5)

- **Chart**: M15 only. **Universe**: the 20 pairs in `mt5_lot_table.csv` — nothing else.
- Each broker day (day boundary 22:00 UTC = 00:00 broker):
  1. Take the last M15 bar **before** the session opens → its EWMA(0.94) per-bar sigma
     (risk-engine convention in `code/ivwall_engine.py`).
  2. Expected move `EM = session_open x sigma x sqrt(expected session bars)` — walls at
     `open ± 2.0 x EM`, **fixed for the whole day, never repainted**.
  3. **Entry**: a bar touches a wall (high ≥ upper / low ≤ lower) and **closes back inside**
     → sell/buy at the open of the next bar, against the move.
  4. **Stop**: 1.0 x ATR(14) beyond the wall. **Target**: halfway back (open ± 0.5 x EM).
     **Time stop**: flat at the last bar of the broker day.
  5. **Skips**: stop distance > 1.2 x EM · spread > 1.0 x ATR · 2 trades max per day per pair ·
     6-bar cooldown after any exit.
- **Risk**: 1% of current equity per trade (50 USD at a 5,000 USD start), compounding.
  Lots = `risk$ / (stop_pips x pip_value_usd)` — pre-computed per pair in
  `mt5_lot_table.csv` (recompute the risk$ part daily as equity moves; volumes round
  down to 0.01).

## Validation snapshot (fully charged spreads)

| Leg | Result |
| --- | --- |
| Basket IS (selection window) | +58.6R, PF 1.86, n=134 |
| Basket OOS (untouched) | +27.8R, **PF 2.00**, t=1.98, n=58 |
| Monte Carlo (10k) | P(profit) 0.9999, 5th pct +45R |
| Block bootstrap | P(profit) 0.9996, DD95 18.0R |
| Deflated Sharpe (96 trials) | **0.88** (only config passing 0.50) |
| All-FX without basket | PF 1.13, MC 0.84, fails 2x-spread stress — do NOT trade |
| Timeframes | 15M only (1H 0.68 / 4H 1.00 / 5M 0.69) |

## $5,000 equity curve (1% compounding risk)

- **2025 leg (4H, the only data reaching 2025)**: 5,000 → 6,175 USD (**+23.5%**, PF 1.52, DD 5.2%)
- **2026 leg (4H)**: 6,175 → 5,993 USD (−2.9%, PF 0.94) — 4H edge decayed; context only
- **2026 15M window (deployable config, 2026-09-10 → 11-20)**: 5,000 → **11,452 USD (+129%)**, PF 1.85, DD 12.2%
- Curve ledgers: `equity_curve_4h_alpha_2025_2026.csv`, `equity_curve_15m_alpha.csv`,
  charts `charts/fig7_equity_5k.png`, `charts/fig9_equity_15m.png`, `charts/fig8_monthly.png`

Honest limits: 15M history is ~70 days; the all-FX variant fails the strictest tests;
paper-trade 20 sessions before sizing up. Doubling spreads flips the all-FX variant
negative — monitor live spread widening.

## Pull and load locally

```bash
# first time
git clone https://github.com/karan1399/Trading-Strategies.git
cd Trading-Strategies/ivwall_alpha

# later updates
git pull origin main
```

Requirements (Python 3.10+):

```bash
pip install pandas pyarrow numpy scipy matplotlib openpyxl
```

Smoke-test the engine locally (needs MT5 parquet data next to the repo root, i.e.
`historical_parquet/` — clone path layout already provides it):

```bash
cd ..
python ivwall_alpha/code/ivwall_engine.py smoke          # 20-symbol smoke run
python ivwall_alpha/code/ivwall_equity_sim.py            # regenerates the equity sim
python ivwall_alpha/code/ivwall_make_charts.py           # regenerates summary.json + charts
```

If `historical_parquet/` is not present locally, re-export it with the repo's
`scripts/ops/dump_mt5_parquet.py` (MT5 terminal required) or point
`PARQUET_ROOT` in `ivwall_engine.py` at your data path.

## Refresh cycle (keep it honest)

1. Re-run `ivwall_runner.py` + `ivwall_stage_b.py` monthly as bars accumulate.
2. Re-select the basket **on IS only** (`ivwall_final_variants.py`), verify once on OOS.
3. Compare live spread vs the cost model; if realized spread > 1.5x model, halve size.
4. Re-validate with `ivwall_robustness.py` (MC + block bootstrap + DSR) before adding risk.
