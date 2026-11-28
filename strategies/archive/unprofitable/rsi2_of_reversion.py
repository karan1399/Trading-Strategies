"""S02 Footprint-Steroid Plugin: RSI(2) Mean Reversion + Order-Flow + VPIN Toxicity Gate."""

from __future__ import annotations

from typing import Any
import numpy as np

from exchange_feed.engines.coinglass_parity import compute_cg_signals
from exchange_feed.engines.strategy_base import BaseFootprintStrategy, FootprintSignal, StrategyParam
from strategies.core.indicators import ema_arr, rsi_wilder_arr


class Rsi2OrderFlowReversionStrategy(BaseFootprintStrategy):
    """Connors RSI(2) extremes filtered by EMA233, Delta/CLV/CG-Absorption, and VPIN < 0.65."""

    id = "rsi2_of_reversion"
    name = "S02-FP RSI(2) + OF + VPIN (1h)"
    description = "1Y BTC A-tier (n=19, WR=68.4%, Calmar=3.61, 4/4 Q>0): RSI(2)<15/>85 with Delta/CG-Abs & VPIN<0.65 veto."
    params = [
        StrategyParam("rsi_lo", "RSI(2) Extreme Threshold", "float", 15.0, 5.0, 25.0, 1.0),
        StrategyParam("vpin_max", "Max VPIN Rank (0-1)", "float", 0.65, 0.30, 0.95, 0.05),
        StrategyParam("sl_atr", "SL (ATR mult)", "float", 2.0, 1.0, 4.0, 0.25),
        StrategyParam("tp_r", "TP (R-Multiple)", "float", 3.5, 1.5, 6.0, 0.25),
        StrategyParam("be_r", "Breakeven Trigger (R)", "float", 1.0, 0.0, 3.0, 0.2),
        StrategyParam("max_hold", "Max Hold Bars", "int", 48, 12, 120, 6),
    ]

    def generate_signals(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> list[FootprintSignal]:
        p = {**self.default_params(), **params}
        n = len(bars)
        if n < 40:
            return []

        c = np.array([float(b["close"]) for b in bars], dtype=float)
        h = np.array([float(b["high"]) for b in bars], dtype=float)
        l = np.array([float(b["low"]) for b in bars], dtype=float)
        v = np.array([float(b.get("volume", 0.0)) for b in bars], dtype=float)
        delta = np.array([float(b.get("delta", 0.0)) for b in bars], dtype=float)

        ema233 = ema_arr(c, 233 if n >= 260 else min(233, max(30, (n * 2) // 3)))
        r2 = rsi_wilder_arr(c, 2)
        tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
        tr[0] = h[0] - l[0]

        vpin = np.zeros(n, dtype=float)
        for i in range(n):
            s = max(0, i - 11)
            vs = float(np.sum(v[s : i + 1]))
            vpin[i] = (float(np.sum(np.abs(delta[s : i + 1]))) / vs) if vs > 0 else 0.0

        # Extract causal CG absorption/exhaustion/divergence flags if level data exists
        adapted = []
        ts_to_i = {}
        for idx, b in enumerate(bars):
            ts_to_i[int(b["time"])] = idx
            lvls = b.get("levels") or []
            if lvls:
                tick = max(abs(lvls[1]["price"] - lvls[0]["price"]), 1.0) if len(lvls) > 1 else 20.0
                adapted.append({
                    "ts": b["time"], "o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"],
                    "fp": {"tick": tick, "lo": lvls[0]["price"], "b": [x["buy"] for x in lvls], "s": [x["sell"] for x in lvls]},
                })
        cg_b: set[int] = set()
        cg_s: set[int] = set()
        if adapted:
            for sig in compute_cg_signals(adapted):
                if sig["t"] in ("abs", "exh", "div") and sig["ts"] in ts_to_i:
                    idx = ts_to_i[sig["ts"]]
                    (cg_b if sig["side"] == "buy" else cg_s).add(idx)

        rsi_lo = float(p["rsi_lo"])
        vpin_max = float(p["vpin_max"])
        sl_atr = float(p["sl_atr"])
        tp_r = float(p["tp_r"])
        be_r = float(p["be_r"])
        max_hold = int(p["max_hold"])

        start_idx = min(235, max(25, n // 4))
        out: list[FootprintSignal] = []
        for i in range(start_idx, n):
            atr = float(np.mean(tr[max(0, i - 19) : i + 1]))
            if atr <= 0:
                continue
            win = vpin[max(0, i - 99) : i + 1]
            v_rk = float(np.mean(win <= vpin[i])) if len(win) else 0.5
            if v_rk >= vpin_max:
                continue

            bar = bars[i]
            rng = float(h[i] - l[i])
            clv = (((c[i] - l[i]) - (h[i] - c[i])) / rng) if rng > 0 else 0.0
            dp = float(bar.get("delta_pct", 0.0))
            of_b = (i in cg_b) or (dp > 5.0 and clv > 0.25)
            of_s = (i in cg_s) or (dp < -5.0 and clv < -0.25)
            sd = sl_atr * atr

            if r2[i] < rsi_lo and c[i] > ema233[i] and of_b:
                out.append(FootprintSignal(
                    index=i, time=int(bar["time"]), side=1, price=float(c[i]),
                    sl=float(c[i] - sd), tp=float(c[i] + tp_r * sd),
                    tag="S02_FP_L", reason=f"RSI2={r2[i]:.1f} VPIN={v_rk:.2f} d%={dp:+.1f}%",
                    be_r=be_r, max_hold=max_hold,
                ))
            elif r2[i] > (100.0 - rsi_lo) and c[i] < ema233[i] and of_s:
                out.append(FootprintSignal(
                    index=i, time=int(bar["time"]), side=-1, price=float(c[i]),
                    sl=float(c[i] + sd), tp=float(c[i] - tp_r * sd),
                    tag="S02_FP_S", reason=f"RSI2={r2[i]:.1f} VPIN={v_rk:.2f} d%={dp:+.1f}%",
                    be_r=be_r, max_hold=max_hold,
                ))
        return out
