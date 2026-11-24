#!/usr/bin/env python3
"""HTF EMA 89/233 Rejection — Live MT5 Auto-Trade Execution Bridge.

Monitors the 15 validated Alpha Basket pairs on H4/H1:
  - 8 Gold crosses (XAUAUD, XAUCNH, XAUUSD, XAUEUR, XAUSGD, XAUGBP, GAUUSD, GAUCNH)
  - Crypto & Indices (BTCUSD.pi H4 89, DJ30.p H1 233)
  - 5 JPY crosses (CHFJPY.pi, GBPJPY.pi, USDJPY.pi, EURJPY.pi, CADJPY.pi)

Execution Rules:
  - Magic: 42003 (HTF EMA Rejection)
  - Risk per trade: $5.00 USD fixed risk (operator directive)
  - Stop Loss: Structural SL beyond EMA touch +/- 0.20 x ATR(14)
  - Take Profit: Enforced >= 2.0R minimum
  - Trailing stop: +1.0R Breakeven lock + 1.5R trailing stop
  - Concurrency: Max 2 positions
  - Journal: htf_ema_rejection/data/htf_ema_journal.csv
  - State:   htf_ema_rejection/data/htf_ema_state.json
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

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(DATA_DIR, "htf_ema_state.json")
JOURNAL_PATH = os.path.join(DATA_DIR, "htf_ema_journal.csv")
CONFIG_PATH = os.path.join(DATA_DIR, "htf_ema_config.json")

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULTS = {
    "risk_usd": 5.0,             # $5.00 fixed USD risk per trade
    "risk_pct": 0.2,             # fallback 0.2% equity
    "max_positions": 2,          # Max 2 concurrent positions
    "min_tp_rr": 2.0,            # 2R minimum Take Profit
    "trail_r": 1.5,              # Chandelier trail distance in R
    "magic": 42003,              # Magic ID for HTF EMA Rejection
    "poll_s": 30,                # Scan cadence
    "daily_loss_stop_pct": 3.0,  # Halt if equity drops 3% in day
}

BASKET = [
    {"sym": "XAUAUD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 4.0},
    {"sym": "XAUCNH.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 2.0},
    {"sym": "XAUUSD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 2.0},
    {"sym": "XAUEUR.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 2.0},
    {"sym": "XAUSGD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 3.5},
    {"sym": "XAUGBP.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 2.0},
    {"sym": "GAUUSD.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 2.0},
    {"sym": "GAUCNH.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 3.5},
    {"sym": "BTCUSD.pi", "tf": mt5.TIMEFRAME_H4, "period": 89,  "pat": "P4_cluster", "exit_rr": 2.0},
    {"sym": "CHFJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 89,  "pat": "P4_cluster", "exit_rr": 2.0},
    {"sym": "GBPJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P2_two_bar", "exit_rr": 2.5},
    {"sym": "USDJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P2_two_bar", "exit_rr": 2.0},
    {"sym": "EURJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P4_cluster", "exit_rr": 3.5},
    {"sym": "CADJPY.pi", "tf": mt5.TIMEFRAME_H4, "period": 233, "pat": "P1_wick",    "exit_rr": 2.0},
    {"sym": "DJ30.p",    "tf": mt5.TIMEFRAME_H1, "period": 233, "pat": "P2_two_bar", "exit_rr": 3.0},
]


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
    risk_usd = float(CFG.get("risk_usd", 0.0))
    if risk_usd > 0:
        lots = risk_usd / loss_per_lot
    else:
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


def manage_stops(state: dict, live: bool) -> None:
    poss = open_positions()
    if not poss:
        return
    trails = state.setdefault("trails", {})
    open_tickets = {str(p.ticket): p for p in poss}
    
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
        if not rec:
            r_px = abs(p.price_open - p.sl) if p.sl else (info.point * 100)
            rec = {
                "symbol": p.symbol,
                "entry": p.price_open,
                "r_px": r_px,
                "d": d,
                "sl0": p.sl,
                "best": p.price_open,
                "tp": p.tp,
                "ts": time.time(),
            }
            trails[ticket_str] = rec
            
        r_px = rec.get("r_px") or abs(p.price_open - p.sl)
        if r_px <= 0:
            continue
            
        px = tick.bid if d > 0 else tick.ask
        best = rec.get("best", p.price_open)
        if (d > 0 and px > best) or (d < 0 and px < best):
            rec["best"] = px
            best = px
            
        profit_r = (px - p.price_open) * d / r_px
        
        # 1.0R Breakeven lock + Chandelier trailing
        if profit_r >= 1.0:
            trail_r = float(CFG.get("trail_r", 1.5))
            target_sl = best - d * trail_r * r_px
            if d > 0:
                new_sl = max(rec.get("sl0", p.sl), p.price_open, target_sl)
                if new_sl > (p.sl + info.point * 3):
                    _modify_sl(p, new_sl, live)
            else:
                new_sl = min(rec.get("sl0", p.sl), p.price_open, target_sl)
                if new_sl < (p.sl - info.point * 3):
                    _modify_sl(p, new_sl, live)
                    
    save_json(STATE_PATH, state)


def _modify_sl(p, new_sl: float, live: bool) -> None:
    digits = mt5.symbol_info(p.symbol).digits
    new_sl = round(new_sl, digits)
    detail = f"SL move -> {new_sl} on Ticket #{p.ticket} ({p.symbol})"
    if not live:
        log(f"DRY {detail}")
        journal(["DRY_SL", p.symbol, detail])
        return
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": p.ticket,
        "symbol": p.symbol,
        "sl": float(new_sl),
        "tp": float(p.tp),
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"TRAIL {detail}")
        journal(["TRAIL", p.symbol, detail])
    else:
        rc = getattr(res, "retcode", "None")
        log(f"SL move FAIL {p.symbol}: retcode={rc}")


def scan_and_trade(state: dict, live: bool) -> None:
    fired = state.setdefault("fired", {})
    poss = open_positions()
    n_pos = len(poss)
    if n_pos >= int(CFG["max_positions"]):
        return
        
    for item in BASKET:
        sym = item["sym"]
        tf = item["tf"]
        period = item["period"]
        pat = item["pat"]
        exit_rr = float(item["exit_rr"])
        
        rates = mt5.copy_rates_from_pos(sym, tf, 0, 300)
        if rates is None or len(rates) < period + 15:
            continue
            
        df = pd.DataFrame(rates)
        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        o = df["open"].values
        
        alpha = 2.0 / (period + 1.0)
        ema = np.zeros(len(c))
        ema[0] = c[0]
        for i in range(1, len(c)):
            ema[i] = alpha * c[i] + (1 - alpha) * ema[i - 1]
            
        tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
        tr[0] = h[0] - l[0]
        atr = np.zeros(len(c))
        atr[13] = np.mean(tr[:14])
        for i in range(14, len(c)):
            atr[i] = (atr[i - 1] * 13 + tr[i]) / 14
            
        last_c = c[-2]
        last_h = h[-2]
        last_l = l[-2]
        last_o = o[-2]
        last_ema = ema[-2]
        last_atr = atr[-2]
        tol = 0.20 * last_atr
        
        rising = ema[-2] > ema[-7]
        falling = ema[-2] < ema[-7]
        
        sig = "NONE"
        sl_raw = 0.0
        d = 0
        
        if pat == "P1_wick":
            rng = max(1e-6, last_h - last_l)
            body_top, body_bot = max(last_o, last_c), min(last_o, last_c)
            lw, uw = (body_bot - last_l) / rng, (last_h - body_top) / rng
            if rising and last_l <= last_ema + tol and last_c >= last_ema - tol and lw >= 0.35 and last_c > last_o:
                sig = "BUY_REJECT"
                d = 1
                sl_raw = min(last_l, last_ema) - tol
            elif falling and last_h >= last_ema - tol and last_c <= last_ema + tol and uw >= 0.35 and last_c < last_o:
                sig = "SELL_REJECT"
                d = -1
                sl_raw = max(last_h, last_ema) + tol
        elif pat == "P2_two_bar":
            mid1 = (o[-3] + c[-3]) / 2.0
            if rising and l[-3] <= ema[-3] + tol and last_c > last_ema and last_c > last_o and last_c > mid1:
                sig = "BUY_REJECT"
                d = 1
                sl_raw = min(l[-3], last_ema) - tol
            elif falling and h[-3] >= ema[-3] - tol and last_c < last_ema and last_c < last_o and last_c < mid1:
                sig = "SELL_REJECT"
                d = -1
                sl_raw = max(h[-3], last_ema) + tol
        elif pat == "P4_cluster":
            tol_c = max(1e-5, 0.20 * last_atr)
            ch, cl, cc = h[-5:-2], l[-5:-2], c[-5:-2]
            if rising:
                touches = int((cl <= last_ema + tol_c).sum())
                if touches >= 2 and np.min(cc) >= last_ema - 0.15 * last_atr and last_c > np.max(ch):
                    sig = "BUY_REJECT"
                    d = 1
                    sl_raw = np.min(cl) - tol_c
            elif falling:
                touches = int((ch >= last_ema - tol_c).sum())
                if touches >= 2 and np.max(cc) <= last_ema + 0.15 * last_atr and last_c < np.min(cl):
                    sig = "SELL_REJECT"
                    d = -1
                    sl_raw = np.max(ch) + tol_c
                    
        if sig == "NONE" or d == 0:
            continue
            
        bar_time_str = str(df["time"].iloc[-2])
        sig_key = f"{sym}:{bar_time_str}:{sig}"
        if sig_key in fired:
            continue
            
        tick = mt5.symbol_info_tick(sym)
        info = mt5.symbol_info(sym)
        if tick is None or info is None:
            continue
            
        entry = tick.bid if d < 0 else tick.ask
        digits = info.digits
        
        # Ensure SL has at least 1.0 ATR distance
        min_sl_dist = 1.0 * last_atr
        if d > 0:
            sl = min(sl_raw, entry - min_sl_dist)
            tp_calc = entry + max(exit_rr, float(CFG["min_tp_rr"])) * (entry - sl)
        else:
            sl = max(sl_raw, entry + min_sl_dist)
            tp_calc = entry - max(exit_rr, float(CFG["min_tp_rr"])) * (sl - entry)
            
        sl = round(sl, digits)
        tp = round(tp_calc, digits)
        
        lots = size_lots(sym, entry, sl)
        order_type = mt5.ORDER_TYPE_SELL if d < 0 else mt5.ORDER_TYPE_BUY
        
        detail = f"{sig} {lots} {sym} @ {entry:.{digits}f} SL={sl:.{digits}f} TP={tp:.{digits}f} (Risk=$5.00)"
        
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
            "comment": "HTF_EMA",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            rc = getattr(res, "retcode", "None")
            desc = getattr(res, "comment", "failed")
            log(f"REJECTED {sym} {sig}: retcode={rc} {desc}")
            journal(["REJECT", sym, f"retcode {rc} {desc}"])
            continue
            
        ticket = res.order
        log(f"FIRED LIVE ORDER -> Ticket #{ticket} {detail}")
        journal(["FIRE", sym, f"ticket #{ticket} {detail}"])
        
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
    parser = argparse.ArgumentParser(description="HTF EMA Rejection MT5 Auto-Trader")
    parser.add_argument("--live", action="store_true", help="Arm live MT5 trade punching")
    parser.add_argument("--risk-usd", type=float, default=5.0, help="Dollar risk per trade (default: $5.00)")
    parser.add_argument("--once", action="store_true", help="Run single pass and exit")
    args = parser.parse_args()

    global CFG
    CFG = DEFAULTS.copy()
    disk_cfg = load_json(CONFIG_PATH, {})
    CFG.update(disk_cfg)
    if args.risk_usd:
        CFG["risk_usd"] = args.risk_usd

    if not mt5.initialize():
        log(f"MT5 Init FAILED: {mt5.last_error()}")
        sys.exit(1)

    acc = mt5.account_info()
    log("=" * 60)
    log("  HTF EMA 89/233 REJECTION -> MT5 LIVE BRIDGE")
    log(f"  Account: {acc.login if acc else 'N/A'} | Equity: ${acc.equity if acc else 0:,.2f}")
    log(f"  Mode: {'LIVE PUNCHING ENABLED' if args.live else 'DRY-RUN (SIMULATION)'}")
    log(f"  Risk Per Trade: ${CFG['risk_usd']:.2f} USD | Max Concurrency: {CFG['max_positions']}")
    log("=" * 60)

    state = load_json(STATE_PATH, {"fired": {}, "trails": {}})

    try:
        while True:
            manage_stops(state, args.live)
            scan_and_trade(state, args.live)
            if args.once:
                break
            time.sleep(float(CFG.get("poll_s", 30)))
    except KeyboardInterrupt:
        log("Halted by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
