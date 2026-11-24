#!/usr/bin/env python3
"""
alpha360_watch.py — Real-time Signal Watcher & MT5 Order Ticket Generator
for the Alpha360 / HTF EMA Rejection Strategy Basket.

Features:
  - Scans the validated Alpha360 basket (15 symbols / optimal plateau picks).
  - Evaluates HTF EMA 89/233 patterns (P1 wick-kiss, P2 two-bar, P3 pierce-fail, P4 cluster stall).
  - Strictly follows closed-bar discipline (Rule 1: no intra-bar signals).
  - Real broker lot sizing calibrated to exact target risk (e.g. $5.50 USD / trade).
  - Direct MT5 live rates with automatic fallback to historical parquet.
  - Supports one-off scan, multi-bar lookback, and continuous live daemon mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Path setup
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dfib_engine import wilder_atr, ema, infer_digits
from htf_reject import precompute_htf

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


TF_MAP_MT5 = {
    "1M": 1,        # mt5.TIMEFRAME_M1
    "5M": 5,        # mt5.TIMEFRAME_M5
    "15M": 15,      # mt5.TIMEFRAME_M15
    "1H": 16385,    # mt5.TIMEFRAME_H1
    "4H": 16388,    # mt5.TIMEFRAME_H4
    "1D": 16408,    # mt5.TIMEFRAME_D1
}


def load_universe_specs() -> dict[str, dict]:
    """Load MT5 broker specs from desk/mt5_symbols_universe.json as fallback."""
    spec_path = REPO_ROOT / "desk" / "mt5_symbols_universe.json"
    if not spec_path.exists():
        return {}
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        specs = {}
        for cat, items in data.items():
            if isinstance(items, list):
                for item in items:
                    specs[item["name"]] = item
        return specs
    except Exception:
        return {}


def load_basket_picks(csv_path: Path) -> pd.DataFrame:
    """Load basket configuration table."""
    if not csv_path.exists():
        # Fallback to htf_ema_rejection if not in local results
        alt_path = REPO_ROOT / "htf_ema_rejection" / "results" / "basket_picks.csv"
        if alt_path.exists():
            csv_path = alt_path
    if not csv_path.exists():
        raise FileNotFoundError(f"Could not find basket_picks.csv at {csv_path}")
    return pd.read_csv(csv_path)


def fetch_rates_mt5(symbol: str, tf_str: str, num_bars: int = 400) -> pd.DataFrame | None:
    """Fetch live candlestick rates from MetaTrader 5."""
    if not MT5_AVAILABLE:
        return None
    tf_enum = TF_MAP_MT5.get(tf_str)
    if tf_enum is None:
        return None

    # Check if symbol exists or needs suffix
    candidates = [symbol, symbol.replace(".pi", ".p"), symbol.replace(".p", ".pi"), symbol.split(".")[0]]
    resolved_sym = None
    for s in candidates:
        info = mt5.symbol_info(s)
        if info is not None:
            resolved_sym = s
            if not info.visible:
                mt5.symbol_select(s, True)
            break

    if resolved_sym is None:
        return None

    rates = mt5.copy_rates_from_pos(resolved_sym, tf_enum, 0, num_bars)
    if rates is None or len(rates) == 0:
        return None

    df = pd.DataFrame(rates)
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["digits"] = info.digits if hasattr(info, "digits") else 2
    return df


def fetch_rates_parquet(symbol: str, tf_str: str) -> pd.DataFrame | None:
    """Load candles from historical_parquet directory."""
    pq_path = REPO_ROOT / "historical_parquet" / tf_str / f"{symbol}.parquet"
    if not pq_path.exists():
        # Try alternate extensions
        base = symbol.split(".")[0]
        for candidate in [f"{base}.pi.parquet", f"{base}.p.parquet", f"{base}.parquet"]:
            alt = REPO_ROOT / "historical_parquet" / tf_str / candidate
            if alt.exists():
                pq_path = alt
                break
    if not pq_path.exists():
        return None

    try:
        df = pd.read_parquet(pq_path)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df["digits"] = infer_digits(df["close"])
        return df
    except Exception:
        return None


def calculate_lots(symbol: str, risk_usd: float, entry: float, sl: float, specs_db: dict) -> tuple[float, float, float]:
    """Calculate broker lot size for exact dollar risk."""
    risk_dist = abs(entry - sl)
    if risk_dist <= 0:
        return 0.01, 0.0, 0.0

    # Try live MT5 info first
    if MT5_AVAILABLE and mt5.terminal_info() is not None:
        candidates = [symbol, symbol.replace(".pi", ".p"), symbol.replace(".p", ".pi"), symbol.split(".")[0]]
        for s in candidates:
            info = mt5.symbol_info(s)
            if info is not None:
                tick_size = info.trade_tick_size or 0.01
                tick_val = info.trade_tick_value or 1.0
                vol_min = info.volume_min or 0.01
                vol_step = info.volume_step or 0.01
                vol_max = info.volume_max or 100.0

                loss_per_lot = (risk_dist / tick_size) * tick_val
                if loss_per_lot > 0:
                    raw_lots = risk_usd / loss_per_lot
                    steps = round((raw_lots - vol_min) / vol_step)
                    lots = vol_min + max(0, steps) * vol_step
                    lots = min(vol_max, max(vol_min, round(lots, 4)))
                    actual_risk = lots * loss_per_lot
                    return lots, actual_risk, risk_dist

    # Fallback to desk/mt5_symbols_universe.json specs
    spec = specs_db.get(symbol) or specs_db.get(symbol.split(".")[0])
    if spec:
        contract_size = float(spec.get("trade_contract_size", 1.0))
        vol_min = float(spec.get("volume_min", 0.01))
        vol_step = float(spec.get("volume_step", 0.01))
        vol_max = float(spec.get("volume_max", 100.0))
        loss_per_lot = risk_dist * contract_size
        if loss_per_lot > 0:
            raw_lots = risk_usd / loss_per_lot
            steps = round((raw_lots - vol_min) / vol_step)
            lots = vol_min + max(0, steps) * vol_step
            lots = min(vol_max, max(vol_min, round(lots, 4)))
            actual_risk = lots * loss_per_lot
            return lots, actual_risk, risk_dist

    # Generic fallback
    return 0.01, risk_usd, risk_dist


def evaluate_basket(basket_df: pd.DataFrame, risk_usd: float, bars_lookback: int, specs_db: dict) -> tuple[list[dict], list[dict]]:
    """Scan all basket assets, return active tickets and watchlist metrics."""
    tickets = []
    watchlist = []

    for _, row in basket_df.iterrows():
        symbol = str(row["symbol"])
        tf_str = str(row["tf"])
        period = int(row["period"])
        pattern = str(row["pattern"])
        slope_gate = bool(row["slope"])
        regime_gate = bool(row["regime"])
        exit_rule = str(row["exit"])

        # Fetch rates
        df = fetch_rates_mt5(symbol, tf_str, num_bars=350)
        source = "MT5"
        if df is None:
            df = fetch_rates_parquet(symbol, tf_str)
            source = "Parquet"

        if df is None or len(df) < period + 20:
            watchlist.append({
                "symbol": symbol,
                "tf": tf_str,
                "pattern": pattern,
                "period": period,
                "price": 0.0,
                "ema": 0.0,
                "dist_atr": 0.0,
                "status": f"NO DATA ({source})",
            })
            continue

        c = df["close"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        atr = wilder_atr(h, l, c, 14)
        ef = ema(c, period)

        # Precompute HTF signals
        sides, setups, rds = precompute_htf(
            df, symbol, tf_str, period, pattern,
            slope_gate=slope_gate, regime_gate=regime_gate
        )

        n_bars = len(c)
        last_idx = n_bars - 1
        curr_price = c[last_idx]
        curr_ema = ef[last_idx]
        curr_atr = atr[last_idx]
        dist_atr = (curr_price - curr_ema) / curr_atr if curr_atr > 0 else 0.0

        # Status inspection
        signal_found = False
        # In live MT5 rates, index -1 is forming bar; closed bars are -2, -3, ...
        # If parquet, all bars are historical closed bars.
        start_check = max(0, n_bars - 1 - bars_lookback)
        end_check = n_bars - 1 if source == "MT5" else n_bars

        for i in range(start_check, end_check):
            side = sides[i]
            if side != 0:
                signal_found = True
                side_str = "BUY" if side == 1 else "SELL"
                entry_price = c[i]
                rd = rds[i]
                sl_price = entry_price - rd if side == 1 else entry_price + rd

                # Format TP
                if exit_rule.startswith("fixed_"):
                    mult = float(exit_rule.replace("fixed_", "").replace("r", ""))
                    tp_price = entry_price + mult * rd if side == 1 else entry_price - mult * rd
                    tp_str = f"{tp_price:.4f} (+{mult:.1f}R)"
                else:
                    tp_price = 0.0
                    tp_str = f"TRAIL (Lock BE at +1.0R, {exit_rule})"

                # Calculate lots
                lots, actual_risk, _ = calculate_lots(symbol, risk_usd, entry_price, sl_price, specs_db)
                bar_time = df["datetime"].iloc[i].strftime("%Y-%m-%d %H:%M UTC")

                tickets.append({
                    "symbol": symbol,
                    "side": side_str,
                    "tf": tf_str,
                    "pattern": pattern,
                    "period": period,
                    "entry": entry_price,
                    "sl": sl_price,
                    "tp": tp_str,
                    "tp_price": tp_price,
                    "rd": rd,
                    "lots": lots,
                    "risk_usd": actual_risk,
                    "target_risk": risk_usd,
                    "bar_time": bar_time,
                    "exit_rule": exit_rule,
                    "bar_idx": i - n_bars + 1,
                    "source": source
                })

        bias = "BULL" if curr_price > curr_ema else "BEAR"
        status_str = "SIGNAL ARMED" if signal_found else f"Near EMA ({dist_atr:+.2f} ATR)" if abs(dist_atr) <= 1.0 else f"Trend ({bias})"

        watchlist.append({
            "symbol": symbol,
            "tf": tf_str,
            "pattern": pattern,
            "period": period,
            "price": curr_price,
            "ema": curr_ema,
            "dist_atr": dist_atr,
            "status": status_str,
        })

    return tickets, watchlist


def print_dashboard(tickets: list[dict], watchlist: list[dict], risk_usd: float, bars_lookback: int):
    """Render formatted dashboard and order tickets to terminal."""
    print("\n" + "=" * 90)
    print("  ALPHA360 HTF EMA REJECTION — SIGNAL WATCHER & ORDER TICKET DESK")
    print(f"  Target Risk: ${risk_usd:.2f} USD/trade | Scan Window: {bars_lookback} closed bar(s) | MT5: {'CONNECTED' if MT5_AVAILABLE else 'OFFLINE'}")
    print("=" * 90)

    # 1. Active Order Tickets
    if tickets:
        print("\n⚡ [ACTIVE ORDER TICKETS — READY TO PUNCH]:")
        for t in tickets:
            print("-" * 90)
            print(f"  TICKET: {t['symbol']} | {t['side']} | Timeframe: {t['tf']} | Source: {t['source']}")
            print(f"  Setup:         {t['pattern']} at EMA{t['period']} | Exit Rule: {t['exit_rule']}")
            print(f"  Bar Closed:    {t['bar_time']} ({abs(t['bar_idx'])} bars ago)")
            print(f"  Entry Price:   {t['entry']:.5f} (Market Open)")
            print(f"  Stop Loss:     {t['sl']:.5f} (Distance: {t['rd']:.5f} / 1.0R)")
            print(f"  Take Profit:   {t['tp']}")
            print(f"  Position Size: {t['lots']:.2f} Lots (Broker Risk: ${t['risk_usd']:.2f} USD / Target: ${t['target_risk']:.2f})")
            print("  Discipline:    Closed bar confirmed. Enter at open of next candle.")
        print("-" * 90)
    else:
        print("\n⚡ [ACTIVE ORDER TICKETS]: None in the current closed-bar window.")

    # 2. Watchlist Status Table
    print("\n📋 [ALPHA360 WATCHLIST MONITORED MEMBERS (15 Symbols)]:")
    print(f"  {'Symbol':<12} {'TF':<5} {'EMA':<8} {'Pattern':<12} {'Price':<12} {'EMA Level':<12} {'Dist (ATR)':<12} {'State'}")
    print("  " + "-" * 86)
    for w in watchlist:
        print(f"  {w['symbol']:<12} {w['tf']:<5} EMA{w['period']:<5} {w['pattern']:<12} {w['price']:<12.5f} {w['ema']:<12.5f} {w['dist_atr']:<+12.2f} {w['status']}")
    print("=" * 90 + "\n")


def punch_order(ticket: dict, live: bool = False) -> bool:
    """Send live order to MT5 with risk safety guards."""
    if not live:
        return False
    if not MT5_AVAILABLE or mt5.terminal_info() is None:
        print("  [EXEC] Cannot punch: MT5 not connected.")
        return False
    sym = ticket["symbol"]
    resolved = None
    for s in [sym, sym.replace(".pi", ".p"), sym.replace(".p", ".pi"), sym.split(".")[0]]:
        if mt5.symbol_info(s) is not None:
            resolved = s
            break
    if not resolved:
        print(f"  [EXEC] Symbol {sym} not found in MT5 MarketWatch.")
        return False
    # Check duplicate open positions with magic 36089
    positions = mt5.positions_get(symbol=resolved)
    if positions:
        for p in positions:
            if p.magic == 36089:
                print(f"  [EXEC] Position already active for {resolved} (ticket #{p.ticket}). Skipping duplicate.")
                return False
    # Check max concurrent positions cap (max 3)
    all_pos = mt5.positions_get()
    if all_pos:
        alpha_pos = [p for p in all_pos if p.magic == 36089]
        if len(alpha_pos) >= 3:
            print(f"  [EXEC] Max concurrent Alpha360 positions reached ({len(alpha_pos)}/3). Skipping.")
            return False
    tick = mt5.symbol_info_tick(resolved)
    if not tick:
        return False
    is_buy = (ticket["side"] == "BUY")
    price = tick.ask if is_buy else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

    tp_price = 0.0
    if isinstance(ticket.get("tp"), (int, float)):
        tp_price = float(ticket["tp"])
    elif isinstance(ticket.get("tp"), str):
        try:
            tp_price = float(ticket["tp"])
        except ValueError:
            tp_price = 0.0

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": resolved,
        "volume": float(ticket["lots"]),
        "type": order_type,
        "price": float(price),
        "sl": float(ticket["sl"]),
        "tp": tp_price,
        "deviation": 20,
        "magic": 36089,
        "comment": "alpha360_htf",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"  ⚡ [EXEC SUCCESS] MT5 Order #{res.order} placed: {resolved} {ticket['side']} {ticket['lots']} lots @ {price:.5f} | SL: {ticket['sl']:.5f}")
        return True
    else:
        err = res.comment if res else mt5.last_error()
        print(f"  ❌ [EXEC FAILED] Order failed for {resolved}: {err} (retcode: {getattr(res, 'retcode', 'N/A')})")
        return False


def main():
    parser = argparse.ArgumentParser(description="Alpha360 HTF EMA Rejection Signal Watcher")
    parser.add_argument("--risk", type=float, default=float(os.environ.get("ALPHA360_RISK_USD", "5.50")),
                        help="Target risk in USD per trade (default: $5.50)")
    parser.add_argument("--bars", type=int, default=1,
                        help="Number of closed bars to scan back (default: 1 for immediate live)")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuous watcher daemon loop")
    parser.add_argument("--interval", type=int, default=60,
                        help="Loop polling interval in seconds (default: 60)")
    parser.add_argument("--live", action="store_true",
                        help="Arm real MT5 order execution for fresh signals")
    parser.add_argument("--picks", type=str, default=str(BASE_DIR / "results" / "basket_picks.csv"),
                        help="Path to basket picks CSV")
    args = parser.parse_args()

    # Initialize MT5 if available
    if MT5_AVAILABLE:
        if not mt5.initialize():
            print(f"[WARN] MT5 initialize failed: {mt5.last_error()}. Falling back to Parquet data.")

    specs_db = load_universe_specs()
    basket_path = Path(args.picks)
    basket_df = load_basket_picks(basket_path)

    print(f"Loaded {len(basket_df)} basket picks from {basket_path.name}")

    try:
        while True:
            tickets, watchlist = evaluate_basket(basket_df, args.risk, args.bars, specs_db)
            print_dashboard(tickets, watchlist, args.risk, args.bars)

            if args.live and tickets:
                for t in tickets:
                    # Only punch fresh signals from the immediate closed bar (bar_idx == -1 or 0)
                    if t.get("bar_idx", 0) in (-1, 0):
                        punch_order(t, live=True)

            if not args.loop:
                break
            time.sleep(args.interval)
    finally:
        if MT5_AVAILABLE:
            try:
                mt5.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
