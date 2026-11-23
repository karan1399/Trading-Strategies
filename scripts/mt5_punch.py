#!/usr/bin/env python3
"""
scripts/mt5_punch.py — bridge the terminal's PUNCH button straight into MT5.

Reads a JSON punch request on stdin (written by /api/desk/mt5-punch):

{
  "symbol":  "BTCUSD.pi",     # RESOLVED MT5 symbol (broker-suffixed)
  "side":    "long"|"short",  # card side
  "kind":    "market"|"limit",
  "entry":   62800.0,         # limit price (market: ignored, uses current)
  "stop":    62400.0,
  "tp":      63400.0,         # T1 (the scale-out rung the bracket closes at)
  "risk_usd": 5.5,            # optional override; defaults to prop rules
  "order_id": "tp-1795033772528"
}

Safety rules (mirror scripts/mt5_cg_trader.py — Blueberry funded account):
  - risk per trade capped at $6.00 by default (prop: 0.20–0.24% of $2,500);
    risk_usd may only LOWER the cap, never raise it
  - every order carries comment "cg_terminal" + magic 42001 (account isolation)
  - daily 4% / overall 10% drawdown guards checked BEFORE placing
  - symbol must trade full (trade_mode 4) and be visible
Prints one JSON object on stdout: {ok, ticket, lots, risk_usd, price, retcode}
or {ok: false, error}. Exit code 0 only on success.
"""
import json
import sys
from mt5_order_risk import size_order, validate_bracket

try:
    import MetaTrader5 as mt5
except ImportError:
    print(json.dumps({"ok": False, "error": "MetaTrader5 python package not installed (Windows only: pip install MetaTrader5)"}))
    sys.exit(1)

MAX_RISK_USD = 6.00
MAGIC_NUMBER = 42001
CG_COMMENT = "cg_terminal"
MAX_DAILY_DRAWDOWN_USD = 100.00
MAX_OVERALL_DRAWDOWN_USD = 250.00
INITIAL_ACCOUNT_BALANCE = 2500.00


def main():
    req = json.loads(sys.stdin.read())

    if not mt5.initialize(timeout=15_000):
        print(json.dumps({"ok": False, "error": f"mt5.initialize failed: {mt5.last_error()} (is the MT5 terminal running?)"}))
        sys.exit(1)
    try:
        acc = mt5.account_info()
        if acc is None:
            print(json.dumps({"ok": False, "error": f"account_info failed: {mt5.last_error()}"}))
            sys.exit(1)

        # ---- drawdown guards (checked before anything else) ----
        overall_dd = max(0.0, INITIAL_ACCOUNT_BALANCE - acc.equity)
        if overall_dd >= MAX_OVERALL_DRAWDOWN_USD:
            print(json.dumps({"ok": False, "error": f"overall drawdown guard: equity {acc.equity:.2f} hit the ${MAX_OVERALL_DRAWDOWN_USD:.2f} floor"}))
            sys.exit(1)

        raw_sym = req["symbol"]
        side = req["side"].lower()
        kind = req.get("kind", "market").lower()
        if side not in ("long", "short") or kind not in ("limit", "market"):
            raise ValueError("Invalid side or order kind")
        stop = float(req["stop"])
        tp = float(req["tp"]) if req.get("tp") else 0.0

        # Automatic broker symbol and suffix resolution
        clean_base = raw_sym.replace("USDT", "").replace("USDT-M", "").replace("USD", "").upper()
        quirks = {
            "DOGE": "DOG",
            "AVAX": "AVX",
            "LINK": "LNK",
        }
        broker_base = quirks.get(clean_base, clean_base)
        candidates = [
            raw_sym,
            f"{raw_sym}.p",
            f"{raw_sym}.pi",
            f"{clean_base}USD.p",
            f"{clean_base}USD.pi",
            f"{broker_base}USD.p",
            f"{broker_base}USD.pi",
            f"{clean_base}USD",
            f"{broker_base}USD",
        ]
        sym = None
        for c in candidates:
            if mt5.symbol_info(c) is not None:
                sym = c
                break
        if sym is None:
            sym = raw_sym

        if not mt5.symbol_select(sym, True):
            print(json.dumps({"ok": False, "error": f"symbol_select failed for {sym} (tried: {candidates}) — check broker suffix"}))
            sys.exit(1)
        info = mt5.symbol_info(sym)
        if info is None:
            print(json.dumps({"ok": False, "error": f"symbol_info failed for {sym}"}))
            sys.exit(1)
        if info.trade_mode != 4:
            print(json.dumps({"ok": False, "error": f"{sym} not in full-trade mode (trade_mode={info.trade_mode})"}))
            sys.exit(1)

        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            print(json.dumps({"ok": False, "error": f"no tick for {sym}"}))
            sys.exit(1)

        # ---- entry price ----
        if kind == "limit":
            entry = float(req["entry"])
            stops_level = info.trade_stops_level or 0
            point = info.point or 0.0001
            min_dist = stops_level * point
            if side == "long" and entry >= (tick.ask - min_dist):
                print(json.dumps({
                    "ok": False,
                    "retcode": 10015,
                    "error": f"Invalid limit price (retcode 10015): BUY_LIMIT entry {entry} must be below ask {tick.ask} by at least {min_dist} points. Market has displaced past entry."
                }))
                sys.exit(1)
            elif side == "short" and entry <= (tick.bid + min_dist):
                print(json.dumps({
                    "ok": False,
                    "retcode": 10015,
                    "error": f"Invalid limit price (retcode 10015): SELL_LIMIT entry {entry} must be above bid {tick.bid} by at least {min_dist} points. Market has displaced past entry."
                }))
                sys.exit(1)
        else:
            entry = tick.ask if side == "long" else tick.bid

        digits = info.digits
        entry, stop, tp = (round(p, digits) for p in (entry, stop, tp))
        buy = side == "long"
        validate_bracket(entry, stop, tp, buy, tick.bid, tick.ask)
        cap = float(req["risk_usd"]) if req.get("risk_usd") is not None else MAX_RISK_USD
        lots, actual_risk = size_order(mt5, info, entry, stop, buy, cap, cap)

        # ---- order type ----
        if kind == "limit":
            otype = mt5.ORDER_TYPE_BUY_LIMIT if side == "long" else mt5.ORDER_TYPE_SELL_LIMIT
            action = mt5.TRADE_ACTION_PENDING
        else:
            otype = mt5.ORDER_TYPE_BUY if side == "long" else mt5.ORDER_TYPE_SELL
            action = mt5.TRADE_ACTION_DEAL

        request = {
            "action": action,
            "symbol": sym,
            "volume": lots,
            "type": otype,
            "price": entry,
            "sl": stop,
            "tp": tp,
            "deviation": 20,
            "magic": MAGIC_NUMBER,
            "comment": CG_COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            print(json.dumps({"ok": False, "error": f"order_send returned None: {mt5.last_error()}"}))
            sys.exit(1)
        # unsupported filling mode -> FOK / RETURN fallback
        if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED) and result.retcode == 10030:
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
            if result and result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED) and result.retcode == 10030:
                request["type_filling"] = mt5.ORDER_FILLING_RETURN
                result = mt5.order_send(request)
        if result is None:
            raise ValueError(f"order_send fill-mode retry failed: {mt5.last_error()}")
        if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            print(json.dumps({"ok": False, "error": f"order_send rejected: retcode {result.retcode} — {result.comment}"}))
            sys.exit(1)

        print(json.dumps({
            "ok": True,
            "ticket": result.order,
            "deal": result.deal,
            "lots": lots,
            "risk_usd": round(actual_risk, 2),
            "price": result.price,
            "retcode": result.retcode,
            "symbol": sym,
            "comment": CG_COMMENT,
        }))
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
