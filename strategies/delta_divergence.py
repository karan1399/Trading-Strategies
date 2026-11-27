"""Delta Divergence Reversal Strategy."""

from __future__ import annotations

from typing import Any

from footprint_Chart.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam


class DeltaDivergenceStrategy(BaseFootprintStrategy):
    """Fades candles where price closes up on negative delta or down on positive delta."""

    id = "delta_divergence"
    name = "Delta Divergence Fade"
    description = "Bearish divergence: green candle with negative delta; Bullish divergence: red candle with positive delta."
    params = [
        StrategyParam("min_abs_delta_pct", "Min |Delta %|", "float", 10.0, 1.0, 80.0, 2.0),
        StrategyParam("sl_pad", "SL Buffer ($)", "float", 25.0, 5.0, 200.0, 5.0),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.0, 1.0, 5.0, 0.25),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        min_dp = float(p["min_abs_delta_pct"])
        pad = float(p["sl_pad"])
        tp_r = float(p["tp_r"])

        signals: list[FootprintSignal] = []
        for i, bar in enumerate(bars):
            o, c = float(bar["open"]), float(bar["close"])
            dp = float(bar.get("delta_pct", 0.0))
            if c > o and dp <= -min_dp:
                sl = float(bar["high"]) + pad
                sd = max(abs(sl - c), pad)
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=-1,
                        price=c,
                        sl=sl,
                        tp=c - sd * tp_r,
                        tag="DIV_SELL",
                        reason=f"Up bar with {dp:.1f}% delta",
                    )
                )
            elif c < o and dp >= min_dp:
                sl = float(bar["low"]) - pad
                sd = max(abs(c - sl), pad)
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=1,
                        price=c,
                        sl=sl,
                        tp=c + sd * tp_r,
                        tag="DIV_BUY",
                        reason=f"Down bar with +{dp:.1f}% delta",
                    )
                )
        return signals
