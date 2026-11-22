# Historical Candlestick / Bar Data (Parquet Store)

This directory contains standardized, compressed, multi-timeframe OHLCV historical candlestick data exported directly from MetaTrader 5 (Blueberry Markets / FundedNext Phase 1 broker inventory).

## Structure
Files are organized by timeframe:
- `1M/`: 1-minute bars (`<SYMBOL>.parquet`)
- `5M/`: 5-minute bars (`<SYMBOL>.parquet`)
- `15M/`: 15-minute bars (`<SYMBOL>.parquet`)
- `1H/`: 1-hour bars (`<SYMBOL>.parquet`)
- `4H/`: 4-hour bars (`<SYMBOL>.parquet`)

## Schema
Each Parquet file is compressed with Snappy and formatted with pyarrow / fastparquet:
- `timestamp`: UTC DatetimeIndex (`datetime64[ns, UTC]`)
- `open`: Float64 open price
- `high`: Float64 bar high
- `low`: Float64 bar low
- `close`: Float64 bar close
- `tick_volume`: Int64 tick volume
- `spread`: Int32 spread in broker points
- `real_volume`: Int64 real volume (if reported by venue)

## Universe
Covers all 151 broker inventory assets including:
- Major & Cross Forex pairs (`EURUSD.pi`, `GBPUSD.pi`, `USDJPY.pi`, etc.)
- TradFi Commodities & Energy (`XAUUSD.pi`, `XAGUSD.pi`, `USOIL.pi`, etc.)
- Crypto USDT-M / USD perps (`BTCUSD.p`, `ETHUSD.p`, `SOLUSD.p`, etc.)

## Data Generator
Maintained via `scripts/ops/dump_mt5_parquet.py`.
