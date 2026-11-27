#!/usr/bin/env python3
"""
telemetry_analytics.py — Vectorized UI-Exact Order-Flow & Memory-Bounded Primitives.

Provides 1:1 ports of the CG Order-Flow Terminal UI math plus bounded cache &
memory-safety primitives so `telemetry_daemon.py` runs fast without heap leaks:
  - BoundedTTLCache: strict entry + byte-budget LRU/TTL cache with eviction.
  - compute_stopmap_walls(): exact NumPy port of `coinglass-walls.tsx` `buildWalls()`.
  - compute_diagonal_imbalances(): exact port of `cg-signals.ts` `diagonalImbalances()`.
  - summarize_stopmap_payload(): full Model 1/2/3 extraction without raw grid bloat.
  - build_compact_bookwalls(): full multi-TF walls, events, ladder & strip metrics.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

try:
    import orjson

    def dumps_bytes(obj: Any, indent: bool = False) -> bytes:
        opts = orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY
        if indent:
            opts |= orjson.OPT_INDENT_2
        return orjson.dumps(obj, option=opts)

except Exception:  # pragma: no cover - stdlib fallback if orjson missing
    import json

    def dumps_bytes(obj: Any, indent: bool = False) -> bytes:
        return json.dumps(obj, indent=2 if indent else None).encode("utf-8")


class BoundedTTLCache:
    """Thread-safe LRU + TTL + byte-budget cache that never leaks heap memory."""

    def __init__(self, max_entries: int = 96, max_bytes: int = 48 * 1024 * 1024):
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        self._store: "OrderedDict[Any, Tuple[float, int, Any]]" = OrderedDict()
        self._total_bytes = 0

    def _evict_if_needed(self) -> None:
        while self._store and (
            len(self._store) > self.max_entries or self._total_bytes > self.max_bytes
        ):
            _, (_, sz, _) = self._store.popitem(last=False)
            self._total_bytes = max(0, self._total_bytes - sz)

    def get(self, key: Any, ttl_secs: float, allow_stale: bool = False) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            ts, _, val = item
            if allow_stale or (ttl_secs > 0 and (now - ts) < ttl_secs):
                self._store.move_to_end(key)
                return val
            return None

    def set(self, key: Any, val: Any, approx_bytes: Optional[int] = None) -> None:
        if approx_bytes is None:
            try:
                approx_bytes = len(dumps_bytes(val, indent=False))
            except Exception:
                approx_bytes = 4096
        if approx_bytes > self.max_bytes // 2:
            # Never pin giant blobs that would thrash the whole LRU budget.
            return
        now = time.time()
        with self._lock:
            old = self._store.pop(key, None)
            if old is not None:
                self._total_bytes = max(0, self._total_bytes - old[1])
            self._store[key] = (now, int(approx_bytes), val)
            self._total_bytes += int(approx_bytes)
            self._evict_if_needed()

    def trim_to(self, ratio: float = 0.5) -> int:
        """Evict oldest entries down to ratio * max_bytes (called under memory pressure)."""
        target = int(self.max_bytes * max(0.0, min(1.0, ratio)))
        evicted = 0
        with self._lock:
            while self._store and (self._total_bytes > target or (ratio == 0.0 and self._store)):
                _, (_, sz, _) = self._store.popitem(last=False)
                self._total_bytes = max(0, self._total_bytes - sz)
                evicted += 1
        return evicted

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._total_bytes = 0

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._store),
                "max_entries": self.max_entries,
                "mb": round(self._total_bytes / (1024 * 1024), 2),
                "max_mb": round(self.max_bytes / (1024 * 1024), 2),
            }


def compute_stopmap_walls(model: Dict[str, Any], thr: float = 0.0) -> Dict[str, Any]:
    """
    Exact vectorized port of `src/components/cg/charts/coinglass-walls.tsx` `buildWalls()`.

    Reconstructs the horizontal liquidation wall bands, sorts by Coinglass score
    (`mx * column_width`), returns top-24 walls with `active` (untouched right-edge)
    vs swept status, `side` ("short" above closeRow, "long" at/below closeRow),
    and `maxShort` / `maxLong` callouts.
    """
    if not isinstance(model, dict):
        return {"walls_top24": [], "max_short": None, "max_long": None, "total_walls_found": 0}

    y_axis = model.get("yAxis") or []
    candles = model.get("candles") or []
    columns = model.get("columns") or []
    rows = len(y_axis)
    cols = len(candles)
    if rows == 0 or cols == 0 or not columns:
        return {"walls_top24": [], "max_short": None, "max_long": None, "total_walls_found": 0}

    lo = float(y_axis[0])
    bin_sz = float(model.get("priceStep") or (float(y_axis[1]) - lo if rows > 1 else 0.0))
    if bin_sz <= 0:
        return {"walls_top24": [], "max_short": None, "max_long": None, "total_walls_found": 0}

    last_px = float(model.get("lastPrice") or (model.get("overview") or {}).get("lastPrice") or 0.0)
    if last_px <= 0 and candles:
        last_px = float(candles[-1].get("c") or 0.0)

    by_ts = {cd.get("ts"): idx for idx, cd in enumerate(candles) if isinstance(cd, dict) and cd.get("ts") is not None}
    total = np.zeros((cols, rows), dtype=np.float64)
    for bar in columns:
        if not isinstance(bar, dict):
            continue
        ci = by_ts.get(bar.get("ts"))
        if ci is None:
            continue
        for cell in bar.get("cells") or ():
            if len(cell) >= 2:
                yi = int(cell[0])
                if 0 <= yi < rows:
                    total[ci, yi] += float(cell[1])

    max_val = float(total.max()) if total.size else 1.0
    if max_val <= 0.0:
        return {"walls_top24": [], "max_short": None, "max_long": None, "total_walls_found": 0}

    raw = np.minimum(1.0, np.power(np.maximum(total, 0.0) / max_val, 0.7))
    if thr > 0.0:
        norm = np.where(raw <= thr, 0.0, np.minimum(1.0, (raw - thr) / (1.0 - thr)))
    else:
        norm = raw

    mask = norm >= 0.3
    # Phase 1: per-row contiguous horizontal runs spanning >= 2 columns
    runs: List[Tuple[int, int, int]] = []  # (r, c0, c1)
    for r in range(rows):
        col_mask = mask[:, r]
        if not np.any(col_mask):
            continue
        l_idx = -1
        for s in range(cols + 1):
            if s < cols and col_mask[s]:
                if l_idx < 0:
                    l_idx = s
            elif l_idx >= 0:
                if s - l_idx >= 2:
                    runs.append((r, l_idx, s - 1))
                l_idx = -1

    if not runs:
        return {"walls_top24": [], "max_short": None, "max_long": None, "total_walls_found": 0}

    # Phase 2: merge vertically adjacent rows with >= 80% column overlap (exact order)
    runs.sort(key=lambda e: (e[0], e[1]))
    groups: List[List[int]] = []  # [rmin, rmax, c0, c1]
    for r, ec0, ec1 in runs:
        elen = ec1 - ec0 + 1
        target: Optional[List[int]] = None
        for g in groups:
            if (
                r - g[1] <= 1
                and r >= g[0] - 1
                and (min(ec1, g[3]) - max(ec0, g[2]) + 1) >= 0.8 * max(elen, g[3] - g[2] + 1)
            ):
                target = g
                break
        if target is not None:
            if r < target[0]:
                target[0] = r
            if r > target[1]:
                target[1] = r
            if ec0 < target[2]:
                target[2] = ec0
            if ec1 > target[3]:
                target[3] = ec1
        else:
            groups.append([r, r, ec0, ec1])

    close_row = min(rows - 1, max(0, math.floor((last_px - lo) / bin_sz))) if last_px > 0 else 0

    # Phase 3: re-split on >= 2 empty columns inside group; score = mx * width
    walls_raw: List[Dict[str, Any]] = []
    for rmin, rmax, gc0, gc1 in groups:
        has_col = np.any(mask[gc0 : gc1 + 1, rmin : rmax + 1], axis=1)
        a = -1
        empties = 0
        span_len = gc1 - gc0 + 1
        for offset in range(span_len + 1):
            n = gc0 + offset
            has = bool(has_col[offset]) if offset < span_len else False
            if has:
                if a < 0:
                    a = n
                empties = 0
            elif a >= 0:
                empties += 1
                if empties >= 2 or n > gc1:
                    c_end = n - empties
                    if c_end - a + 1 >= 2:
                        sub = total[a : c_end + 1, rmin : rmax + 1]
                        sub_norm = norm[a : c_end + 1, rmin : rmax + 1]
                        # Match TS loop order: outer s in [a..c_end], inner r in [rmin..rmax]
                        flat_idx = int(np.argmax(sub))
                        _, dr = divmod(flat_idx, rmax - rmin + 1)
                        rpk = rmin + dr
                        mx = float(sub.max())
                        npk = float(sub_norm.max())
                        if mx > 0.0 and npk > 0.0:
                            end_idx = min(c_end, cols - 1)
                            walls_raw.append({
                                "price": round(lo + (rpk + 0.5) * bin_sz, 6),
                                "midPrice": round(lo + ((rmin + rmax + 1) / 2.0) * bin_sz, 6),
                                "rLo": round(lo + rmin * bin_sz, 6),
                                "rHi": round(lo + (rmax + 1) * bin_sz, 6),
                                "mx_usd": round(mx, 2),
                                "score": round(mx * (c_end - a + 1), 2),
                                "n": round(npk, 4),
                                "side": "short" if rpk > close_row else "long",
                                "active": c_end >= cols - 1,
                                "bars": int(c_end - a + 1),
                                "startTs": candles[a].get("ts"),
                                "endTs": candles[end_idx].get("ts"),
                            })
                    a = -1
                    empties = 0

    walls_raw.sort(key=lambda w: w["score"], reverse=True)
    top24 = walls_raw[:24]
    max_short = max((w for w in top24 if w["side"] == "short"), key=lambda w: w["mx_usd"], default=None)
    max_long = max((w for w in top24 if w["side"] == "long"), key=lambda w: w["mx_usd"], default=None)

    active_short = sorted([w for w in top24 if w["active"] and w["side"] == "short"], key=lambda w: w["price"])
    active_long = sorted([w for w in top24 if w["active"] and w["side"] == "long"], key=lambda w: w["price"], reverse=True)

    return {
        "total_walls_found": len(walls_raw),
        "walls_top24": top24,
        "max_short": max_short,
        "max_long": max_long,
        "active_short_prices": [w["price"] for w in active_short],
        "active_long_prices": [w["price"] for w in active_long],
        "active_count": len(active_short) + len(active_long),
        "swept_count": len(top24) - (len(active_short) + len(active_long)),
    }


def _consecutive_stacks(levels: List[Dict[str, Any]], tick: float, min_streak: int = 2) -> List[Dict[str, Any]]:
    """Detect contiguous price-ladder diagonal imbalance streaks (e.g., 3+ consecutive levels)."""
    if not levels or tick <= 0:
        return []
    ordered = sorted(levels, key=lambda x: x["price"])
    zones: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = [ordered[0]]
    for item in ordered[1:]:
        if abs(item["price"] - cur[-1]["price"] - tick) <= tick * 0.25:
            cur.append(item)
        else:
            if len(cur) >= min_streak:
                zones.append({
                    "lo": cur[0]["price"],
                    "hi": cur[-1]["price"],
                    "count": len(cur),
                    "total_usd": round(sum(x["usd"] for x in cur), 2),
                })
            cur = [item]
    if len(cur) >= min_streak:
        zones.append({
            "lo": cur[0]["price"],
            "hi": cur[-1]["price"],
            "count": len(cur),
            "total_usd": round(sum(x["usd"] for x in cur), 2),
        })
    return zones


def compute_diagonal_imbalances(fp: Optional[Dict[str, Any]], ratios: Iterable[float] = (3.0, 2.0)) -> Dict[str, Any]:
    """
    Exact Python port of `src/lib/cg/cg-signals.ts` `diagonalImbalances(fp, ratio)`.

    At each bucket `i`, compares `fp.b[i]` against `fp.s[i - 1]` (buyer diagonal imbalance)
    and `fp.s[i]` against `fp.b[i + 1]` (seller diagonal imbalance).
    """
    if not isinstance(fp, dict):
        return {
            "buy_3x_count": 0, "sell_3x_count": 0,
            "buy_2x_count": 0, "sell_2x_count": 0,
            "buy_3x_levels": [], "sell_3x_levels": [],
            "buy_stack_zones": [], "sell_stack_zones": [],
        }
    b_arr = fp.get("b") or []
    s_arr = fp.get("s") or []
    n = min(len(b_arr), len(s_arr))
    tick = float(fp.get("tick") or 0.0)
    lo = float(fp.get("lo") or 0.0)
    if n == 0 or tick <= 0:
        return {
            "buy_3x_count": 0, "sell_3x_count": 0,
            "buy_2x_count": 0, "sell_2x_count": 0,
            "buy_3x_levels": [], "sell_3x_levels": [],
            "buy_stack_zones": [], "sell_stack_zones": [],
        }

    b = np.asarray(b_arr[:n], dtype=np.float64)
    s = np.asarray(s_arr[:n], dtype=np.float64)
    prices = lo + (np.arange(n, dtype=np.float64) + 0.5) * tick

    diag_sell = np.empty(n, dtype=np.float64)
    diag_sell[0] = 0.0
    diag_sell[1:] = s[:-1]

    diag_buy = np.empty(n, dtype=np.float64)
    diag_buy[-1] = 0.0
    diag_buy[:-1] = b[1:]

    buy_ratios = b / np.maximum(diag_sell, 1e-9)
    sell_ratios = s / np.maximum(diag_buy, 1e-9)

    out: Dict[str, Any] = {}
    ratio_list = sorted({float(r) for r in ratios}, reverse=True)
    for r_val in ratio_list:
        tag = "3x" if abs(r_val - 3.0) < 1e-6 else ("2x" if abs(r_val - 2.0) < 1e-6 else f"{r_val:g}x")
        b_mask = (b > 0.0) & (b >= r_val * diag_sell) & (diag_sell >= 0.0) & (buy_ratios >= r_val)
        s_mask = (s > 0.0) & (s >= r_val * diag_buy) & (sell_ratios >= r_val)
        b_idx = np.flatnonzero(b_mask)
        s_idx = np.flatnonzero(s_mask)
        out[f"buy_{tag}_count"] = int(b_idx.size)
        out[f"sell_{tag}_count"] = int(s_idx.size)
        if tag == "3x":
            buy_rows = [
                {
                    "price": round(float(prices[i]), 6),
                    "usd": round(float(b[i]), 2),
                    "diag_opp": round(float(diag_sell[i]), 2),
                    "ratio": round(float(buy_ratios[i]), 2) if diag_sell[i] > 0 else None,
                }
                for i in b_idx[::-1]
            ]
            sell_rows = [
                {
                    "price": round(float(prices[i]), 6),
                    "usd": round(float(s[i]), 2),
                    "diag_opp": round(float(diag_buy[i]), 2),
                    "ratio": round(float(sell_ratios[i]), 2) if diag_buy[i] > 0 else None,
                }
                for i in s_idx[::-1]
            ]
            out["buy_3x_levels"] = buy_rows[:12]
            out["sell_3x_levels"] = sell_rows[:12]
            out["buy_stack_zones"] = _consecutive_stacks(buy_rows, tick, min_streak=2)
            out["sell_stack_zones"] = _consecutive_stacks(sell_rows, tick, min_streak=2)

    return out


def summarize_stopmap_payload(sm_resp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Distill Model 1/2/3 `/api/cg/stopmap` into full-fidelity JSON without storing
    the redundant 288x153 `columns` raw pixel tuple matrix in every asset file.
    """
    m = sm_resp.get("model") if isinstance(sm_resp.get("model"), dict) else sm_resp
    if not isinstance(m, dict):
        return {}

    y_axis = m.get("yAxis") or []
    is_grid_model = bool(y_axis and m.get("columns") and m.get("candles"))
    out: Dict[str, Any] = {
        "model": m.get("model"),
        "mode": m.get("mode") or sm_resp.get("reference"),
        "range": m.get("range"),
        "symbol": m.get("symbol") or sm_resp.get("symbol"),
        "source": sm_resp.get("source") or m.get("source"),
        "stale": sm_resp.get("stale"),
        "updateTime": m.get("updateTime") or sm_resp.get("updatedAt"),
        "lastPrice": m.get("lastPrice") or (m.get("overview") or {}).get("lastPrice"),
    }
    if is_grid_model:
        walls_summary = compute_stopmap_walls(m)
        out.update({
            "kind": "native_heatmap_grid",
            "priceStep": m.get("priceStep"),
            "rangeLow": m.get("rangeLow"),
            "rangeHigh": m.get("rangeHigh"),
            "instrument": m.get("instrument") or sm_resp.get("instrument"),
            "window": sm_resp.get("window"),
            "overview": m.get("overview"),
            "keyLevels": m.get("keyLevels"),
            "walls": walls_summary,
            "lastColumn": (m.get("lastColumn") or [])[-80:] if isinstance(m.get("lastColumn"), list) else m.get("lastColumn"),
            "profile": _downsample_list(m.get("profile"), 80),
            "cumulative": _downsample_list(m.get("cumulative"), 80),
            "yAxis_summary": {
                "count": len(y_axis),
                "lo": y_axis[0],
                "hi": y_axis[-1],
            },
        })
    else:
        # Model 2 / Model 3 or Rolling Multi-Venue Snapshot Fallback (omit bloated raw 2D grid matrix)
        raw_grid = m.get("grid")
        grid_meta = None
        if isinstance(raw_grid, list):
            grid_meta = {"rows": len(raw_grid)}
        elif isinstance(raw_grid, dict):
            grid_meta = {k: v for k, v in raw_grid.items() if not isinstance(v, (list, dict))}
        out.update({
            "kind": "rolling_snapshot" if "clusters" in m else "summary",
            "clusters": m.get("clusters"),
            "bands": m.get("bands"),
            "totals": m.get("totals"),
            "cumulative": _downsample_list(m.get("cumulative"), 80),
            "profile": _downsample_list(m.get("profile"), 80),
            "tail": _downsample_list(m.get("tail"), 40) if isinstance(m.get("tail"), list) else m.get("tail"),
            "grid_meta": grid_meta,
            "keyLevels": m.get("keyLevels"),
            "overview": m.get("overview"),
            "snapshots": m.get("snapshots"),
            "archivedSince": m.get("archivedSince"),
            "note": m.get("note"),
        })
    return {k: v for k, v in out.items() if v is not None}


def _downsample_list(val: Any, max_len: int = 80) -> Any:
    if not isinstance(val, list) or len(val) <= max_len:
        return val
    step = max(1, len(val) // max_len)
    sampled = val[::step]
    if val and sampled[-1] != val[-1]:
        sampled.append(val[-1])
    return sampled[:max_len]


def build_compact_bookwalls(bw_resp: Dict[str, Any], strip_tail: int = 2, max_ladder: int = 120) -> Dict[str, Any]:
    """
    Preserve FULL Book Walls UI data (`walls`, `events`, bounded `ladder`,
    `depth`, `imbalance`, `largestBid/Ask`, `grid`, latest `strip` columns,
    plus persistence metrics) while eliminating 1MB+ pixel-grid bloat.
    """
    m = bw_resp.get("model") if isinstance(bw_resp.get("model"), dict) else bw_resp
    if not isinstance(m, dict):
        return {}
    walls = m.get("walls") or []
    depth = m.get("depth") if isinstance(m.get("depth"), dict) else {}
    bid_depth = m.get("bidDepth") or depth.get("bidUsd")
    ask_depth = m.get("askDepth") or depth.get("askUsd")

    bid_walls = [w for w in walls if isinstance(w, dict) and w.get("side") == "b"]
    ask_walls = [w for w in walls if isinstance(w, dict) and w.get("side") == "a"]
    untouched_bids = [w for w in bid_walls if not w.get("touched")]
    untouched_asks = [w for w in ask_walls if not w.get("touched")]
    max_age_ms = max((w.get("ageMs") or 0 for w in walls if isinstance(w, dict)), default=0)

    strip = m.get("strip") or []
    compact_strip = strip[-strip_tail:] if (isinstance(strip, list) and strip_tail > 0) else []
    raw_ladder = m.get("ladder") or []
    if isinstance(raw_ladder, list) and len(raw_ladder) > max_ladder:
        mid_px = float(m.get("last") or 0.0)
        if mid_px > 0:
            compact_ladder = sorted(
                (r for r in raw_ladder if isinstance(r, dict) and r.get("price") is not None),
                key=lambda r: abs(float(r["price"]) - mid_px),
            )[:max_ladder]
        else:
            compact_ladder = raw_ladder[:max_ladder]
    else:
        compact_ladder = raw_ladder

    return {
        "supported": bw_resp.get("supported", True),
        "source": bw_resp.get("source"),
        "venues": bw_resp.get("venues"),
        "minWallUsd": bw_resp.get("minWallUsd"),
        "updatedAt": bw_resp.get("updatedAt"),
        "snapshots": m.get("snapshots"),
        "from": m.get("from"),
        "to": m.get("to"),
        "grid": m.get("grid"),
        "last": m.get("last"),
        "imbalance": m.get("imbalance"),
        "imbalance5mAgo": m.get("imbalance5mAgo"),
        "bidDepth": bid_depth,
        "askDepth": ask_depth,
        "largestBid": m.get("largestBid"),
        "largestAsk": m.get("largestAsk"),
        "walls": walls[:40],
        "events": (m.get("events") or [])[:30],
        "ladder_total": len(raw_ladder) if isinstance(raw_ladder, list) else 0,
        "ladder": compact_ladder,
        "strip_recent": compact_strip,
        "persistence": {
            "total_walls": len(walls),
            "bid_walls": len(bid_walls),
            "ask_walls": len(ask_walls),
            "untouched_bid_usd": round(sum(float(w.get("usd") or 0) for w in untouched_bids), 2),
            "untouched_ask_usd": round(sum(float(w.get("usd") or 0) for w in untouched_asks), 2),
            "max_wall_age_ms": max_age_ms,
        },
    }
