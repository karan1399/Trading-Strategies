"""Live CG Order-Flow Terminal SL & TP Optimizer for unified_alpha_pilot.py.

Non-blocking, fail-open telemetry optimizer that grounds live stops and targets in
real-time CoinGlass / Terminal order-flow structure without ever breaching hard
broker risk bounds ($5.00-$6.00 risk cap, spread fraction <= max_spread_risk_frac,
minimum TP R-multiple >= min_tp_r).

Sources (prioritized by latency & freshness):
  1. In-memory TTL cache (default 90s per CG symbol)
  2. Fast HTTP endpoints on http://127.0.0.1:3000 (timeout <= 1.2s):
       - /api/cg/market?sym=<CG>      (session/day/week auction VAL/POC/VAH)
       - /api/cg/livebook?sym=<CG>    (Binance real-time top-1000 walls & ladder)
       - /api/cg/liqlevels?sym=<CG>   (liquidation keyLevels when fast/cached)
  3. Local snapshot fallback: data/telemetry/assets/<CG>.json
     (clusters 3d/7d/30d, book_walls.futures.m15, market_structure.auction)

Scale Guard: Any external price level p whose ratio p/entry falls outside
[0.85, 1.15] is silently discarded so contract/unit differences (e.g. GAUUSD vs
XAUUSD troy oz) can NEVER corrupt an MT5 order bracket.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEMETRY_ASSETS_DIR = os.path.join(ROOT, "data", "telemetry", "assets")

# Verified MT5 symbol -> Coinglass / Terminal base symbol map
MT5_TO_CG = {
    "BTCUSD.pi": "BTC", "ETHUSD.pi": "ETH", "SOLUSD.p": "SOL", "BNBUSD.p": "BNB",
    "XRPUSD.pi": "XRP", "ADAUSD.p": "ADA", "AVXUSD.p": "AVAX", "DOGUSD.p": "DOGE",
    "LNKUSD.p": "LINK", "LTCUSD.pi": "LTC", "DOTUSD.pi": "DOT", "NERUSD.p": "NEAR",
    "UNIUSD.p": "UNI", "AVEUSD.p": "AAVE", "FILUSD.p": "FIL", "XLMUSD.p": "XLM",
    "TRXUSD.p": "TRX", "XMRUSD.p": "XMR", "BCHUSD.p": "BCH", "ALGUSD.p": "ALGO",
    "ATMUSD.p": "ATOM", "AXSUSD.p": "AXS", "CHZUSD.p": "CHZ", "COMUSD.p": "COMP",
    "EGLUSD.p": "EGLD", "FLWUSD.p": "FLOW", "IOTUSD.p": "IOTA", "KSMUSD.p": "KSM",
    "NEOUSD.p": "NEO", "XSIUSD.p": "SUSHI", "THTUSD.p": "THETA", "XTZUSD.p": "XTZ",
    "ZECUSD.p": "ZEC", "MANUSD.p": "MANA", "INCUSD.p": "1INCH", "ARWUSD.p": "AR",
    "BATUSD.p": "BAT", "CHRUSD.p": "CHR", "CRVUSD.p": "CRV", "ENJUSD.p": "ENJ",
    "BARUSD.p": "HBAR", "KAVUSD.p": "KAVA", "KNCUSD.p": "KNC", "ONTUSD.p": "ONT",
    "QTMUSD.p": "QTUM", "SNDUSD.p": "SAND", "SKLUSD.p": "SKL", "SNXUSD.p": "SNX",
    "YFIUSD.p": "YFI", "ZILUSD.p": "ZIL", "ZRXUSD.p": "ZRX", "DSHUSD.p": "DASH",
    "GRTUSD.p": "GRT", "SUIUSD.p": "SUI", "APTUSD.p": "APT", "ARBUSD.p": "ARB",
    "INJUSD.p": "INJ", "OPUUSD.p": "OP", "TIAUSD.p": "TIA", "SEIUSD.p": "SEI",
    "XAUUSD.pi": "XAU", "XAGUSD.pi": "XAG", "XPTUSD.pi": "XPT", "XPDUSD.pi": "XPD",
    "USWTI.p": "CL", "UKBRENT.p": "UKO", "GAS.p": "NATGAS",
    "SP500.p": "SPX500", "NAS100.p": "NDX100", "DJ30.p": "US30", "GER40.p": "GER40",
    "UK100.p": "UK100", "FR40.p": "FR40", "STOXX50.p": "STOXX50", "SWISS20.p": "SWISS20",
    "NETH25.p": "NETH25", "JP225.p": "JP225", "HK50.p": "HK50", "AU200.p": "AU200",
    "US2000.p": "US2000", "CHINAH.p": "CHINAH", "CHINA50.p": "CHINA50",
}

_TTL_CACHE: dict[str, tuple[float, dict]] = {}


@dataclass
class CGOptResult:
    sl: float
    tp: float
    rd: float
    tp_r: float
    sl_adjusted: bool
    tp_adjusted: bool
    source: str
    note: str


def resolve_cg_symbol(mt5_sym: str) -> str | None:
    """Resolve MT5 broker symbol to native telemetry asset name or Coinglass ticker."""
    stem = mt5_sym.split(".")[0].upper()
    if os.path.exists(os.path.join(TELEMETRY_ASSETS_DIR, f"{stem}.json")):
        return stem
    mapped = MT5_TO_CG.get(mt5_sym)
    if mapped:
        return mapped
    if stem.endswith("USD") and len(stem) >= 6:
        return stem[:3]
    return stem or None


def _http_get_json(url: str, timeout_s: float = 1.2) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_disk_asset(cg_sym: str, max_age_s: float = 900.0) -> dict:
    p = os.path.join(TELEMETRY_ASSETS_DIR, f"{cg_sym}.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        ts_val = float(d.get("fetched_at_ts") or d.get("updated_ts") or os.path.getmtime(p))
        if max_age_s > 0 and (time.time() - ts_val) > max_age_s:
            return {}
        return d
    except Exception:
        return {}


def fetch_cg_context(
    mt5_sym: str,
    base_url: str = "http://127.0.0.1:3000",
    ttl_seconds: float = 90.0,
    http_timeout_s: float = 1.2,
    use_http: bool = True,
) -> dict:
    """Collect scale-neutral liquidation levels, resting book walls, and auction nodes."""
    cg_sym = resolve_cg_symbol(mt5_sym)
    if not cg_sym:
        return {"cg_sym": None, "source": "none"}

    now = time.time()
    cached = _TTL_CACHE.get(cg_sym)
    if cached and (now - cached[0]) <= ttl_seconds:
        return cached[1]

    disk = _load_disk_asset(cg_sym)
    liq_above: list[tuple[float, float]] = []  # (price, strength)
    liq_below: list[tuple[float, float]] = []
    ask_walls: list[tuple[float, float]] = []  # (price, weight)
    bid_walls: list[tuple[float, float]] = []
    auction_nodes: list[float] = []
    sources = []

    # 1. Disk telemetry baseline (instant)
    if disk:
        sources.append("disk")
        clusters = (disk.get("liquidations") or {}).get("clusters") or {}
        for win in ("7d", "3d", "30d"):
            kl = ((clusters.get(win) or {}).get("stats") or {}).get("keyLevels") or {}
            for row in kl.get("above") or []:
                px, st = float(row.get("price") or 0), float(row.get("strength") or 0)
                if px > 0:
                    liq_above.append((px, st))
            for row in kl.get("below") or []:
                px, st = float(row.get("price") or 0), float(row.get("strength") or 0)
                if px > 0:
                    liq_below.append((px, st))
        m15_bw = ((disk.get("book_walls") or {}).get("futures") or {}).get("m15") or {}
        for w in m15_bw.get("walls") or []:
            px, usd, sd = float(w.get("price") or 0), float(w.get("usd") or 0), str(w.get("side") or "")
            if px > 0:
                if sd == "a":
                    ask_walls.append((px, usd))
                elif sd == "b":
                    bid_walls.append((px, usd))
        auc = (disk.get("market_structure") or {}).get("auction") or {}
        for tf_k in ("session", "day", "week"):
            node = auc.get(tf_k) or {}
            for fld in ("poc", "vah", "val"):
                val = float(node.get(fld) or 0)
                if val > 0:
                    auction_nodes.append(val)

    # 2. Live low-latency CG HTTP enrichment (<= 1.2s per call)
    if use_http and base_url:
        base = base_url.rstrip("/")
        mkt = _http_get_json(f"{base}/api/cg/market?sym={cg_sym}", timeout_s=http_timeout_s)
        if mkt and mkt.get("ok"):
            sources.append("http_market")
            auc = mkt.get("auction") or {}
            live_nodes = []
            for tf_k in ("session", "day", "week"):
                node = auc.get(tf_k) or {}
                for fld in ("poc", "vah", "val"):
                    val = float(node.get(fld) or 0)
                    if val > 0:
                        live_nodes.append(val)
            if live_nodes:
                auction_nodes = live_nodes

        lb = _http_get_json(f"{base}/api/cg/livebook?sym={cg_sym}", timeout_s=http_timeout_s)
        if lb and lb.get("ok"):
            sources.append("http_livebook")
            mod = lb.get("model") or {}
            for w in mod.get("walls") or []:
                px, usd, sd = float(w.get("price") or 0), float(w.get("usd") or 0), str(w.get("side") or "")
                if px > 0:
                    if sd == "a":
                        ask_walls.append((px, usd))
                    elif sd == "b":
                        bid_walls.append((px, usd))
            for lv in mod.get("ladder") or []:
                px = float(lv.get("price") or 0)
                usd = float(lv.get("usd") or 0)
                mid = float((mod.get("last") or {}).get("price") or 0)
                if px > 0 and mid > 0 and usd >= 500_000:
                    if px > mid:
                        ask_walls.append((px, usd))
                    elif px < mid:
                        bid_walls.append((px, usd))

        sm = _http_get_json(
            f"{base}/api/cg/stopmap?sym={cg_sym}&range=3d&mode=coin",
            timeout_s=max(http_timeout_s, 1.8),
        )
        if sm and sm.get("ok"):
            sources.append("http_stopmap")
            smod = sm.get("model") or {}
            kl = smod.get("keyLevels") or {}
            ov_max = float((smod.get("overview") or {}).get("largestLevel") or 0.0)
            lvl_max = max(
                [ov_max]
                + [
                    float(row.get("usd") or 0.0)
                    for side_k in ("above", "below")
                    for row in (kl.get(side_k) or [])
                ]
                + [1.0]
            )
            for row in kl.get("above") or []:
                px, usd = float(row.get("price") or 0.0), float(row.get("usd") or 0.0)
                if px > 0 and usd > 0:
                    liq_above.append((px, round(min(100.0, (usd / lvl_max) * 100.0), 1)))
            for row in kl.get("below") or []:
                px, usd = float(row.get("price") or 0.0), float(row.get("usd") or 0.0)
                if px > 0 and usd > 0:
                    liq_below.append((px, round(min(100.0, (usd / lvl_max) * 100.0), 1)))

    ctx = {
        "cg_sym": cg_sym,
        "source": "+".join(sources) if sources else "none",
        "liq_above": liq_above,
        "liq_below": liq_below,
        "ask_walls": ask_walls,
        "bid_walls": bid_walls,
        "auction_nodes": auction_nodes,
    }
    _TTL_CACHE[cg_sym] = (now, ctx)
    return ctx


def _scale_ok(px: float, ref: float) -> bool:
    if ref <= 0 or px <= 0:
        return False
    ratio = px / ref
    return 0.85 <= ratio <= 1.15


def optimize_bracket_with_cg(
    symbol: str,
    side: int,
    entry: float,
    sl: float,
    tp: float,
    atr: float,
    live_spread: float = 0.0,
    min_tp_r: float = 2.0,
    exempt_min_r: bool = False,
    max_spread_risk_frac: float = 0.25,
    spec=None,
    risk_usd_max: float = 6.0,
    digits: int = 5,
    cg_ctx: dict | None = None,
    cg_cfg: dict | None = None,
) -> CGOptResult:
    """Refine SL/TP strictly inside structural, risk-cap, and spread invariants."""
    rd0 = abs(entry - sl)
    tp0 = abs(tp - entry)
    if rd0 <= 0 or tp0 <= 0 or entry <= 0:
        return CGOptResult(sl, tp, rd0, tp0 / max(rd0, 1e-12), False, False, "none", "")

    if cg_ctx is None:
        cfg = cg_cfg or {}
        if not cfg.get("enabled", True):
            return CGOptResult(sl, tp, rd0, tp0 / rd0, False, False, "disabled", "")
        cg_ctx = fetch_cg_context(
            symbol,
            base_url=str(cfg.get("base_url", "http://127.0.0.1:3000")),
            ttl_seconds=float(cfg.get("ttl_seconds", 90.0)),
            http_timeout_s=float(cfg.get("http_timeout_s", 1.2)),
            use_http=bool(cfg.get("use_http", True)),
        )

    src = cg_ctx.get("source", "none")
    if src == "none":
        return CGOptResult(sl, tp, rd0, tp0 / rd0, False, False, "none", "")

    orig_r = tp0 / rd0
    notes = []

    # ---- Step A: SL Liquidation Cold-Zone Clearance (only widen if safe within caps!)
    new_sl = float(sl)
    sl_adjusted = False
    risk_clusters = cg_ctx.get("liq_below", []) if side == 1 else cg_ctx.get("liq_above", [])
    buf = max(0.25 * atr if atr > 0 else 0.0, 0.10 * rd0)
    cand_sl = new_sl

    for px, strength in risk_clusters:
        if strength < 45.0 or not _scale_ok(px, entry):
            continue
        if side == 1:
            if (sl - 0.35 * rd0) <= px <= (sl + 0.15 * rd0):
                prop = px - buf
                if prop < cand_sl:
                    cand_sl = prop
        else:
            if (sl - 0.15 * rd0) <= px <= (sl + 0.35 * rd0):
                prop = px + buf
                if prop > cand_sl:
                    cand_sl = prop

    if cand_sl != new_sl:
        cand_rd = abs(entry - cand_sl)
        spr_ok = (max_spread_risk_frac <= 0.0) or ((2.0 * live_spread) / cand_rd <= max_spread_risk_frac)
        lot_ok = True
        if spec is not None and spec.value_per_price_unit() > 0:
            lot_ok = (spec.vol_min * cand_rd * spec.value_per_price_unit()) <= (risk_usd_max + 1e-6)
        r_after = tp0 / cand_rd
        r_ok = exempt_min_r or (r_after + 1e-9 >= min_tp_r)
        if cand_rd > rd0 and cand_rd <= 1.45 * rd0 and spr_ok and lot_ok and r_ok:
            new_sl = round(cand_sl, digits)
            sl_adjusted = True
            notes.append(f"sl_cold({sl:.{digits}f}->{new_sl:.{digits}f})")

    rd_final = abs(entry - new_sl)
    new_tp = float(tp)
    tp_adjusted = False
    min_tp_dist = 0.0 if exempt_min_r else (min_tp_r * rd_final)

    # ---- Step B: Opposing Book-Wall / Auction Front-Run vs Liquidation Magnet Extension
    opp_walls = [
        (px, wt) for (px, wt) in (cg_ctx.get("ask_walls", []) if side == 1 else cg_ctx.get("bid_walls", []))
        if _scale_ok(px, entry)
    ]
    for auc_px in cg_ctx.get("auction_nodes", []):
        if _scale_ok(auc_px, entry):
            if (side == 1 and auc_px > entry) or (side == -1 and auc_px < entry):
                opp_walls.append((auc_px, 1.0))

    best_front_wall = None
    best_dist = tp0
    for px, _ in opp_walls:
        dist = (px - entry) if side == 1 else (entry - px)
        if (0.80 * tp0) <= dist <= (tp0 * 1.01) and dist >= (min_tp_dist + 0.08 * rd_final):
            if dist < best_dist:
                best_dist = dist
                best_front_wall = px

    if best_front_wall is not None:
        prop_tp = best_front_wall - side * (0.08 * rd_final)
        prop_dist = abs(prop_tp - entry)
        if prop_dist + 1e-9 >= min_tp_dist and ((side == 1 and prop_tp < new_tp) or (side == -1 and prop_tp > new_tp)):
            new_tp = round(prop_tp, digits)
            tp_adjusted = True
            notes.append(f"tp_wall({tp:.{digits}f}->{new_tp:.{digits}f})")
    else:
        opp_clusters = cg_ctx.get("liq_above", []) if side == 1 else cg_ctx.get("liq_below", [])
        best_mag = None
        best_mag_str = 0.0
        for px, strength in opp_clusters:
            if strength < 65.0 or not _scale_ok(px, entry):
                continue
            dist = (px - entry) if side == 1 else (entry - px)
            if (tp0 * 1.01) < dist <= (tp0 * 1.25) and strength > best_mag_str:
                best_mag = px
                best_mag_str = strength
        if best_mag is not None:
            prop_tp = best_mag - side * (0.05 * rd_final)
            prop_dist = abs(prop_tp - entry)
            if prop_dist > abs(tp - entry) and prop_dist + 1e-9 >= min_tp_dist:
                new_tp = round(prop_tp, digits)
                tp_adjusted = True
                notes.append(f"tp_mag({tp:.{digits}f}->{new_tp:.{digits}f})")

    # Hard bracket invariant guard
    if (side == 1 and not (new_sl < entry < new_tp)) or (side == -1 and not (new_tp < entry < new_sl)):
        return CGOptResult(sl, tp, rd0, orig_r, False, False, src, "invariant_revert")

    tp_r_final = abs(new_tp - entry) / rd_final
    return CGOptResult(
        sl=new_sl, tp=new_tp, rd=rd_final, tp_r=tp_r_final,
        sl_adjusted=sl_adjusted, tp_adjusted=tp_adjusted,
        source=src, note=" ".join(notes),
    )
