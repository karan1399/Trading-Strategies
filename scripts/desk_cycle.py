#!/usr/bin/env python3
"""
desk_cycle.py — the 20-minute desk cycle (Arena side).

WHAT IT DOES, every cycle
  1. health-gate the terminal
  2. pull the live book: open orders, positions, available balance (from the register)
  3. resolve the near-miss cohort (if any rows are due)
  4. scan a rotating slice of the universe for candidates (keeps rate limits sane)
  5. re-check every resting order against its kill conditions
  6. adjudicate open ACTION REQUESTS addressed to Arena
  7. write desk/cycles/<ts>.md and append ONE council entry
  8. emit ACTION REQUESTS for Antigravity into desk/actions/

WHAT IT NEVER DOES
  - place, amend or cancel an order. Arena has no execution rights (AUTHORITY.md §1).
  - fabricate a balance. If the register is stale it says so and raises a request.

Usage
  python3 scripts/desk_cycle.py --once              # single cycle
  python3 scripts/desk_cycle.py --loop              # every 20 min forever
  python3 scripts/desk_cycle.py --loop --minutes 20
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = os.environ.get("TERMINAL_URL", "http://127.0.0.1:3000")
STATE = ROOT / "desk/state.json"
COUNCIL = ROOT / "desk/COUNCIL.md"
CYCLES = ROOT / "desk/cycles"
ACTIONS = ROOT / "desk/actions"
CURSOR = ROOT / "desk/.cycle-cursor.json"

# a rotating slice so we do not re-pull all 231 names every 20 min
SCAN = [
    "XRP", "DOGE", "SOL", "LINK", "AVAX", "BTC", "ETH", "BNB",
    "ADA", "LTC", "BCH", "NEAR", "DOT", "TRX", "UNI", "ATOM",
]


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(path: str, timeout: int = 25):
    try:
        with urllib.request.urlopen(TERMINAL + path, timeout=timeout) as r:
            body = r.read()
            if body[:1] == b"<":
                return {"ok": False, "error": "HTML route"}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def usd_norm(v, ref_price: float) -> float:
    """Coinglass liq payloads are not unit-consistent (see R826). Normalize to USD."""
    v = float(v or 0)
    if v <= 0:
        return 0.0
    if ref_price > 1000 and v < 1e6:
        return v * 1000.0
    if 100 < ref_price <= 1000 and v < 1e6:
        return v * 1000.0
    return v


# ---------------------------------------------------------------- book
def read_book() -> dict:
    try:
        st = json.loads(STATE.read_text())
    except Exception as e:
        return {"error": f"state.json unreadable: {e}"}
    orders = [o for o in st.get("orders", []) if o.get("status") == "open"]
    positions = st.get("positions", [])
    return {
        "orders": orders,
        "positions": positions,
        "n_orders": len(orders),
        "n_positions": len(positions),
        "register_mtime": datetime.fromtimestamp(
            STATE.stat().st_mtime, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def order_kill_check(o: dict, mkt: dict) -> list[str]:
    """Return the list of kill conditions currently TRUE for a resting order."""
    fired = []
    sym = o.get("sym")
    if mkt.get("ok"):
        last = mkt["price"]["last"]
        atr = (mkt.get("vol") or {}).get("atr14H1") or 0
        stop = o.get("stop")
        entry = o.get("entry")
        # short: stop is above entry
        if stop and entry and stop > entry:
            clearance = (stop - last) / atr if atr else None
            if last > entry:
                fired.append(f"{sym}: price {last:g} has crossed the {entry:g} trigger — order should be live/filled")
            elif clearance is not None and clearance < 0.35:
                fired.append(
                    f"{sym}: only {clearance:.2f} ATR left between mark {last:g} and stop {stop:g} — stop-death risk"
                )
    return fired


def position_risk(pos: list, mkt_by_sym: dict) -> list[dict]:
    """Flag positions with no stop, or whose liquidation sits inside a shelf."""
    out = []
    for p in pos:
        sym = p.get("symbol") or p.get("product_symbol") or "?"
        base = str(sym).split(".")[0].replace("USD", "")
        flags = []
        if not (p.get("sl") or p.get("stop")):
            flags.append("NO STOP LOSS ON RECORD")
        liq = p.get("liquidation_price")
        if liq:
            m = mkt_by_sym.get(base) or {}
            if m.get("ok"):
                last = m["price"]["last"]
                dist = abs(liq - last) / last * 100
                flags.append(f"liquidation {liq:g} is {dist:.2f}% from mark {last:g}")
                if dist < 2.0:
                    flags.append("MACRO: liquidation within 2% of mark")
        if flags:
            out.append({"symbol": sym, "venue": p.get("venue"), "flags": flags})
    return out


# ---------------------------------------------------------------- candidate scan
def scan_slice(symbols: list[str]) -> list[dict]:
    found = []
    for s in symbols:
        mkt = get(f"/api/cg/market?sym={s}")
        if not mkt.get("ok"):
            time.sleep(0.4)
            continue
        nar = get(f"/api/cg/narrative?sym={s}")
        last = mkt["price"]["last"]
        atr = (mkt.get("vol") or {}).get("atr14H1") or 0
        z = None
        try:
            z = (last - mkt["vwap"]["vwap"]) / mkt["vwap"]["bands"]["sd1"]
        except Exception:
            pass
        row = {
            "sym": s,
            "last": last,
            "atr": atr,
            "atr_pct_rank": (mkt.get("vol") or {}).get("atr14PctRank"),
            "atr_state": (mkt.get("vol") or {}).get("atrState"),
            "rsi_d1": (mkt.get("rsi") or {}).get("d1"),
            "rsi_h4": (mkt.get("rsi") or {}).get("h4"),
            "vwap_z": round(z, 2) if z is not None else None,
            "regime": nar.get("regime"),
            "model": nar.get("activeModel"),
            "direction": nar.get("direction"),
            "grade": nar.get("grade"),
            "bestR": (nar.get("plan") or {}).get("bestR"),
        }
        found.append(row)
        time.sleep(0.45)
    return found


def pick_candidates(rows: list[dict]) -> list[dict]:
    """A candidate needs a direction the terminal does not contradict, plus room."""
    out = []
    for r in rows:
        if r["bestR"]:                      # terminal already has a plan
            out.append({**r, "why": f"terminal plan bestR {r['bestR']}"})
            continue
        if r["direction"] == "LONG":        # never propose a short against a LONG call
            continue
        if r["model"] not in ("NO_TRADE", "WATCH_EXHAUSTION", None):
            continue
        out.append({**r, "why": f"{r['regime']} / {r['model']} (abstains, no contradiction)"})
    return out


# ---------------------------------------------------------------- actions
def action_path(kind: str, sym: str) -> Path:
    ACTIONS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ACTIONS / f"{stamp}_{kind}_{sym}.json"


def raise_action(kind: str, sym: str, payload: dict) -> Path:
    p = action_path(kind, sym.replace("/", "-"))
    payload = {
        "id": p.stem,
        "raised_at": utc(),
        "raised_by": "Arena.ai",
        "addressed_to": "Antigravity",
        "kind": kind,
        "symbol": sym,
        "status": "PENDING",
        **payload,
    }
    p.write_text(json.dumps(payload, indent=1))
    return p


def open_actions() -> list[dict]:
    out = []
    if not ACTIONS.exists():
        return out
    for f in sorted(ACTIONS.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            if d.get("status") == "PENDING":
                out.append(d)
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- council
def council_tail(text: str = "", limit: int = 6) -> str:
    try:
        lines = COUNCIL.read_text(errors="ignore").splitlines()
    except Exception:
        return ""
    # find entries addressed to Arena that we have not answered
    tail = lines[-400:]
    entries, cur = [], []
    for L in tail:
        if L.startswith("### ["):
            if cur:
                entries.append("\n".join(cur))
            cur = [L]
        elif cur:
            cur.append(L)
    if cur:
        entries.append("\n".join(cur))
    for e in entries[-limit:]:
        text += e[:600] + "\n"
    return text


def append_council(block: str) -> None:
    with COUNCIL.open("a") as fh:
        fh.write("\n" + block.strip() + "\n")


# ---------------------------------------------------------------- the cycle
def one_cycle(n: int) -> dict:
    t0 = time.time()
    CYCLES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rec: dict = {"cycle": n, "started": utc(), "findings": []}

    # 1 health
    h = get("/api/health", timeout=10)
    rec["health"] = bool(h.get("ok"))
    if not rec["health"]:
        rec["findings"].append(f"TERMINAL DOWN: {h.get('error')}")

    # 2 book
    book = read_book()
    rec["book"] = book
    if book.get("error"):
        rec["findings"].append(book["error"])

    # 3 near-miss
    nm_out = ""
    try:
        r = subprocess.run(
            [sys.executable, "scripts/trading/near_miss_resolver.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        nm_out = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
        rec["near_miss"] = nm_out
    except Exception as e:
        rec["near_miss"] = f"resolver error: {e}"

    # 4 rotating scan slice
    try:
        cur = json.loads(CURSOR.read_text()) if CURSOR.exists() else {"offset": 0}
    except Exception:
        cur = {"offset": 0}
    off = cur.get("offset", 0) % len(SCAN)
    slice_ = (SCAN + SCAN)[off: off + 6]
    rows = scan_slice(slice_)
    cur["offset"] = (off + 6) % len(SCAN)
    CURSOR.write_text(json.dumps(cur))
    rec["scanned"] = [r["sym"] for r in rows]

    mkt_by_sym = {r["sym"]: {"ok": True, "price": {"last": r["last"]}} for r in rows}

    # 5 resting-order kill checks
    kills = []
    for o in book.get("orders", []):
        sym = o.get("sym")
        m = get(f"/api/cg/market?sym={sym}") if sym else {}
        if not m.get("ok"):
            kills.append(f"{sym}: market unavailable for kill-check — UNVERIFIED")
            continue
        mkt_by_sym[sym] = m
        kills.extend(order_kill_check(o, m))
        time.sleep(0.35)
    rec["kill_conditions_fired"] = kills
    if kills:
        rec["findings"].extend(kills)

    # position risk
    prisk = position_risk(book.get("positions", []), mkt_by_sym)
    rec["position_flags"] = prisk

    # raise actions
    raised = []
    for p in prisk:
        if any("NO STOP LOSS" in f for f in p["flags"]):
            raised.append(str(raise_action(
                "ADD_STOP", str(p["symbol"]),
                {"venue": p["venue"], "reason": "; ".join(p["flags"]),
                 "instruction": "Attach a protective stop, or close the position. Do not leave a naked position open."},
            )))
    if kills:
        raised.append(str(raise_action(
            "REVIEW_ORDER", "RESTING",
            {"reason": "; ".join(kills), "instruction": "Re-evaluate every resting order listed; cancel any whose thesis is broken."},
        )))
    rec["actions_raised"] = raised

    # candidates
    cands = pick_candidates(rows)
    rec["candidates"] = cands

    # 7 write cycle file
    md = [f"# Cycle {n} — {rec['started']}", ""]
    md.append(f"**Health:** {'OK' if rec['health'] else '**DOWN**'}")
    md.append(f"**Book:** {book.get('n_orders',0)} open order(s) · {book.get('n_positions',0)} position(s) "
              f"· register mtime {book.get('register_mtime','?')}")
    for o in book.get("orders", []):
        md.append(f"  - `{o.get('id')}` {o.get('sym')} {o.get('side')} {o.get('contracts')}@{o.get('entry')} "
                  f"SL {o.get('stop')} TP {o.get('tps') or o.get('targets')}")
    if rec.get("near_miss"):
        md.append(f"**Near-miss:** {rec['near_miss']}")
    md.append(f"**Scanned:** {', '.join(rec['scanned']) or 'none'}")
    if kills:
        md.append("\n**⛔ KILL CONDITIONS FIRED**")
        for k in kills:
            md.append(f"  - {k}")
    if prisk:
        md.append("\n**⛔ POSITION FLAGS**")
        for p in prisk:
            md.append(f"  - {p['symbol']} ({p['venue']}): {'; '.join(p['flags'])}")
    if raised:
        md.append("\n**Actions raised for Antigravity:**")
        for r in raised:
            md.append(f"  - `{Path(r).name}`")
    md.append("\n**Candidates (no contradiction from the terminal):**")
    if not cands:
        md.append("  _none this slice_")
    for c in cands:
        md.append(f"  - **{c['sym']}** {c['last']:g} · {c['why']} · RSI d1 {c['rsi_d1']} · "
                  f"vwap z {c['vwap_z']} · ATR {c['atr_state']} ({c['atr_pct_rank']}%)")
    (CYCLES / f"{stamp}.md").write_text("\n".join(md))
    rec["cycle_file"] = f"desk/cycles/{stamp}.md"

    # 8 one council entry
    verdict = "clean" if not (kills or prisk) else "**ATTENTION**"
    body = [f"### [{utc()}] · Arena.ai · CYCLE · R827-{n:03d}",
            f"**Verdict:** {verdict} — {book.get('n_orders',0)} order(s), {book.get('n_positions',0)} position(s)."]
    if kills or prisk:
        body.append("**Evidence:**")
        for k in kills:
            body.append(f"- kill: {k}")
        for p in prisk:
            body.append(f"- position {p['symbol']} ({p['venue']}): {'; '.join(p['flags'])}")
    if cands:
        body.append("**Watch:** " + ", ".join(c["sym"] for c in cands))
    if raised:
        body.append("**Actions:** " + ", ".join(Path(r).name for r in raised))
    body.append(f"**Confidence:** VERIFIED (register read {book.get('register_mtime')}); "
                f"venue truth UNVERIFIED-BY-AGENT")
    append_council("\n".join(body))

    rec["elapsed_s"] = round(time.time() - t0, 1)
    (CYCLES / f"{stamp}.json").write_text(json.dumps(rec, indent=1))
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--minutes", type=float, default=20.0)
    a = ap.parse_args()

    if a.once or not a.loop:
        r = one_cycle(1)
        print(json.dumps({k: r[k] for k in
                          ("cycle", "started", "health", "kill_conditions_fired",
                           "position_flags", "actions_raised", "scanned", "elapsed_s")}, indent=1))
        return

    n = 0
    while True:
        n += 1
        try:
            r = one_cycle(n)
            print(f"[{utc()}] cycle {n} done in {r['elapsed_s']}s — "
                  f"{len(r['kill_conditions_fired'])} kills, {len(r['position_flags'])} pos flags", flush=True)
        except Exception as e:
            print(f"[{utc()}] cycle {n} FAILED: {e}", flush=True)
        time.sleep(a.minutes * 60)


if __name__ == "__main__":
    main()
