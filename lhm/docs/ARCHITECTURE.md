# System Architecture

## Pipeline (implemented)

```
Binance Vision zips ──> build_store.py / merge_metrics.py ──> parquet store (data/store)
                                                                     │
Coinglass (signed API, cg_client.py) ──> dumps (validation reference)│
                                                                     ▼
                                     build_events.py ──> events_5m.parquet
                                                                     │
                                                                     ▼
                              lhm_engine.py / lhm_window.py (Model 1 reconstruction)
                                                                     │
                                        calibrate_model.py (parity harness)
```

## Planned phases

### Phase A — Parity (in progress)
Close the event-selection gap (see MODEL1_ALGORITHM.md open items): derive the
event rule from OI delta / taker flow; target cell-level r > 0.9 and top-level
match > 95%.

### Phase B — Time-evolving heatmap + orderflow
- Historical replay of the heatmap at any timestamp (levels forming, persisting,
  being swept; cascades).
- Footprint/orderflow integration: per-bar delta, cumulative delta, OI change,
  liquidation-cascade detection (sweep -> re-seed chains from the event stream).

### Phase C — Strategy layer
- Rule-based baseline: magnet-zone entries (fade into dense clusters), stop beyond
  cluster, target the next magnet; minimum 2R filter.
- ML: features (cluster distance/strength, side imbalance, OI slope, funding,
  taker pressure, sweep momentum) -> probabilistic target model; walk-forward
  validation, 2021-2026, no leakage; report expectancy, PF, drawdown.

### Phase D — Terminal
- Web app: candles + heatmap overlay (CG-like), historical time playback,
  horizontal/vertical zoom, long/short trade marking with win/loss from the
  strategy engine.

## Repo conventions

- Python scripts are standalone and idempotent (safe to re-run).
- Data never committed; only code + docs.
- Coinglass credentials only via env (`CG_EMAIL`, `CG_PASSWORD`), never in files.
