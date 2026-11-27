"""100% Coinglass Footprint API loader, bar constructor (Time/Volume/Range),
and exact line-by-line Python port of src/lib/cg/cg-signals.ts (ey(X)).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from footprint_Chart.data_pipeline.binance_dumper import DATA_DIR
from footprint_Chart.engines.footprint_engine import _compute_va, auto_row_size

CG_LOCAL_BASE = "http://127.0.0.1:3000"
_JSON_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _get_json(url: str, timeout: float = 12.0, ttl: float = 6.0) -> dict[str, Any]:
    now = time.time()
    cached = _JSON_CACHE.get(url)
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    req = urllib.request.Request(url, headers={"User-Agent": "footprint_Chart/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _JSON_CACHE[url] = (now, data)
        return data
    except Exception:
        if cached:
            return cached[1]
        raise


def _ladder_round(e: float) -> float:
    """Coinglass q(e): round to {1, 2, 2.5, 5, 10} * 10^k."""
    if not (e > 0):
        return 1.0
    t = 10.0 ** math.floor(math.log10(e))
    s = e / t
    mult = 1.0 if s <= 1.0 else (2.0 if s <= 2.0 else (2.5 if s <= 2.5 else (5.0 if s <= 5.0 else 10.0)))
    return mult * t


def fp_from_buckets(buckets: list[list[float | int | str]]) -> dict[str, Any] | None:
    """Exact port of eT(buckets) -> native grid {tick, lo, b, s, bu, su, bc, sc}."""
    if not buckets:
        return None
    lo = float("inf")
    tick = 0.0
    for bk in buckets:
        e = float(bk[0])
        l = float(bk[1])
        a = min(e, l)
        if a < lo:
            lo = a
        if tick == 0.0 and abs(l - e) > 0:
            tick = abs(l - e)
    if tick <= 0 or not math.isfinite(lo):
        return None

    rows: list[tuple[int, list]] = []
    n = 0
    for bk in buckets:
        idx = round((min(float(bk[0]), float(bk[1])) - lo) / tick)
        if idx + 1 > n:
            n = idx + 1
        rows.append((idx, bk))

    n = max(1, n)
    b = [0.0] * n
    s = [0.0] * n
    bu = [0.0] * n
    su = [0.0] * n
    bc = [0] * n
    sc = [0] * n
    for idx, bk in rows:
        b[idx] += float(bk[2])
        s[idx] += float(bk[3])
        bu[idx] += float(bk[4])
        su[idx] += float(bk[5])
        bc[idx] += int(float(bk[8])) if len(bk) > 8 else 0
        sc[idx] += int(float(bk[9])) if len(bk) > 9 else 0
    return {"tick": round(tick, 10), "lo": lo, "b": b, "s": s, "bu": bu, "su": su, "bc": bc, "sc": sc}


def merge_fp(fs: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """Exact port of eu(fs): merge native FPs onto coarsest tick grid."""
    lst = [f for f in fs if f]
    if not lst:
        return None
    tick = max(f["tick"] for f in lst)
    cells: dict[int, list[float]] = {}
    lo_r = 10**12
    hi_r = -(10**12)
    for f in lst:
        for e in range(len(f["b"])):
            if not (f["b"][e] or f["s"][e]):
                continue
            r = math.floor((f["lo"] + (e + 0.5) * f["tick"]) / tick + 1e-9)
            if r not in cells:
                cells[r] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            c = cells[r]
            c[0] += f["b"][e]
            c[1] += f["s"][e]
            c[2] += f["bu"][e]
            c[3] += f["su"][e]
            c[4] += f["bc"][e]
            c[5] += f["sc"][e]
            lo_r = min(lo_r, r)
            hi_r = max(hi_r, r)
    if not cells:
        return None
    length = hi_r - lo_r + 1
    b = [0.0] * length
    s = [0.0] * length
    bu = [0.0] * length
    su = [0.0] * length
    bc = [0] * length
    sc = [0] * length
    for r, vals in cells.items():
        i = r - lo_r
        b[i], s[i], bu[i], su[i] = vals[0], vals[1], vals[2], vals[3]
        bc[i], sc[i] = int(vals[4]), int(vals[5])
    return {"tick": tick, "lo": round(lo_r * tick, 12), "b": b, "s": s, "bu": bu, "su": su, "bc": bc, "sc": sc}


def auto_threshold(m1_bars: list[dict[str, Any]], bt: str) -> float:
    """Exact port of ef(S, bt): vol = 10x avg 1m vol (last 240); range = 0.25% last close."""
    if not m1_bars:
        return 1.0
    if bt == "vol":
        total = 0.0
        cnt = 0
        for bar in m1_bars[-240:]:
            fp = bar.get("fp")
            if fp:
                total += sum(fp["b"]) + sum(fp["s"])
                cnt += 1
        return _ladder_round((total / max(1, cnt)) * 10.0)
    return _ladder_round(0.0025 * float(m1_bars[-1]["c"]))


def build_derived_bars(m1_bars: list[dict[str, Any]], bt: str, thr: float) -> list[dict[str, Any]]:
    """Exact port of eg(S, bt, thr): build Volume or Range bars from m1 bars."""
    groups: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for bar in m1_bars:
        hi, lo = float(bar["h"]), float(bar["l"])
        if cur is not None:
            cur["h"] = max(cur["h"], hi)
            cur["l"] = min(cur["l"], lo)
            cur["c"] = float(bar["c"])
            if bar.get("oic") is not None:
                cur["oc"] = bar["oic"]
            if cur["oo"] is None:
                cur["oo"] = bar.get("oio")
        else:
            cur = {
                "ts": bar["ts"], "o": float(bar["o"]), "h": hi, "l": lo, "c": float(bar["c"]),
                "fs": [], "oo": bar.get("oio"), "oc": bar.get("oic"), "v": 0.0,
            }
        fp = bar.get("fp")
        if fp:
            cur["fs"].append(fp)
            cur["v"] += sum(fp["b"]) + sum(fp["s"])
        if (cur["v"] >= thr) if bt == "vol" else ((cur["h"] - cur["l"]) >= thr):
            groups.append(cur)
            cur = None
    if cur is not None:
        groups.append(cur)

    out: list[dict[str, Any]] = []
    for g in groups:
        fp = merge_fp(g["fs"])
        if not fp:
            continue
        out.append({"ts": g["ts"], "o": g["o"], "h": g["h"], "l": g["l"], "c": g["c"], "fp": fp, "oio": g["oo"], "oic": g["oc"]})
    return out


def _fmt_qty(v: float) -> str:
    s = abs(v)
    if s == 0:
        return "0"
    sign = "-" if v < 0 else ""
    if s >= 1e6:
        return sign + (f"{s/1e6:.0f}M" if s >= 1e7 else f"{s/1e6:.1f}M")
    if s >= 1e4:
        return sign + f"{s/1e3:.0f}K"
    if s >= 1e3:
        return sign + f"{s/1e3:.1f}K"
    if s >= 100:
        return sign + f"{s:.0f}"
    if s >= 10:
        return sign + f"{s:.1f}"
    if s >= 0.01:
        return sign + f"{s:.2f}"
    return sign + "<.01"


def compute_cg_signals(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exact 1:1 Python port of computeCgSignals(X) + cgSigText from src/lib/cg/cg-signals.ts (ey)."""
    from datetime import datetime, timezone

    t = len(bars)
    sigs: list[dict[str, Any]] = []
    vol = [0.0] * t
    for k_idx, bar in enumerate(bars):
        fp = bar.get("fp")
        if fp:
            vol[k_idx] = sum(fp["b"]) + sum(fp["s"])

    for l in range(5, t - 1):
        a = bars[l].get("fp")
        if not a or vol[l] <= 0:
            continue
        prior = sorted(vol[e] for e in range(max(0, l - 20), l) if vol[e] > 0)
        if len(prior) < 5:
            continue
        if vol[l] < 0.8 * prior[len(prior) >> 1]:
            continue

        b_arr, s_arr = a["b"], a["s"]
        n = len(b_arr)
        o_vol = vol[l]
        bar_delta = 0.0
        tot_b = 0.0
        tot_s = 0.0
        p_top = -1
        u_bot = -1
        for e in range(n):
            tt = b_arr[e] + s_arr[e]
            bar_delta += b_arr[e] - s_arr[e]
            tot_b += b_arr[e]
            tot_s += s_arr[e]
            if tt > 0:
                if u_bot < 0:
                    u_bot = e
                p_top = e
        if p_top < 0:
            continue

        f_open = float(bars[l]["o"])
        g_high = float(bars[l]["h"])
        m_low = float(bars[l]["l"])
        b_close = float(bars[l]["c"])
        rng = g_high - m_low
        close_pos = (b_close - m_low) / rng if rng > 0 else 0.5
        mid_px = (g_high + m_low) / 2.0
        w_rows = p_top - u_bot + 1
        row_px = lambda e: a["lo"] + e * a["tick"]

        # div: single-bar divergence
        if b_close > f_open and bar_delta < 0 and -bar_delta >= 0.15 * o_vol:
            r_val = round(bar_delta / o_vol, 4)
            pct = abs(round(100 * r_val))
            sigs.append({
                "i": l, "t": "div", "side": "sell", "p": g_high, "r": r_val, "ts": bars[l]["ts"],
                "name": "Divergence", "title": "Rally without taker buying",
                "desc": f"Closed up on negative Delta (-{pct}%), bearish",
            })
        if b_close < f_open and bar_delta > 0 and bar_delta >= 0.15 * o_vol:
            r_val = round(bar_delta / o_vol, 4)
            pct = abs(round(100 * r_val))
            sigs.append({
                "i": l, "t": "div", "side": "buy", "p": m_low, "r": r_val, "ts": bars[l]["ts"],
                "name": "Divergence", "title": "Drop without taker selling",
                "desc": f"Closed down on positive Delta (+{pct}%), bullish",
            })

        if w_rows < 5 or rng <= 0:
            continue

        zone_n = max(2, round(0.2 * w_rows))
        t_top_b = sum(b_arr[p_top - zone_n + 1 : p_top + 1])
        s_top_s = sum(s_arr[p_top - zone_n + 1 : p_top + 1])
        j_bot_b = sum(b_arr[u_bot : u_bot + zone_n])
        c_bot_s = sum(s_arr[u_bot : u_bot + zone_n])

        # abs: absorption
        if tot_s > 0 and (c_bot_s / tot_s) >= 0.3 and c_bot_s > 1.2 * j_bot_b and close_pos >= 0.6:
            sigs.append({
                "i": l, "t": "abs", "side": "buy", "p": m_low, "q": c_bot_s, "u": c_bot_s * mid_px,
                "lo": row_px(u_bot), "hi": row_px(u_bot + zone_n), "ts": bars[l]["ts"],
                "name": "Absorption", "title": "Selling absorbed at the low",
                "desc": f"{_fmt_qty(c_bot_s)} of taker selling at the low (30%+ of the bar's selling), yet closed near the high, bullish",
            })
        if tot_b > 0 and (t_top_b / tot_b) >= 0.3 and t_top_b > 1.2 * s_top_s and close_pos <= 0.4:
            sigs.append({
                "i": l, "t": "abs", "side": "sell", "p": g_high, "q": t_top_b, "u": t_top_b * mid_px,
                "lo": row_px(p_top - zone_n + 1), "hi": row_px(p_top + 1), "ts": bars[l]["ts"],
                "name": "Absorption", "title": "Buying absorbed at the high",
                "desc": f"{_fmt_qty(t_top_b)} of taker buying at the high (30%+ of the bar's buying), yet closed near the low, bearish",
            })

        # exh: exhaustion
        prev_hi = max(float(bars[e]["h"]) for e in range(max(0, l - 10), l))
        prev_lo = min(float(bars[e]["l"]) for e in range(max(0, l - 10), l))
        d_rows = max(1, round(0.1 * w_rows))
        top_ext = sum(b_arr[e] + s_arr[e] for e in range(p_top - d_rows + 1, p_top + 1))
        bot_ext = sum(b_arr[e] + s_arr[e] for e in range(u_bot, u_bot + d_rows))
        if g_high > prev_hi and top_ext <= 0.02 * o_vol and close_pos <= 0.5:
            sigs.append({
                "i": l, "t": "exh", "side": "sell", "p": g_high, "ts": bars[l]["ts"],
                "name": "Exhaustion", "title": "Buyer exhaustion",
                "desc": "New 10-bar high with almost no volume in the top 10% of the bar, closed in the lower half, bearish",
            })
        if m_low < prev_lo and bot_ext <= 0.02 * o_vol and close_pos >= 0.5:
            sigs.append({
                "i": l, "t": "exh", "side": "buy", "p": m_low, "ts": bars[l]["ts"],
                "name": "Exhaustion", "title": "Seller exhaustion",
                "desc": "New 10-bar low with almost no volume in the bottom 10% of the bar, closed in the upper half, bullish",
            })

        # trap: trapped traders
        p5 = max(float(bars[e]["h"]) for e in range(max(0, l - 5), l))
        a5 = min(float(bars[e]["l"]) for e in range(max(0, l - 5), l))
        if g_high > p5 and tot_b > 0 and (t_top_b / tot_b) >= 0.3 and t_top_b > 1.5 * s_top_s:
            for i2 in range(l + 1, min(l + 4, t - 1)):
                if float(bars[i2]["h"]) > g_high:
                    break
                if float(bars[i2]["c"]) <= m_low + 0.25 * rng:
                    sigs.append({
                        "i": l, "t": "trap", "side": "sell", "p": g_high, "j": i2, "q": t_top_b, "u": t_top_b * mid_px,
                        "lo": row_px(p_top - zone_n + 1), "hi": row_px(p_top + 1), "ts": bars[l]["ts"],
                        "name": "Trapped longs", "title": "Failed breakout",
                        "desc": f"Broke the 5-bar high on {_fmt_qty(t_top_b)} of taker buying at the top, then fell back into the lower part of the bar {i2 - l} bars later. Breakout buyers trapped, bearish",
                    })
                    break
        if m_low < a5 and tot_s > 0 and (c_bot_s / tot_s) >= 0.3 and c_bot_s > 1.5 * j_bot_b:
            for i2 in range(l + 1, min(l + 4, t - 1)):
                if float(bars[i2]["l"]) < m_low:
                    break
                if float(bars[i2]["c"]) >= g_high - 0.25 * rng:
                    sigs.append({
                        "i": l, "t": "trap", "side": "buy", "p": m_low, "j": i2, "q": c_bot_s, "u": c_bot_s * mid_px,
                        "lo": row_px(u_bot), "hi": row_px(u_bot + zone_n), "ts": bars[l]["ts"],
                        "name": "Trapped shorts", "title": "Failed breakdown",
                        "desc": f"Broke the 5-bar low on {_fmt_qty(c_bot_s)} of taker selling at the bottom, then rallied back into the upper part of the bar {i2 - l} bars later. Breakdown sellers trapped, bullish",
                    })
                    break

    # sdiv: swing divergence on CVD
    cvd = [0.0] * t
    acc = 0.0
    for k_idx, bar in enumerate(bars):
        fp = bar.get("fp")
        if fp:
            acc += sum(fp["b"]) - sum(fp["s"])
        cvd[k_idx] = acc

    last_hi = -1
    last_lo = -1
    for a in range(3, t - 4):
        is_hi = True
        is_lo = True
        for e in range(a - 3, a + 4):
            if e == a:
                continue
            if (float(bars[e]["h"]) >= float(bars[a]["h"])) if e < a else (float(bars[e]["h"]) > float(bars[a]["h"])):
                is_hi = False
            if (float(bars[e]["l"]) <= float(bars[a]["l"])) if e < a else (float(bars[e]["l"]) < float(bars[a]["l"])):
                is_lo = False
        if not is_hi and not is_lo:
            continue
        prior = sorted(vol[e] for e in range(max(0, a - 20), a) if vol[e] > 0)
        thr = 0.25 * (prior[len(prior) >> 1] if prior else 0.0)
        if is_hi:
            if last_hi >= 0 and a - last_hi <= 40 and float(bars[a]["h"]) > float(bars[last_hi]["h"]) and (cvd[last_hi] - cvd[a]) > thr:
                dc = cvd[a] - cvd[last_hi]
                t0 = datetime.fromtimestamp(bars[last_hi]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                sigs.append({
                    "i": a, "t": "sdiv", "side": "sell", "p": float(bars[a]["h"]),
                    "i0": last_hi, "p0": float(bars[last_hi]["h"]), "dc": dc, "ts": bars[a]["ts"],
                    "name": "Bear div", "title": "Higher high, lower Cumulative Delta",
                    "desc": f"Higher high than {t0} UTC, yet Cumulative Delta fell {_fmt_qty(abs(dc))} over the swing. Buyers aren't following through, bearish",
                })
            last_hi = a
        if is_lo:
            if last_lo >= 0 and a - last_lo <= 40 and float(bars[a]["l"]) < float(bars[last_lo]["l"]) and (cvd[a] - cvd[last_lo]) > thr:
                dc = cvd[a] - cvd[last_lo]
                t0 = datetime.fromtimestamp(bars[last_lo]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                sigs.append({
                    "i": a, "t": "sdiv", "side": "buy", "p": float(bars[a]["l"]),
                    "i0": last_lo, "p0": float(bars[last_lo]["l"]), "dc": dc, "ts": bars[a]["ts"],
                    "name": "Bull div", "title": "Lower low, higher Cumulative Delta",
                    "desc": f"Lower low than {t0} UTC, yet Cumulative Delta rose {_fmt_qty(abs(dc))} over the swing. Sellers aren't following through, bullish",
                })
            last_lo = a

    sigs.sort(key=lambda x: x["i"])
    return sigs


def _fetch_raw_series(symbol: str, cg_itv: str, limit: int) -> list[dict[str, Any]]:
    inst = f"Binance_{symbol.upper()}"
    bars: list[dict[str, Any]] = []
    try:
        of_res = _get_json(f"{CG_LOCAL_BASE}/api/cg/orderflow?symbol={urllib.parse.quote(inst)}&interval={cg_itv}&limit={limit}")
        kl_res = _get_json(f"{CG_LOCAL_BASE}/api/cg/kline?symbol={urllib.parse.quote(inst)}&type=kline&interval={cg_itv}&limit={limit}")
        try:
            oi_res = _get_json(f"{CG_LOCAL_BASE}/api/cg/kline?symbol={urllib.parse.quote(inst)}&type=oi&interval={cg_itv}&limit={limit}")
        except Exception:
            oi_res = {"rows": []}

        k_map = {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in (kl_res.get("rows") or [])}
        oi_map = {int(r[0]): (float(r[1]), float(r[4])) for r in (oi_res.get("rows") or [])}

        for item in of_res.get("bars") or []:
            ts = int(item["ts"])
            fp = fp_from_buckets(item["buckets"])
            if not fp:
                continue
            if ts in k_map:
                o, h, l, c = k_map[ts]
            else:
                o = h = l = c = fp["lo"] + (len(fp["b"]) * 0.5) * fp["tick"]
            oio, oic = oi_map.get(ts, (None, None))
            bars.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "fp": fp, "oio": oio, "oic": oic})
    except Exception:
        base = symbol.upper().removesuffix("USDT").removesuffix("USD")
        fp_res = _get_json(f"{CG_LOCAL_BASE}/api/cg/fp?sym={urllib.parse.quote(base)}&itv={cg_itv}&limit={min(limit, 300)}")
        for item in fp_res.get("bars") or []:
            raw_fp = item.get("fp")
            if not raw_fp or not raw_fp.get("b"):
                continue
            tick = float(raw_fp["tick"])
            lo = float(raw_fp["lo"])
            b_arr = [float(v) for v in raw_fp["b"]]
            s_arr = [float(v) for v in raw_fp["s"]]
            bc_arr = [int(v) for v in (raw_fp.get("bc") or [0] * len(b_arr))]
            sc_arr = [int(v) for v in (raw_fp.get("sc") or [0] * len(s_arr))]
            bu_arr = [float(v) for v in raw_fp["bu"]] if "bu" in raw_fp else [b_arr[i] * (lo + (i + 0.5) * tick) for i in range(len(b_arr))]
            su_arr = [float(v) for v in raw_fp["su"]] if "su" in raw_fp else [s_arr[i] * (lo + (i + 0.5) * tick) for i in range(len(s_arr))]
            fp = {"tick": tick, "lo": lo, "b": b_arr, "s": s_arr, "bu": bu_arr, "su": su_arr, "bc": bc_arr, "sc": sc_arr}
            bars.append({
                "ts": int(item["ts"]),
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
                "fp": fp,
                "oio": item.get("oio"),
                "oic": item.get("oic"),
            })
        if not bars:
            raise
    bars.sort(key=lambda x: x["ts"])
    return bars


def _load_m1_from_parquet(symbol: str, max_bars: int = 1000, data_dir: Path = DATA_DIR) -> list[dict[str, Any]]:
    """Build native m1 fp bars from local Binance parquet when upstream m1 orderflow is rate-limited."""
    path = data_dir / f"{symbol.upper()}_agg_footprint.parquet"
    if not path.is_file():
        return []
    df = pq.read_table(path).to_pandas()
    if df.empty:
        return []
    bars_ts = sorted(df["bar_1m"].unique())[-max_bars:]
    sub = df[df["bar_1m"].isin(bars_ts)].sort_values(["bar_1m", "price_bucket"])
    if sub.empty:
        return []
    tick = float(sub["price_bucket"].diff().dropna().loc[lambda s: s > 0].min() or 5.0)
    out: list[dict[str, Any]] = []
    for ts, grp in sub.groupby("bar_1m", sort=True):
        lo = float(grp["price_bucket"].min())
        hi_p = float(grp["price_bucket"].max())
        n = max(1, round((hi_p - lo) / tick) + 1)
        b_arr, s_arr = [0.0] * n, [0.0] * n
        bu_arr, su_arr = [0.0] * n, [0.0] * n
        bc_arr, sc_arr = [0] * n, [0] * n
        for row in grp.itertuples(index=False):
            idx = max(0, min(n - 1, round((float(row.price_bucket) - lo) / tick)))
            b_arr[idx] += float(row.buy_qty)
            s_arr[idx] += float(row.sell_qty)
            bu_arr[idx] += float(row.buy_usd)
            su_arr[idx] += float(row.sell_usd)
            bc_arr[idx] += int(row.buy_trades)
            sc_arr[idx] += int(row.sell_trades)
        first = grp.iloc[0]
        fp = {"tick": tick, "lo": lo, "b": b_arr, "s": s_arr, "bu": bu_arr, "su": su_arr, "bc": bc_arr, "sc": sc_arr}
        out.append({
            "ts": int(ts),
            "o": float(first["open"]),
            "h": float(first["high"]),
            "l": float(first["low"]),
            "c": float(first["close"]),
            "fp": fp,
            "oio": None,
            "oic": None,
        })
    return out


def _intrabar_delta_map(symbol: str, tf_sec: int, unit: str, data_dir: Path = DATA_DIR) -> dict[int, tuple[float, float]]:
    """Compute time-ordered intrabar Max Delta / Min Delta excursions from local 1m parquet."""
    path = data_dir / f"{symbol.upper()}_agg_footprint.parquet"
    if not path.is_file():
        return {}
    cols = ["bar_1m", "buy_qty", "sell_qty", "buy_usd", "sell_usd", "buy_trades", "sell_trades"]
    try:
        df = pq.read_table(path, columns=cols).to_pandas()
    except Exception:
        return {}
    if df.empty:
        return {}
    # Keep only the recent 5 days to stay fast (<20ms)
    cutoff = int(df["bar_1m"].max()) - 5 * 86400
    df = df[df["bar_1m"] >= cutoff]
    if unit == "coin":
        df["d"] = df["buy_qty"] - df["sell_qty"]
    elif unit == "usd":
        df["d"] = df["buy_usd"] - df["sell_usd"]
    else:
        df["d"] = df["buy_trades"].astype(float) - df["sell_trades"].astype(float)
    m1 = df.groupby("bar_1m", as_index=False)["d"].sum().sort_values("bar_1m")
    m1["bar_ts"] = (m1["bar_1m"] // tf_sec) * tf_sec
    res: dict[int, tuple[float, float]] = {}
    for b_ts, grp in m1.groupby("bar_ts", sort=False):
        cum = 0.0
        mx = 0.0
        mn = 0.0
        for val in grp["d"]:
            cum += float(val)
            if cum > mx:
                mx = cum
            if cum < mn:
                mn = cum
        res[int(b_ts)] = (mx, mn)
    return res


def load_coinglass_parity(
    symbol: str = "BTCUSDT",
    timeframe: str = "15m",
    bar_type: str = "time",  # "time" | "vol" | "range"
    unit: str = "coin",  # "coin" | "usd" | "trades"
    row_size: float | None = None,
    imbalance_ratio: float = 3.0,
    limit: int = 80,
) -> dict[str, Any]:
    """Build Coinglass-identical bars, VPVR, zero-auctions, and exact Coinglass signals."""
    tf_map = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "1h": "h1", "4h": "h4"}
    tf_sec_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
    cg_itv = tf_map.get(timeframe, "m15")
    tf_sec = tf_sec_map.get(timeframe, 900)

    if bar_type in ("vol", "range"):
        try:
            m1_raw = _fetch_raw_series(symbol, "m1", 300)
        except Exception:
            m1_raw = []
        if len(m1_raw) < 30:
            m1_raw = _load_m1_from_parquet(symbol, max_bars=1000)
        thr = auto_threshold(m1_raw, bar_type)
        raw_bars = build_derived_bars(m1_raw, bar_type, thr)[-max(limit, 300):]
    else:
        raw_bars = _fetch_raw_series(symbol, cg_itv, max(limit, 300))
        thr = None

    intrabar_ext = _intrabar_delta_map(symbol, tf_sec, unit) if bar_type == "time" else {}

    # Coinglass exact signals on native Coin volume grid BEFORE display truncation/re-bucketing
    cg_sigs = compute_cg_signals(raw_bars)
    raw_bars = raw_bars[-limit:]
    keep_ts = {b["ts"] for b in raw_bars}
    cg_sigs = [s for s in cg_sigs if s["ts"] in keep_ts]

    eff_row = float(row_size) if (row_size is not None and row_size > 0) else (
        20.0 if (raw_bars and raw_bars[-1]["c"] >= 20000 and timeframe == "15m") else (
            5.0 if (raw_bars and raw_bars[-1]["c"] >= 20000) else max(round(raw_bars[-1]["c"] * 0.0005, 4), 0.01) if raw_bars else 20.0
        )
    )

    out_bars: list[dict[str, Any]] = []
    vpvr_map: dict[float, dict[str, float]] = {}

    for b in raw_bars:
        fp = b["fp"]
        buckets_by_lvl: dict[float, dict[str, float]] = {}
        for idx in range(len(fp["b"])):
            p_native = fp["lo"] + idx * fp["tick"]
            p_lvl = round(math.floor(p_native / eff_row + 1e-9) * eff_row, 6)
            cell = buckets_by_lvl.setdefault(p_lvl, {"bc": 0.0, "sc": 0.0, "bu": 0.0, "su": 0.0, "bt": 0, "st": 0})
            cell["bc"] += fp["b"][idx]
            cell["sc"] += fp["s"][idx]
            cell["bu"] += fp["bu"][idx]
            cell["su"] += fp["su"][idx]
            cell["bt"] += fp["bc"][idx]
            cell["st"] += fp["sc"][idx]

        levels: list[dict[str, Any]] = []
        large_cells = 0
        max_cum_d = 0.0
        min_cum_d = 0.0
        cum_d = 0.0
        for p_lvl in sorted(buckets_by_lvl.keys()):
            c = buckets_by_lvl[p_lvl]
            if unit == "coin":
                bv, sv = c["bc"], c["sc"]
            elif unit == "usd":
                bv, sv = c["bu"], c["su"]
            else:
                bv, sv = float(c["bt"]), float(c["st"])
            if bv == 0 and sv == 0:
                continue
            tot = bv + sv
            dlt = bv - sv
            cum_d += dlt
            max_cum_d = max(max_cum_d, dlt)
            min_cum_d = min(min_cum_d, dlt)
            if (c["bu"] + c["su"]) >= 1_000_000:
                large_cells += 1
            levels.append({
                "price": p_lvl,
                "buy": round(bv, 3 if unit == "coin" else 2),
                "sell": round(sv, 3 if unit == "coin" else 2),
                "total": round(tot, 3 if unit == "coin" else 2),
                "delta": round(dlt, 3 if unit == "coin" else 2),
                "buy_trades": int(c["bt"]),
                "sell_trades": int(c["st"]),
                "buy_imb": False,
                "sell_imb": False,
                "zero_top": False,
                "zero_bot": False,
            })
            vp = vpvr_map.setdefault(p_lvl, {"price": p_lvl, "buy": 0.0, "sell": 0.0, "total": 0.0})
            vp["buy"] += bv
            vp["sell"] += sv
            vp["total"] += tot

        buy_imb_cnt = 0
        sell_imb_cnt = 0
        for idx in range(len(levels)):
            if idx > 0:
                ps = levels[idx - 1]["sell"]
                if levels[idx]["buy"] > 0 and ((ps > 0 and levels[idx]["buy"] >= imbalance_ratio * ps) or (ps == 0 and levels[idx]["buy"] >= 5)):
                    levels[idx]["buy_imb"] = True
                    buy_imb_cnt += 1
            if idx < len(levels) - 1:
                nb = levels[idx + 1]["buy"]
                if levels[idx]["sell"] > 0 and ((nb > 0 and levels[idx]["sell"] >= imbalance_ratio * nb) or (nb == 0 and levels[idx]["sell"] >= 5)):
                    levels[idx]["sell_imb"] = True
                    sell_imb_cnt += 1

        zero_top = bool(levels and levels[-1]["sell"] == 0 and levels[-1]["buy"] > 0)
        zero_bot = bool(levels and levels[0]["buy"] == 0 and levels[0]["sell"] > 0)
        if zero_top:
            levels[-1]["zero_top"] = True
        if zero_bot:
            levels[0]["zero_bot"] = True

        poc, val, vah = _compute_va(levels, "total")
        bar_buy = sum(x["buy"] for x in levels)
        bar_sell = sum(x["sell"] for x in levels)
        bar_vol = bar_buy + bar_sell
        bar_delta = bar_buy - bar_sell
        if b["ts"] in intrabar_ext:
            ib_mx, ib_mn = intrabar_ext[b["ts"]]
            max_cum_d = max(ib_mx, bar_delta, 0.0)
            min_cum_d = min(ib_mn, bar_delta, 0.0)
        else:
            max_cum_d = max(bar_delta, 0.0)
            min_cum_d = min(bar_delta, 0.0)
        coin_vol = sum(fp["b"]) + sum(fp["s"])
        bar_trades = sum(fp["bc"]) + sum(fp["sc"])
        avg_size = (coin_vol / bar_trades) if bar_trades > 0 else 0.0
        oi_chg = round(float(b["oic"]) - float(b["oio"]), 1) if (b.get("oic") is not None and b.get("oio") is not None) else None

        out_bars.append({
            "time": b["ts"],
            "open": round(b["o"], 2),
            "high": round(b["h"], 2),
            "low": round(b["l"], 2),
            "close": round(b["c"], 2),
            "volume": round(bar_vol, 3 if unit == "coin" else 2),
            "buy_vol": round(bar_buy, 3 if unit == "coin" else 2),
            "sell_vol": round(bar_sell, 3 if unit == "coin" else 2),
            "delta": round(bar_delta, 3 if unit == "coin" else 2),
            "delta_pct": round((bar_delta / bar_vol * 100.0) if bar_vol > 0 else 0.0, 1),
            "max_delta": round(max_cum_d, 2),
            "min_delta": round(min_cum_d, 2),
            "trades": int(bar_trades),
            "avg_size": round(avg_size, 4),
            "oi_change": oi_chg,
            "large_cells": large_cells,
            "poc": poc,
            "val": val,
            "vah": vah,
            "buy_imbalances": buy_imb_cnt,
            "sell_imbalances": sell_imb_cnt,
            "zero_top": zero_top,
            "zero_bot": zero_bot,
            "levels": levels,
        })

    # Re-index cg_sigs relative to sliced out_bars
    ts_to_idx = {b["time"]: idx for idx, b in enumerate(out_bars)}
    for s in cg_sigs:
        s["bar_index"] = ts_to_idx.get(s["ts"], -1)

    vpvr_levels = sorted(vpvr_map.values(), key=lambda x: x["price"])
    vp_poc, vp_val, vp_vah = _compute_va(vpvr_levels, "total")
    return {
        "source": "coinglass_live",
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "bar_type": bar_type,
        "bar_threshold": thr,
        "unit": unit,
        "row_size": eff_row,
        "updated_at": int(time.time()),
        "bars": out_bars,
        "cg_signals": [s for s in cg_sigs if s["bar_index"] >= 0],
        "vpvr": {"poc": vp_poc, "val": vp_val, "vah": vp_vah, "levels": vpvr_levels},
    }
