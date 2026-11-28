# `exchange_feed/` — Structured Coinglass-Style Footprint & Multi-OOS Lab

```text
exchange_feed/
├── app.py                        # FastAPI server (:8060)
├── launch.py / Footprint_Lab.bat # One-click Windows launcher
├── data_pipeline/                # Binance REST, Vision daily/2021+ history dumper, metrics & WS sync
│   ├── binance_dumper.py
│   ├── vision_backfill.py
│   ├── history_dumper.py
│   ├── metrics_store.py
│   └── live_sync.py
├── engines/                      # Order-flow bar builders, Coinglass signal parity & plugin base
│   ├── footprint_engine.py
│   ├── inhouse_engine.py
│   ├── coinglass_parity.py
│   └── strategy_base.py
├── backtesting/                  # Single-strategy & 2021-2026 1m-exact multi-sleeve portfolio engines
│   ├── backtest.py
│   └── portfolio_engine.py
├── strategies/                   # Auto-discovered BaseFootprintStrategy chart plugins
├── static/                       # Web UI (index.html)
├── data/                         # Local parquet store (aggTrades, 1m klines, 8h funding, monthly/)
└── tests/                        # End-to-end API & backtest verification suite
```

## Launch
- Double-click `Footprint Lab.lnk` or run `python -m exchange_feed.app` (`http://127.0.0.1:8060`).
- Legacy `exchange_feed.core.*` imports automatically resolve via `exchange_feed/__init__.py`.
