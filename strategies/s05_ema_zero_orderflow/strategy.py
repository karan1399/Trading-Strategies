"""EMA 89/233 Touch-Rejection & Complete Auction Zero (0|X, X|0) Order-Flow Strategy."""

from __future__ import annotations

from typing import Any

from footprint_Chart.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam


def _ema_series(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


class EmaZeroOrderFlowStrategy(BaseFootprintStrategy):
    """Combines two verified EMA 89/233 trend-aligned order-flow reversal triggers:
    1) EMA 89 Pullback Rejection: Stacked EMA89/EMA233 trend, candle pierces EMA89 & rejects with Delta% + CLV.
    2) Complete Auction (0|X Top / X|0 Bot): Finished auction aligned with sloping EMA89 + EMA233 trend.
    """

    id = "ema_zero_orderflow"
    name = "EMA89/233 + Zero Auction Pullback (1h)"
    description = "EMA89/233 stacked pullback rejections (Delta%+CLV) and Finished Zero Auctions (0|X, X|0) with ATR trail."
    params = [
        StrategyParam("enable_ema_touch", "Enable EMA89 Touch Rejection (1=Yes)", "int", 1, 0, 1, 1),
        StrategyParam("enable_zero_trend", "Enable Zero Auction + EMA89/233 (1=Yes)", "int", 1, 0, 1, 1),
        StrategyParam("min_vpm_rank", "Zero Auction Min VPM Rank (0-1)", "float", 0.40, 0.0, 0.90, 0.05),
        StrategyParam("min_delta_pct", "Touch Min |Delta %|", "float", 5.0, 0.0, 30.0, 1.0),
        StrategyParam("min_clv", "Touch Min |CLV|", "float", 0.25, 0.0, 0.8, 0.05),
        StrategyParam("sl_atr", "Initial SL (ATR mult)", "float", 2.0, 1.0, 4.0, 0.25),
        StrategyParam("trail_atr", "Trail Stop (ATR mult)", "float", 2.5, 1.0, 5.0, 0.25),
        StrategyParam("be_r", "Breakeven Trigger (R)", "float", 1.0, 0.0, 3.0, 0.2),
        StrategyParam("min_tp_r", "Hard TP R (0=Trail Only)", "float", 0.0, 0.0, 6.0, 0.5),
        StrategyParam("max_hold", "Max Hold Bars", "int", 96, 12, 240, 12),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        use_touch = int(p["enable_ema_touch"]) == 1
        use_zero = int(p["enable_zero_trend"]) == 1
        min_vpm = float(p["min_vpm_rank"])
        min_dp = float(p["min_delta_pct"])
        min_clv = float(p["min_clv"])
        sl_atr = float(p["sl_atr"])
        trail_atr = float(p["trail_atr"])
        be_r = float(p["be_r"])
        min_tp_r = float(p["min_tp_r"])
        max_hold = int(p["max_hold"])

        n = len(bars)
        if n < 30:
            return []

        closes = [float(b["close"]) for b in bars]
        ema89 = _ema_series(closes, 89 if n >= 120 else min(89, max(21, n // 3)))
        ema233 = _ema_series(closes, 233 if n >= 260 else min(233, max(55, (n * 2) // 3)))

        trs: list[float] = []
        for idx, b in enumerate(bars):
            hi, lo = float(b["high"]), float(b["low"])
            pc = float(bars[idx - 1]["close"]) if idx > 0 else hi
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))

        start_idx = min(235, max(25, n // 4))
        signals: list[FootprintSignal] = []
        vols = [float(b.get("volume", 0.0)) for b in bars]
        for i in range(start_idx, n):
            atr = sum(trs[max(0, i - 19) : i + 1]) / min(20, i + 1)
            if atr <= 0:
                continue

            bar = bars[i]
            hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
            rng = hi - lo
            clv = (((close - lo) - (hi - close)) / rng) if rng > 0 else 0.0
            dp = float(bar.get("delta_pct", 0.0))

            vol_win = sorted(vols[max(0, i - 49) : i + 1])
            v_rank = (sum(1 for x in vol_win if x <= vols[i]) / len(vol_win)) if vol_win else 1.0

            e89_up = ema89[i] > ema233[i] and ema89[i] > ema89[max(0, i - 3)] and close > ema233[i]
            e89_dn = ema89[i] < ema233[i] and ema89[i] < ema89[max(0, i - 3)] and close < ema233[i]

            touch_L = use_touch and e89_up and lo <= (ema89[i] + 0.25 * atr) and close > ema89[i] and dp > min_dp and clv > min_clv
            touch_S = use_touch and e89_dn and hi >= (ema89[i] - 0.25 * atr) and close < ema89[i] and dp < -min_dp and clv < -min_clv
            zero_L = use_zero and e89_up and bool(bar.get("zero_bot")) and v_rank >= min_vpm
            zero_S = use_zero and e89_dn and bool(bar.get("zero_top")) and v_rank >= min_vpm

            sd = sl_atr * atr
            td = trail_atr * atr
            tp_dist = (min_tp_r * sd) if min_tp_r > 0 else None

            if touch_L or zero_L:
                tag = "EMA89_ZERO_B" if (touch_L and zero_L) else ("EMA89_REJ_B" if touch_L else "ZERO_TREND_B")
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=1,
                        price=close,
                        sl=close - sd,
                        tp=(close + tp_dist) if tp_dist else None,
                        tag=tag,
                        reason=f"{tag} | EMA89={ema89[i]:.0f}>EMA233={ema233[i]:.0f} d%={dp:+.1f}% CLV={clv:+.2f}",
                        be_r=be_r,
                        trail_dist=td,
                        max_hold=max_hold,
                    )
                )
            elif touch_S or zero_S:
                tag = "EMA89_ZERO_S" if (touch_S and zero_S) else ("EMA89_REJ_S" if touch_S else "ZERO_TREND_S")
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=-1,
                        price=close,
                        sl=close + sd,
                        tp=(close - tp_dist) if tp_dist else None,
                        tag=tag,
                        reason=f"{tag} | EMA89={ema89[i]:.0f}<EMA233={ema233[i]:.0f} d%={dp:+.1f}% CLV={clv:+.2f}",
                        be_r=be_r,
                        trail_dist=td,
                        max_hold=max_hold,
                    )
                )
        return signals
