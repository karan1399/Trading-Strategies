"""RSI(2) Mean Reversion strategy plugin."""

from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from Charts.core.indicators import ema_series, wilder_atr, wilder_rsi
from Charts.core.strategy_base import BaseStrategy, Signal, StrategyParam


class Rsi2MeanReversionStrategy(BaseStrategy):
    """Short-term RSI(2) extreme pullback aligned with macro EMA trend."""

    id = "rsi2_meanrev"
    name = "RSI(2) Trend Mean Reversion"
    description = "Buys RSI(2) oversold dips above Trend EMA; sells RSI(2) overbought spikes below Trend EMA."
    params = [
        StrategyParam("rsi_len", "RSI Period", "int", 2, 2, 14, 1),
        StrategyParam("oversold", "Oversold Threshold", "float", 10.0, 2.0, 35.0, 1.0),
        StrategyParam("overbought", "Overbought Threshold", "float", 90.0, 65.0, 98.0, 1.0),
        StrategyParam("trend_ema", "Trend EMA", "int", 200, 50, 400, 10),
        StrategyParam("sl_atr", "SL (x ATR14)", "float", 1.8, 0.5, 5.0, 0.25),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.0, 1.0, 5.0, 0.2),
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict[str, Any]) -> list[Signal]:
        p = {**self.default_params(), **params}
        if len(df) < int(p["trend_ema"]) + 5:
            return []

        close = df["close"].astype(float)
        rsi = wilder_rsi(close, int(p["rsi_len"])).to_numpy()
        trend = ema_series(close, int(p["trend_ema"])).to_numpy()
        atr = wilder_atr(df, 14).to_numpy()
        c = close.to_numpy()
        times = df["timestamp"].to_numpy(dtype=int)

        signals: list[Signal] = []
        for i in range(int(p["trend_ema"]), len(df)):
            if not np.isfinite(atr[i]) or atr[i] <= 0:
                continue
            sl_dist = float(atr[i]) * float(p["sl_atr"])
            tp_dist = sl_dist * float(p["tp_r"])
            if c[i] > trend[i] and rsi[i] <= float(p["oversold"]) and rsi[i - 1] > float(p["oversold"]):
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=1,
                        price=float(c[i]),
                        sl=float(c[i] - sl_dist),
                        tp=float(c[i] + tp_dist),
                        tag="RSI2_BUY",
                        reason=f"RSI({int(p['rsi_len'])})={rsi[i]:.1f} <= {p['oversold']} above EMA",
                    )
                )
            elif c[i] < trend[i] and rsi[i] >= float(p["overbought"]) and rsi[i - 1] < float(p["overbought"]):
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=-1,
                        price=float(c[i]),
                        sl=float(c[i] + sl_dist),
                        tp=float(c[i] - tp_dist),
                        tag="RSI2_SELL",
                        reason=f"RSI({int(p['rsi_len'])})={rsi[i]:.1f} >= {p['overbought']} below EMA",
                    )
                )
        return signals
