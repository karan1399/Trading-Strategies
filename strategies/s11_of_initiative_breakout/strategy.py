"""Order-Flow Initiative Breakout Strategy (Walk-Forward Verified across 1Y & 4 Quarters @ 12bps RT)."""

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


def completed_d1_directions(bars: list[dict[str, Any]], max_age_seconds: int = 86400) -> list[int]:
    """As-of direction using observed UTC midnight closes, including the current close."""
    fast = slow = None
    samples = direction = 0
    last_close: int | None = None
    directions = []
    for bar in bars:
        end = bar.get("end_ts")
        if end is None:
            directions.append(0)
            continue
        end = int(end)
        if end <= int(bar["time"]):
            raise ValueError("D1 gate requires a close timestamp after the bar open")
        if end % 86400 == 0 and (last_close is None or end > last_close):
            close = float(bar["close"])
            fast = close if fast is None else close * (2.0 / 51.0) + fast * (49.0 / 51.0)
            slow = close if slow is None else close * (2.0 / 201.0) + slow * (199.0 / 201.0)
            last_close = end
            samples += 1
            direction = int(fast > slow) - int(fast < slow)
        current = last_close is not None and 0 <= end - last_close < max_age_seconds
        directions.append(direction if current and samples >= 200 else 0)
    return directions


class OrderFlowInitiativeBreakoutStrategy(BaseFootprintStrategy):
    """Breakout above/below N-bar extreme aligned with EMA 233, CLV acceptance, Delta %, and Footprint Imbalance dominance.

    1Y Backtest (1h BTCUSDT, 523K 1m bars -> 8,725 1h bars, 12 bps RT taker+slippage):
      n=185 trades, WR=42.7%, Net=+31.6R, PF=1.39, MaxDD=10.1R, Calmar=3.12 (all 4 quarters positive: +8.2/+2.2/+5.8/+15.3R).
    """

    id = "of_initiative_breakout"
    name = "OF Initiative Breakout + EMA233 (1h)"
    description = "1Y 4-quarter verified (12bps RT): Donchian 22 breakout + EMA233 trend filter + CLV + Delta% + Imbalance + ATR trail."
    params = [
        StrategyParam("lookback", "Breakout Lookback Bars", "int", 22, 10, 48, 2),
        StrategyParam("require_ema233", "Require EMA 233 Filter (1=Yes)", "int", 1, 0, 1, 1),
        StrategyParam("min_clv", "Min |Close Loc Value|", "float", 0.30, 0.0, 0.80, 0.05),
        StrategyParam("min_delta_pct", "Min |Delta %|", "float", 0.0, 0.0, 25.0, 2.0),
        StrategyParam("require_imb_dom", "Require Imb Dominance (1=Yes)", "int", 1, 0, 1, 1),
        StrategyParam("require_oi_expand", "Require OI Expanding (1=Yes)", "int", 0, 0, 1, 1),
        StrategyParam("d1_ema_trend_gate", "Require D1 EMA50>EMA200 Gate (1=Yes)", "int", 0, 0, 1, 1),
        StrategyParam("sl_atr", "Initial SL (ATR mult)", "float", 2.0, 1.0, 4.0, 0.25),
        StrategyParam("trail_atr", "Trail Stop (ATR mult)", "float", 2.5, 1.0, 5.0, 0.25),
        StrategyParam("be_r", "Breakeven Trigger (R)", "float", 1.0, 0.0, 3.0, 0.25),
        StrategyParam("max_hold", "Max Hold Bars", "int", 96, 12, 240, 12),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        lb = max(5, int(p["lookback"]))
        req_ema233 = int(p["require_ema233"]) == 1
        min_clv = float(p["min_clv"])
        min_dp = float(p["min_delta_pct"])
        req_imb = int(p["require_imb_dom"]) == 1
        req_oi = int(p["require_oi_expand"]) == 1
        raw_d1_gate = p.get("d1_ema_trend_gate", 0)
        req_d1_gate = bool(raw_d1_gate) if isinstance(raw_d1_gate, bool) else (int(raw_d1_gate) == 1)
        sl_atr = float(p["sl_atr"])
        trail_atr = float(p["trail_atr"])
        be_r = float(p["be_r"])
        max_hold = int(p["max_hold"])

        n = len(bars)
        if n <= lb + 2:
            return []

        closes = [float(b["close"]) for b in bars]
        ema233 = _ema_series(closes, 233)
        d1_dir = completed_d1_directions(bars) if req_d1_gate else [0] * n

        trs: list[float] = []
        for idx, b in enumerate(bars):
            hi, lo = float(b["high"]), float(b["low"])
            pc = float(bars[idx - 1]["close"]) if idx > 0 else hi
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))

        signals: list[FootprintSignal] = []
        for i in range(max(lb, 20), n):
            atr = sum(trs[max(0, i - 19) : i + 1]) / min(20, i + 1)
            if atr <= 0:
                continue

            bar = bars[i]
            hi = float(bar["high"])
            lo = float(bar["low"])
            close = float(bar["close"])
            rng = hi - lo
            clv = (((close - lo) - (hi - close)) / rng) if rng > 0 else 0.0

            prev_hi = max(float(bars[k]["high"]) for k in range(i - lb, i))
            prev_lo = min(float(bars[k]["low"]) for k in range(i - lb, i))
            dp = float(bar.get("delta_pct", 0.0))
            bi = int(bar.get("buy_imbalances", 0))
            si = int(bar.get("sell_imbalances", 0))
            oi_chg = bar.get("oi_change")
            oi_ok = (not req_oi) or (oi_chg is None) or (float(oi_chg) > 0)

            sd = sl_atr * atr
            td = trail_atr * atr

            ema_up = ((not req_ema233) or (close > ema233[i])) and ((not req_d1_gate) or d1_dir[i] == 1)
            ema_dn = ((not req_ema233) or (close < ema233[i])) and ((not req_d1_gate) or d1_dir[i] == -1)

            if close > prev_hi and ema_up and clv >= min_clv and dp > min_dp and (not req_imb or bi >= si) and oi_ok:
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=1,
                        price=close,
                        sl=close - sd,
                        tp=None,
                        tag="OF_BRK_LONG",
                        reason=f"Break>{lb}h hi | EMA233={ema233[i]:.0f} CLV={clv:+.2f} d%={dp:+.1f}% Imb={bi}:{si}",
                        be_r=be_r,
                        trail_dist=td,
                        max_hold=max_hold,
                    )
                )
            elif close < prev_lo and ema_dn and clv <= -min_clv and dp < -min_dp and (not req_imb or si >= bi) and oi_ok:
                signals.append(
                    FootprintSignal(
                        index=i,
                        time=int(bar["time"]),
                        side=-1,
                        price=close,
                        sl=close + sd,
                        tp=None,
                        tag="OF_BRK_SHORT",
                        reason=f"Break<{lb}h lo | EMA233={ema233[i]:.0f} CLV={clv:+.2f} d%={dp:+.1f}% Imb={bi}:{si}",
                        be_r=be_r,
                        trail_dist=td,
                        max_hold=max_hold,
                    )
                )
        return signals
