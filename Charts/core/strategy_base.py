"""Strategy plugin base classes and automatic discovery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any
import pandas as pd

DEFAULT_STRATEGIES_DIR = Path(__file__).resolve().parent.parent.parent / "strategies"


@dataclass(frozen=True)
class StrategyParam:
    """Declarative parameter definition that auto-renders UI controls."""

    name: str
    label: str
    type: str = "float"  # "int", "float", "bool"
    default: Any = 0
    min_val: float | None = None
    max_val: float | None = None
    step: float | None = None


@dataclass
class Signal:
    """Single buy (+1) or sell (-1) strategy signal anchored to bar index."""

    index: int
    timestamp: int
    side: int  # +1 = BUY, -1 = SELL
    price: float
    sl: float | None = None
    tp: float | None = None
    tag: str = ""
    reason: str = ""


class BaseStrategy(ABC):
    """Abstract base class for plug-and-play Charts strategies."""

    id: str = ""
    name: str = ""
    description: str = ""
    params: list[StrategyParam] = []

    def default_params(self) -> dict[str, Any]:
        """Return parameter defaults keyed by param.name."""
        return {p.name: p.default for p in self.params}

    def metadata(self) -> dict[str, Any]:
        """Serialize strategy metadata and parameter schema for the API/UI."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "params": [asdict(p) for p in self.params],
        }

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, params: dict[str, Any]) -> list[Signal]:
        """Inspect OHLCV DataFrame and return causal buy/sell Signal objects."""

    def extra_overlays(self, df: pd.DataFrame, params: dict[str, Any]) -> list[dict]:
        """Optional strategy-specific line overlays (channels, pivots, etc.)."""
        return []


def discover_strategies(strategies_dir: Path = DEFAULT_STRATEGIES_DIR) -> dict[str, BaseStrategy]:
    """Dynamically load every BaseStrategy subclass in Charts/strategies/*.py."""
    registry: dict[str, BaseStrategy] = {}
    if not strategies_dir.is_dir():
        return registry

    for py_file in sorted(strategies_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        mod_name = f"charts_strategy_{py_file.stem}"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(mod_name, py_file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                instance = obj()
                if instance.id:
                    registry[instance.id] = instance
    return registry
