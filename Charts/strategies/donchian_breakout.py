"""Donchian Channel Breakout CTA strategy plugin with channel overlays."""

from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from Charts.core.indicators import wilder_atr
from Charts.core.strategy_base import BaseStrategy, Signal, StrategyParam


class DonchianBreakoutStrategy(BaseStrategy):
    """Breaks above/below prior N-bar high/low channel with ATR risk brackets."""

    id = "donchian_cta"
    name = "Donchian Channel Breakout"
    description = "Causal N-bar Donchian breakout with upper/lower channel overlays and ATR SL/TP."
    params = [
        StrategyParam("lookback", "Channel Lookback", "int", 20, 5, 100, 1),
        StrategyParam("sl_atr", "SL (x ATR14)", "float", 2.0, 0.5, 5.0, 0.25),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.5, 1.0, 6.0, 0.25),
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict[str, Any]) -> list[Signal]:
        p = {**self.default_params(), **params}
        lb = int(p["lookback"])
        if len(df) < lb + 5:
            return []

        hi_ch = df["high"].astype(float).shift(1).rolling(lb).max().to_numpy()
        lo_ch = df["low"].astype(float).shift(1).rolling(lb).min().to_numpy()
        atr = wilder_atr(df, 14).to_numpy()
        c = df["close"].to_numpy(dtype=float)
        times = df["timestamp"].to_numpy(dtype=int)

        signals: list[Signal] = []
        for i in range(lb + 1, len(df)):
            if not (np.isfinite(hi_ch[i]) and np.isfinite(lo_ch[i]) and np.isfinite(atr[i]) and atr[i] > 0):
                continue
            sl_dist = float(atr[i]) * float(p["sl_atr"])
            tp_dist = sl_dist * float(p["tp_r"])
            if c[i] > hi_ch[i] and c[i - 1] <= hi_ch[i - 1]:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=1,
                        price=float(c[i]),
                        sl=float(c[i] - sl_dist),
                        tp=float(c[i] + tp_dist),
                        tag="DON_BUY",
                        reason=f"Close > {lb}-bar high ({hi_ch[i]:.5g})",
                    )
                )
            elif c[i] < lo_ch[i] and c[i - 1] >= lo_ch[i - 1]:
                signals.append(
                    Signal(
                        index=i,
                        timestamp=int(times[i]),
                        side=-1,
                        price=float(c[i]),
                        sl=float(c[i] + sl_dist),
                        tp=float(c[i] - tp_dist),
                        tag="DON_SELL",
                        reason=f"Close < {lb}-bar low ({lo_ch[i]:.5g})",
                    )
                )
        return signals

    def extra_overlays(self, df: pd.DataFrame, params: dict[str, Any]) -> list[dict]:
        p = {**self.default_params(), **params}
        lb = int(p["lookback"])
        hi_ch = df["high"].astype(float).shift(1).rolling(lb).max()
        lo_ch = df["low"].astype(float).shift(1).rolling(lb).min()
        times = df["timestamp"].tolist()
        return [
            {
                "id": f"don_hi_{lb}",
                "name": f"Donchian High ({lb})",
                "pane": "main",
                "color": "#34d399",
                "data": [
                    {"time": int(t), "value": round(float(v), 6)}
                    for t, v in zip(times, hi_ch.tolist())
                    if v is not None and np.isfinite(v)
                ],
            },
            {
                "id": f"don_lo_{lb}",
                "name": f"Donchian Low ({lb})",
                "pane": "main",
                "color": "#fb7185",
                "data": [
                    {"time": int(t), "value": round(float(v), 6)}
                    for t, v in zip(times, lo_ch.tolist())
                    if v is not None and np.isfinite(v)
                ],
            },
        ]
