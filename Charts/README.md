# Charts — Modular Historical Parquet Viewer & Strategy Lab

Interactive TradingView-style charting and causal backtesting app reading directly from:
`C:\Users\SIGMA\Documents\Trading-Strategies\historical_parquet`

## Launch
```powershell
python -m Charts.app
```
Then open: `http://127.0.0.1:8050` (override port via `CHARTS_PORT`).

## Extensible Plugin Architecture

### 1. Add a New Strategy (Zero Core Edits)
Drop a `.py` file into `Charts/strategies/` subclassing `BaseStrategy`:
- Define `id`, `name`, `description`, and `params: list[StrategyParam]` (UI controls auto-generate).
- Implement `generate_signals(self, df, params) -> list[Signal]`.
- Optionally implement `extra_overlays(self, df, params)` to draw custom lines/channels.
- Click **Reload Plugins** in the UI to hot-load it immediately.

### 2. Add a New Indicator
Register a function with `@register_indicator(id, name, pane="main"|"oscillator", color="#...")` in `Charts/core/indicators.py`. It automatically appears as a toggle in the UI.

### 3. Swap / Extend Data Sources
`Charts/core/data_loader.py` reads normalized OHLCV (`timestamp, datetime, open, high, low, close, volume, spread`) from `CHARTS_PARQUET_ROOT`.
