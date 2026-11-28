"""Structured Footprint Chart & Institutional Order-Flow Studio."""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import types

PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_ROOT / "data"

_CORE_MAP: dict[str, str] = {
    "binance_dumper": "exchange_feed.data_pipeline.binance_dumper",
    "vision_backfill": "exchange_feed.data_pipeline.vision_backfill",
    "history_dumper": "exchange_feed.data_pipeline.history_dumper",
    "metrics_store": "exchange_feed.data_pipeline.metrics_store",
    "live_sync": "exchange_feed.data_pipeline.live_sync",
    "footprint_engine": "exchange_feed.engines.footprint_engine",
    "inhouse_engine": "exchange_feed.engines.inhouse_engine",
    "coinglass_parity": "exchange_feed.engines.coinglass_parity",
    "strategy_base": "exchange_feed.engines.strategy_base",
    "backtest": "exchange_feed.backtesting.backtest",
    "portfolio_engine": "exchange_feed.backtesting.portfolio_engine",
}


class _CoreCompatLoader(importlib.abc.Loader):
    def __init__(self, target: str | None) -> None:
        self.target = target

    def create_module(self, spec):
        if self.target is None:
            pkg = types.ModuleType(spec.name)
            pkg.__path__ = []
            sys.modules[spec.name] = pkg
            return pkg
        mod = importlib.import_module(self.target)
        sys.modules[spec.name] = mod
        return mod

    def exec_module(self, module) -> None:
        return None


class _CoreCompatFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname == "exchange_feed.core":
            spec = importlib.machinery.ModuleSpec(fullname, _CoreCompatLoader(None), is_package=True)
            spec.submodule_search_locations = []
            return spec
        if fullname.startswith("exchange_feed.core."):
            sub = fullname[len("exchange_feed.core."):]
            mapped = _CORE_MAP.get(sub)
            if mapped is None:
                return None
            return importlib.machinery.ModuleSpec(fullname, _CoreCompatLoader(mapped), is_package=False)
        return None


if not any(isinstance(f, _CoreCompatFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _CoreCompatFinder())

__all__ = ["DATA_DIR", "PACKAGE_ROOT"]
