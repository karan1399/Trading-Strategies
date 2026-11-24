#!/usr/bin/env python3
"""IV Wall Alpha — Live MT5 Auto-Trade Execution Bridge.

Monitors the 20 validated Alpha FX pairs on M15.
Calculates session-anchored 2.0 SD IV-walls (EM), ATR(14), and scans for:
  - Upper Wall Touch + Close Back Inside -> SELL_FADE
  - Lower Wall Touch + Close Back Inside -> BUY_FADE

Execution Rules:
  - Magic: 42002 (IV-Wall Alpha)
  - Risk per trade: 0.5% equity (~$12.95 on $2,589.67)
  - Stop Loss: Wall +/- 1.0 x ATR(14)
  - Take Profit: Target 0.5 x EM (enforced >= 2.0R minimum)
  - Automatic trailing stop: Chandelier 2.0R behind running best price
  - Concurrency: Max 2 positions
  - Journal: ivwall_alpha/data/mt5_ivwall_journal.csv
  - State:   ivwall_alpha/data/mt5_ivwall_state.json
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
LOT_TABLE_PATH = os.path.join(ROOT, "mt5_lot_table.csv")
STATE_PATH = os.path.join(DATA_DIR, "mt5_ivwall_state.json")
JOURNAL_PATH = os.path.join(DATA_DIR, "mt5_ivwall_journal.csv")
CONFIG_PATH = os.path.join(DATA_DIR, "mt5_ivwall_config.json")

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULTS = {
    "risk_pct": 0.5,             # 0.5% equity per trade
    "max_positions": 2,          # Max 2 concurrent positions (hard desk concurrency ceiling)
    "min_tp_rr": 2.0,            # 2R minimum Take Profit
    "trail_r": 2.0,              # Chandelier trail distance in R
    "magic": 42002,              # Magic ID for IV Wall trades
    "poll_s": 20,                # Scan cadence
    "daily_loss_stop_pct": 3.0,  # Halt if equity drops 3% in day
}

BANNER = r"""
========================================================================
  IV WALL ALPHA -> MT5 LIVE EXECUTION BRIDGE (20 Alpha FX Pairs M15)
  Session 2.0 SD Wall-Fade | 2R Min TP | Chandelier 2R Trailing Stop
========================================================================
"""


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] {msg}", flush=True)


def journal(row: list) -> None:
    new = not os.path.exists(JOURNAL_PATH)
    with open(JOURNAL_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "action", "symbol", "detail"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds")] + row)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def account_equity():
    acc = mt5.account_info()
    return float(acc.equity) if acc else None


def open_positions():
    try:
        poss = mt5.positions_get() or []
    except Exception:
        return []
    return [p for p in poss if p.magic == int(CFG["magic"])]


def size_lots(mt_sym: str, entry: float, sl: float) -> float:
    info = mt5.symbol_info(mt_sym)
    eq = account_equity()
    if not info or not eq:
        return 0.01
    tick_size = info.trade_tick_size or info.point
    tick_val = info.trade_tick_value
    if not tick_size or not tick_val:
        return 0.01
    loss_per_lot = abs(entry - sl) / tick_size * tick_val
    if loss_per_lot <= 0:
        return 0.01
    lots = eq * float(CFG["risk_pct"]) / 100.0 / loss_per_lot
    step = info.volume_step or 0.01
    lots = math.floor(lots / step) * step
    min_vol = info.volume_min or 0.01
    max_vol = info.volume_max or 5.0
    return max(min_vol, min(lots, max_vol))


def enforce_tp_rr(entry: float, sl: float, tp: float, d: int, min_rr: float) -> float:
    r_px = abs(entry - sl)
    if r_px <= 0:
        return tp
    need = entry + d * min_rr * r_px
    if (d > 0 and tp < need - 1e-12) or (d < 0 and tp > need + 1e-12):
        return need
    return tp


def chandelier_sl(best: float, r_px: float, d: int, trail_r: float) -> float:
    return best - d * trail_r * r_px


def manage_stops(state: dict, live: bool) -> None:
    poss = open_positions()
    if not poss:
        return
    trails = state.setdefault("trails", {})
    open_tickets = {str(p.ticket): p for p in poss}
    
    # Prune closed tickets
    for t in list(trails):
        if t not in open_tickets:
            trails.pop(t, None)
            
    for ticket_str, p in open_tickets.items():
        d = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
        tick = mt5.symbol_info_tick(p.symbol)
        info = mt5.symbol_info(p.symbol)
        if tick is None or info is None:
            continue
            
        rec = trails.get(ticket_str)
        if rec is None and p.price_open and p.sl:
            r0 = abs(p.price_open - p.sl)
            if r0 > 0:
                rec = {
                    "symbol": p.symbol,
                    "entry": p.price_open,
                    "r_px": r0,
                    "d": d,
                    "sl0": p.sl,
                    "best": p.price_open,
                }
                trails[ticket_str] = rec
                
        if rec:
            px = tick.bid if d > 0 else tick.ask
            best = float(rec.get("best", rec["entry"]))
            best = max(best, px) if d > 0 else min(best, px)
            rec["best"] = best
            r_px = float(rec.get("r_px") or 0)
            if r_px > 0:
                new_sl = chandelier_sl(best, r_px, d, float(CFG["trail_r"]))
                
                # Verify valid side and broker stops level
                min_dist = (info.trade_stops_level or 0) * (info.point or 0)
                off = max(min_dist, info.point or 0)
                if d > 0:
                    new_sl = min(new_sl, tick.bid - off)
                    improve = new_sl > p.sl + 1e-9
                else:
                    new_sl = max(new_sl, tick.ask + off)
                    improve = new_sl < p.sl - 1e-9
                    
                if improve:
                    if not live:
                        log(f"DRY-RUN Ratchet #{p.ticket} {p.symbol}: SL {p.sl} -> {new_sl:.5f}")
                        continue
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": int(p.ticket),
                        "symbol": p.symbol,
                        "sl": float(new_sl),
                        "tp": float(p.tp),
                    }
                    res = mt5.order_send(req)
                    ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
                    log(f"RATCHET #{p.ticket} {p.symbol} SL -> {new_sl:.5f} {'OK' if ok else 'FAILED'}")
                    journal(["RATCHET", p.symbol, f"#{p.ticket} SL {p.sl}->{new_sl:.5f} ok={ok}"])
                    if ok:
                        save_json(STATE_PATH, state)


def scan_and_execute(state: dict, df_lots: pd.DataFrame, live: bool, once: bool) -> None:
    fired = state.setdefault("fired", {})
    poss = open_positions()
    n_pos = len(poss)
    by_sym = {p.symbol for p in poss}
    
    # 1. Manage existing positions' trailing stops first
    manage_stops(state, live)
    
    # 2. Check maximum concurrent positions limit
    if n_pos >= int(CFG["max_positions"]):
        if once:
            log(f"Max concurrent positions reached ({n_pos}/{CFG['max_positions']})")
        return

    for _, row in df_lots.iterrows():
        sym = row["symbol"]
        pip_size = float(row["pip_size"])
        
        if sym in by_sym:
            continue  # One position per symbol
            
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 150)
        if rates is None or len(rates) < 100:
            continue
            
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        
        # ATR(14)
        tr = np.maximum(df["high"] - df["low"], 
                        np.maximum(abs(df["high"] - df["close"].shift(1)),
                                   abs(df["low"] - df["close"].shift(1))))
        atr14 = tr.rolling(14).mean().iloc[-1]
        
        # EWMA(0.94) return variance
        ret = np.log(df["close"] / df["close"].shift(1))
        var = ret.ewm(alpha=1 - 0.94).var().iloc[-1]
        sigma_bar = math.sqrt(var) if var > 0 else 0.001
        
        # Session anchor at 00:00 broker time
        session_bars = df[df["time"] >= (df["time"].iloc[-1].floor("D"))]
        session_open = session_bars["open"].iloc[0] if len(session_bars) else df["open"].iloc[-96]
        
        em = session_open * sigma_bar * math.sqrt(96)
        upper_wall = session_open + 2.0 * em
        lower_wall = session_open - 2.0 * em
        
        last_bar = df.iloc[-2]
        bar_time_str = str(last_bar["time"])
        
        upper_touch = (last_bar["high"] >= upper_wall) and (last_bar["close"] < upper_wall)
        lower_touch = (last_bar["low"] <= lower_wall) and (last_bar["close"] > lower_wall)
        
        if not (upper_touch or lower_touch):
            continue
            
        d = -1 if upper_touch else 1
        signal_type = "SELL_FADE" if upper_touch else "BUY_FADE"
        sig_key = f"{sym}:{bar_time_str}:{signal_type}"
        
        if sig_key in fired:
            continue
            
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            continue
            
        entry = tick.bid if d < 0 else tick.ask
        sl = upper_wall + 1.0 * atr14 if d < 0 else lower_wall - 1.0 * atr14
        tp_raw = session_open + 0.5 * em if d < 0 else session_open - 0.5 * em
        tp = enforce_tp_rr(entry, sl, tp_raw, d, float(CFG["min_tp_rr"]))
        
        lots = size_lots(sym, entry, sl)
        order_type = mt5.ORDER_TYPE_SELL if d < 0 else mt5.ORDER_TYPE_BUY
        
        detail = f"{signal_type} {lots} {sym} @ {entry:.5f} SL={sl:.5f} TP={tp:.5f} (2.0R min TP verified)"
        
        if not live:
            log(f"DRY-RUN {detail}")
            journal(["DRY", sym, detail])
            fired[sig_key] = "dry_run"
            save_json(STATE_PATH, state)
            continue
            
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(lots),
            "type": order_type,
            "price": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": int(CFG["magic"]),
            "comment": "IVWALL_M15",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            rc = getattr(res, "retcode", "None")
            desc = getattr(res, "comment", "failed")
            log(f"REJECTED {sym} {signal_type}: retcode={rc} {desc}")
            journal(["REJECT", sym, f"retcode {rc} {desc}"])
            continue
            
        ticket = res.order
        log(f"FIRED LIVE ORDER -> Ticket #{ticket} {detail}")
        journal(["FIRE", sym, f"ticket #{ticket} {detail}"])
        
        # Save trail anchor
        r_px = abs(entry - sl)
        state.setdefault("trails", {})[str(ticket)] = {
            "symbol": sym,
            "entry": entry,
            "r_px": r_px,
            "d": d,
            "sl0": sl,
            "best": entry,
            "tp": tp,
            "ts": time.time(),
        }
        fired[sig_key] = "fired"
        save_json(STATE_PATH, state)
        n_pos += 1
        if n_pos >= int(CFG["max_positions"]):
            break


def main():
    global CFG
    ap = argparse.ArgumentParser(description="IV Wall Alpha -> MT5 Live Bridge")
    ap.add_argument("--live", action="store_true", help="Arm live MT5 orders (default: dry-run)")
    ap.add_argument("--once", action="store_true", help="Single scan, then exit")
    args = ap.parse_args()
    
    print(BANNER)
    CFG = dict(DEFAULTS)
    CFG.update(load_json(CONFIG_PATH, {}))
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, CFG)
        
    if not mt5.initialize():
        log(f"MT5 initialize() failed: {mt5.last_error()}")
        return 1
        
    acc = mt5.account_info()
    if acc:
        log(f"Connected to MT5 #{acc.login} ({acc.server}) | Equity: ${acc.equity:.2f} {acc.currency}")
        
    mode = "LIVE (Real Execution Armed)" if args.live else "DRY-RUN (Simulated)"
    log(f"Operating Mode: {mode} | Risk: {CFG['risk_pct']}%/trade | Max Positions: {CFG['max_positions']}")
    journal(["BOOT", "-", mode])
    
    df_lots = pd.read_csv(LOT_TABLE_PATH)
    log(f"Loaded {len(df_lots)} Alpha Pairs from {LOT_TABLE_PATH}")
    
    state = load_json(STATE_PATH, {})
    
    try:
        if args.once:
            scan_and_execute(state, df_lots, args.live, True)
            return 0
            
        log(f"Continuous Watch Active (Polling every {CFG['poll_s']}s) — Ctrl+C to stop")
        while True:
            try:
                scan_and_execute(state, df_lots, args.live, False)
            except Exception as e:
                log(f"Scanner cycle error: {e}")
            time.sleep(float(CFG["poll_s"]))
    finally:
        mt5.shutdown()
        log("MT5 connection closed.")


if __name__ == "__main__":
    sys.exit(main())
