"""Composable StrategyStack — stack s01..s11 signal sleeves + overlays in one pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from strategies.core.registry import STRATEGY_CATALOG, load_signal_fn
from strategies.s08_vol_target_overlay import vol_target_risk_usd


@dataclass
class StackLayer:
    strategy_id: str
    weight: float = 1.0
    params: Dict[str, Any] = field(default_factory=dict)


class StrategyStack:
    """
    Stack multiple directional strategies (`s01`..`s11`) one on top of another
    and apply the `s08_vol_target_overlay` dollar-risk layer uniformly.
    """

    def __init__(self, layers: List[StackLayer], risk_overlay: Optional[Dict[str, Any]] = None):
        self.layers = layers
        self.risk_overlay = risk_overlay or {
            "enabled": True,
            "target_usd": 5.5,
            "min_usd": 5.0,
            "max_usd": 6.0,
            "tf": "H4",
        }

    def evaluate(
        self,
        o: np.ndarray,
        h: np.ndarray,
        l: np.ndarray,
        c: np.ndarray,
        times_min: np.ndarray,
        atr: np.ndarray,
        i: int,
        extra: Optional[Dict[str, Any]] = None,
        mode: str = "all",
    ) -> List[Dict[str, Any]]:
        """
        Evaluate stacked layers at closed bar index `i`:
          - mode="all"      : return every firing layer (independent multi-sleeve fanout)
          - mode="consensus": emit only if net weighted side agreement exists (multi-factor filter)
        """
        fires: List[Dict[str, Any]] = []
        for layer in self.layers:
            fn = load_signal_fn(layer.strategy_id)
            sig = fn(o, h, l, c, times_min, atr, i, layer.params, extra)
            if sig is not None:
                meta = STRATEGY_CATALOG.get(layer.strategy_id, {})
                fires.append({
                    **sig,
                    "strategy_id": layer.strategy_id,
                    "magic": meta.get("magic"),
                    "weight": layer.weight,
                })
        if mode == "consensus" and fires:
            net = sum(float(f["side"]) * float(f["weight"]) for f in fires)
            if abs(net) < 1e-9:
                return []
            dom_side = 1 if net > 0 else -1
            return [f for f in fires if f["side"] == dom_side]
        return fires

    def size_risk_usd(self, c: np.ndarray, tf: Optional[str] = None) -> float:
        """Apply s08_vol_target_overlay bounded within [$min_usd, $max_usd]."""
        ov = self.risk_overlay
        target = float(ov.get("target_usd", 5.5))
        if not ov.get("enabled", True):
            return target
        return float(
            vol_target_risk_usd(
                c,
                tf or str(ov.get("tf", "H4")),
                target,
                float(ov.get("min_usd", 5.0)),
                float(ov.get("max_usd", 6.0)),
            )
        )
