"""Diagonal Imbalance Stack Continuation Strategy."""

from __future__ import annotations

from typing import Any

from footprint_Chart.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam


class ImbalanceStackStrategy(BaseFootprintStrategy):
    """Trades momentum continuation when a bar prints >= N buy/sell diagonal imbalances."""

    id = "imbalance_stack"
    name = "Imbalance Stack Continuation"
    description = "Enters in direction of dominant diagonal imbalances + aligned bar delta."
    params = [
        StrategyParam("min_imbalances", "Min Imbalances", "int", 2, 1, 10, 1),
        StrategyParam("sl_pad", "SL Buffer ($)", "float", 25.0, 5.0, 200.0, 5.0),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.0, 1.0, 5.0, 0.25),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        min_imb = int(p["min_imbalances"])
        pad = float(p["sl_pad"])
        tp_r = float(p["tp_r"])

        signals: list[FootprintSignal] = []
        for i, bar in enumerate(bars):
            bi = int(bar.get("buy_imbalances", 0))
            si = int(bar.get("sell_imbalances", 0))
            dlt = float(bar.get("delta", 0.0))
            c = float(bar["close"])
            if bi >= min_imb and bi > si and dlt > 0 and c >= float(bar["open"]):
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
                        tag="IMB_BUY",
                        reason=f"{bi} buy imbalances + delta {dlt:+.1f}",
                    )
                )
            elif si >= min_imb and si > bi and dlt < 0 and c <= float(bar["open"]):
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
                        tag="IMB_SELL",
                        reason=f"{si} sell imbalances + delta {dlt:+.1f}",
                    )
                )
        return signals
