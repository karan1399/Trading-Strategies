"""Delta Exchange India Live Pure-PA Executor (`Roll-TierA15 Safe 1.0%`) + IP-Block WhatsApp/Webhook Alerts.

Safety & Research Controls:
  - Defaults to DRY-RUN mode (`--dry-run` implicit unless `--live` is explicitly passed).
  - Pure OHLC Price Action (imports exact PA01..PA10 builders from scripts/delta_pa_trend_optimizer.py).
  - Evaluates ONLY closed 1H / 4H candles (`candle_ts + tf_sec <= now_utc`) -> zero repainting.
  - Floors quantity within the equity and hard USD risk and margin limits,
    max 1 open position/order per symbol, max 6 concurrent positions portfolio-wide, and <= 20x / 80% margin cap.
  - Monitors Delta API IP whitelist continuously (`ip_not_whitelisted_for_api_key`) and pushes instant
    alerts to CallMeBot WhatsApp, custom JSON Webhook (Make/n8n/Twilio/Discord/Slack), Telegram,
    local `live_alerts.log`, and Windows desktop toast/beep.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import email.utils
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts.delta_pa_trend_optimizer as opt  # noqa: E402

BASE_URL = "https://api.india.delta.exchange"
REPORT_PATH = DATA_DIR / "delta_pa_optimization_report.json"
CATALOG_PATH = DATA_DIR / "delta_products_catalog.json"
STATE_PATH = DATA_DIR / "live_state.json"
ALERT_LOG_PATH = DATA_DIR / "live_alerts.log"
ALERT_STATUS_PATH = DATA_DIR / "live_alert_status.json"

TF_SEC = {"1H": 3600, "4H": 14400}
TF_RES = {"1H": "1h", "4H": "4h"}
INR_PER_USD = 85.0


# ---------------------------------------------------------------------------
# Environment & Alert Dispatcher (WhatsApp / Webhook / Telegram / Local Toast)
# ---------------------------------------------------------------------------
def load_env_config() -> dict[str, str]:
    """Load credentials and webhook settings from process env and workspace .env."""
    keys = (
        "DELTA_API_KEY",
        "DELTA_API_SECRET",
        "CALLMEBOT_PHONE",
        "CALLMEBOT_APIKEY",
        "ALERT_WEBHOOK_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    )
    cfg: dict[str, str] = {k: os.getenv(k, "").strip() for k in keys if os.getenv(k)}
    for candidate in (DATA_DIR / ".env", ROOT_DIR / ".env", ROOT_DIR.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in keys and not cfg.get(k):
                cfg[k] = v.strip().strip('"').strip("'")
    return cfg


def _windows_toast(title: str, message: str) -> None:
    if sys.platform != "win32":
        return
    try:
        safe_t = title.replace("'", "''")[:80]
        safe_m = message.replace("'", "''").replace("\n", " | ")[:220]
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
            f"$n.BalloonTipTitle = '{safe_t}';"
            f"$n.BalloonTipText = '{safe_m}';"
            "$n.Visible = $true; $n.ShowBalloonTip(7000); Start-Sleep -Milliseconds 300; $n.Dispose()"
        )
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("\a", end="", flush=True)
    except Exception:
        pass


def send_alert(
    event: str,
    title: str,
    message: str,
    cfg: dict[str, str],
    severity: str = "INFO",
    extra: dict[str, Any] | None = None,
    toast: bool = True,
) -> dict[str, str]:
    """Dispatch notification across CallMeBot WhatsApp, generic Webhook, Telegram & local log."""
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    full_text = f"[{severity}] {title}\n{message}\n({ts_iso})"
    results: dict[str, str] = {}

    ALERT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts_iso} | {severity} | {event} | {title} | {message.replace(chr(10), ' ')}\n")

    ALERT_STATUS_PATH.write_text(
        json.dumps({"timestamp_utc": ts_iso, "severity": severity, "event": event, "title": title, "message": message, **(extra or {})}, indent=2),
        encoding="utf-8",
    )

    if toast:
        _windows_toast(title, message)

    # 1) CallMeBot WhatsApp API
    phone, ckey = cfg.get("CALLMEBOT_PHONE", ""), cfg.get("CALLMEBOT_APIKEY", "")
    if phone and ckey:
        try:
            url = (
                "https://api.callmebot.com/whatsapp.php"
                f"?phone={urllib.parse.quote(phone)}&text={urllib.parse.quote(full_text)}&apikey={urllib.parse.quote(ckey)}"
            )
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "DeltaPA/1.0"}), timeout=10) as resp:
                results["whatsapp_callmebot"] = f"HTTP {resp.status}"
        except Exception as exc:
            results["whatsapp_callmebot"] = f"ERR: {exc}"

    # 2) Generic Webhook POST (Make.com / n8n / Zapier WhatsApp Cloud API / Twilio / Discord / Slack)
    wh = cfg.get("ALERT_WEBHOOK_URL", "")
    if wh:
        try:
            payload = {
                "timestamp_utc": ts_iso,
                "severity": severity,
                "event": event,
                "title": title,
                "message": message,
                "text": full_text,   # Discord/Slack fallback compatible
                "content": full_text,
                **(extra or {}),
            }
            req = urllib.request.Request(
                wh,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "DeltaPA/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                results["webhook"] = f"HTTP {resp.status}"
        except Exception as exc:
            results["webhook"] = f"ERR: {exc}"

    # 3) Optional Telegram Bot
    t_tok, t_cid = cfg.get("TELEGRAM_BOT_TOKEN", ""), cfg.get("TELEGRAM_CHAT_ID", "")
    if t_tok and t_cid:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{t_tok}/sendMessage",
                data=json.dumps({"chat_id": t_cid, "text": full_text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                results["telegram"] = f"HTTP {resp.status}"
        except Exception as exc:
            results["telegram"] = f"ERR: {exc}"

    return results


# ---------------------------------------------------------------------------
# Delta Exchange India Authenticated REST v2 Client + IP Block Parser
# ---------------------------------------------------------------------------
def parse_ip_block(resp: dict[str, Any]) -> tuple[bool, str]:
    """Detect Delta API IP whitelist rejection and extract the client IP if present."""
    if resp.get("success"):
        return False, ""
    raw_err = str(resp.get("error", ""))
    if "ip_not_whitelisted" in raw_err:
        try:
            parsed = json.loads(raw_err)
            ip = str(parsed.get("error", {}).get("context", {}).get("client_ip") or "")
            return True, ip
        except Exception:
            return True, ""
    return False, ""


def detect_public_ip() -> str:
    try:
        with urllib.request.urlopen(urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "DeltaPA/1.0"}), timeout=5) as r:
            return r.read().decode("utf-8").strip()
    except Exception:
        return "unknown"


class DeltaClient:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.time_offset = 0.0
        self.sync_time()

    def sync_time(self) -> float:
        try:
            req = urllib.request.Request(f"{BASE_URL}/v2/products", headers={"User-Agent": "DeltaPA/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                dh = resp.headers.get("Date")
                if dh:
                    self.time_offset = email.utils.parsedate_to_datetime(dh).timestamp() - time.time()
        except Exception:
            pass
        return self.time_offset

    def _sign(self, method: str, ts: str, path: str, query: str, body: str) -> str:
        payload = f"{method}{ts}{path}{query}{body}"
        return hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def request(self, method: str, path: str, query: str = "", body_obj: dict[str, Any] | None = None, retry: int = 1) -> dict[str, Any]:
        body = json.dumps(body_obj, separators=(",", ":")) if body_obj is not None else ""
        ts = str(int(time.time() + self.time_offset))
        headers = {
            "api-key": self.api_key,
            "signature": self._sign(method, ts, path, query, body),
            "timestamp": ts,
            "Content-Type": "application/json",
            "User-Agent": "DeltaPA/1.0",
        }
        req = urllib.request.Request(f"{BASE_URL}{path}{query}", data=(body.encode("utf-8") if body else None), headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                dh = resp.headers.get("Date")
                if dh:
                    self.time_offset = email.utils.parsedate_to_datetime(dh).timestamp() - time.time()
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="ignore")
            try:
                ej = json.loads(err_body)
                if ej.get("error", {}).get("code") == "expired_signature" and retry > 0:
                    st = ej.get("error", {}).get("context", {}).get("server_time")
                    if st:
                        self.time_offset = float(st) - time.time()
                    else:
                        self.sync_time()
                    return self.request(method, path, query, body_obj, retry=retry - 1)
            except Exception:
                pass
            return {"success": False, "status": he.code, "error": err_body}
        except Exception as exc:
            return {"success": False, "status": 0, "error": str(exc)}

    def get_public(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": "DeltaPA/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Pure Price-Action Universe & Risk Sizing Helpers
# ---------------------------------------------------------------------------
def round_to_tick(px: float, tick: float) -> str:
    if tick <= 0:
        return f"{px:.6f}".rstrip("0").rstrip(".")
    steps = round(px / tick)
    val = steps * tick
    dec = max(0, -int(math.floor(math.log10(tick))) if tick < 1 else 0)
    return f"{val:.{dec}f}"


def compute_safe_contracts(
    equity_usd: float,
    free_margin_usd: float,
    entry_px: float,
    sl_dist: float,
    contract_value: float,
    initial_margin_pct: float,
    risk_pct: float = 1.0,
    max_risk_usd: float = 0.50,
    max_margin_usd: float = 1.50,
    stress_bps: float = 25.0,
) -> tuple[int, str]:
    """Floor contracts to the binding equity, dollar and initial-margin budgets."""
    required = (equity_usd, entry_px, sl_dist, contract_value, risk_pct, max_risk_usd, max_margin_usd)
    if not all(math.isfinite(v) and v > 0 for v in required) or not math.isfinite(stress_bps) or stress_bps < 0:
        return 0, "invalid_inputs"
    if not all(math.isfinite(v) and v >= 0 for v in (free_margin_usd, initial_margin_pct)):
        return 0, "invalid_margin_inputs"
    notional_1ct = contract_value * entry_px
    loss_1ct = contract_value * sl_dist + notional_1ct * stress_bps / 10_000.0
    eff_im_pct = max(float(initial_margin_pct or 5.0), 5.0)
    mgn_1ct = notional_1ct * (eff_im_pct / 100.0)

    target_risk = min(equity_usd * min(risk_pct, 1.0) / 100.0, max_risk_usd)
    cts = int(math.floor(target_risk / loss_1ct))
    if cts == 0:
        return 0, f"minimum contract stressed risk ${loss_1ct:.4f} exceeds budget ${target_risk:.4f}"
    max_mgn_cts = int(math.floor(min(max(0.0, free_margin_usd), max_margin_usd) / max(1e-9, mgn_1ct)))
    cts = min(cts, max_mgn_cts)
    if cts <= 0:
        return 0, f"insufficient_free_margin(need=${mgn_1ct:.2f},free=${free_margin_usd:.2f})"
    return cts, "ok"


def load_tier_a15_recipes() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rep = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    cat = {c["symbol"]: c for c in json.loads(CATALOG_PATH.read_text(encoding="utf-8"))}
    tier_a = [x for x in rep["assets"] if x.get("verdict") == "CHAMPION_TREND"
              and x.get("research_status") != "SHADOW_TRIAL" and float(x.get("turnover_usd") or 0) >= 375_000]
    return tier_a, cat


def fetch_closed_candles(client: DeltaClient, symbol: str, tf_label: str, bars_needed: int = 300) -> pd.DataFrame:
    res = TF_RES[tf_label]
    step = TF_SEC[tf_label]
    now = int(time.time())
    start = now - (bars_needed + 10) * step
    q = f"/v2/history/candles?symbol={urllib.parse.quote(symbol)}&resolution={res}&start={start}&end={now}"
    raw = client.get_public(q).get("result", [])
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df["timestamp"] = df["time"].astype(np.int64)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    # Keep ONLY fully closed candles!
    df = df[df["timestamp"] + step <= now].reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# State Persistence & Watchdog Logic
# ---------------------------------------------------------------------------
def load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_bars": {}, "managed_trades": {}, "last_ip_alert_ts": 0, "last_blocked_ip": ""}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def check_wallet_and_ip(client: DeltaClient, cfg: dict[str, str], state: dict[str, Any]) -> tuple[bool, float, float]:
    """Read live Delta wallet; if IP is blocked, alert once per cooldown/IP change."""
    wb = client.request("GET", "/v2/wallet/balances")
    blocked, ctx_ip = parse_ip_block(wb)
    if blocked:
        ip = ctx_ip or detect_public_ip()
        now = int(time.time())
        cooldown = 900  # 15m repeat cooldown, or immediate if IP changed
        if ip != state.get("last_blocked_ip") or (now - int(state.get("last_ip_alert_ts", 0))) >= cooldown:
            send_alert(
                event="IP_BLOCKED",
                severity="CRITICAL",
                title="Delta API IP Whitelist Blocked",
                message=f"Delta India rejected API key (401 ip_not_whitelisted).\nWhitelist current IP: {ip}",
                cfg=cfg,
                extra={"client_ip": ip},
            )
            state["last_ip_alert_ts"] = now
            state["last_blocked_ip"] = ip
            save_state(state)
        return False, 0.0, 0.0

    if not wb.get("success"):
        print(f"[WARN] Wallet read failed: {wb}", flush=True)
        return False, 0.0, 0.0

    # Reset IP block flag once recovered
    if state.get("last_blocked_ip"):
        send_alert(
            event="IP_RESTORED",
            severity="INFO",
            title="Delta API Connection Restored",
            message=f"Whitelisted connection active again (previous blocked IP was {state['last_blocked_ip']}).",
            cfg=cfg,
            toast=False,
        )
        state["last_blocked_ip"] = ""
        state["last_ip_alert_ts"] = 0
        save_state(state)

    total_eq_usd = 0.0
    free_mgn_usd = 0.0
    for b in wb.get("result", []):
        sym = str(b.get("asset_symbol", "")).upper()
        bal = float(b.get("balance") or 0.0)
        avail = float(b.get("available_balance") or 0.0)
        if sym == "USD":
            total_eq_usd += bal
            free_mgn_usd += avail
        elif sym == "INR":
            total_eq_usd += bal / INR_PER_USD
            free_mgn_usd += avail / INR_PER_USD

    # Keep 20% equity cushion unencumbered (80% max margin lock)
    capped_free = min(free_mgn_usd, max(0.0, total_eq_usd * 0.80 - (total_eq_usd - free_mgn_usd)))
    return True, total_eq_usd, capped_free


def manage_trailing_stops(
    client: DeltaClient,
    live_pos_map: dict[str, dict[str, Any]],
    open_orders: list[dict[str, Any]],
    prod_map: dict[str, dict[str, Any]],
    state: dict[str, Any],
    cfg: dict[str, str],
    live_mode: bool,
) -> None:
    """Update Break-Even & ATR Chandelier Trailing Stops on bot-managed open positions."""
    managed = state.get("managed_trades", {})
    stale_syms = [s for s in managed if s not in live_pos_map]
    for s in stale_syms:
        managed.pop(s, None)

    for sym, m in list(managed.items()):
        pos = live_pos_map[sym]
        tf_m = str(m.get("tf") or "4H")
        mh_bars = 96 if tf_m == "4H" else 144
        cur_sz = float(pos.get("size") or 0)
        prod_id = int(pos.get("product_id") or prod_map.get(sym, {}).get("id") or 0)
        tick = float(prod_map.get(sym, {}).get("tick_size") or 0.01)
        if m.get("opened_ts") and (time.time() - float(m["opened_ts"])) >= mh_bars * TF_SEC.get(tf_m, 14400):
            if cur_sz != 0 and live_mode:
                ex_side = "sell" if cur_sz > 0 else "buy"
                ex_res = client.request("POST", "/v2/orders", body_obj={
                    "product_id": prod_id,
                    "product_symbol": sym, "size": int(abs(cur_sz)), "side": ex_side,
                    "order_type": "market_order", "time_in_force": "ioc", "reduce_only": True,
                })
                if ex_res.get("success"):
                    managed.pop(sym, None)
                    send_alert("TIME_STOP", f"{sym} MaxHold Exit", f"Closed {sym} after {mh_bars} {tf_m} bars.", cfg, toast=False)
                continue
        mark = float(pos.get("mark_price") or m["entry_px"])
        sd = int(m["side"])
        rd = float(m["rd"])
        atr = float(m["atr"])
        ent = float(m["entry_px"])
        m["best_px"] = max(float(m["best_px"]), mark) if sd == 1 else min(float(m["best_px"]), mark)
        best = float(m["best_px"])
        new_sl = float(m["current_sl"])

        # Auto-repair missing >=2.0R reduce-only TP limit order on Delta
        tp_ord = next(
            (o for o in open_orders if o.get("product_symbol") == sym and o.get("reduce_only") and "stop" not in str(o.get("order_type", "") or o.get("stop_order_type", "")).lower()),
            None,
        )
        if live_mode and cur_sz != 0 and tp_ord is None and prod_id > 0:
            tp_mult = max(2.0, float(m.get("tp_r") or 6.0))
            tp_px = round_to_tick(ent + sd * tp_mult * rd, tick)
            ex_side = "sell" if cur_sz > 0 else "buy"
            tp_res = client.request("POST", "/v2/orders", body_obj={
                "product_id": prod_id,
                "product_symbol": sym,
                "size": int(abs(cur_sz)),
                "side": ex_side,
                "order_type": "limit_order",
                "limit_price": tp_px,
                "reduce_only": True,
                "time_in_force": "gtc",
            })
            if tp_res.get("success"):
                m["tp_px"] = float(tp_px)
                send_alert("TP_ATTACH", f"{sym} Min-2R+ TP Attached", f"Attached missing TP @ {tp_px} ({tp_mult:.1f}R)", cfg, toast=False)

        if sd == 1:
            if (best - ent) >= float(m["be_r"]) * rd and new_sl < ent + 0.05 * rd:
                new_sl = max(new_sl, ent + 0.05 * rd)
            if (best - ent) >= 2.0 * rd:
                new_sl = max(new_sl, ent + 1.0 * rd)
            if (best - ent) >= 3.0 * rd:
                new_sl = max(new_sl, ent + 2.0 * rd)
            if (best - ent) >= 4.0 * rd:
                new_sl = max(new_sl, ent + 3.0 * rd)
            if (best - ent) >= rd:
                cand = best - float(m["tr_mult"]) * atr
                if cand > new_sl:
                    new_sl = cand
            improved = new_sl > float(m["current_sl"]) and new_sl < mark
        else:
            if (ent - best) >= float(m["be_r"]) * rd and new_sl > ent - 0.05 * rd:
                new_sl = min(new_sl, ent - 0.05 * rd)
            if (ent - best) >= 2.0 * rd:
                new_sl = min(new_sl, ent - 1.0 * rd)
            if (ent - best) >= 3.0 * rd:
                new_sl = min(new_sl, ent - 2.0 * rd)
            if (ent - best) >= 4.0 * rd:
                new_sl = min(new_sl, ent - 3.0 * rd)
            if (ent - best) >= rd:
                cand = best + float(m["tr_mult"]) * atr
                if cand < new_sl:
                    new_sl = cand
            improved = new_sl < float(m["current_sl"]) and new_sl > mark

        if not improved:
            continue

        sl_str = round_to_tick(new_sl, tick)
        # Find open stop-loss order for this symbol
        sl_ord = next(
            (o for o in open_orders if o.get("product_symbol") == sym and "stop" in str(o.get("order_type", "") or o.get("stop_order_type", "")).lower()),
            None,
        )
        if sl_ord and live_mode:
            upd = client.request(
                "PUT",
                "/v2/orders",
                body_obj={"id": sl_ord["id"], "product_id": sl_ord["product_id"], "stop_price": sl_str},
            )
            if upd.get("success"):
                m["current_sl"] = float(sl_str)
                send_alert("SL_TRAIL", f"{sym} SL Ratchet", f"Moved Stop-Loss -> {sl_str} (Mark {mark:.4f})", cfg, toast=False)
        else:
            print(f"  [TRAIL {'DRY' if not live_mode else 'NO-SL-ORD'}] {sym} new SL -> {sl_str} (Mark {mark:.4f})", flush=True)
            if not live_mode:
                m["current_sl"] = float(sl_str)


# ---------------------------------------------------------------------------
# Single Scan & Execution Cycle
# ---------------------------------------------------------------------------
def run_cycle(client: DeltaClient, cfg: dict[str, str], tier_a: list[dict[str, Any]], cat: dict[str, dict[str, Any]], state: dict[str, Any], live_mode: bool) -> None:
    ok, eq_usd, free_usd = check_wallet_and_ip(client, cfg, state)
    if not ok:
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] API unavailable / IP blocked — alert handled.", flush=True)
        return

    # Refresh live product margin metadata safely
    prod_map = dict(cat)
    try:
        for p in client.get_public("/v2/products").get("result", []):
            sym = str(p.get("symbol", ""))
            if sym in prod_map:
                prod_map[sym]["initial_margin"] = float(p.get("initial_margin") or 5.0)
                prod_map[sym]["tick_size"] = float(p.get("tick_size") or prod_map[sym].get("tick_size") or 0.01)
                prod_map[sym]["contract_value"] = float(p.get("contract_value") or prod_map[sym].get("contract_value") or 1.0)
                prod_map[sym]["id"] = int(p.get("id") or prod_map[sym]["id"])
    except Exception:
        pass

    pos_r = client.request("GET", "/v2/positions/margined")
    ord_r = client.request("GET", "/v2/orders", query="?state=open")
    if (not pos_r.get("success") or not isinstance(pos_r.get("result"), list)
            or not ord_r.get("success") or not isinstance(ord_r.get("result"), list)):
        print("[STATE_UNAVAILABLE] Position or pending-order readback failed; cycle skipped.", flush=True)
        return
    live_pos = [p for p in pos_r["result"] if abs(float(p.get("size") or 0)) > 0]
    open_orders = ord_r["result"]

    live_pos_map = {str(p.get("product_symbol")): p for p in live_pos}
    occupied_syms = set(live_pos_map.keys()) | {str(o.get("product_symbol")) for o in open_orders if not o.get("reduce_only")}
    active_slots = len(occupied_syms)

    print(
        f"\n[{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}] MODE={'LIVE' if live_mode else 'DRY-RUN'} | "
        f"Equity=${eq_usd:.2f} (₹{eq_usd*INR_PER_USD:.0f}) | Free=${free_usd:.2f} | ActiveSlots={active_slots}/6 {sorted(occupied_syms)}",
        flush=True,
    )

    manage_trailing_stops(client, live_pos_map, open_orders, prod_map, state, cfg, live_mode)

    # Fetch closed candles per (symbol, tf) cleanly & scan Tier-A15 ensemble signals
    candle_cache: dict[tuple[str, str], pd.DataFrame] = {}
    sig_cache: dict[tuple[str, str], tuple[dict[str, list[tuple[str, np.ndarray]]], np.ndarray]] = {}

    for asset in tier_a:
        sym = asset["symbol"]
        if sym in occupied_syms:
            continue
        if active_slots >= 6:
            break

        prod = prod_map.get(sym, {})
        for r_item in asset["recipe"]:
            sid, tf_l, cfg_s = r_item["sid"], r_item["tf"], r_item["cfg"]
            parts = dict(kv.split("=") for kv in cfg_s.split(",") if "=" in kv)
            var_cfg = cfg_s.split(",")[0]
            sl_m, tr_m, be_m = float(parts["sl"]), float(parts["tr"]), float(parts["be"])

            ckey = (sym, tf_l)
            if ckey not in candle_cache:
                candle_cache[ckey] = fetch_closed_candles(client, sym, tf_l, bars_needed=280)
                df_u = candle_cache[ckey]
                if len(df_u) >= 240:
                    atr_arr = opt._atr_wilder(df_u["high"].to_numpy(float), df_u["low"].to_numpy(float), df_u["close"].to_numpy(float), 14)
                    sig_cache[ckey] = (opt._build_pa_signals(df_u), atr_arr)
            df_u = candle_cache[ckey]
            if len(df_u) < 240 or ckey not in sig_cache:
                continue

            sigs, atr_arr = sig_cache[ckey]
            sa = next((v for k, v in sigs.get(sid, []) if k == var_cfg), None)
            if sa is None:
                continue

            bar_ts = int(df_u["timestamp"].iloc[-1])
            if time.time() - (bar_ts + TF_SEC[tf_l]) > 15 * 60:
                continue
            state_key = f"{sym}|{tf_l}"
            if int(state["last_bars"].get(state_key, 0)) >= bar_ts:
                continue

            sd = int(sa[-1])
            if sd == 0:
                continue

            close_px = float(df_u["close"].iloc[-1])
            atr14 = float(atr_arr[-1])
            if not np.isfinite(atr14) or atr14 <= 0:
                continue

            sl_dist = sl_m * atr14
            sl_pct = sl_dist / max(close_px, 1e-9)
            # Ensure Delta liquidation distance (>~90%/lev) clears SL distance by >=2.25x
            safe_lev = max(1, min(10, int(math.floor(0.40 / max(sl_pct, 1e-4)))))
            tp_mult = max(2.0, float(parts.get("tp", 0.0) or 0.0) or 6.0)
            cv = float(prod.get("contract_value") or 1.0)
            im_pct = max(float(prod.get("initial_margin") or 5.0), 100.0 / safe_lev)
            tick = float(prod.get("tick_size") or 0.01)

            cts, reason = compute_safe_contracts(eq_usd, free_usd, close_px, sl_dist, cv, im_pct)
            if cts <= 0:
                print(f"  [SKIP] {sym} {tf_l} {sid} ({'LONG' if sd==1 else 'SHORT'}): {reason}", flush=True)
                continue

            sl_px = close_px - sd * sl_dist
            sl_str = round_to_tick(sl_px, tick)
            tp_px = close_px + sd * tp_mult * sl_dist
            tp_str = round_to_tick(tp_px, tick)
            side_str = "buy" if sd == 1 else "sell"
            risk_usd = cts * cv * sl_dist

            msg = (
                f"{sym} {tf_l} {side_str.upper()} {cts} ct ({safe_lev}x) | Strat={sid} ({cfg_s}) | "
                f"Px≈{close_px:.4f} | SL={sl_str} | TP={tp_str} ({tp_mult:.1f}R) | Risk=${risk_usd:.2f} (₹{risk_usd*INR_PER_USD:.0f})"
            )

            if live_mode:
                client.request("POST", "/v2/orders/leverage", body_obj={"product_id": int(prod["id"]), "leverage": str(safe_lev)})
                order_payload = {
                    "product_id": int(prod["id"]),
                    "product_symbol": sym,
                    "size": int(cts),
                    "side": side_str,
                    "order_type": "market_order",
                    "time_in_force": "ioc",
                    "bracket_stop_loss_price": sl_str,
                    "bracket_take_profit_price": tp_str,
                    "bracket_stop_trigger_method": "mark_price",
                    "client_order_id": f"pa15_{sym}_{tf_l}_{bar_ts}",
                }
                res = client.request("POST", "/v2/orders", body_obj=order_payload)
                if res.get("success"):
                    state["managed_trades"][sym] = {
                        "side": sd, "entry_px": close_px, "best_px": close_px, "rd": sl_dist,
                        "atr": atr14, "tr_mult": tr_m, "be_r": be_m, "tp_r": tp_mult,
                        "current_sl": float(sl_str), "tp_px": float(tp_str),
                        "opened_ts": bar_ts, "sid": sid, "tf": tf_l, "size": cts,
                    }
                    occupied_syms.add(sym)
                    active_slots += 1
                    free_usd = max(0.0, free_usd - cts * cv * close_px * (max(im_pct, 5.0) / 100.0))
                    print(f"  [EXECUTED LIVE] {msg}", flush=True)
                    send_alert("TRADE_EXECUTED", f"Delta LIVE {side_str.upper()} {sym}", msg, cfg, severity="TRADE")
                    break
                else:
                    b_ip, ip_s = parse_ip_block(res)
                    print(f"  [ORDER FAILED] {msg} -> {res}", flush=True)
                    send_alert("ORDER_FAILED", f"Order Failed {sym}", f"{msg}\nErr: {res.get('error')}", cfg, severity="ERROR", extra={"ip": ip_s or ""})
            else:
                print(f"  [DRY-RUN SIGNAL] {msg}", flush=True)
                send_alert("DRY_RUN_SIGNAL", f"Dry-Run Signal {sym}", msg, cfg, severity="INFO", toast=False)
                break

        # Mark current closed candles inspected after checking all recipes on that symbol
        for tf_l in ("1H", "4H"):
            if (sym, tf_l) in candle_cache and len(candle_cache[(sym, tf_l)]):
                state["last_bars"][f"{sym}|{tf_l}"] = int(candle_cache[(sym, tf_l)]["timestamp"].iloc[-1])

    save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser(description="Delta India Live Roll-TierA15 Safe 1.0% Pure-PA Trader")
    ap.add_argument("--live", action="store_true", help="Enable real order submission on Delta Exchange India (default is safe dry-run)")
    ap.add_argument("--once", action="store_true", help="Run one immediate check/scan cycle and exit")
    ap.add_argument("--alert-test", action="store_true", help="Send a test alert across configured WhatsApp/Webhook/Telegram channels and exit")
    ap.add_argument("--heartbeat-min", type=int, default=15, help="IP-whitelist & margin watchdog interval in minutes (default 15)")
    args = ap.parse_args()

    cfg = load_env_config()
    if args.alert_test:
        res = send_alert("TEST_ALERT", "Delta PA Alert Test", "Test alert from delta_pa_live_trader.py (Roll-TierA15 Safe 1.0%).", cfg)
        print("Alert dispatch result:", res)
        return

    if not cfg.get("DELTA_API_KEY") or not cfg.get("DELTA_API_SECRET"):
        raise SystemExit("Missing DELTA_API_KEY / DELTA_API_SECRET in environment or .env")

    tier_a, cat = load_tier_a15_recipes()
    client = DeltaClient(cfg["DELTA_API_KEY"], cfg["DELTA_API_SECRET"])
    state = load_state()

    print(f"Loaded Roll-TierA15 Safe 1.0% ({len(tier_a)} Champions) | LiveMode={args.live}", flush=True)
    run_cycle(client, cfg, tier_a, cat, state, live_mode=args.live)
    if args.once:
        return

    next_hb = time.time() + args.heartbeat_min * 60
    next_trail = time.time() + 60
    last_hour_run = int(time.time()) // 3600
    while True:
        time.sleep(15)
        now = time.time()
        cur_hour = int(now) // 3600
        # Run full PA signal scan 10 seconds after each UTC hour closes
        if cur_hour > last_hour_run and (int(now) % 3600) >= 10:
            last_hour_run = cur_hour
            next_hb = now + args.heartbeat_min * 60
            next_trail = now + 60
            run_cycle(client, cfg, tier_a, cat, state, live_mode=args.live)
        elif now >= next_trail and state.get("managed_trades"):
            next_trail = now + 60
            pos_r = client.request("GET", "/v2/positions/margined")
            ord_r = client.request("GET", "/v2/orders", query="?state=open")
            if pos_r.get("success") and ord_r.get("success"):
                lp = {str(p.get("product_symbol")): p for p in pos_r.get("result", []) if abs(float(p.get("size") or 0)) > 0}
                oo = ord_r.get("result", [])
                manage_trailing_stops(client, lp, oo, cat, state, cfg, args.live)
                save_state(state)
        elif now >= next_hb:
            next_hb = now + args.heartbeat_min * 60
            check_wallet_and_ip(client, cfg, state)


if __name__ == "__main__":
    main()
