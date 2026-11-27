"""Causal bar-by-bar backtest simulator for Charts strategy signals."""

from __future__ import annotations

import math
from typing import Any
import numpy as np
import pandas as pd

from Charts.core.indicators import wilder_atr
from Charts.core.strategy_base import Signal


def _estimate_spread_price(df: pd.DataFrame, idx: int) -> float:
    """Convert MT5 integer spread points to approximate price distance."""
    raw_spread = float(df["spread"].iloc[idx]) if "spread" in df.columns else 0.0
    if raw_spread <= 0:
        return 0.0
    close_px = abs(float(df["close"].iloc[idx]))
    # Heuristic: for FX (~1.x) 1 point = 1e-5; for JPY (~150) 1e-3; indices/crypto ~1e-2..1.0
    if close_px < 10:
        tick = 1e-5
    elif close_px < 500:
        tick = 1e-3
    elif close_px < 10000:
        tick = 1e-2
    else:
        tick = 1e-1
    return raw_spread * tick


def run_backtest(
    df: pd.DataFrame,
    signals: list[Signal],
    risk_usd: float = 10.0,
    sl_atr_mult: float = 1.5,
    tp_r_mult: float = 2.0,
    use_spread: bool = True,
    allow_overlapping: bool = False,
) -> dict[str, Any]:
    """Simulate signals causally (signal bar i -> entry at bar i+1 open).

    Conservative intrabar fill rule: if both SL and TP are hit on the same bar,
    SL is triggered first.
    """
    if df.empty or not signals:
        return {
            "trades": [],
            "equity_curve": [],
            "metrics": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_r": 0.0,
                "total_pnl_usd": 0.0,
                "profit_factor": 0.0,
                "expectancy_r": 0.0,
                "max_drawdown_usd": 0.0,
                "sharpe": 0.0,
            },
        }

    atr = wilder_atr(df, 14).to_numpy()
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = df["timestamp"].to_numpy(dtype=int)
    n = len(df)

    # Map entry_bar_idx -> first Signal from prior bar
    sig_by_entry: dict[int, Signal] = {}
    for s in sorted(signals, key=lambda x: x.index):
        entry_idx = s.index + 1
        if 1 <= entry_idx < n and entry_idx not in sig_by_entry:
            sig_by_entry[entry_idx] = s

    trades: list[dict[str, Any]] = []
    open_pos: dict[str, Any] | None = None
    cum_pnl = 0.0
    peak_pnl = 0.0
    max_dd = 0.0
    equity_curve: list[dict[str, Any]] = []

    for i in range(n):
        # 1. Check exit on active position
        if open_pos is not None:
            side = open_pos["side"]
            sl = open_pos["sl"]
            tp = open_pos["tp"]
            exit_px = None
            exit_reason = None

            if side == 1:
                if lows[i] <= sl:
                    exit_px = sl
                    exit_reason = "SL"
                elif highs[i] >= tp:
                    exit_px = tp
                    exit_reason = "TP"
            else:
                if highs[i] >= sl:
                    exit_px = sl
                    exit_reason = "SL"
                elif lows[i] <= tp:
                    exit_px = tp
                    exit_reason = "TP"

            if exit_px is not None:
                sl_dist = open_pos["sl_dist"]
                spread_px = open_pos["spread_px"]
                raw_move = (exit_px - open_pos["entry_price"]) if side == 1 else (open_pos["entry_price"] - exit_px)
                net_r = (raw_move - spread_px) / sl_dist
                pnl_usd = net_r * risk_usd
                cum_pnl += pnl_usd
                peak_pnl = max(peak_pnl, cum_pnl)
                max_dd = max(max_dd, peak_pnl - cum_pnl)
                trades.append(
                    {
                        **open_pos,
                        "exit_index": i,
                        "exit_time": int(times[i]),
                        "exit_price": round(float(exit_px), 6),
                        "exit_reason": exit_reason,
                        "r_multiple": round(float(net_r), 3),
                        "pnl_usd": round(float(pnl_usd), 2),
                    }
                )
                equity_curve.append({"time": int(times[i]), "value": round(cum_pnl, 2)})
                open_pos = None

        # 2. Check new entry at bar i open
        if (open_pos is None or allow_overlapping) and i in sig_by_entry:
            sig = sig_by_entry[i]
            entry_px = float(opens[i])
            bar_atr = float(atr[i - 1]) if np.isfinite(atr[i - 1]) and atr[i - 1] > 0 else abs(closes[i - 1]) * 0.005
            sl_dist = abs(entry_px - sig.sl) if sig.sl is not None else max(bar_atr * sl_atr_mult, 1e-6)
            if sl_dist <= 0:
                continue
            sl_px = float(sig.sl) if sig.sl is not None else (entry_px - sl_dist if sig.side == 1 else entry_px + sl_dist)
            tp_px = (
                float(sig.tp)
                if sig.tp is not None
                else (entry_px + sl_dist * tp_r_mult if sig.side == 1 else entry_px - sl_dist * tp_r_mult)
            )
            spread_px = _estimate_spread_price(df, i) if use_spread else 0.0
            open_pos = {
                "signal_index": int(sig.index),
                "signal_time": int(sig.timestamp),
                "entry_index": i,
                "entry_time": int(times[i]),
                "side": int(sig.side),
                "side_label": "BUY" if sig.side == 1 else "SELL",
                "entry_price": round(entry_px, 6),
                "sl": round(sl_px, 6),
                "tp": round(tp_px, 6),
                "sl_dist": float(sl_dist),
                "spread_px": float(spread_px),
                "tag": sig.tag,
                "reason": sig.reason,
            }

    # Close any open position at last bar close as MARK_TO_MARKET
    if open_pos is not None and n > 0:
        last_px = float(closes[-1])
        side = open_pos["side"]
        raw_move = (last_px - open_pos["entry_price"]) if side == 1 else (open_pos["entry_price"] - last_px)
        net_r = (raw_move - open_pos["spread_px"]) / open_pos["sl_dist"]
        pnl_usd = net_r * risk_usd
        cum_pnl += pnl_usd
        peak_pnl = max(peak_pnl, cum_pnl)
        max_dd = max(max_dd, peak_pnl - cum_pnl)
        trades.append(
            {
                **open_pos,
                "exit_index": n - 1,
                "exit_time": int(times[-1]),
                "exit_price": round(last_px, 6),
                "exit_reason": "OPEN_MTM",
                "r_multiple": round(float(net_r), 3),
                "pnl_usd": round(float(pnl_usd), 2),
            }
        )
        equity_curve.append({"time": int(times[-1]), "value": round(cum_pnl, 2)})

    r_vals = [t["r_multiple"] for t in trades]
    pnl_vals = [t["pnl_usd"] for t in trades]
    wins = sum(1 for r in r_vals if r > 0)
    losses = sum(1 for r in r_vals if r <= 0)
    gross_win = sum(p for p in pnl_vals if p > 0)
    gross_loss = abs(sum(p for p in pnl_vals if p < 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.99 if gross_win > 0 else 0.0)
    mean_r = float(np.mean(r_vals)) if r_vals else 0.0
    std_r = float(np.std(r_vals, ddof=1)) if len(r_vals) > 1 else 0.0
    sharpe = (mean_r / std_r * math.sqrt(len(r_vals))) if std_r > 0 else 0.0

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "metrics": {
            "total_trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / len(trades) * 100.0) if trades else 0.0, 1),
            "total_r": round(sum(r_vals), 2),
            "total_pnl_usd": round(sum(pnl_vals), 2),
            "profit_factor": round(pf, 2),
            "expectancy_r": round(mean_r, 3),
            "max_drawdown_usd": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
        },
    }
