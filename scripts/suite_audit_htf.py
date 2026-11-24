#!/usr/bin/env python3
"""Red-team audit of htf_ema_rejection (15-pair basket) — independent re-implementation.

Standards (same as VC/ORB audits):
  - digits inferred per symbol (mode of decimals), spread = data points x 10^-digits
  - ENFORCED non-zero spread: zero-spread bars replaced by the symbol's median
    non-zero spread; charged BOTH sides (entry + exit bar)
  - degenerate zero-range bars excluded from signal generation
  - chronological 70/30 split BY SESSION DAYS; selection on IS only, OOS once
  - two selections compared: (a) their frozen basket_picks cell, (b) my own
    IS-only plateau selection over a pre-registered grid
  - pooled OOS daily-R t-stat + deflation (Harvey haircut) vs search-space size
  - gold-concentration split (gold family vs ex-gold)
Outputs: printed tables + scripts/suite_audit_out/htf_audit_cells.csv
"""
import math
import os

import numpy as np
import pandas as pd

ROOT = "/home/z/my-project"
PQ = os.path.join(ROOT, "historical_parquet")
OUT = os.path.join(ROOT, "scripts", "suite_audit_out")
os.makedirs(OUT, exist_ok=True)

BASKET = [  # symbol, tf, period, pattern, slope, regime, exit  (from results/basket_picks.csv)
    ("XAUAUD.pi", "4H", 233, "P4_cluster", True, False, "fixed_4.0r"),
    ("XAUCNH.pi", "4H", 233, "P4_cluster", False, True, "trail_0.5r"),
    ("XAUUSD.pi", "4H", 233, "P4_cluster", False, False, "fixed_2.0r"),
    ("XAUEUR.pi", "4H", 233, "P4_cluster", True, False, "fixed_1.5r"),
    ("XAUSGD.pi", "4H", 233, "P4_cluster", True, False, "fixed_3.5r"),
    ("XAUGBP.pi", "4H", 233, "P4_cluster", False, False, None),  # fill below if needed
    ("CHFJPY.pi", "4H", 89, "P4_cluster", None, None, None),
    ("GBPJPY.pi", "4H", 233, "P2_two_bar", None, None, None),
    ("USDJPY.pi", "4H", 233, "P2_two_bar", None, None, None),
    ("EURJPY.pi", "4H", 233, "P4_cluster", None, None, None),
    ("CADJPY.pi", "4H", 233, "P1_wick", None, None, None),
    ("BTCUSD.pi", "4H", 89, "P4_cluster", None, None, None),
    ("GAUUSD.pi", "4H", 233, "P4_cluster", None, None, None),
    ("GAUCNH.pi", "4H", 233, "P4_cluster", None, None, None),
    ("DJ30.p", "1H", 233, "P2_two_bar", None, None, None),
]
GOLD_FAMILY = {"XAUAUD.pi", "XAUCNH.pi", "XAUUSD.pi", "XAUEUR.pi", "XAUSGD.pi",
               "XAUGBP.pi", "GAUUSD.pi", "GAUCNH.pi"}

EXITS_GRID = ["fixed_1.5r", "fixed_2.0r", "fixed_2.5r", "fixed_3.0r", "fixed_3.5r",
              "fixed_4.0r", "trail_0.5r", "trail_1.0r", "trail_1.5r"]
PATTERNS = ["P1_wick", "P2_two_bar", "P3_pierce", "P4_cluster"]
PERIODS = [89, 233]
SLOPES = [True, False]
REGIMES = [True, False]


# ---------------------------------------------------------------- data ------
def infer_digits(close: pd.Series) -> int:
    s = close.astype(str).str.extract(r"\.(\d+)")[0].fillna("")
    s = s.str.rstrip("0")
    dd = s.str.len()
    if not len(dd):
        return 2
    return int(min(int(dd.mode().iloc[0]), 8))


def load_symbol(tf: str, symbol: str) -> pd.DataFrame | None:
    p = os.path.join(PQ, tf, symbol + ".parquet")
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)[["datetime", "open", "high", "low", "close", "spread"]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.sort_values("datetime").reset_index(drop=True)
    dg = infer_digits(df["close"])
    df["digits"] = dg
    sp = df["spread"].to_numpy(float) * 10.0 ** -dg      # price units
    nz = sp[sp > 0]
    floor = float(np.median(nz)) if len(nz) else 0.0
    df["spr"] = np.where(sp > 0, sp, floor)              # enforced non-zero
    df["spr_raw"] = sp
    return df


def wilder_atr(h, l, c, period=14):
    n = len(c)
    atr = np.zeros(n)
    if n <= period:
        return atr
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def ema(c, period):
    n = len(c)
    out = np.zeros(n)
    if n < period:
        return out
    out[period - 1] = c[:period].mean()
    a = 2.0 / (period + 1.0)
    for i in range(period, n):
        out[i] = a * c[i] + (1 - a) * out[i - 1]
    return out


# ------------------------------------------------------------- signals ------
def cluster_break(o, h, l, c, atr, i, level, side_bias):
    if i < 3:
        return 0.0, 0.0
    tol = max(1e-5, 0.20 * atr[i])
    ch, cl, cc = h[i - 3:i], l[i - 3:i], c[i - 3:i]
    if side_bias == 1:
        touches = int((cl >= level - tol).sum())
        if touches >= 2 and np.min(cc) >= level - 0.15 * atr[i] and c[i] > np.max(ch):
            struct = np.min(cl) - 0.20 * atr[i]
            return struct, max(c[i] - struct, 1.2 * atr[i])
    else:
        touches = int((ch <= level + tol).sum())
        if touches >= 2 and np.max(cc) <= level + 0.15 * atr[i] and c[i] < np.min(cl):
            struct = np.max(ch) + 0.20 * atr[i]
            return struct, max(struct - c[i], 1.2 * atr[i])
    return 0.0, 0.0


def precompute(df, period, pattern, slope_gate=True, regime_gate=False):
    """Port of htf_reject.precompute_htf; zero-range bars skipped."""
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    rng_ok = (h - l) > 0
    atr = wilder_atr(h, l, c, 14)
    ef = ema(c, period)
    es = ema(c, 233) if regime_gate else None
    n = len(c)
    sides = np.zeros(n, np.int8); rds = np.zeros(n)
    start = max(20, period + 10, 233 + 10 if regime_gate else 0)
    tol_k = 0.20
    for i in range(start, n - 1):
        if not rng_ok[i]:
            continue
        e = ef[i]
        if e <= 0:
            continue
        rising = e > ef[i - 5] if i >= 5 else False
        falling = e < ef[i - 5] if i >= 5 else False
        if slope_gate and not (rising or falling):
            continue
        tol = tol_k * atr[i]
        side = 0
        if pattern == "P1_wick":
            rng = max(1e-6, h[i] - l[i])
            bt, bb = max(o[i], c[i]), min(o[i], c[i])
            lw, uw = (bb - l[i]) / rng, (h[i] - bt) / rng
            if rising and l[i] <= e + tol and c[i] >= e - tol and lw >= 0.35 and c[i] > o[i]:
                side = 1
            elif falling and h[i] >= e - tol and c[i] <= e + tol and uw >= 0.35 and c[i] < o[i]:
                side = -1
            if side:
                sl = min(l[i], e) - tol if side == 1 else max(h[i], e) + tol
                rds[i] = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P2_two_bar":
            mid1 = (o[i - 1] + c[i - 1]) / 2.0
            if rising and l[i - 1] <= ef[i - 1] + tol and c[i] > ef[i] and c[i] > o[i] and c[i] > mid1:
                side = 1
            elif falling and h[i - 1] >= ef[i - 1] - tol and c[i] < ef[i] and c[i] < o[i] and c[i] < mid1:
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rds[i] = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P3_pierce":
            if rising and c[i - 1] < ef[i - 1] - tol and c[i] > ef[i] + tol and c[i] > o[i]:
                side = 1
            elif falling and c[i - 1] > ef[i - 1] + tol and c[i] < ef[i] - tol and c[i] < o[i]:
                side = -1
            if side:
                sl = min(l[i - 1], l[i]) - tol if side == 1 else max(h[i - 1], h[i]) + tol
                rds[i] = max(abs(c[i] - sl), 1.2 * atr[i])
        elif pattern == "P4_cluster":
            sb = 1 if rising else (-1 if falling else 0)
            struct, rd = cluster_break(o, h, l, c, atr, i, e, sb)
            if rd > 0 and ((struct < c[i] and rising) or (struct > c[i] and falling)):
                side = 1 if rising else -1
                rds[i] = max(rd, 1.2 * atr[i])
        if side == 0:
            continue
        if regime_gate:
            if side == 1 and not c[i] > es[i]:
                continue
            if side == -1 and not c[i] < es[i]:
                continue
        sides[i] = side
    return sides, rds


# ------------------------------------------------------------ simulator ------
def parse_exit(mode):
    if "trail" in mode:
        step = {"trail_0.5r": 0.5, "trail_1.0r": 1.0, "trail_1.5r": 1.5}[mode]
        return True, step, 6.0 if step <= 1.0 else 8.0
    return False, 0.0, float(mode.split("_")[1].rstrip("r"))


def simulate(df, sides, rds, exit_mode, cooldown=6):
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    spr = df["spr"].to_numpy(float)
    times = df["datetime"].to_numpy()
    atr = wilder_atr(h, l, c, 14)
    n = len(c)
    is_trail, trail_step, rr = parse_exit(exit_mode)
    trades = []
    in_pos = side = entry_i = 0
    pe = psl = ptp = prd = pmfe = 0.0
    cool = 0
    for i in range(20, n - 1):
        if in_pos:
            bullish = c[i] >= o[i]
            seq = (("L", l[i]), ("H", h[i])) if bullish else (("H", h[i]), ("L", l[i]))
            exited, xp, why = False, 0.0, ""
            for tt, px in seq:
                if side == 1:
                    if tt == "L" and px <= psl:
                        exited, xp, why = True, psl, "SL"
                        break
                    if tt == "H":
                        pmfe = max(pmfe, (px - pe) / prd)
                        if ptp > 0 and px >= ptp:
                            exited, xp, why = True, ptp, "TP"
                            break
                else:
                    if tt == "H" and px >= psl:
                        exited, xp, why = True, psl, "SL"
                        break
                    if tt == "L":
                        pmfe = max(pmfe, (pe - px) / prd)
                        if ptp > 0 and px <= ptp:
                            exited, xp, why = True, ptp, "TP"
                            break
            if not exited and is_trail:
                if pmfe >= 1.0:
                    be = pe + (0.05 * prd if side == 1 else -0.05 * prd)
                    if (side == 1 and be > psl) or (side == -1 and be < psl):
                        psl = be
                if pmfe >= 1.5:
                    tr = pe + ((pmfe - trail_step) * prd if side == 1 else -(pmfe - trail_step) * prd)
                    if (side == 1 and tr > psl) or (side == -1 and tr < psl):
                        psl = tr
            if exited:
                r_raw = (xp - pe) / prd if side == 1 else (pe - xp) / prd
                cost = spr[entry_i] + spr[i]
                trades.append({"entry_day": str(pd.Timestamp(times[entry_i]).date()),
                               "r_net": r_raw - cost / prd,
                               "spread_r": cost / prd})
                in_pos = 0
                cool = i + cooldown
                continue
        if in_pos or i < cool:
            continue
        if sides[i] != 0:
            nxt = i + 1
            pe, entry_i, side = o[nxt], nxt, int(sides[i])
            prd = max(1e-5, rds[i] if rds[i] > 0 else 1.5 * atr[i], 1.2 * atr[i])
            psl = pe - prd if side == 1 else pe + prd
            ptp = pe + rr * prd if side == 1 else pe - rr * prd
            pmfe, in_pos = 0.0, 1
    return pd.DataFrame(trades)


def day_split_metrics(tr: pd.DataFrame, all_days: list, q=0.70):
    k = int(len(all_days) * q)
    is_days, oos_days = set(all_days[:k]), set(all_days[k:])
    if not len(tr):
        tr = pd.DataFrame({"entry_day": [], "r_net": []})
    tr = tr.copy()
    tr["leg"] = np.where(tr["entry_day"].isin(is_days), "IS", "OOS")
    out = {}
    for leg, g in tr.groupby("leg"):
        r = g["r_net"].to_numpy(float)
        w, lo = r[r > 0], r[r < 0]
        out[leg] = {
            "n": int(len(r)), "r": float(r.sum()),
            "pf": float(w.sum() / abs(lo.sum())) if lo.sum() < 0 else 99.0,
            "t": float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 2 else 0.0,
        }
    out["_n_is_days"], out["_n_oos_days"] = len(is_days), len(oos_days)
    out["_oos_days"] = sorted(oos_days)
    return out, tr


# ---------------------------------------------------------------- main ------
def main():
    picks = pd.read_csv(os.path.join(ROOT, "htf_ema_rejection", "results", "basket_picks.csv"))
    their = {}
    for _, r in picks.iterrows():
        their[r["symbol"]] = dict(tf=r["tf"], period=int(r["period"]), pattern=r["pattern"],
                                  slope=bool(r["slope"]), regime=bool(r["regime"]), exit=r["exit"])

    rows = []
    pooled_my = {"gold": {}, "exgold": {}}
    pooled_their = {"gold": {}, "exgold": {}}
    grid_trials = 0
    print("=" * 100)
    print("HTF EMA REJECTION RED-TEAM AUDIT — enforced spreads, day-split IS/OOS")
    print("=" * 100)
    for sym, tf, per, pat, slope, regime, ex in BASKET:
        df = load_symbol(tf, sym)
        if df is None:
            print(f"{sym:<10} {tf}  MISSING DATA — skipped")
            continue
        sp_raw = df["spr_raw"].to_numpy(float)
        zero_frac = float((sp_raw <= 0).mean())
        ensp = df["spr"].to_numpy(float)
        days = sorted({str(d) for d in pd.to_datetime(df["datetime"]).dt.date})
        # --- their frozen cell (exit from picks CSV)
        tp = their.get(sym, {})
        their_exit = tp.get("exit") or ex or "fixed_3.0r"
        s_t, r_t = precompute(df, tp.get("period", per), tp.get("pattern", pat),
                              tp.get("slope", True), tp.get("regime", False))
        tr_t = simulate(df, s_t, r_t, their_exit)
        m_t = day_split_metrics(tr_t, days)[0] if len(tr_t) else {}
        # --- my IS-only plateau selection over pre-registered grid
        k = int(len(days) * 0.70)
        is_days = set(days[:k])
        cells = {}   # (pattern, period) -> list of (cfg, is metrics)
        for pat2 in PATTERNS:
            for per2 in PERIODS:
                for sl in SLOPES:
                    for rg in REGIMES:
                        s2, r2 = precompute(df, per2, pat2, sl, rg)
                        for ex2 in EXITS_GRID:
                            tr2 = simulate(df, s2, r2, ex2)
                            grid_trials += 1
                            if not len(tr2):
                                continue
                            g = tr2[tr2["entry_day"].isin(is_days)]
                            r = g["r_net"].to_numpy(float)
                            if len(r) < 20:
                                continue
                            w, lo = r[r > 0], r[r < 0]
                            pf = w.sum() / abs(lo.sum()) if lo.sum() < 0 else 99.0
                            if pf < 1.20:
                                continue
                            cells.setdefault((pat2, per2), []).append(
                                (pat2, per2, sl, rg, ex2, float(r.sum()), pf, int(len(r))))
        my_pick, my_m = None, {"IS": {"n": 0, "r": 0.0, "pf": 0.0, "t": 0.0}, "OOS": {"n": 0, "r": 0.0, "pf": 0.0, "t": 0.0}}
        for key, lst in cells.items():
            if len(lst) >= 3:  # cell needs >=3 qualified configs
                lst2 = sorted(lst, key=lambda x: x[5])
                med = lst2[len(lst2) // 2]
                if my_pick is None or med[5] < my_pick[5]:
                    my_pick = med  # median-of-plateau; choose the most conservative (median R) per cell, then weakest cell
        if my_pick:
            pat2, per2, sl, rg, ex2 = my_pick[0], my_pick[1], my_pick[2], my_pick[3], my_pick[4]
            s3, r3 = precompute(df, per2, pat2, sl, rg)
            tr3 = simulate(df, s3, r3, ex2)
            my_m, tr3 = day_split_metrics(tr3, days)
            if len(tr3):
                fam = "gold" if sym in GOLD_FAMILY else "exgold"
                for _, t in tr3[tr3["leg"] == "OOS"].iterrows():
                    pooled_my[fam].setdefault(t["entry_day"], 0.0)
                    pooled_my[fam][t["entry_day"]] += t["r_net"]
        fam = "gold" if sym in GOLD_FAMILY else "exgold"
        if len(tr_t):
            _, tr_t_tag = day_split_metrics(tr_t, days)
            for _, t in tr_t_tag[tr_t_tag["leg"] == "OOS"].iterrows():
                pooled_their[fam].setdefault(t["entry_day"], 0.0)
                pooled_their[fam][t["entry_day"]] += t["r_net"]
        rows.append({
            "symbol": sym, "tf": tf, "days": len(days),
            "spread_zero_pct": round(100 * zero_frac, 1),
            "spread_enforced_median": round(float(np.median(ensp)), 6),
            "their_cfg": f"{tp.get('period', per)}/{tp.get('pattern', pat)}/{their_exit}",
            "their_is_r": round(m_t.get("IS", {}).get("r", 0.0), 1),
            "their_is_pf": round(m_t.get("IS", {}).get("pf", 0.0), 2),
            "their_is_n": m_t.get("IS", {}).get("n", 0),
            "their_oos_r": round(m_t.get("OOS", {}).get("r", 0.0), 1),
            "their_oos_pf": round(m_t.get("OOS", {}).get("pf", 0.0), 2),
            "their_oos_n": m_t.get("OOS", {}).get("n", 0),
            "my_pick": f"{my_pick[1]}/{my_pick[0]}/{my_pick[4]}" if my_pick else "-",
            "my_oos_r": round(my_m.get("OOS", {}).get("r", 0.0), 1),
            "my_oos_pf": round(my_m.get("OOS", {}).get("pf", 0.0), 2),
            "my_oos_n": my_m.get("OOS", {}).get("n", 0),
        })
        r = rows[-1]
        print(f"{sym:<10} {tf} zd={r['spread_zero_pct']:>5}%  spr={r['spread_enforced_median']:<9g} "
              f"THEIR [{r['their_cfg']:<22}] IS {r['their_is_r']:>7.1f}R PF{r['their_is_pf']:<5} n={r['their_is_n']:<4} "
              f"OOS {r['their_oos_r']:>7.1f}R PF{r['their_oos_pf']:<5} n={r['their_oos_n']:<4} | "
              f"MY [{r['my_pick']:<20}] OOS {r['my_oos_r']:>7.1f}R PF{r['my_oos_pf']:<5} n={r['my_oos_n']}")

    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(OUT, "htf_audit_cells.csv"), index=False)

    print("\n--- pooled OOS daily-R (their frozen picks) ---")
    for fam in ("gold", "exgold"):
        d = pooled_their[fam]
        v = np.array(list(d.values()))
        if len(v) > 2:
            t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v)))
            print(f"{fam:>7}: n_days={len(v):3d}  sum={v.sum():+8.1f}R  t={t:+.2f}")
        else:
            print(f"{fam:>7}: insufficient data")
    print("--- pooled OOS daily-R (my IS-only plateau picks) ---")
    n_cfg_per_symbol = len(PATTERNS) * len(PERIODS) * len(SLOPES) * len(REGIMES) * len(EXITS_GRID)
    n_series = n_cfg_per_symbol * max(1, sum(1 for row in rows))
    haircut = math.sqrt(2 * math.log(n_series))
    haircut_theirs = math.sqrt(2 * math.log(86634))
    for fam in ("gold", "exgold"):
        d = pooled_my[fam]
        v = np.array(list(d.values()))
        if len(v) > 2:
            t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v)))
            print(f"{fam:>7}: n_days={len(v):3d}  sum={v.sum():+8.1f}R  t={t:+.2f}  "
                  f"deflated({n_series} series)={t - haircut:+.2f}  deflated(86634)={t - haircut_theirs:+.2f}")
        else:
            print(f"{fam:>7}: insufficient data")
    print(f"\ngrid: {n_cfg_per_symbol} configs/symbol, {grid_trials} sims total; "
          f"haircut(mine)={haircut:.2f}, haircut(86,634)={haircut_theirs:.2f}")


if __name__ == "__main__":
    main()
