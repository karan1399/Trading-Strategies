"""UTC Day VWAP Rejection / Reclaim strategy plugin."""

from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from Charts.core.indicators import wilder_atr
from Charts.core.strategy_base import BaseStrategy, Signal, StrategyParam


class VwapRejectionStrategy(BaseStrategy):
    """Trades rejections/reclaims around UTC Day VWAP +/- sigma bands."""

    id = "vwap_reclaim"
    name = "UTC Day VWAP Reclaim"
    description = "Buys when price reclaims Day VWAP from below; sells when price rejects Day VWAP from above."
    params = [
        StrategyParam("min_dist_atr", "Min Prior Excursion (xATR)", "float", 0.3, 0.0, 2.0, 0.1),
        StrategyParam("sl_atr", "SL (x ATR14)", "float", 1.5, 0.5, 4.0, 0.25),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.0, 1.0, 5.0, 0.2),
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict[str, Any]) -> list[Signal]:
        p = {**self.default_params(), **params}
        if len(df) < 30:
            return []

        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        vol = df["volume"].replace(0.0, 1.0)
        day_key = df["datetime"].dt.strftime("%Y-%m-%d")
        vwap = ((tp * vol).groupby(day_key).cumsum() / vol.groupby(day_key).cumsum()).to_numpy()
        atr = wilder_atr(df, 14).to_numpy()
        c = df["close"].to_numpy(dtype=float)
        times = df["timestamp"].to_numpy(dtype=int)
        min_d = float(p["min_dist_atr"])

        signals: list[Signal] = []
        for i in range(20, len(df)):
            if not (np.isfinite(vwap[i]) and np.isfinite(atr[i]) and atr[i] > 0):
                continue
            sl_dist = float(atr[i]) * float(p["sl_atr"])
            tp_dist = sl_dist * float(p["tp_r"])
            if c[i - 1] < vwap[i - 1] - min_d * atr[i] and c[i] > vwap[i]:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=1,
                        price=float(c[i]),
                        sl=float(c[i] - sl_dist),
                        tp=float(c[i] + tp_dist),
                        tag="VWAP_BUY",
                        reason="Reclaimed Day VWAP from below",
                    )
                )
            elif c[i - 1] > vwap[i - 1] + min_d * atr[i] and c[i] < vwap[i]:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=-1,
                        price=float(c[i]),
                        sl=float(c[i] + sl_dist),
                        tp=float(c[i] - tp_dist),
                        tag="VWAP_SELL",
                        reason="Rejected Day VWAP from above",
                    )
                )
        return signals
