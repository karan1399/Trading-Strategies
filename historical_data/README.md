# Historical Data Directory

Centralized physical storage for all historical market datasets in `Trading-Strategies`:

- `mt5_parquet/` — MT5 historical OHLCV `.parquet` files (`1M`, `5M`, `15M`, `1H`, `4H`), linked via NTFS junction from `historical_parquet/` for 100% backwards compatibility.
- `binance_footprint/` — Binance Vision historical trades, 5m metrics, and cached footprint `.parquet` / `.pkl` bundles, linked via NTFS junction from `footprint_Chart/data/` for 100% backwards compatibility.

- `delta_exchange/` — Delta Exchange live trader, catalog, state, OHLC candles, `.env`, linked via NTFS junction from `Delta_Exchange_Data/` for 100% backwards compatibility.

> All three directories are physically stored here. NTFS junctions at the repo root (`historical_parquet`, `footprint_Chart/data`, `Delta_Exchange_Data`) maintain backwards compatibility with every existing script and test without any code changes.
