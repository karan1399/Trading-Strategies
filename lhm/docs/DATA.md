# Data Inventory

All consolidated data lives in `data/store/` as parquet (fast, compact). Raw zips are
kept under `data/binance/` and `data/raw/` (not committed).

## Store (parquet)

| file | rows | coverage | contents |
|---|---|---|---|
| `btc_5m.parquet` | 628,403 | 2021-01-01 -> 2026-11-21 | 5m klines: ts, o/h/l/c, vol_usd, n_trades, taker_buy_usd |
| `btc_15m/1h/4h/1d.parquet` | — | same | terminal candles |
| `btc_metrics.parquet` | 620,039 | 2020-12-28 -> 2026-11-21 | 5-min: oi_btc, oi_usd, taker_ls, count_ls, top_sum_ls, top_count_ls |
| `funding.parquet` | 6,483 | 2020-12 -> 2026-11 | funding rate history |
| `events_5m.parquet` | ~120k | 2021-01 -> 2026-11 | event stream (ts, entry, amount, long_frac) |
| `calibration.json` | — | — | latest calibration grid result |
| `aggtrades/aggtrades-YYYY-MM-DD.parquet` | ~1-2M/day | rolling recent window | tick: agg_trade_id, price, qty, transact_time, is_buyer_maker |

## Sources (Binance Vision, `data.binance.vision/data/futures/um`)

- `monthly/klines/BTCUSDT/{5m,15m,1h,4h,1d}` — full history, ~92 files
- `daily/klines/BTCUSDT/5m` — current month
- `daily/metrics/BTCUSDT` — 2020-12-28 -> now (2020-12..2021-11 from first download
  phase under `data/raw/metrics/`; both merged by `merge_metrics.py`)
- `daily/aggTrades/BTCUSDT` — tick level
- funding: `monthly/fundingRate/BTCUSDT`

## Regeneration

```bash
python scripts/binance_downloader.py 2021-01 2026-11 2026-11-21   # zips
python scripts/build_store.py        # klines + metrics -> parquet
python scripts/merge_metrics.py      # metrics from all sources (full coverage)
python scripts/build_events.py       # event stream
python scripts/download_aggtrades.py # tick data, newest-first, resumable
```

## Notes

- 5m gaps: 152 occurrences > 5min (exchange maintenance); longest 10.5h.
- Metrics 5-min cadence starts 2020-12-28 on Vision (dataset availability).
- aggTrades policy: disk-bounded, newest first; only parquet kept (zips discarded
  after conversion). ~1-2M trades/day, ~10-20MB/day as zstd parquet.
- Coinglass comparison dumps: `research/coinglass/dumps/` (m1/m2/m3 x 12h..2y x
  agg/binance) + two 5-min-apart snapshots for the increment rule.
