"""S03 Footprint-Steroid Plugin: EMA 233 Touch Rejection + Stacked Imbalance >= 2."""

from __future__ import annotations

from typing import Any
import numpy as np

from footprint_Chart.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam
from strategies.core.indicators import ema_arr


class Ema233ImbalanceRejectionStrategy(BaseFootprintStrategy):
    """Triggers when price pierces EMA233 and closes through with >=2 diagonal imbalances + CLV."""

    id = "ema233_imb_rejection"
    name = "S03-FP EMA233 + ImbStack >= 2 (2h)"
    description = "1Y BTC A-tier (n=25, WR=60.0%, PF=1.56, Calmar=1.51): EMA233 touch rejection confirmed by >=2 stacked imbalances."
    params = [
        StrategyParam("min_imb", "Min Stacked Imbalances", "int", 2, 1, 6, 1),
        StrategyParam("min_clv", "Min |CLV|", "float", 0.25, 0.0, 0.80, 0.05),
        StrategyParam("sl_atr", "SL (ATR mult)", "float", 2.5, 1.0, 4.0, 0.25),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 3.5, 1.5, 6.0, 0.25),
        StrategyParam("be_r", "Breakeven Trigger (R)", "float", 1.0, 0.0, 3.0, 0.2),
        StrategyParam("max_hold", "Max Hold Bars", "int", 72, 12, 168, 12),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        n = len(bars)
        if n < 40:
            return []

        c = np.array([float(b["close"]) for b in bars], dtype=float)
        h = np.array([float(b["high"]) for b in bars], dtype=float)
        l = np.array([float(b["low"]) for b in bars], dtype=float)
        ema233 = ema_arr(c, 233 if n >= 260 else min(233, max(30, (n * 2) // 3)))
        tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
        tr[0] = h[0] - l[0]

        min_imb = int(p["min_imb"])
        min_clv = float(p["min_clv"])
        sl_atr = float(p["sl_atr"])
        tp_r = float(p["tp_r"])
        be_r = float(p["be_r"])
        max_hold = int(p["max_hold"])

        start_idx = min(235, max(25, n // 4))
        out: list[FootprintSignal] = []
        for i in range(start_idx, n):
            atr = float(np.mean(tr[max(0, i - 19) : i + 1]))
            if atr <= 0:
                continue
            bar = bars[i]
            rng = float(h[i] - l[i])
            clv = (((c[i] - l[i]) - (h[i] - c[i])) / rng) if rng > 0 else 0.0
            bi = int(bar.get("buy_imbalances", 0))
            si = int(bar.get("sell_imbalances", 0))
            sd = sl_atr * atr

            if l[i] <= ema233[i] + 0.30 * atr and c[i] > ema233[i] and c[i - 2] > ema233[i - 2] and bi >= min_imb and clv > min_clv:
                out.append(FootprintSignal(
                    index=i, time=int(bar["time"]), side=1, price=float(c[i]),
                    sl=float(c[i] - sd), tp=float(c[i] + tp_r * sd),
                    tag="S03_FP_E233_L", reason=f"EMA233 Touch BuyImb={bi} CLV={clv:+.2f}",
                    be_r=be_r, max_hold=max_hold,
                ))
            elif h[i] >= ema233[i] - 0.30 * atr and c[i] < ema233[i] and c[i - 2] < ema233[i - 2] and si >= min_imb and clv < -min_clv:
                out.append(FootprintSignal(
                    index=i, time=int(bar["time"]), side=-1, price=float(c[i]),
                    sl=float(c[i] + sd), tp=float(c[i] - tp_r * sd),
                    tag="S03_FP_E233_S", reason=f"EMA233 Touch SellImb={si} CLV={clv:+.2f}",
                    be_r=be_r, max_hold=max_hold,
                ))
        return out
