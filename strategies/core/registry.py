"""Canonical strategy registry mapping s01..s11 + legacy ig_s* aliases."""
from __future__ import annotations

import importlib
from typing import Callable, Dict, Any

STRATEGY_CATALOG: Dict[str, Dict[str, Any]] = {
    "s01_williams_pullback":     {"module": "strategies.sleeves.trend.s01_williams_pullback.strategy",             "magic": 42008, "tf": "H4",  "kind": "signal",  "role": "Williams %R Trend Pullback"},
    "s02_rsi2_meanrev":          {"module": "strategies.sleeves.mean_reversion.s02_rsi2_meanrev.strategy",       "magic": 42009, "tf": "H1",  "kind": "signal",  "role": "RSI(2) Trend Mean Reversion"},
    "s03_htf_ema_rejection":     {"module": "strategies.sleeves.mean_reversion.s03_htf_ema_rejection.strategy",  "magic": 42003, "tf": "H4",  "kind": "signal",  "role": "EMA233 P4 Cluster Stall Rejection"},
    "s04_vc_squeeze_breakout":   {"module": "strategies.sleeves.breakout.s04_vc_squeeze_breakout.strategy",      "magic": 42004, "tf": "H4",  "kind": "signal",  "role": "TTM BB-in-KC Squeeze Breakout"},
    "s05_zscore_breakout":       {"module": "strategies.sleeves.breakout.s05_zscore_breakout.strategy",          "magic": 42007, "tf": "H4",  "kind": "signal",  "role": "Rolling Vol Z-Score Trend Breakout"},
    "s06_iv_wall_fade":          {"module": "strategies.sleeves.mean_reversion.s06_iv_wall_fade.strategy",       "magic": 42002, "tf": "M15", "kind": "signal",  "role": "Session 2-SD EWMA IV Wall Fade"},
    "s07_daily_pivot_breakout":  {"module": "strategies.sleeves.breakout.s07_daily_pivot_breakout.strategy",     "magic": 42006, "tf": "H4",  "kind": "signal",  "role": "D1 Turning-Point Pivot Breakout"},
    "s08_vol_target_overlay":    {"module": "strategies.overlays.s08_vol_target_overlay.overlay",                "magic": None,  "tf": "ALL", "kind": "overlay", "role": "Causal Inverse-Vol Dollar-Risk Scaler"},
    "s09_donchian_trend_cta":    {"module": "strategies.sleeves.trend.s09_donchian_trend_cta.strategy",          "magic": 42010, "tf": "H4",  "kind": "signal",  "role": "Donchian Channel Trend CTA"},
    "s10_ema_pullback":          {"module": "strategies.sleeves.trend.s10_ema_pullback.strategy",                "magic": 42011, "tf": "H4",  "kind": "signal",  "role": "Dual-EMA Pullback Trend Continuation"},
    "s11_or30_london_breakout":  {"module": "strategies.sleeves.breakout.s11_or30_london_breakout.strategy",     "magic": 42005, "tf": "M15", "kind": "signal",  "role": "London Open OR30 Breakout"},
}

LEGACY_MODULE_ALIASES: Dict[str, str] = {
    "strategies.ig_s1_williams_pullback.strategy": "strategies.sleeves.trend.s01_williams_pullback.strategy",
    "strategies.ig_s2_rsi2_meanrev.strategy":      "strategies.sleeves.mean_reversion.s02_rsi2_meanrev.strategy",
    "strategies.ig_s5_zscore_breakout.strategy":   "strategies.sleeves.breakout.s05_zscore_breakout.strategy",
    "strategies.ig_s7_daily_pivot.strategy":       "strategies.sleeves.breakout.s07_daily_pivot_breakout.strategy",
    "strategies.ig_s8_vol_target.overlay":         "strategies.overlays.s08_vol_target_overlay.overlay",
    "strategies.ig_s9_donchian_cta.strategy":      "strategies.sleeves.trend.s09_donchian_trend_cta.strategy",
    "strategies.ig_s10_ema_pullback.strategy":     "strategies.sleeves.trend.s10_ema_pullback.strategy",
    "strategies.s01_williams_pullback.strategy":   "strategies.sleeves.trend.s01_williams_pullback.strategy",
    "strategies.s02_rsi2_meanrev.strategy":        "strategies.sleeves.mean_reversion.s02_rsi2_meanrev.strategy",
    "strategies.s03_htf_ema_rejection.strategy":   "strategies.sleeves.mean_reversion.s03_htf_ema_rejection.strategy",
    "strategies.s04_vc_squeeze_breakout.strategy": "strategies.sleeves.breakout.s04_vc_squeeze_breakout.strategy",
    "strategies.s05_zscore_breakout.strategy":     "strategies.sleeves.breakout.s05_zscore_breakout.strategy",
    "strategies.s06_iv_wall_fade.strategy":        "strategies.sleeves.mean_reversion.s06_iv_wall_fade.strategy",
    "strategies.s07_daily_pivot_breakout.strategy":"strategies.sleeves.breakout.s07_daily_pivot_breakout.strategy",
    "strategies.s08_vol_target_overlay.overlay":   "strategies.overlays.s08_vol_target_overlay.overlay",
    "strategies.s09_donchian_trend_cta.strategy":  "strategies.sleeves.trend.s09_donchian_trend_cta.strategy",
    "strategies.s10_ema_pullback.strategy":        "strategies.sleeves.trend.s10_ema_pullback.strategy",
    "strategies.s11_or30_london_breakout.strategy":"strategies.sleeves.breakout.s11_or30_london_breakout.strategy",
    "htf_gold":                                    "strategies.sleeves.mean_reversion.s03_htf_ema_rejection.strategy",
    "vc_gold":                                     "strategies.sleeves.breakout.s04_vc_squeeze_breakout.strategy",
    "ivwall":                                      "strategies.sleeves.mean_reversion.s06_iv_wall_fade.strategy",
    "orb_nas":                                     "strategies.sleeves.breakout.s11_or30_london_breakout.strategy",
}

_CACHE: Dict[str, Callable] = {}


def resolve_module_path(name_or_module: str) -> str:
    """Resolve short ID (s01..s11), legacy ig_s* module path, or canonical module path."""
    if name_or_module in STRATEGY_CATALOG:
        return STRATEGY_CATALOG[name_or_module]["module"]
    return LEGACY_MODULE_ALIASES.get(name_or_module, name_or_module)


def load_signal_fn(module_path: str) -> Callable:
    """Dynamically load causal `signal_at` callable from canonical or legacy sleeve path."""
    resolved = resolve_module_path(module_path)
    fn = _CACHE.get(resolved)
    if fn is not None:
        return fn
    mod = importlib.import_module(resolved)
    fn = getattr(mod, "signal_at")
    _CACHE[resolved] = fn
    return fn
