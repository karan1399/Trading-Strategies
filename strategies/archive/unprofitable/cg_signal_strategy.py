"""Strategy plugin combining Coinglass native signals (Abs/Div/sDiv/Trap/Exh) & Zero Auctions."""

from __future__ import annotations

from typing import Any

from exchange_feed.engines.coinglass_parity import compute_cg_signals
from exchange_feed.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam


class CoinglassSignalStrategy(BaseFootprintStrategy):
    """Trades Coinglass native Absorption / Divergence / Trap / Exhaustion signals."""

    id = "cg_signals_strategy"
    name = "Coinglass Engine (Abs/Trap/Div/Exh)"
    description = "Runs the exact Coinglass footprint signal engine, optionally filtered by 0|X / X|0 auction completion."
    params = [
        StrategyParam("require_zero", "Require Zero Auction (1=Yes,0=No)", "int", 0, 0, 1, 1),
        StrategyParam("sl_pad", "SL Buffer ($)", "float", 25.0, 5.0, 200.0, 5.0),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 2.0, 1.0, 6.0, 0.25),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        req_z = int(p["require_zero"]) == 1
        pad = float(p["sl_pad"])
        tp_r = float(p["tp_r"])

        # Adapt UI bars into fp shape expected by compute_cg_signals
        adapted = []
        for b in bars:
            lvls = b.get("levels") or []
            if not lvls:
                continue
            lo = lvls[0]["price"]
            tick = max(abs(lvls[1]["price"] - lvls[0]["price"]), 1.0) if len(lvls) > 1 else 20.0
            adapted.append({
                "ts": b["time"],
                "o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"],
                "fp": {
                    "tick": tick,
                    "lo": lo,
                    "b": [x["buy"] for x in lvls],
                    "s": [x["sell"] for x in lvls],
                    "bc": [x.get("buy_trades", 0) for x in lvls],
                    "sc": [x.get("sell_trades", 0) for x in lvls],
                },
            })

        raw_sigs = compute_cg_signals(adapted)
        ts_to_bar = {b["time"]: (idx, b) for idx, b in enumerate(bars)}
        out: list[FootprintSignal] = []
        for s in raw_sigs:
            if s["ts"] not in ts_to_bar:
                continue
            # Strictly causal execution index: trap confirms at bar s['j']; sdiv pivot confirms 3 bars later at s['i'] + 3
            if s["t"] == "trap" and s.get("j") is not None:
                idx = int(s["j"])
                if not (0 <= idx < len(bars)):
                    continue
                bar = bars[idx]
            elif s["t"] == "sdiv":
                idx = int(s["i"]) + 3
                if not (0 <= idx < len(bars)):
                    continue
                bar = bars[idx]
            else:
                idx, bar = ts_to_bar[s["ts"]]
            side = 1 if s["side"] == "buy" else -1
            if req_z and not ((side == 1 and bar.get("zero_bot")) or (side == -1 and bar.get("zero_top"))):
                continue
            c = float(bar["close"])
            sl = (float(bar["low"]) - pad) if side == 1 else (float(bar["high"]) + pad)
            sd = max(abs(c - sl), pad)
            out.append(
                FootprintSignal(
                    index=idx,
                    time=int(bar["time"]),
                    side=side,
                    price=c,
                    sl=sl,
                    tp=c + side * sd * tp_r,
                    tag=f"CG_{s['t'].upper()}",
                    reason=f"{s['name']}: {s['title']} (causal@{idx})",
                )
            )
        return out
