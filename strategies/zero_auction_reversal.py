"""Finished Auction Zero (0 | X Top, X | 0 Bottom) Reversal Strategy."""

from __future__ import annotations

from typing import Any

from footprint_Chart.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam


class ZeroAuctionReversalStrategy(BaseFootprintStrategy):
    """Detects finished auctions (0 seller at bar high or 0 buyer at bar low) to fade exhaustion."""

    id = "zero_auction_reversal"
    name = "Finished Auction Zero (0|X & X|0)"
    description = "Shorts finished-auction tops (0 | X at high); buys finished-auction bottoms (X | 0 at low)."
    params = [
        StrategyParam("min_extreme_val", "Min Opposite Trades/Vol at Zero", "float", 1.0, 0.0, 100.0, 1.0),
        StrategyParam("require_delta_confirm", "Require Delta Confirm (1=Yes,0=No)", "int", 0, 0, 1, 1),
        StrategyParam("sl_pad_ticks", "SL Buffer Beyond Extreme ($)", "float", 20.0, 5.0, 200.0, 5.0),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.0, 1.0, 6.0, 0.25),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        min_v = float(p["min_extreme_val"])
        req_d = int(p["require_delta_confirm"]) == 1
        pad = float(p["sl_pad_ticks"])
        tp_r = float(p["tp_r"])

        signals: list[FootprintSignal] = []
        for i, bar in enumerate(bars):
            levels = bar.get("levels") or []
            if not levels:
                continue
            top = levels[-1]
            bot = levels[0]

            # Top finished auction: 0 | X (no seller prints at the very top)
            if bar.get("zero_top") and float(top.get("buy", 0)) >= min_v:
                if not req_d or float(bar.get("delta", 0)) <= 0:
                    entry = float(bar["close"])
                    sl = float(bar["high"]) + pad
                    sl_dist = max(abs(sl - entry), pad)
                    signals.append(
                        FootprintSignal(
                            index=i,
                            time=int(bar["time"]),
                            side=-1,
                            price=entry,
                            sl=sl,
                            tp=entry - sl_dist * tp_r,
                            tag="ZERO_TOP_SELL",
                            reason=f"Finished auction top 0|{top['buy']} @ {top['price']}",
                        )
                    )
                    continue

            # Bottom finished auction: X | 0 (no buyer prints at the very bottom)
            if bar.get("zero_bot") and float(bot.get("sell", 0)) >= min_v:
                if not req_d or float(bar.get("delta", 0)) >= 0:
                    entry = float(bar["close"])
                    sl = float(bar["low"]) - pad
                    sl_dist = max(abs(entry - sl), pad)
                    signals.append(
                        FootprintSignal(
                            index=i,
                            time=int(bar["time"]),
                            side=1,
                            price=entry,
                            sl=sl,
                            tp=entry + sl_dist * tp_r,
                            tag="ZERO_BOT_BUY",
                            reason=f"Finished auction bottom {bot['sell']}|0 @ {bot['price']}",
                        )
                    )
        return signals
