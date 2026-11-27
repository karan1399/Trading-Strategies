"""VWAP 2-Sigma Order-Flow Reclaim Strategy (Walk-Forward Verified across 1Y & 4 Quarters @ 12bps RT)."""

from __future__ import annotations

import math
from typing import Any

from footprint_Chart.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam


class VwapOrderFlowReclaimStrategy(BaseFootprintStrategy):
    """Mean-reverts +/-2 sigma rolling VWAP extremes once footprint delta/CLV/zero-auction confirms exhaustion."""

    id = "vwap_of_reclaim"
    name = "VWAP 2σ OF Reclaim (2h Verified)"
    description = "Fades >2σ rolling VWAP stretches confirmed by Delta, CLV reclaim, or Finished Auction Zero."
    params = [
        StrategyParam("vwap_len", "Rolling VWAP Window", "int", 48, 20, 120, 4),
        StrategyParam("z_thresh", "Min |VWAP Z-Score|", "float", 2.0, 1.2, 3.5, 0.1),
        StrategyParam("short_only", "Shorts Only (+2σ Fades, 1=Yes)", "int", 0, 0, 1, 1),
        StrategyParam("sl_atr", "SL (ATR mult)", "float", 3.0, 1.5, 5.0, 0.25),
        StrategyParam("tp_atr", "TP (ATR mult)", "float", 5.5, 2.0, 9.0, 0.5),
        StrategyParam("be_r", "Breakeven Trigger (R)", "float", 1.2, 0.0, 3.0, 0.2),
        StrategyParam("max_hold", "Max Hold Bars", "int", 48, 12, 120, 6),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        w = max(12, int(p["vwap_len"]))
        zt = float(p["z_thresh"])
        short_only = int(p["short_only"]) == 1
        sl_atr = float(p["sl_atr"])
        tp_atr = float(p["tp_atr"])
        be_r = float(p["be_r"])
        max_hold = int(p["max_hold"])

        n = len(bars)
        if n <= w + 2:
            return []

        trs: list[float] = []
        for idx, b in enumerate(bars):
            hi, lo = float(b["high"]), float(b["low"])
            if idx == 0:
                trs.append(hi - lo)
            else:
                pc = float(bars[idx - 1]["close"])
                trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))

        signals: list[FootprintSignal] = []
        for i in range(max(w, 20), n):
            win = bars[i - w + 1 : i + 1]
            v_sum = sum(float(x.get("volume", 0.0)) for x in win)
            if v_sum <= 0:
                continue
            vw = sum(float(x["close"]) * float(x.get("volume", 0.0)) for x in win) / v_sum
            closes = [float(x["close"]) for x in win]
            mean_c = sum(closes) / len(closes)
            var_c = sum((c - mean_c) ** 2 for c in closes) / max(1, len(closes) - 1)
            sd_c = math.sqrt(var_c)
            if sd_c <= 0:
                continue

            bar = bars[i]
            close = float(bar["close"])
            vz = (close - vw) / sd_c
            hi, lo = float(bar["high"]), float(bar["low"])
            rng = hi - lo
            clv = (((close - lo) - (hi - close)) / rng) if rng > 0 else 0.0
            dp = float(bar.get("delta_pct", 0.0))
            atr = sum(trs[max(0, i - 19) : i + 1]) / min(20, i + 1)
            if atr <= 0:
                continue

            if not short_only and vz < -zt and (dp > 0 or clv > 0.2 or bool(bar.get("zero_bot"))):
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=1,
                        price=close,
                        sl=close - sl_atr * atr,
                        tp=close + tp_atr * atr,
                        tag="VWAP_RECLAIM_L",
                        reason=f"VWAP Z={vz:+.2f} < -{zt:.1f} | d%={dp:+.1f}% CLV={clv:+.2f}",
                        be_r=be_r,
                        max_hold=max_hold,
                    )
                )
            elif vz > zt and (dp < 0 or clv < -0.2 or bool(bar.get("zero_top"))):
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=-1,
                        price=close,
                        sl=close + sl_atr * atr,
                        tp=close - tp_atr * atr,
                        tag="VWAP_RECLAIM_S",
                        reason=f"VWAP Z={vz:+.2f} > +{zt:.1f} | d%={dp:+.1f}% CLV={clv:+.2f}",
                        be_r=be_r,
                        max_hold=max_hold,
                    )
                )
        return signals
