"""Structured Stackable Strategy Library (`core/`, `sleeves/`, `overlays/`, `tests/`)."""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from typing import Dict

_LEGACY_PREFIXES: Dict[str, str] = {
    "strategies._indicators": "strategies.core.indicators",
    "strategies.fp_features": "strategies.core.fp_features",
    "strategies.registry": "strategies.core.registry",
    "strategies.stack": "strategies.core.stack",
    "strategies.s01_williams_pullback": "strategies.sleeves.trend.s01_williams_pullback",
    "strategies.s09_donchian_trend_cta": "strategies.sleeves.trend.s09_donchian_trend_cta",
    "strategies.s10_ema_pullback": "strategies.sleeves.trend.s10_ema_pullback",
    "strategies.s02_rsi2_meanrev": "strategies.sleeves.mean_reversion.s02_rsi2_meanrev",
    "strategies.s03_htf_ema_rejection": "strategies.sleeves.mean_reversion.s03_htf_ema_rejection",
    "strategies.s06_iv_wall_fade": "strategies.sleeves.mean_reversion.s06_iv_wall_fade",
    "strategies.s04_vc_squeeze_breakout": "strategies.sleeves.breakout.s04_vc_squeeze_breakout",
    "strategies.s05_zscore_breakout": "strategies.sleeves.breakout.s05_zscore_breakout",
    "strategies.s07_daily_pivot_breakout": "strategies.sleeves.breakout.s07_daily_pivot_breakout",
    "strategies.s11_or30_london_breakout": "strategies.sleeves.breakout.s11_or30_london_breakout",
    "strategies.s08_vol_target_overlay": "strategies.overlays.s08_vol_target_overlay",
}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self.target = target

    def create_module(self, spec):  # noqa: D401
        mod = importlib.import_module(self.target)
        sys.modules[spec.name] = mod
        return mod

    def exec_module(self, module) -> None:
        return None


class _LegacyStrategyAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        for old_p, new_p in _LEGACY_PREFIXES.items():
            if fullname == old_p or fullname.startswith(old_p + "."):
                mapped = new_p + fullname[len(old_p):]
                real_spec = importlib.util.find_spec(mapped)
                if real_spec is None:
                    return None
                is_pkg = real_spec.submodule_search_locations is not None
                spec = importlib.machinery.ModuleSpec(fullname, _AliasLoader(mapped), is_package=is_pkg)
                if is_pkg:
                    spec.submodule_search_locations = list(real_spec.submodule_search_locations or [])
                return spec
        return None


if not any(isinstance(f, _LegacyStrategyAliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _LegacyStrategyAliasFinder())

from strategies.core.registry import STRATEGY_CATALOG, load_signal_fn, resolve_module_path
from strategies.core.stack import StackLayer, StrategyStack

__all__ = [
    "STRATEGY_CATALOG",
    "StackLayer",
    "StrategyStack",
    "load_signal_fn",
    "resolve_module_path",
]
