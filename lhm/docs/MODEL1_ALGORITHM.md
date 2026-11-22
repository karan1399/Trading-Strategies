# Model 1 — Liquidation Heatmap Algorithm

Confirmed mechanics (from dump analysis + two-snapshot live diff):

## Grid

- Uniform price rows, ~300 rows covering `[rangeLow, rangeHigh]`.
- `spacing = (rangeHigh - rangeLow) / rows`; row r price = `rangeLow + r*spacing`.

## Level creation (events)

A fraction of bars (~19% on 5m data) create "position events" with:

- **entry price** ~ the bar close,
- **amount** ~ 0.5-1.4x the bar's USD volume (calibration parameter `FLOW_K`),
- **long/short split** ~ 50/50 with a tilt (calibration parameter `ALPHA`).

Each event writes **6 increments** — leverage buckets `L in {25, 50, 100}` on both
sides with weight ratio **4:2:1** (100x:50x:25x), i.e. bucket share = w/7 of the
side amount:

```
long  liq price = entry * (1 - 1/L)
short liq price = entry * (1 + 1/L + MMR)      # MMR ~ 0.0010 (maintenance margin)
```

## Level removal

- A level is **removed** when the bar's `[low, high]` range crosses the level price
  (sweep) — confirmed: the vast majority of removals coincide with a price cross.
- Levels have a **~24h lifetime** (expire after 288 5m bars) — this explains the
  rare removals without a price cross.

## Value semantics

- Values are cumulative live estimated liquidation notional (USD) at that price row.
- Colors: purple -> yellow by relative intensity; heatmap = "relative", not exact
  pending contracts.

## Reconstruction engine

`scripts/lhm_engine.py` (dense grid) and `scripts/lhm_window.py` (windowed, exact
24h expiry, per-column snapshots). Event stream generator:
`scripts/build_events.py`:

```
event bar   = volume > rolling(12)-percentile(P80)
amount      = FLOW_K * bar volume USD
long_frac   = 0.5 + ALPHA * (taker_buy_frac - 0.5)
cascade     = re-seed on bars that sweep existing levels (add_cascade_events)
```

## Parity status (vs live 24h Binance dump, 93-column overlap)

| metric | value |
|---|---|
| cell-level Pearson r | 0.212 |
| cosine similarity | 0.496 |
| top-20 level match (within 1.5 rows) | 16/20 -> 80% (with tuned params) |
| total magnitude ratio (best FLOW_K) | ~1.0 at flow_k 0.03, pctl 80 |

Calibration grid (`scripts/calibrate_model.py`) shows:

- `FLOW_K` only scales magnitude (Pearson r is scale-invariant) -> magnitude is
  essentially solved; **event-bar selection** drives the remaining correlation gap.
- Stricter event thresholds (P88/P92) *reduce* parity (fewer, bigger events) —
  Coinglass fires on more bars than a pure volume-percentile rule suggests.

## Open items (path to 100% parity)

1. **Event-bar selection rule**: volume percentile is an approximation. Candidates:
   OI delta (from 5-min metrics), taker-flow imbalance, delta between taker volume
   and OI change (new position opening). The metrics store (2020-12+) supports all.
2. **Long/short split**: verify with taker buy/sell ratio and OI long/short accounts
   (`top_count_ls`, `count_ls` in metrics).
3. **MMR constant**: 0.0010 fits; confirm across price regimes.
4. **Entry price**: bar close vs VWAP of the bar (aggTrades make this testable).
5. **Multi-exchange merge**: aggregate mode weights exchanges (OI-weighted?).
