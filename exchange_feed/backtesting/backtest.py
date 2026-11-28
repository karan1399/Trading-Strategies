"""Causal bar-by-bar simulator for footprint strategies with realistic fees & slippage."""

from __future__ import annotations

import math
from typing import Any

from exchange_feed.engines.strategy_base import FootprintSignal


def run_footprint_backtest(
    bars: list[dict[str, Any]],
    signals: list[FootprintSignal],
    risk_usd: float = 10.0,
    tp_r_mult: float = 2.0,
    fee_bps: float = 10.0,
    slippage_bps: float = 2.0,
) -> dict[str, Any]:
    """Simulate footprint signals causally (signal bar i -> entry at bar i+1 open).

    Realistic execution costs:
      - fee_bps: round-trip exchange fee in bps (default 10.0 bps = 5 bps x 2 Binance Futures taker).
      - slippage_bps: round-trip spread + slippage in bps (default 2.0 bps = 1 bp/side).
    """
    cost_bps = max(0.0, float(fee_bps) + float(slippage_bps))
    if not bars or not signals:
        return {
            "trades": [],
            "equity_curve": [],
            "metrics": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_r": 0.0,
                "gross_r": 0.0,
                "total_fees_r": 0.0,
                "total_fees_usd": 0.0,
                "expectancy_r": 0.0,
                "sharpe": 0.0,
                "total_pnl_usd": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_usd": 0.0,
                "fee_bps": round(float(fee_bps), 2),
                "slippage_bps": round(float(slippage_bps), 2),
            },
        }

    n = len(bars)
    sig_by_entry = {}
    for s in sorted(signals, key=lambda x: x.index):
        e_idx = s.index + 1
        if 1 <= e_idx < n and e_idx not in sig_by_entry:
            sig_by_entry[e_idx] = s

    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    open_pos: dict[str, Any] | None = None
    cum_pnl = 0.0
    peak_pnl = 0.0
    max_dd = 0.0

    def _record_exit(pos: dict[str, Any], ex_idx: int, ex_time: int, ex_px: float, reason: str) -> None:
        nonlocal cum_pnl, peak_pnl, max_dd
        side = pos["side"]
        raw_move = (ex_px - pos["entry_price"]) if side == 1 else (pos["entry_price"] - ex_px)
        gross_r = raw_move / pos["sl_dist"]
        fee_r = (cost_bps / 10000.0 * pos["entry_price"]) / pos["sl_dist"]
        net_r = gross_r - fee_r
        pnl_usd = net_r * risk_usd
        cum_pnl += pnl_usd
        peak_pnl = max(peak_pnl, cum_pnl)
        max_dd = max(max_dd, peak_pnl - cum_pnl)
        trades.append(
            {
                **pos,
                "exit_index": ex_idx,
                "exit_time": int(ex_time),
                "exit_price": round(float(ex_px), 2),
                "exit_reason": reason,
                "gross_r": round(float(gross_r), 3),
                "fee_r": round(float(fee_r), 3),
                "r_multiple": round(float(net_r), 3),
                "pnl_usd": round(float(pnl_usd), 2),
            }
        )
        equity_curve.append({"time": int(ex_time), "value": round(cum_pnl, 2)})

    for i, bar in enumerate(bars):
        if open_pos is not None:
            side = open_pos["side"]
            sl = open_pos["sl"]
            tp = open_pos["tp"]
            hi = float(bar["high"])
            lo = float(bar["low"])
            exit_px = None
            exit_reason = None

            if side == 1:
                if lo <= sl:
                    exit_px, exit_reason = sl, ("TRAIL_SL" if open_pos["trailed"] else "SL")
                elif tp is not None and hi >= tp:
                    exit_px, exit_reason = tp, "TP"
                else:
                    open_pos["best"] = max(open_pos["best"], hi)
                    if open_pos["be_r"] > 0 and (open_pos["best"] - open_pos["entry_price"]) >= open_pos["be_r"] * open_pos["sl_dist"]:
                        be_px = open_pos["entry_price"] + 0.05 * open_pos["sl_dist"]
                        if be_px > open_pos["sl"]:
                            open_pos["sl"] = be_px
                            open_pos["trailed"] = True
                    if open_pos["trail_dist"] > 0:
                        t_px = open_pos["best"] - open_pos["trail_dist"]
                        if t_px > open_pos["sl"]:
                            open_pos["sl"] = t_px
                            open_pos["trailed"] = True
            else:
                if hi >= sl:
                    exit_px, exit_reason = sl, ("TRAIL_SL" if open_pos["trailed"] else "SL")
                elif tp is not None and lo <= tp:
                    exit_px, exit_reason = tp, "TP"
                else:
                    open_pos["best"] = min(open_pos["best"], lo)
                    if open_pos["be_r"] > 0 and (open_pos["entry_price"] - open_pos["best"]) >= open_pos["be_r"] * open_pos["sl_dist"]:
                        be_px = open_pos["entry_price"] - 0.05 * open_pos["sl_dist"]
                        if be_px < open_pos["sl"]:
                            open_pos["sl"] = be_px
                            open_pos["trailed"] = True
                    if open_pos["trail_dist"] > 0:
                        t_px = open_pos["best"] + open_pos["trail_dist"]
                        if t_px < open_pos["sl"]:
                            open_pos["sl"] = t_px
                            open_pos["trailed"] = True

            if exit_px is None and open_pos["max_hold"] > 0 and (i - open_pos["entry_index"]) >= open_pos["max_hold"]:
                exit_px, exit_reason = float(bar["close"]), "TIME_EXIT"

            if exit_px is not None:
                _record_exit(open_pos, i, int(bar["time"]), float(exit_px), exit_reason)
                open_pos = None

        if open_pos is None and i in sig_by_entry:
            s = sig_by_entry[i]
            entry_px = float(bar["open"])
            prev_range = max(float(bars[i - 1]["high"]) - float(bars[i - 1]["low"]), entry_px * 0.001)
            sl_px = float(s.sl) if s.sl is not None else (entry_px - prev_range if s.side == 1 else entry_px + prev_range)
            sl_dist = abs(entry_px - sl_px)
            if sl_dist <= 0:
                continue
            if s.tp is not None:
                tp_px: float | None = float(s.tp)
            elif float(getattr(s, "trail_dist", 0.0) or 0.0) > 0:
                tp_px = None  # Trailing-stop managed trade
            else:
                tp_px = entry_px + sl_dist * tp_r_mult if s.side == 1 else entry_px - sl_dist * tp_r_mult

            open_pos = {
                "signal_index": int(s.index),
                "entry_index": i,
                "entry_time": int(bar["time"]),
                "side": int(s.side),
                "side_label": "BUY" if s.side == 1 else "SELL",
                "entry_price": round(entry_px, 2),
                "sl": round(sl_px, 2),
                "tp": round(tp_px, 2) if tp_px is not None else None,
                "sl_dist": float(sl_dist),
                "best": entry_px,
                "be_r": float(getattr(s, "be_r", 0.0) or 0.0),
                "trail_dist": float(getattr(s, "trail_dist", 0.0) or 0.0),
                "max_hold": int(getattr(s, "max_hold", 0) or 0),
                "trailed": False,
                "tag": s.tag,
                "reason": s.reason,
            }

    if open_pos is not None:
        last_bar = bars[-1]
        _record_exit(open_pos, n - 1, int(last_bar["time"]), float(last_bar["close"]), "OPEN_MTM")

    r_vals = [t["r_multiple"] for t in trades]
    gross_vals = [t["gross_r"] for t in trades]
    fee_r_vals = [t["fee_r"] for t in trades]
    pnl_vals = [t["pnl_usd"] for t in trades]
    wins = sum(1 for r in r_vals if r > 0)
    gross_win = sum(p for p in pnl_vals if p > 0)
    gross_loss = abs(sum(p for p in pnl_vals if p < 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.99 if gross_win > 0 else 0.0)

    n_tr = len(r_vals)
    exp_r = (sum(r_vals) / n_tr) if n_tr else 0.0
    if n_tr >= 2:
        mean_r = exp_r
        var_r = sum((x - mean_r) ** 2 for x in r_vals) / (n_tr - 1)
        std_r = math.sqrt(var_r)
        sharpe = (mean_r / std_r * math.sqrt(n_tr)) if std_r > 1e-12 else 0.0
    else:
        sharpe = 0.0

    total_fees_r = sum(fee_r_vals)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "metrics": {
            "total_trades": n_tr,
            "wins": wins,
            "losses": n_tr - wins,
            "win_rate": round((wins / n_tr * 100.0) if n_tr else 0.0, 1),
            "total_r": round(sum(r_vals), 2),
            "gross_r": round(sum(gross_vals), 2),
            "total_fees_r": round(total_fees_r, 2),
            "total_fees_usd": round(total_fees_r * risk_usd, 2),
            "expectancy_r": round(exp_r, 3),
            "sharpe": round(sharpe, 2),
            "total_pnl_usd": round(sum(pnl_vals), 2),
            "profit_factor": round(pf, 2),
            "max_drawdown_usd": round(max_dd, 2),
            "fee_bps": round(float(fee_bps), 2),
            "slippage_bps": round(float(slippage_bps), 2),
        },
    }
