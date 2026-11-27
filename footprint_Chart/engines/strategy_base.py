"""Extensible Footprint Strategy plugin base & auto-discovery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any

STRATEGIES_DIR = Path(__file__).resolve().parent.parent.parent / "strategies"


@dataclass(frozen=True)
class StrategyParam:
    """Declarative parameter rendered dynamically in the Footprint Lab UI."""

    name: str
    label: str
    type: str = "float"  # "int", "float"
    default: Any = 0
    min_val: float | None = None
    max_val: float | None = None
    step: float | None = None


@dataclass
class FootprintSignal:
    """Causal signal anchored to a footprint bar."""

    index: int
    time: int
    side: int  # +1 BUY, -1 SELL
    price: float
    sl: float | None = None
    tp: float | None = None
    tag: str = ""
    reason: str = ""
    be_r: float = 0.0
    trail_dist: float = 0.0
    max_hold: int = 0


class BaseFootprintStrategy(ABC):
    """Base class for any footprint-based strategy plugin."""

    id: str = ""
    name: str = ""
    description: str = ""
    params: list[StrategyParam] = []

    def default_params(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "params": [asdict(p) for p in self.params],
        }

    @abstractmethod
    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        """Inspect aggregated footprint bars and emit causal FootprintSignal objects."""


def discover_strategies(strategies_dir: Path = STRATEGIES_DIR) -> dict[str, BaseFootprintStrategy]:
    """Auto-discover all BaseFootprintStrategy subclasses in footprint_Chart/strategies/."""
    registry: dict[str, BaseFootprintStrategy] = {}
    if not strategies_dir.is_dir():
        return registry

    for py_file in sorted(strategies_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        mod_name = f"fp_chart_strategy_{py_file.stem}"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(mod_name, py_file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseFootprintStrategy) and obj is not BaseFootprintStrategy:
                inst = obj()
                if inst.id:
                    registry[inst.id] = inst
    return registry
