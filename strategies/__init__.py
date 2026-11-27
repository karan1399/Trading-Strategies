"""Unified Strategy Library — all price-action, footprint, overlay, and core strategy modules."""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from typing import Dict

_LEGACY_PREFIXES: Dict[str, str] = {
    "strategies._indicators": "strategies.core.indicators",
    "strategies.fp_features": "strategies.core.fp_features",
    "strategies.registry": "strategies.core.registry",
    "strategies.stack": "strategies.core.stack",
    # Renamed numbered strategy aliases (s01..s16 clean layout -> legacy unnumbered imports)
    "strategies.donchian_breakout": "strategies.s02_donchian_breakout",
    "strategies.ema233_imb_rejection": "strategies.s03_ema233_imb_rejection",
    "strategies.ema_zero_orderflow": "strategies.s05_ema_zero_orderflow",
    "strategies.imbalance_stack": "strategies.s06_imbalance_stack",
    "strategies.of_initiative_breakout": "strategies.s11_of_initiative_breakout",
    "strategies.oi_2h_liq_break": "strategies.s12_oi_2h_liq_break",
    "strategies.oi_2h_reclaim_fade": "strategies.s13_oi_2h_reclaim_fade",
    "strategies.oi_4h_sqz_mom": "strategies.s14_oi_4h_sqz_mom",
    "strategies.rsi2_mean_reversion": "strategies.s15_rsi2_mean_reversion",
    "strategies.zero_auction_reversal": "strategies.s16_zero_auction_reversal",
    # Unprofitable archived strategy redirects (preserves full backward compatibility)
    "strategies.bollinger_zscore": "strategies.archive.unprofitable.bollinger_zscore",
    "strategies.cg_signal_strategy": "strategies.archive.unprofitable.cg_signal_strategy",
    "strategies.delta_divergence": "strategies.archive.unprofitable.delta_divergence",
    "strategies.ema_pullback": "strategies.archive.unprofitable.ema_pullback",
    "strategies.rsi2_of_reversion": "strategies.archive.unprofitable.rsi2_of_reversion",
    "strategies.s02_rsi2_meanrev": "strategies.archive.unprofitable.s02_rsi2_meanrev",
    "strategies.s03_htf_ema_rejection": "strategies.archive.unprofitable.s03_htf_ema_rejection",
    "strategies.s05_zscore_breakout": "strategies.archive.unprofitable.s05_zscore_breakout",
    "strategies.s06_iv_wall_fade": "strategies.archive.unprofitable.s06_iv_wall_fade",
    "strategies.s11_or30_london_breakout": "strategies.archive.unprofitable.s11_or30_london_breakout",
    "strategies.vwap_of_reclaim": "strategies.archive.unprofitable.vwap_of_reclaim",
    "strategies.vwap_rejection": "strategies.archive.unprofitable.vwap_rejection",
    # Backward-compatible aliases from old nested sleeves/overlays/split folders -> unified strategies/
    "strategies.sleeves.trend.s01_williams_pullback": "strategies.s01_williams_pullback",
    "strategies.sleeves.trend.s09_donchian_trend_cta": "strategies.s09_donchian_trend_cta",
    "strategies.sleeves.trend.s10_ema_pullback": "strategies.s10_ema_pullback",
    "strategies.sleeves.mean_reversion.s02_rsi2_meanrev": "strategies.archive.unprofitable.s02_rsi2_meanrev",
    "strategies.sleeves.mean_reversion.s03_htf_ema_rejection": "strategies.archive.unprofitable.s03_htf_ema_rejection",
    "strategies.sleeves.mean_reversion.s06_iv_wall_fade": "strategies.archive.unprofitable.s06_iv_wall_fade",
    "strategies.sleeves.breakout.s04_vc_squeeze_breakout": "strategies.s04_vc_squeeze_breakout",
    "strategies.sleeves.breakout.s05_zscore_breakout": "strategies.archive.unprofitable.s05_zscore_breakout",
    "strategies.sleeves.breakout.s07_daily_pivot_breakout": "strategies.s07_daily_pivot_breakout",
    "strategies.sleeves.breakout.s11_or30_london_breakout": "strategies.archive.unprofitable.s11_or30_london_breakout",
    "strategies.overlays.s08_vol_target_overlay": "strategies.s08_vol_target_overlay",
    "strategies.ig_s1_williams_pullback": "strategies.s01_williams_pullback",
    "strategies.ig_s2_rsi2_meanrev": "strategies.archive.unprofitable.s02_rsi2_meanrev",
    "strategies.ig_s5_zscore_breakout": "strategies.archive.unprofitable.s05_zscore_breakout",
    "strategies.ig_s7_daily_pivot": "strategies.s07_daily_pivot_breakout",
    "strategies.ig_s8_vol_target": "strategies.s08_vol_target_overlay",
    "strategies.ig_s9_donchian_cta": "strategies.s09_donchian_trend_cta",
    "strategies.ig_s10_ema_pullback": "strategies.s10_ema_pullback",
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


class _NamespaceLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
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
        prefix = fullname + "."
        if any(old_p.startswith(prefix) for old_p in _LEGACY_PREFIXES):
            spec = importlib.machinery.ModuleSpec(fullname, _NamespaceLoader(), is_package=True)
            spec.submodule_search_locations = []
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
