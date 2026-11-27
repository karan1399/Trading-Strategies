"""EMA Trend Pullback strategy plugin (TC-09 13/89/233 aligned)."""

from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from Charts.core.indicators import ema_series, wilder_atr, wilder_rsi
from Charts.core.strategy_base import BaseStrategy, Signal, StrategyParam


class EmaPullbackStrategy(BaseStrategy):
    """Enters on pullbacks to fast EMA in the direction of slow/trend EMAs."""

    id = "ema_pullback"
    name = "EMA Trend Pullback (TC-09)"
    description = "Trend-aligned pullback: Fast EMA cross/reclaim inside EMA 89/233 bias with RSI filter."
    params = [
        StrategyParam("fast_ema", "Fast EMA", "int", 13, 5, 50, 1),
        StrategyParam("slow_ema", "Slow EMA", "int", 89, 20, 200, 1),
        StrategyParam("trend_ema", "Trend EMA", "int", 233, 50, 400, 1),
        StrategyParam("rsi_buy_max", "RSI Max (Buy)", "float", 48.0, 20.0, 70.0, 1.0),
        StrategyParam("rsi_sell_min", "RSI Min (Sell)", "float", 52.0, 30.0, 80.0, 1.0),
        StrategyParam("sl_atr", "SL (x ATR14)", "float", 1.5, 0.5, 5.0, 0.25),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.2, 1.0, 6.0, 0.2),
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict[str, Any]) -> list[Signal]:
        p = {**self.default_params(), **params}
        if len(df) < int(p["trend_ema"]) + 5:
            return []

        close = df["close"].astype(float)
        fast = ema_series(close, int(p["fast_ema"])).to_numpy()
        slow = ema_series(close, int(p["slow_ema"])).to_numpy()
        trend = ema_series(close, int(p["trend_ema"])).to_numpy()
        rsi = wilder_rsi(close, 14).to_numpy()
        atr = wilder_atr(df, 14).to_numpy()

        c = close.to_numpy()
        o = df["open"].to_numpy(dtype=float)
        times = df["timestamp"].to_numpy(dtype=int)

        signals: list[Signal] = []
        start = max(int(p["trend_ema"]), 20)
        for i in range(start, len(df)):
            if not np.isfinite(atr[i]) or atr[i] <= 0:
                continue
            sl_dist = float(atr[i]) * float(p["sl_atr"])
            tp_dist = sl_dist * float(p["tp_r"])

            bull_bias = c[i] > trend[i] and fast[i] > slow[i]
            bear_bias = c[i] < trend[i] and fast[i] < slow[i]

            if bull_bias and c[i - 1] <= fast[i - 1] and c[i] > fast[i] and c[i] > o[i] and rsi[i] <= float(p["rsi_buy_max"]):
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=1,
                        price=float(c[i]),
                        sl=float(c[i] - sl_dist),
                        tp=float(c[i] + tp_dist),
                        tag="EMA_PB_BUY",
                        reason=f"Bull reclaim > EMA{int(p['fast_ema'])} (RSI {rsi[i]:.1f})",
                    )
                )
            elif bear_bias and c[i - 1] >= fast[i - 1] and c[i] < fast[i] and c[i] < o[i] and rsi[i] >= float(p["rsi_sell_min"]):
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=-1,
                        price=float(c[i]),
                        sl=float(c[i] + sl_dist),
                        tp=float(c[i] - tp_dist),
                        tag="EMA_PB_SELL",
                        reason=f"Bear reject < EMA{int(p['fast_ema'])} (RSI {rsi[i]:.1f})",
                    )
                )
        return signals
