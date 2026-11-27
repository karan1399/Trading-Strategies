#!/usr/bin/env python3
"""
delta_exec.py — Shared Delta Exchange India live execution bridge.
Enforces ILK-01 (interlock assertion before any order batch), contract-aware
integer sizing (never clamps up past risk cap), server-side bracket SL/TP,
and portfolio risk bounding. Reuses scripts/delta_direct.py for signing.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import delta_direct  # noqa: E402

DEFAULT_DELTA_MAP: dict[str, str] = {
    "BTC": "BTCUSD", "BTCUSDT": "BTCUSD", "BTCUSD.pi": "BTCUSD",
    "ETH": "ETHUSD", "ETHUSDT": "ETHUSD", "ETHUSD.pi": "ETHUSD",
    "SOL": "SOLUSD", "SOLUSDT": "SOLUSD", "SOLUSD.p": "SOLUSD",
    "BNB": "BNBUSD", "BNBUSDT": "BNBUSD", "BNBUSD.p": "BNBUSD",
    "XRP": "XRPUSD", "XRPUSDT": "XRPUSD", "XRPUSD.pi": "XRPUSD",
    "ADA": "ADAUSD", "ADAUSDT": "ADAUSD", "ADAUSD.p": "ADAUSD",
    "AVAX": "AVAXUSD", "AVAXUSDT": "AVAXUSD", "AVXUSD.p": "AVAXUSD",
    "DOGE": "DOGEUSD", "DOGEUSDT": "DOGEUSD", "DOGUSD.p": "DOGEUSD",
    "LINK": "LINKUSD", "LINKUSDT": "LINKUSD", "LNKUSD.p": "LINKUSD",
    "LTC": "LTCUSD", "LTCUSDT": "LTCUSD", "LTCUSD.pi": "LTCUSD",
    "DOT": "DOTUSD", "DOTUSDT": "DOTUSD", "DOTUSD.pi": "DOTUSD",
    "NEAR": "NEARUSD", "NEARUSDT": "NEARUSD", "NERUSD.p": "NEARUSD",
    "UNI": "UNIUSD", "UNIUSDT": "UNIUSD", "UNIUSD.p": "UNIUSD",
    "AAVE": "AAVEUSD", "AAVEUSDT": "AAVEUSD", "AVEUSD.p": "AAVEUSD",
    "FIL": "FILUSD", "FILUSDT": "FILUSD", "FILUSD.p": "FILUSD",
    "XLM": "XLMUSD", "XLMUSDT": "XLMUSD", "XLMUSD.p": "XLMUSD",
    "TRX": "TRXUSD", "TRXUSDT": "TRXUSD", "TRXUSD.p": "TRXUSD",
    "XMR": "XMRUSD", "XMRUSDT": "XMRUSD", "XMRUSD.p": "XMRUSD",
    "BCH": "BCHUSD", "BCHUSDT": "BCHUSD", "BCHUSD.p": "BCHUSD",
    "PAXG": "PAXGUSD", "PAXGUSDT": "PAXGUSD", "PAXGUSD": "PAXGUSD",
    "XAU": "XAUTUSD", "XAUUSDT": "XAUTUSD", "XAUUSD.pi": "XAUTUSD",
}


def snap_tick(price: float, tick_size: float) -> str:
    """Snap a price to the product's tick_size and return exact string."""
    if tick_size <= 0:
        return f"{price:.8g}"
    d_px = Decimal(str(price))
    d_tk = Decimal(str(tick_size))
    snapped = (d_px / d_tk).to_integral_value(rounding=ROUND_DOWN) * d_tk
    return format(snapped.normalize(), "f") if "." in str(tick_size) else str(int(snapped))


def delta_contracts_for_risk(
    entry: float,
    stop: float,
    risk_target_usd: float,
    risk_max_usd: float,
    contract_value: float,
) -> tuple[int, float, str]:
    """Integer contract sizing: floors down and NEVER sizes up past risk_max_usd."""
    dist = abs(float(entry) - float(stop))
    if dist <= 0 or contract_value <= 0 or risk_target_usd <= 0:
        return 0, 0.0, "invalid sizing input"
    risk_per_contract = dist * float(contract_value)
    if risk_per_contract <= 0:
        return 0, 0.0, "non-positive risk per contract"
    raw = int(math.floor(risk_target_usd / risk_per_contract + 1e-9))
    if raw < 1:
        if risk_per_contract <= risk_max_usd + 1e-9:
            return 1, round(risk_per_contract, 4), ""
        return 0, round(risk_per_contract, 4), (
            f"1-contract risk ${risk_per_contract:.4f} > cap ${risk_max_usd:.4f}"
        )
    actual = round(raw * risk_per_contract, 4)
    if actual > risk_max_usd + 1e-9:
        raw = max(0, int(math.floor(risk_max_usd / risk_per_contract + 1e-9)))
        actual = round(raw * risk_per_contract, 4)
        if raw < 1:
            return 0, round(risk_per_contract, 4), f"capped risk ${risk_per_contract:.4f} > ${risk_max_usd:.4f}"
    return raw, actual, ""


class DeltaLive:
    """Delta Exchange India live router with ILK-01 interlock and bracket enforcement."""

    def __init__(self, symbol_map: dict[str, str] | None = None, events_dir: str | Path | None = None):
        self.symbol_map = {**DEFAULT_DELTA_MAP, **(symbol_map or {})}
        self.events_dir = Path(events_dir) if events_dir else (ROOT / "desk" / "events")
        self._products_cache: dict[str, dict] = {}
        self._products_ts: float = 0.0
        self._last_ilk_log_ts: float = 0.0

    def map_symbol(self, sym: str) -> str | None:
        key = str(sym or "").strip()
        if not key:
            return None
        direct = self.symbol_map.get(key) or self.symbol_map.get(key.upper())
        if direct:
            return direct
        up = key.upper()
        if up.endswith("USDT"):
            candidate = up[:-4] + "USD"
            if self.product_spec(candidate):
                self.symbol_map[key] = candidate
                return candidate
        return None

    def _refresh_products(self, force: bool = False) -> dict[str, dict]:
        if not force and self._products_cache and (time.time() - self._products_ts) < 300:
            return self._products_cache
        url = f"{delta_direct.BASE_URL}/v2/products"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            prods = json.load(r).get("result") or []
        out = {}
        for p in prods:
            if p.get("contract_type") in ("perpetual_futures", "futures") and p.get("state") == "live":
                out[p["symbol"]] = p
        self._products_cache = out
        self._products_ts = time.time()
        return out

    def product_spec(self, delta_sym: str) -> dict | None:
        try:
            prods = self._refresh_products()
            return prods.get(delta_sym)
        except Exception:
            return None

    def ticker(self, delta_sym: str) -> dict | None:
        try:
            url = f"{delta_direct.BASE_URL}/v2/tickers/{delta_sym}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                return json.load(r).get("result")
        except Exception:
            return None

    def _record_interlock_halt(self, err_msg: str, batch_ids: list[str] | None = None) -> None:
        now = time.time()
        if now - self._last_ilk_log_ts < 900:
            return
        self._last_ilk_log_ts = now
        try:
            self.events_dir.mkdir(parents=True, exist_ok=True)
            ts_tag = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now))
            payload = {
                "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "host": os.environ.get("COMPUTERNAME", "windows-host"),
                "venue": "delta",
                "probe": "GET /v2/wallet/balances",
                "value": False,
                "error": str(err_msg)[:300],
                "batch_ids": batch_ids or [],
            }
            (self.events_dir / f"interlock_{ts_tag}.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def interlock_check(self, batch_ids: list[str] | None = None) -> tuple[bool, float, float, str]:
        """Assert ILK-01: credentials valid + /v2/wallet/balances succeeds."""
        if not delta_direct.API_KEY or not delta_direct.API_SECRET:
            msg = "missing DELTA_API_KEY/DELTA_API_SECRET"
            self._record_interlock_halt(msg, batch_ids)
            return False, 0.0, 0.0, msg
        wb = delta_direct.request_delta("GET", "/v2/wallet/balances")
        if not wb.get("success"):
            msg = str(wb.get("error") or wb)[:240]
            self._record_interlock_halt(msg, batch_ids)
            return False, 0.0, 0.0, msg
        bal = 0.0
        avail = 0.0
        for row in wb.get("result") or []:
            if row.get("asset_symbol") in ("USD", "USDT", "USDC"):
                bal += float(row.get("balance") or 0)
                avail += float(row.get("available_balance") or 0)
        return True, bal, avail, ""

    def book_counts_and_risk(self) -> tuple[int, float]:
        """Return (filled_positions_count, total_bracket_risk_usd); limit orders do not consume slots."""
        prods = self._products_cache or {}
        pos_resp = delta_direct.request_delta("GET", "/v2/positions/margined")
        ord_resp = delta_direct.request_delta("GET", "/v2/orders", query="?state=open")
        positions = (pos_resp.get("result") or []) if pos_resp.get("success") else []
        orders = (ord_resp.get("result") or []) if ord_resp.get("success") else []
        active_pos = [p for p in positions if abs(float(p.get("size") or 0)) > 0]
        entry_orders = [o for o in orders if not o.get("reduce_only")]
        risk = 0.0
        for o in entry_orders:
            sym = o.get("product_symbol") or ""
            cv = float((prods.get(sym) or {}).get("contract_value") or 0)
            ep = float(o.get("limit_price") or 0)
            sl = float(o.get("bracket_stop_loss_price") or 0)
            sz = abs(float(o.get("size") or 0))
            if cv > 0 and ep > 0 and sl > 0:
                risk += abs(ep - sl) * cv * sz
        for p in active_pos:
            sym = p.get("product_symbol") or ""
            cv = float((prods.get(sym) or {}).get("contract_value") or 0)
            ep = float(p.get("entry_price") or 0)
            sz = abs(float(p.get("size") or 0))
            # estimate open position risk from matching stop order if present
            sl_px = 0.0
            for o in orders:
                if o.get("product_symbol") == sym and o.get("reduce_only") and o.get("stop_price"):
                    sl_px = float(o.get("stop_price") or 0)
                    break
            if cv > 0 and ep > 0 and sl_px > 0:
                risk += abs(ep - sl_px) * cv * sz
        return len(active_pos), round(risk, 4)

    def send_bracket(
        self,
        source_symbol: str,
        side: str,
        entry: float,
        stop: float,
        tp: float,
        risk_pct: float = 1.0,
        risk_max_pct: float = 1.0,
        max_portfolio_risk_pct: float = 2.0,
        max_concurrent: int = 0,
        client_order_id: str = "",
        order_type: str = "limit",
    ) -> dict:
        """Validate ILK-01, size in integer contracts, and submit bracketed Delta order."""
        dsym = self.map_symbol(source_symbol)
        if not dsym:
            return {"status": "SKIP", "why": f"no Delta mapping for {source_symbol}"}

        ok, equity, avail, err = self.interlock_check([client_order_id] if client_order_id else None)
        if not ok:
            return {"status": "HALT_ILK01", "why": err, "symbol": dsym}
        if equity <= 0:
            return {"status": "SKIP", "why": f"Delta equity=${equity:.4f} <= 0", "symbol": dsym}

        spec = self.product_spec(dsym)
        if not spec:
            return {"status": "SKIP", "why": f"Delta product spec not found for {dsym}"}

        cv = float(spec.get("contract_value") or 0)
        tk_sz = float(spec.get("tick_size") or 0.01)
        pid = int(spec["id"])

        cnt, book_risk = self.book_counts_and_risk()
        if int(max_concurrent or 0) > 0 and cnt >= int(max_concurrent):
            return {"status": "SKIP", "why": f"Delta max_concurrent {cnt}>={max_concurrent}", "symbol": dsym}

        risk_target = equity * risk_pct / 100.0
        risk_max = equity * risk_max_pct / 100.0
        size, est_risk, reason = delta_contracts_for_risk(entry, stop, risk_target, risk_max, cv)
        if size < 1:
            return {"status": "SKIP", "why": reason or "size < 1", "symbol": dsym}

        max_book = equity * max_portfolio_risk_pct / 100.0
        if book_risk + est_risk > max_book + 1e-9:
            return {
                "status": "SKIP",
                "why": f"Delta portfolio cap ${book_risk:.4f}+${est_risk:.4f} > ${max_book:.4f}",
                "symbol": dsym,
            }

        is_buy = side.lower() in ("long", "buy", "1")
        d_side = "buy" if is_buy else "sell"
        otype = "market_order" if order_type == "market" else "limit_order"
        body_dict: dict = {
            "product_id": pid,
            "product_symbol": dsym,
            "size": size,
            "side": d_side,
            "order_type": otype,
            "bracket_stop_loss_price": snap_tick(stop, tk_sz),
            "bracket_stop_loss_limit_price": snap_tick(stop, tk_sz),
            "bracket_take_profit_price": snap_tick(tp, tk_sz),
            "bracket_take_profit_limit_price": snap_tick(tp, tk_sz),
            "bracket_stop_trigger_method": "mark_price",
        }
        if otype == "limit_order":
            body_dict["limit_price"] = snap_tick(entry, tk_sz)
            body_dict["time_in_force"] = "gtc"
        if client_order_id:
            body_dict["client_order_id"] = str(client_order_id)[:32]

        resp = delta_direct.request_delta("POST", "/v2/orders", body=json.dumps(body_dict))
        if resp.get("success"):
            res = resp.get("result") or {}
            return {
                "status": "OK",
                "symbol": dsym,
                "order_id": res.get("id"),
                "size": size,
                "est_risk_usd": est_risk,
                "side": d_side,
                "entry": body_dict.get("limit_price", "market"),
                "sl": body_dict["bracket_stop_loss_price"],
                "tp": body_dict["bracket_take_profit_price"],
            }
        return {"status": "REJECT", "symbol": dsym, "why": str(resp.get("error") or resp)[:240]}
