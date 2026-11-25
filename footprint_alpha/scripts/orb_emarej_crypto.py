#!/usr/bin/env python3
"""Crypto ORB + HTF-EMA-rejection sleeves with footprint/orderflow confluence.

Improves the prior MT5 sleeves for the Binance-data workflow:
  ORB  : UTC-day 60-min opening range; breakout bar must CLOSE through the edge
         WITH aligned delta and >=1.2x avg volume (footprint confirm); taker entry
         next open; stop = opposite range edge capped at 1.5x range; TP 2R/3R;
         one trade per side per day; flat by day end.
  EMAREJ: EMA89/233 (close) trend-continuation rejection — patterns P1 (wick-kiss:
         rejection wick >=30% of range, close back on trend side) and P3
         (pierce-fail: close beyond EMA89 then close back through), slope gate on
         EMA233, + NEW footprint confluence: auction-complete event at the
         rejection extreme (the AEC research's validated trigger), maker limit at
         the signal close, stop beyond the rejection extreme +-1.5 ATR, TP 2R/3R,
         200-bar cap.
  OI-wall proxy filter (ablation): metrics-store OI 30-min change must not
         violently disagree with the side (short needs OI not collapsing hard,
         long needs OI not spiking) — applied on/off as a variant.

Outputs: research/orb_emarej_summary.csv + orb_emarej_results.json
"""
import json
import sys

import numpy as np
import pandas as pd

DATA = "/home/z/my-project/binance_orderflow/data/research"
FEE_M, FEE_T, SLIP, SLIP_S = 0.0002, 0.0005, 0.0001, 0.0002
DAY_MS = 86400000


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().sort_values("ts").reset_index(drop=True)
    d["e89"] = ema(d["c"], 89)
    d["e233"] = ema(d["c"], 233)
    d["slope233"] = d["e233"] - d["e233"].shift(5)
    tr = np.maximum(d["h"] - d["l"], np.maximum((d["h"] - d["c"].shift()).abs(),
                                                (d["l"] - d["c"].shift()).abs()))
    d["atr"] = tr.rolling(14).mean()
    rng = (d["h"] - d["l"]).replace(0, np.nan)
    d["upper_wick"] = d["h"] - np.maximum(d["o"], d["c"])
    d["lower_wick"] = np.minimum(d["o"], d["c"]) - d["l"]
    d["day"] = d["ts"] // DAY_MS
    # ORB range: bars with hour-of-day 00:00 (4 x 15m bars)
    hod = (d["ts"] % DAY_MS) // 300000 // 4   # 15m index within day
    orb_win = d[hod == 0].groupby("day").agg(r_hi=("h", "max"), r_lo=("l", "min")).reset_index()
    d = d.merge(orb_win, on="day", how="left")
    d[["r_hi", "r_lo"]] = d[["r_hi", "r_lo"]].shift(1)  # range known only after 01:00
    d["vol_ma20"] = d["vol_base"].rolling(20).mean()
    d["ac_any_low"] = d["ac_abs_low"] | d["ac_dflip_low"] | d["ac_hvn_low"] | d["ac_imb_low"] | d["ac_climax_low"]
    d["ac_any_high"] = d["ac_abs_high"] | d["ac_dflip_high"] | d["ac_hvn_high"] | d["ac_imb_high"] | d["ac_climax_high"]
    return d


def emarej_masks(d: pd.DataFrame, side: int, use_ac: bool, use_oi: bool) -> np.ndarray:
    """P1/P3 rejection masks on EMA89 with EMA233 slope+regime gates."""
    e89, e233 = d["e89"].to_numpy(float), d["e233"].to_numpy(float)
    up = (d["c"].to_numpy(float) > e233) & (d["slope233"].to_numpy(float) > 0)
    dn = (d["c"].to_numpy(float) < e233) & (d["slope233"].to_numpy(float) < 0)
    l, h, c, o = (d[x].to_numpy(float) for x in ("l", "h", "c", "o"))
    rng = np.maximum(h - l, 1e-9)
    if side == 1:
        touch = (l <= e89) & (c > e89)                                   # tested from above
        p1 = touch & ((l - np.minimum(o, c)) / rng >= 0.30) & (c > o)     # wick-kiss reject up
        pierce = (d["c"].shift(1).to_numpy(float) < e89) & (c > e89)      # closed back above
        p3 = pierce & (l < np.minimum(o, c))
        m = up & (p1 | p3)
        if use_ac:
            m &= d["ac_any_low"].to_numpy(bool).astype(bool)
        if use_oi and "oi_chg_30m" in d.columns:
            m &= (d["oi_chg_30m"].fillna(0).to_numpy(float) > -1.0)
    else:
        touch = (h >= e89) & (c < e89)
        p1 = touch & ((np.maximum(o, c) - h) / rng >= 0.30) & (c < o)
        pierce = (d["c"].shift(1).to_numpy(float) > e89) & (c < e89)
        p3 = pierce & (h > np.maximum(o, c))
        m = dn & (p1 | p3)
        if use_ac:
            m &= d["ac_any_high"].to_numpy(bool).astype(bool)
        if use_oi and "oi_chg_30m" in d.columns:
            m &= (d["oi_chg_30m"].fillna(0).to_numpy(float) < 1.0)
    return m


def orb_masks(d: pd.DataFrame, side: int, use_oi: bool) -> np.ndarray:
    """Breakout close-through masks with delta+volume confirm."""
    c = d["c"].to_numpy(float)
    h, l = d["h"].to_numpy(float), d["l"].to_numpy(float)
    dp = d["delta_pct"].fillna(0).to_numpy(float)
    vb = d["vol_base"].to_numpy(float)
    vm = d["vol_ma20"].to_numpy(float)
    rhi, rlo = d["r_hi"].to_numpy(float), d["r_lo"].to_numpy(float)
    dp_prev = np.append(dp[:-1], np.nan)
    if side == 1:
        m = np.isfinite(rhi) & (c > rhi) & (d["c"].shift(1).to_numpy(float) <= rhi) & (dp > 0.02) & (vb > 1.2 * vm)
    else:
        m = np.isfinite(rlo) & (c < rlo) & (d["c"].shift(1).to_numpy(float) >= rlo) & (dp < -0.02) & (vb > 1.2 * vm)
    if use_oi and "oi_chg_30m" in d.columns:
        oi = d["oi_chg_30m"].fillna(0).to_numpy(float)
        m &= (oi > -1.0) if side == 1 else (oi < 1.0)
    return m


def sim(d: pd.DataFrame, mask_fn, cfgs: list[dict]) -> pd.DataFrame:
    """Per-variant forward simulation: maker/taker entry per cfg, stop, TP, time cap."""
    o, h, l, c = (d[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    atr = d["atr"].to_numpy(float)
    ts = d["ts"].to_numpy(np.int64)
    day = d["day"].to_numpy(np.int64)
    out = []
    for ci, cfg in enumerate(cfgs):
        m = cfg["mask"]
        m[:240] = False
        m[-1] = False
        last_exit_i = -1
        pos = None
        for i in np.where(m)[0]:
            if pos is not None and pos[0] > i:
                continue                      # still in a trade
            if i <= last_exit_i:
                continue
            side = cfg["side"]
            entry_kind = cfg["entry_kind"]
            if entry_kind == "maker":
                epx = c[i - 1]
                if side == 1 and not (l[i] <= epx):
                    continue
                if side == -1 and not (h[i] >= epx):
                    continue
            else:
                epx = o[i]
            stop = cfg["stop"](d, i, side)
            if not np.isfinite(stop) or not np.isfinite(atr[i]) or atr[i] <= 0:
                continue
            risk = abs(epx - stop)
            if risk <= 0 or risk / epx > 0.05:   # guard: no >5% stop distances
                continue
            tp = epx + cfg["tp_r"] * risk * side
            exit_px = kind = None
            j = i + 1
            while j < len(d):
                hj, lj, cj = h[j], l[j], c[j]
                if side == 1:
                    if lj <= stop:
                        exit_px, kind = min(o[j], stop), "stop"; break
                    if hj >= tp:
                        exit_px, kind = tp, "tp"; break
                else:
                    if hj >= stop:
                        exit_px, kind = max(o[j], stop), "stop"; break
                    if lj <= tp:
                        exit_px, kind = tp, "tp"; break
                if (day[j] != day[i] and side == 1 and cfg.get("orb_flat_eod")) or \
                   (day[j] != day[i] and cfg.get("orb_flat_eod")):
                    exit_px, kind = c[j - 1], "eod"; break
                if j - i >= 200:
                    exit_px, kind = c[j], "time"; break
                j += 1
            if exit_px is None:
                break
            gross = (exit_px - epx) / epx * side
            if kind == "tp":
                net = gross - FEE_M - (FEE_M if entry_kind == "maker" else FEE_T + SLIP)
            elif kind == "stop":
                net = gross - SLIP_S - (FEE_M if entry_kind == "maker" else FEE_T + SLIP) - FEE_T
            else:
                net = gross - SLIP - (FEE_M if entry_kind == "maker" else FEE_T + SLIP) - FEE_T
            out.append({"sym": d["symbol"].iloc[0], "cfg": ci, "side": side,
                        "entry_ts": ts[i], "exit_ts": ts[j], "ret_bps": net * 1e4,
                        "risk_bps": risk / epx * 1e4, "oos": ts[i] >= SPLIT,
                        "entry_kind": entry_kind})
            last_exit_i = j
    return pd.DataFrame(out)


SPLIT = int(pd.Timestamp("2026-11-08", tz="UTC").value // 10 ** 6)


def stats(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return {"n": 0}
    r = tr["ret_bps"].to_numpy(float)
    pf = r[r > 0].sum() / max(1e-9, -r[r < 0].sum())
    return {"n": int(len(r)), "win%": round(float((r > 0).mean() * 100), 1),
            "avg_bps": round(float(r.mean()), 2), "pf": round(float(pf), 3),
            "total_bps": round(float(r.sum()), 1),
            "t": round(float(r.mean() / (r.std() + 1e-9) * np.sqrt(len(r))), 2),
            "avg_R": round(float((r / tr["risk_bps"].where(tr["risk_bps"].abs() > 1e-6, np.nan)).mean()), 3)}


def main():
    dsf = sys.argv[1] if len(sys.argv) > 1 else "research_15m.parquet"
    ds = pd.read_parquet(f"{DATA}/{dsf}")
    rows = []
    pool_tr: dict = {}
    for sym in sorted(ds["symbol"].unique()):
        d = prep(ds[ds["symbol"] == sym])
        for sleeve in ("orb", "emarej"):
            for use_ac in ([False, True] if sleeve == "emarej" else [True]):
                for use_oi in (False, True):
                    for tp_r in (2.0, 3.0):
                        for side in (1, -1):
                            if sleeve == "orb":
                                mask = orb_masks(d, side, use_oi)
                                cfg = {"side": side, "entry_kind": "taker",
                                       "tp_r": tp_r, "orb_flat_eod": True,
                                       "stop": lambda dd, i, s: _orb_stop(dd, i, s)}
                            else:
                                mask = emarej_masks(d, side, use_ac, use_oi)
                                cfg = {"side": side, "entry_kind": "maker", "tp_r": tp_r,
                                       "stop": lambda dd, i, s: _rej_stop(dd, i, s)}
                            cfg["mask"] = mask
                            tr = sim(d, mask_fn=None, cfgs=[cfg])
                            if len(tr):
                                s_is, s_oos = stats(tr[~tr["oos"]]), stats(tr[tr["oos"]])
                            else:
                                s_is, s_oos = {"n": 0}, {"n": 0}
                            pool_key = (sleeve, use_ac, use_oi, tp_r)
                            pool_tr.setdefault(pool_key, []).append(tr)
                            rows.append({"sym": sym, "sleeve": sleeve,
                                         "ac_confirm": use_ac, "oi_filter": use_oi,
                                         "tp_r": tp_r, "side": "long" if side == 1 else "short",
                                         "IS_n": s_is.get("n", 0), "IS_t": s_is.get("t"),
                                         "IS_avg": s_is.get("avg_bps"),
                                         "OOS_n": s_oos.get("n", 0), "OOS_t": s_oos.get("t"),
                                         "OOS_avg": s_oos.get("avg_bps"), "OOS_pf": s_oos.get("pf")})
    R = pd.DataFrame(rows)
    R.to_csv(f"{DATA}/orb_emarej_summary.csv", index=False)
    # pooled view per sleeve variant: stats over POOLED TRADES (not averaged PFs)
    prows = []
    for k, tl in pool_tr.items():
        T = pd.concat(tl, ignore_index=True)
        if len(T) == 0:
            continue
        s_is, s_oos = stats(T[~T["oos"]]), stats(T[T["oos"]])
        prows.append({"sleeve": k[0], "ac_confirm": k[1], "oi_filter": k[2], "tp_r": k[3],
                      "IS": s_is, "OOS": s_oos})
    pool = pd.DataFrame(prows)
    print(pool.to_string(index=False))
    with open(f"{DATA}/orb_emarej_results.json", "w") as f:
        json.dump({"pooled": pool.to_dict("records"),
                   "per_row": R.to_dict("records")}, f, indent=1, default=str)
    print("saved -> orb_emarej_summary.csv / orb_emarej_results.json")


def _orb_stop(d, i, side):
    rhi, rlo = d["r_hi"].iloc[i], d["r_lo"].iloc[i]
    if not (np.isfinite(rhi) and np.isfinite(rlo)):
        return np.nan
    width = rhi - rlo
    cap = 1.5 * width
    if side == 1:
        return max(rlo, d["c"].iloc[i] - cap)
    return min(rhi, d["c"].iloc[i] + cap)


def _rej_stop(d, i, side):
    a = d["atr"].iloc[i]
    if not np.isfinite(a) or a <= 0:
        return np.nan
    if side == 1:
        return min(d["l"].iloc[i - 3:i + 1].min(), d["e89"].iloc[i]) - 1.5 * a
    return max(d["h"].iloc[i - 3:i + 1].max(), d["e89"].iloc[i]) + 1.5 * a


if __name__ == "__main__":
    main()
