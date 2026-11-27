"""FastAPI server for Charts: historical_parquet viewer & strategy tester."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from Charts.core.backtest import run_backtest
from Charts.core.data_loader import DEFAULT_PARQUET_ROOT, list_symbols, list_timeframes, load_ohlcv
from Charts.core.indicators import compute_indicators, list_indicators
from Charts.core.strategy_base import BaseStrategy, discover_strategies

CHARTS_DIR = Path(__file__).resolve().parent
STATIC_DIR = CHARTS_DIR / "static"

app = FastAPI(title="Trading-Strategies Charts & Strategy Lab", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

STRATEGIES: dict[str, BaseStrategy] = discover_strategies()


class ChartRequest(BaseModel):
    """Request payload for loading candles, indicators, and strategy backtest."""

    timeframe: str = "15M"
    symbol: str
    limit: int = Field(default=1500, ge=50, le=50000)
    indicators: list[str] = Field(default_factory=lambda: ["ema_13", "ema_89", "ema_233"])
    strategy_id: str | None = "ema_pullback"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    risk_usd: float = Field(default=10.0, gt=0.0, le=10000.0)
    use_spread: bool = True


@app.get("/")
def index() -> FileResponse:
    """Serve the interactive Charts UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta")
def get_meta() -> dict[str, Any]:
    """Return available timeframes, initial symbols, indicators, and strategies."""
    tfs = list_timeframes(DEFAULT_PARQUET_ROOT)
    default_tf = "15M" if "15M" in tfs else (tfs[0] if tfs else "")
    syms = list_symbols(default_tf, DEFAULT_PARQUET_ROOT) if default_tf else []
    return {
        "parquet_root": str(DEFAULT_PARQUET_ROOT),
        "timeframes": tfs,
        "default_timeframe": default_tf,
        "symbols": syms,
        "indicators": list_indicators(),
        "strategies": [s.metadata() for s in STRATEGIES.values()],
    }


@app.get("/api/symbols/{timeframe}")
def get_symbols(timeframe: str) -> dict[str, Any]:
    """Return sorted symbol names for a given timeframe."""
    syms = list_symbols(timeframe, DEFAULT_PARQUET_ROOT)
    if not syms:
        raise HTTPException(status_code=404, detail=f"No parquet symbols found for timeframe '{timeframe}'")
    return {"timeframe": timeframe, "symbols": syms}


@app.post("/api/strategies/reload")
def reload_strategies() -> dict[str, Any]:
    """Hot-reload strategy plugins from Charts/strategies/."""
    global STRATEGIES
    STRATEGIES = discover_strategies()
    return {"count": len(STRATEGIES), "strategies": [s.metadata() for s in STRATEGIES.values()]}


@app.post("/api/chart")
def run_chart(req: ChartRequest) -> dict[str, Any]:
    """Load parquet OHLCV bars, compute indicators, and run selected strategy."""
    try:
        df = load_ohlcv(req.symbol, req.timeframe, limit=req.limit, root=DEFAULT_PARQUET_ROOT)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if df.empty:
        raise HTTPException(status_code=404, detail=f"Empty dataset for {req.symbol} ({req.timeframe})")

    candles = [
        {
            "time": int(row.timestamp),
            "open": round(float(row.open), 6),
            "high": round(float(row.high), 6),
            "low": round(float(row.low), 6),
            "close": round(float(row.close), 6),
        }
        for row in df.itertuples(index=False)
    ]
    volume = [
        {
            "time": int(row.timestamp),
            "value": round(float(row.volume), 4),
            "color": "rgba(34, 197, 94, 0.35)" if row.close >= row.open else "rgba(239, 68, 68, 0.35)",
        }
        for row in df.itertuples(index=False)
    ]

    indicators = compute_indicators(df, req.indicators)

    signals_out: list[dict[str, Any]] = []
    backtest_out: dict[str, Any] = {
        "trades": [],
        "equity_curve": [],
        "metrics": {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_r": 0.0,
            "total_pnl_usd": 0.0,
            "profit_factor": 0.0,
            "expectancy_r": 0.0,
            "max_drawdown_usd": 0.0,
            "sharpe": 0.0,
        },
    }

    if req.strategy_id and req.strategy_id != "none":
        strat = STRATEGIES.get(req.strategy_id)
        if strat is None:
            raise HTTPException(status_code=404, detail=f"Unknown strategy '{req.strategy_id}'")
        signals = strat.generate_signals(df, req.strategy_params)
        extra = strat.extra_overlays(df, req.strategy_params)
        if extra:
            indicators.extend(extra)
        backtest_out = run_backtest(
            df,
            signals,
            risk_usd=req.risk_usd,
            use_spread=req.use_spread,
        )
        signals_out = [
            {
                "index": int(s.index),
                "time": int(s.timestamp),
                "side": int(s.side),
                "side_label": "BUY" if s.side == 1 else "SELL",
                "price": round(float(s.price), 6),
                "sl": round(float(s.sl), 6) if s.sl is not None else None,
                "tp": round(float(s.tp), 6) if s.tp is not None else None,
                "tag": s.tag,
                "reason": s.reason,
            }
            for s in signals
        ]

    return {
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "bars": len(candles),
        "candles": candles,
        "volume": volume,
        "indicators": indicators,
        "signals": signals_out,
        **backtest_out,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("CHARTS_PORT", "8050"))
    uvicorn.run("Charts.app:app", host="127.0.0.1", port=port, reload=False)
