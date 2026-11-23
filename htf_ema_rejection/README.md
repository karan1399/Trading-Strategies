# HTF 89/233 EMA Rejection — Strategy Research & Alpha Basket

Specified from four chart examples (XAUUSD H1 pullback-rejections at the 89/233
EMA pair) and validated end-to-end on `historical_parquet/` with real broker
spreads and strict IS/OOS discipline.

**Verdict: VALIDATED — the alpha basket passes out-of-sample (IS +413.8R PF 1.87 →
OOS +48.3R PF 1.20, n=414), with a mandatory concentration disclosure: the edge is
carried by the gold-cross family (gold-only OOS PF 1.51; ex-gold OOS PF 0.92).**

Full report: [`HTF_EMA_Rejection_Strategy_Report.pdf`](HTF_EMA_Rejection_Strategy_Report.pdf) ·
workbook: [`HTF_EMA_Rejection_Basket.xlsx`](HTF_EMA_Rejection_Basket.xlsx) ·
numbers: [`htf_summary.json`](htf_summary.json)

## The four pattern families (from the charts)

| Pattern | Rule (long; short mirrored) |
| --- | --- |
| P1 wick-kiss | bar touches rising EMA, rejection wick ≥ 35% of range, bullish close back above |
| P2 two-bar | bar i-1 tests EMA, bar i closes beyond it past bar i-1's midpoint |
| P3 pierce-fail | bar i-1 closes beyond EMA, bar i closes back through it (fakeout) |
| P4 cluster | 3-bar cluster at the EMA (≥2 touches), breakaway with the trend |

Filters: fast-EMA 5-bar slope gate (on/off swept) + 233-EMA regime gate (on/off
swept). Exits: fixed 1.5–4.0R and trail 0.5/1.0/1.5R (BE lock at +1R). Signals on
closed bars, entry next open, structural stops, 6-bar cooldown, spread charged
both sides.

## Alpha basket (pre-registered plateau selection, IS only)

Rules: is_n ≥ 20, is_pf ≥ 1.20, cell (symbol × tf × period × pattern) needs ≥3
qualified configs, median-of-plateau pick, one cell per symbol, cap 15.
Result — 8 gold crosses + 2 GAU variants at EMA233/89 4H cluster-stall, 5 JPY/index
members. Battery: MC p=1.00, BB p=1.00, DSR 1.00 (86,634 trials, mixed stream —
OOS row is the gate), spreads ×2 keeps PF 1.62. $5,000 equity sim at
concurrency-sized risk (0.14%/trade, max 14 open): **$5,000 → $10,154 (+103%),
max DD 4.85%**.

## Files

- `htf_reject.py` — pattern engine (P1–P4 + filters), reuses the corrected
  Delta-Fibonacci simulator core
- `htf_sweep.py` — full-universe sweep (86,634 configs, resumable)
- `htf_select.py` — plateau selection → basket → single OOS validation → battery
- `htf_summarize.py` — summary JSON, workbook, charts, equity sim
- `results/` — sweep outputs, basket picks, 1,381-trade ledgers
- `charts/` — report figures

## Re-run from the repo root

```bash
pip install pandas pyarrow numpy scipy matplotlib openpyxl
cd htf_ema_rejection
python3 htf_sweep.py     # ~11 min
python3 htf_select.py    # ~2 min
python3 htf_summarize.py # ~1 min
```

Reads `../historical_parquet/` by default (adjust `PARQUET_ROOT`).

## Risk notes

- Treat all gold members as ONE risk unit (same trade in different quote
  currencies); the basket's effective independent bets ≪ 15.
- Non-gold members are not individually validated — small satellite risk only,
  each must earn a rolling-100-trade PF > 1.0 to stay.
- Re-run monthly as bars accumulate; the OOS window extends with new data.
