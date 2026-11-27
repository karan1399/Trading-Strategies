"""Bollinger Z-Score Mean Reversion strategy plugin."""

from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from Charts.core.indicators import wilder_atr
from Charts.core.strategy_base import BaseStrategy, Signal, StrategyParam


class BollingerZscoreStrategy(BaseStrategy):
    """Fades extreme Z-score deviations back inside +/- sigma bands."""

    id = "bb_zscore"
    name = "Bollinger Z-Score Reversion"
    description = "Enters when price Z-score crosses back inside +/- threshold from an extreme."
    params = [
        StrategyParam("window", "BB Window", "int", 20, 10, 100, 2),
        StrategyParam("z_thresh", "Z Threshold", "float", 2.0, 1.0, 3.5, 0.2),
        StrategyParam("sl_atr", "SL (x ATR14)", "float", 1.5, 0.5, 4.0, 0.25),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.0, 1.0, 5.0, 0.2),
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict[str, Any]) -> list[Signal]:
        p = {**self.default_params(), **params}
        w = int(p["window"])
        if len(df) < w + 5:
            return []

        close = df["close"].astype(float)
        mean = close.rolling(w).mean()
        std = close.rolling(w).std(ddof=0).replace(0.0, np.nan)
        z = ((close - mean) / std).to_numpy()
        atr = wilder_atr(df, 14).to_numpy()
        c = close.to_numpy()
        times = df["timestamp"].to_numpy(dtype=int)
        zt = float(p["z_thresh"])

        signals: list[Signal] = []
        for i in range(w + 1, len(df)):
            if not (np.isfinite(z[i]) and np.isfinite(z[i - 1]) and np.isfinite(atr[i]) and atr[i] > 0):
                continue
            sl_dist = float(atr[i]) * float(p["sl_atr"])
            tp_dist = sl_dist * float(p["tp_r"])
            if z[i - 1] < -zt and z[i] >= -zt:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=1,
                        price=float(c[i]),
                        sl=float(c[i] - sl_dist),
                        tp=float(c[i] + tp_dist),
                        tag="BB_Z_BUY",
                        reason=f"Z-score reclaimed -{zt:.1f} ({z[i]:.2f})",
                    )
                )
            elif z[i - 1] > zt and z[i] <= zt:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=-1,
                        price=float(c[i]),
                        sl=float(c[i] + sl_dist),
                        tp=float(c[i] - tp_dist),
                        tag="BB_Z_SELL",
                        reason=f"Z-score rejected +{zt:.1f} ({z[i]:.2f})",
                    )
                )
        return signals
