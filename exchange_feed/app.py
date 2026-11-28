"""FastAPI backend for exchange_feed (Coinglass-style Footprint Studio)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from exchange_feed.backtesting.backtest import run_footprint_backtest
from exchange_feed.data_pipeline.binance_dumper import DATA_DIR, dump_binance_footprint
from exchange_feed.engines.coinglass_parity import load_coinglass_parity
from exchange_feed.engines.footprint_engine import build_footprint_bars, list_datasets
from exchange_feed.engines.inhouse_engine import load_inhouse_footprint
from exchange_feed.data_pipeline.live_sync import SYNC_STATE, fill_startup_gap, run_live_metrics_and_materializer, run_live_ws_persister
from exchange_feed.data_pipeline.metrics_store import list_materialized_parquets
from exchange_feed.backtesting.portfolio_engine import run_multi_sleeve_portfolio
from exchange_feed.engines.strategy_base import BaseFootprintStrategy, discover_strategies
from exchange_feed.data_pipeline.mt5_universe import load_mt5_contract_specs
from exchange_feed.data_pipeline.vision_backfill import read_backfill_status
from exchange_feed.backtesting.multi_asset_optimizer import MULTI_REPORT_PATH, run_full_multi_asset_optimization
from exchange_feed.backtesting.ml_meta_engine import FINAL_BLUEPRINT_PATH, evaluate_live_mt5_signals, train_and_save_final_live_ml

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """On startup: run gap-filler, live WS persister, and OI/materializer concurrently."""
    gap_task = asyncio.create_task(asyncio.to_thread(fill_startup_gap, "BTCUSDT"))
    ws_task = asyncio.create_task(run_live_ws_persister("BTCUSDT"))
    metrics_task = asyncio.create_task(run_live_metrics_and_materializer("BTCUSDT"))
    try:
        yield
    finally:
        gap_task.cancel()
        ws_task.cancel()
        metrics_task.cancel()


app = FastAPI(title="exchange_feed — Coinglass-Style Footprint Studio", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

STRATEGIES: dict[str, BaseFootprintStrategy] = discover_strategies()


class FootprintRequest(BaseModel):
    symbol: str = "BTCUSDT"
    source: str = "inhouse"  # "inhouse" | "coinglass" | "parquet"
    timeframe: str = "15m"
    bar_type: str = "time"  # "time" | "vol" | "range"
    unit: str = "coin"  # "coin" | "usd" | "trades"
    row_size: float | None = None
    imbalance_ratio: float = Field(default=3.0, ge=1.2, le=10.0)
    limit: int = Field(default=150, ge=5, le=1500)
    strategy_id: str | None = "of_initiative_breakout"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    risk_usd: float = Field(default=10.0, gt=0.0, le=10000.0)
    fee_bps: float = Field(default=10.0, ge=0.0, le=50.0)
    slippage_bps: float = Field(default=2.0, ge=0.0, le=50.0)


class DumpRequest(BaseModel):
    symbol: str = "BTCUSDT"
    hours: float = Field(default=2.0, gt=0.1, le=24.0)
    tick_bucket: float | None = Field(default=None, gt=0.0)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta")
def get_meta() -> dict[str, Any]:
    return {
        "datasets": list_datasets(DATA_DIR),
        "mt5_specs": load_mt5_contract_specs(DATA_DIR),
        "sources": ["inhouse", "coinglass", "parquet"],
        "timeframes": ["1m", "5m", "15m", "30m", "1h", "4h"],
        "bar_types": ["time", "vol", "range"],
        "units": ["coin", "usd", "trades"],
        "strategies": [s.metadata() for s in STRATEGIES.values()],
        "backfill": read_backfill_status(),
        "live_sync": SYNC_STATE,
        "parquets": list_materialized_parquets("BTCUSDT", DATA_DIR),
    }


@app.get("/api/live")
def get_live_price(symbol: str = "BTCUSDT") -> dict[str, Any]:
    """Low-latency live price + OI/Funding/Basis fallback combining backend WS and :3000 Binance FAPI ticker."""
    import json
    import time
    import urllib.request

    sym = symbol.upper()
    now_ms = int(time.time() * 1000)
    extra = {
        "oi_btc": SYNC_STATE.get("live_oi"),
        "funding_rate": SYNC_STATE.get("live_funding"),
        "basis_bps": SYNC_STATE.get("live_basis_bps"),
    }
    if sym == "BTCUSDT" and SYNC_STATE.get("live_price") and (now_ms - int(SYNC_STATE.get("live_ts") or 0)) < 5000:
        return {"ok": True, "symbol": sym, "price": float(SYNC_STATE["live_price"]), "source": "backend-ws", "ts": SYNC_STATE["live_ts"], **extra}
    base = sym[:-4] if sym.endswith("USDT") else sym
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:3000/api/cg/live?sym={base}", timeout=3) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        if d.get("ok") and d.get("price"):
            return {"ok": True, "symbol": sym, "price": float(d["price"]), "bid": d.get("bid"), "ask": d.get("ask"), "source": d.get("source", "cg-live"), "ts": d.get("fetchedAt", now_ms), **extra}
    except Exception:
        pass
    return {"ok": False, "symbol": sym, "price": SYNC_STATE.get("live_price"), "source": "stale", **extra}


@app.post("/api/strategies/reload")
def reload_strategies() -> dict[str, Any]:
    global STRATEGIES
    STRATEGIES = discover_strategies()
    return {"count": len(STRATEGIES), "strategies": [s.metadata() for s in STRATEGIES.values()]}


class PortfolioRequest(BaseModel):
    symbol: str = "BTCUSDT"
    start_equity: float = Field(default=5000.0, gt=100.0, le=10_000_000.0)
    risk_pct: float = Field(default=1.25, gt=0.1, le=10.0)
    max_concurrent: int = Field(default=3, ge=1, le=6)
    fee_bps: float = Field(default=10.0, ge=0.0, le=50.0)
    slippage_bps: float = Field(default=2.0, ge=0.0, le=50.0)
    funding_bps_8h: float = Field(default=1.0, ge=0.0, le=10.0)
    be_r: float = Field(default=0.0, ge=0.0, le=3.0)
    trail_atr: float = Field(default=3.5, ge=0.5, le=6.0)
    mc_runs: int = Field(default=20000, ge=1000, le=50000)
    profile: str = "optimal_8s"
    entry_delay_mins: int = Field(default=1, ge=0, le=10)
    realistic_costs: bool = True
    use_real_funding: bool = True
    max_same_side: int = Field(default=2, ge=1, le=6)


@app.post("/api/portfolio-backtest")
def portfolio_backtest(req: PortfolioRequest) -> dict[str, Any]:
    try:
        return run_multi_sleeve_portfolio(
            symbol=req.symbol.upper(),
            start_equity=req.start_equity,
            risk_pct=req.risk_pct,
            max_concurrent=req.max_concurrent,
            fee_bps=req.fee_bps,
            slippage_bps=req.slippage_bps,
            funding_bps_8h=req.funding_bps_8h,
            be_r=req.be_r,
            trail_atr=req.trail_atr,
            mc_runs=req.mc_runs,
            data_dir=DATA_DIR,
            profile=req.profile,
            entry_delay_mins=req.entry_delay_mins,
            realistic_costs=req.realistic_costs,
            use_real_funding=req.use_real_funding,
            max_same_side=req.max_same_side,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/multi-asset-portfolio")
def get_multi_asset_portfolio(refresh: bool = False, cost_mode: str = "mt5") -> dict[str, Any]:
    if not refresh and cost_mode == "mt5" and MULTI_REPORT_PATH.is_file():
        import json
        return json.loads(MULTI_REPORT_PATH.read_text(encoding="utf-8"))
    return run_full_multi_asset_optimization(data_dir=DATA_DIR, cost_mode=cost_mode, mc_runs=20000, save_report=(cost_mode == "mt5"))


@app.get("/api/final-live-portfolio")
def get_final_live_portfolio(refresh: bool = False) -> dict[str, Any]:
    if not refresh and FINAL_BLUEPRINT_PATH.is_file():
        import json
        return json.loads(FINAL_BLUEPRINT_PATH.read_text(encoding="utf-8"))
    return train_and_save_final_live_ml(data_dir=DATA_DIR, save_bundle=True)


@app.get("/api/live-ml-signals")
def get_live_ml_signals() -> dict[str, Any]:
    return evaluate_live_mt5_signals(data_dir=DATA_DIR)


@app.post("/api/dump")
def dump_data(req: DumpRequest) -> dict[str, Any]:
    try:
        out = dump_binance_footprint(
            symbol=req.symbol.upper(),
            hours=req.hours,
            tick_bucket=req.tick_bucket,
        )
        return {"ok": True, "path": str(out), "datasets": list_datasets(DATA_DIR)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/footprint")
def get_footprint(req: FootprintRequest) -> dict[str, Any]:
    sym = req.symbol.upper()
    if req.source == "inhouse":
        try:
            data = load_inhouse_footprint(
                symbol=sym,
                timeframe=req.timeframe,
                bar_type=req.bar_type,
                unit=req.unit,
                row_size=req.row_size,
                imbalance_ratio=req.imbalance_ratio,
                limit=req.limit,
                data_dir=DATA_DIR,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    elif req.source == "coinglass":
        try:
            data = load_coinglass_parity(
                symbol=sym,
                timeframe=req.timeframe,
                bar_type=req.bar_type,
                unit=req.unit,
                row_size=req.row_size,
                imbalance_ratio=req.imbalance_ratio,
                limit=req.limit,
            )
        except Exception as exc:
            try:
                data = load_inhouse_footprint(
                    symbol=sym,
                    timeframe=req.timeframe,
                    bar_type=req.bar_type,
                    unit=req.unit,
                    row_size=req.row_size,
                    imbalance_ratio=req.imbalance_ratio,
                    limit=req.limit,
                    data_dir=DATA_DIR,
                )
                data["source"] = f"inhouse_fallback ({exc})"
            except FileNotFoundError as exc2:
                raise HTTPException(status_code=502, detail=f"Coinglass upstream error: {exc}; {exc2}") from exc2
    else:
        try:
            data = build_footprint_bars(
                symbol=sym,
                timeframe=req.timeframe,
                unit=req.unit,
                row_size=req.row_size,
                imbalance_ratio=req.imbalance_ratio,
                limit=req.limit,
                data_dir=DATA_DIR,
            )
            data["source"] = "parquet_1y"
            data["bar_type"] = "time"
            data["cg_signals"] = []
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    bars = data["bars"]
    signals_out: list[dict[str, Any]] = []
    bt_out = {
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
            "max_drawdown_usd": 0.0,
        },
    }

    if req.strategy_id and req.strategy_id != "none":
        strat = STRATEGIES.get(req.strategy_id)
        if strat is None:
            raise HTTPException(status_code=404, detail=f"Unknown strategy '{req.strategy_id}'")
        sigs = strat.generate_signals(bars, req.strategy_params)
        bt_out = run_footprint_backtest(
            bars,
            sigs,
            risk_usd=req.risk_usd,
            fee_bps=req.fee_bps,
            slippage_bps=req.slippage_bps,
        )
        signals_out = [
            {
                "index": int(s.index),
                "time": int(s.time),
                "side": int(s.side),
                "side_label": "BUY" if s.side == 1 else "SELL",
                "price": round(float(s.price), 4 if float(s.price) < 100.0 else 2),
                "sl": round(float(s.sl), 4 if float(s.sl) < 100.0 else 2) if s.sl is not None else None,
                "tp": round(float(s.tp), 4 if float(s.tp) < 100.0 else 2) if s.tp is not None else None,
                "tag": s.tag,
                "reason": s.reason,
            }
            for s in sigs
        ]

    return {
        **data,
        "signals": signals_out,
        **bt_out,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("FOOTPRINT_PORT", "8060"))
    uvicorn.run("exchange_feed.app:app", host="127.0.0.1", port=port, reload=False)
