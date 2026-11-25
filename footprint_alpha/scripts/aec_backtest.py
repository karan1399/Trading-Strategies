#!/usr/bin/env python3
"""AEC-Trend 360-degree scenario backtester.

Auction-Complete + EMA-Confluence trend strategy, vectorized across the whole
scenario grid with a per-asset numpy state machine (one position per scenario):

  ENTRY  (at next bar open, after signal bar i): regime(i) AND pullback-touch(i)
         AND auction-complete(i)   [ac_none variant = baseline without footprint]
  STOP   hard: setup-zone extreme -/+ stop_mult * ATR(14, 5m)   (always on)
  EXIT   ATR chandelier trail (2.5/4.0), EMA50 body cross, or trail+EMA combo

Costs: 5 bps taker fee per side + 1 bp slippage (+1 extra bp on stop fills).
Conservative fills: gap-through-stop fills at the open.

Outputs (binance_orderflow/data/research/):
  aec_trades.parquet, aec_scenarios.csv (IS + OOS stats per scenario)
"""
import glob
import itertools
import json
import os
import time

import numpy as np
import pandas as pd

DATA = "/home/z/my-project/binance_orderflow/data/research"
FEE = 0.0005          # taker fee per side
FEE_MAKER = 0.0002    # maker fee per side (limit fills)
SLIP = 0.0001         # slippage per taker fill
SLIP_STOP = 0.0002    # extra slippage on stop fills
WARMUP = 60

REGIMES = ["strict", "strict_htf"]
ENTRIES = ["touch_b2", "reclaim"]
ACS = ["ac_abs", "ac_hvn", "ac_imb", "ac_climax", "ac_strong", "ac_any", "ac_none"]
STOPS = [2.5, 3.5]
EXITS = ["ema50", "tp2r_ema50", "tp3r_ema50", "tp5r_ema50"]   # tp2r = 2R minimum per trade
SIDES = ["long", "short"]
TP_MULT = {"ema50": 0.0, "tp2r_ema50": 2.0, "tp3r_ema50": 3.0, "tp5r_ema50": 5.0}


def build_grid():
    grid = []
    for r, b, a, s, e, sd in itertools.product(REGIMES, ENTRIES, ACS, STOPS, EXITS, SIDES):
        grid.append({"regime": r, "entry": b, "ac": a, "stop_mult": s, "exit": e, "side": sd})
    return grid


def simulate_asset(df: pd.DataFrame, grid: list[dict], split_ts: int,
                   entry_override: np.ndarray | None = None):
    """Run all scenarios for one asset. Returns trade records + per-scenario rows.

    entry_override: optional bars x scenarios bool matrix replacing the derived
    entry conditions (used by the Monte Carlo permutation tests).
    """
    n = len(df)
    o = df["o"].to_numpy(float); h = df["h"].to_numpy(float)
    l = df["l"].to_numpy(float); c = df["c"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    ema50 = df["ema50b"].to_numpy(float)
    ts = df["ts"].to_numpy(np.int64)
    n_s = len(grid)
    # ---- entry condition matrix: bars x scenarios (computed once)
    col_reg = np.array([0 if g["regime"] == "strict" else 1 for g in grid])
    col_entry = np.array([ENTRIES.index(g["entry"]) for g in grid])
    col_side = np.array([1 if g["side"] == "long" else -1 for g in grid])
    reg_up = df["trend_up_strict"].to_numpy(bool)
    reg_dn = df["trend_dn_strict"].to_numpy(bool)
    htf_up = df["htf_up"].to_numpy(bool)
    htf_dn = df["htf_dn"].to_numpy(bool)
    long_reg = np.stack([reg_up, reg_up & htf_up], axis=1)
    dn_reg = np.stack([reg_dn, reg_dn & htf_dn], axis=1)
    pb = np.stack([df["pb_low_b2"].to_numpy(bool),
                   df["touch_low5"].to_numpy(bool) & df["depth_up_ok"].to_numpy(bool) & df["reclaim_up"].to_numpy(bool),
                   df["pb_high_b2"].to_numpy(bool),
                   df["touch_high5"].to_numpy(bool) & df["depth_dn_ok"].to_numpy(bool) & df["reclaim_dn"].to_numpy(bool)],
                  axis=1)  # 0..1 long entries, 2..3 short entries
    entry_col_for_side = np.where(col_side == 1, col_entry, col_entry + 2)
    pb_m = pb[:, entry_col_for_side]
    ac_cache: dict = {}

    def ac_col(name: str, side: str) -> np.ndarray:
        key = (name, side)
        if key not in ac_cache:
            if name == "ac_none":
                ac_cache[key] = np.ones(n, dtype=bool)
            else:
                ac_cache[key] = df[f"{name}_{'low' if side == 'long' else 'high'}"].to_numpy(bool)
        return ac_cache[key]

    ac_matrix = np.stack([ac_col(g["ac"], g["side"]) for g in grid], axis=1)
    is_oos = ts >= split_ts
    long_reg = np.stack([reg_up, reg_up & htf_up], axis=1)
    dn_reg = np.stack([reg_dn, reg_dn & htf_dn], axis=1)
    M_long = long_reg[:, col_reg] & pb_m & ac_matrix & (col_side == 1)
    M_short = dn_reg[:, col_reg] & pb_m & ac_matrix & (col_side == -1)
    if entry_override is not None:
        M = np.asarray(entry_override, dtype=bool)[:n]
    else:
        M = M_long | M_short
    M[:WARMUP] = False
    M[-1] = False                     # no entry on the last bar (needs next open)
    M[~np.isfinite(atr)] = False
    # ---- exit parameters per scenario (TP multiples; 200-bar time cap for all)
    trail_mult = np.zeros(n_s)
    tp_mult = np.array([TP_MULT[g["exit"]] for g in grid])
    use_tp = tp_mult > 0
    use_time = np.ones(n_s, dtype=bool)
    stop_mult = np.array([g["stop_mult"] for g in grid])
    # ---- state (maker execution: entries are one-bar GTC limits at the signal close)
    pos = np.zeros(n_s, dtype=np.int8)          # 0 flat, 1 long, -1 short
    entry_px = np.full(n_s, np.nan)
    stop = np.full(n_s, np.nan)
    trail = np.full(n_s, np.nan)
    tp = np.full(n_s, np.nan)
    entry_i = np.zeros(n_s, dtype=np.int64)
    entry_oos = np.zeros(n_s, dtype=bool)
    pending = np.zeros(n_s, dtype=np.int8)      # resting limit side, 0 none
    limit_px = np.full(n_s, np.nan)
    pend_stop = np.full(n_s, np.nan)
    risk_px = np.full(n_s, np.nan)
    trades = []
    for i in range(1, n):
        atr_i = atr[i] if np.isfinite(atr[i]) else atr[i - 1]
        # ---------- 1) fill/expire resting limits (placed on signal bar i-1)
        pend = pending != 0
        if pend.any():
            pi = np.where(pend)[0]
            lim = limit_px[pi]
            filled = np.where(pending[pi] == 1, l[i] <= lim, h[i] >= lim)
            fi = pi[filled]
            if len(fi):
                epx = lim[filled].astype(float)
                side_w = pending[fi].astype(np.int8)
                pos[fi] = side_w
                entry_px[fi] = epx
                stop[fi] = pend_stop[fi]
                risk = np.abs(epx - stop[fi])
                risk_px[fi] = risk
                tp[fi] = np.where(side_w == 1, epx + tp_mult[fi] * risk,
                                  epx - tp_mult[fi] * risk)
                trail[fi] = np.where(side_w == 1, epx - trail_mult[fi] * atr_i,
                                     epx + trail_mult[fi] * atr_i)
                entry_i[fi] = i
                entry_oos[fi] = is_oos[i]
            pending[pi] = 0               # one-bar GTC: filled or expired
        # ---------- 2) exits for open positions
        opened = pos != 0
        if opened.any():
            idx = np.where(opened)[0]
            lo, hi, cl = l[i], h[i], c[i]
            long_m = pos[idx] == 1
            has_trail = trail_mult[idx] > 0
            tr_up = h[i] - trail_mult[idx] * atr_i
            tr_dn = l[i] + trail_mult[idx] * atr_i
            trail[idx] = np.where(has_trail & long_m, np.fmax(trail[idx], tr_up),
                                  np.where(has_trail & ~long_m, np.fmin(trail[idx], tr_dn),
                                           trail[idx]))
            stop_hit = np.where(long_m, lo <= stop[idx], hi >= stop[idx])
            trail_hit = (np.where(long_m, lo <= trail[idx], hi >= trail[idx])) & has_trail
            tp_hit = (np.where(long_m, hi >= tp[idx], lo <= tp[idx])) & use_tp[idx]
            ema_hit = np.where(long_m, cl < ema50[i], cl > ema50[i])
            time_hit = (i - entry_i[idx] >= 200) & use_time[idx]
            exit_price_i = np.where(long_m,
                                    np.where(stop_hit, np.fmin(o[i], stop[idx]),
                                             np.where(tp_hit, tp[idx],
                                                      np.where(trail_hit, trail[idx], cl))),
                                    np.where(stop_hit, np.fmax(o[i], stop[idx]),
                                             np.where(tp_hit, tp[idx],
                                                      np.where(trail_hit, trail[idx], cl))))
            kind_i = np.where(stop_hit, "stop",
                              np.where(tp_hit, "tp",
                                       np.where(trail_hit, "trail",
                                                np.where(ema_hit, "ema50",
                                                         np.where(time_hit, "time", "none")))))
            done = kind_i != "none"
            if done.any():
                pos_ids = idx[done]
                epxs = entry_px[pos_ids]
                xpxs = exit_price_i[done]
                xks = kind_i[done]
                for k, j in enumerate(pos_ids):
                    sd = int(pos[j])
                    epx = float(epxs[k])
                    xpx = float(xpxs[k])
                    xk = str(xks[k])
                    gross = (xpx - epx) / epx if sd == 1 else (epx - xpx) / epx
                    if xk == "tp":
                        net = gross - 2 * FEE_MAKER            # limit exit
                    elif xk == "stop":
                        net = gross - FEE_MAKER - FEE - SLIP - (SLIP_STOP - SLIP)
                    else:
                        net = gross - FEE_MAKER - FEE - SLIP   # taker exit
                    trades.append((ts[entry_i[j]], ts[i], sd, epx, xpx, xk,
                                   net * 10000.0, i - entry_i[j], bool(entry_oos[j]), j,
                                   float(risk_px[j]) / epx * 10000.0))
                pos[pos_ids] = 0
                entry_px[pos_ids] = np.nan; stop[pos_ids] = np.nan
                trail[pos_ids] = np.nan; tp[pos_ids] = np.nan; risk_px[pos_ids] = np.nan
        # ---------- 3) place new limits from signal bar i-1 (active during bar i+1)
        sig = M[i - 1]
        flat = (pos == 0) & (pending == 0)
        want = sig & flat
        if want.any():
            wi = np.where(want)[0]
            side_w = col_side[wi]
            lo3 = np.min(l[max(0, i - 3):i])
            hi3 = np.max(h[max(0, i - 3):i])
            a_i = atr[i - 1] if np.isfinite(atr[i - 1]) else np.nanmean(atr[max(0, i - 30):i])
            sm = stop_mult[wi]
            limit_px[wi] = c[i - 1]
            pend_stop[wi] = np.where(side_w == 1, lo3 - sm * a_i, hi3 + sm * a_i)
            pending[wi] = np.where(side_w == 1, 1, -1).astype(np.int8)
    tcols = ["entry_ts", "exit_ts", "side", "entry", "exit", "kind", "ret_bps", "bars",
             "oos", "scen", "risk_bps"]
    tr = pd.DataFrame(trades, columns=tcols)
    tr["symbol"] = df["symbol"].iloc[0]
    return tr


def summarize(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return {"n": 0}
    r = tr["ret_bps"].to_numpy(float)
    win = (r > 0).mean()
    pf = r[r > 0].sum() / max(1e-9, -r[r < 0].sum())
    return {"n": int(len(r)), "win%": round(win * 100, 1), "avg_bps": round(float(r.mean()), 2),
            "med_bps": round(float(np.median(r)), 2), "pf": round(float(pf), 3),
            "total_bps": round(float(r.sum()), 1),
            "tstat": round(float(r.mean() / (r.std() + 1e-9) * np.sqrt(len(r))), 2)}


def main():
    import sys as _sys
    dsf = _sys.argv[1] if len(_sys.argv) > 1 else "research_15m.parquet"
    print("dataset:", dsf)
    ds = pd.read_parquet(f"{DATA}/{dsf}")
    split_ts = int(pd.Timestamp("2026-11-08", tz="UTC").value // 10 ** 6)
    grid = build_grid()
    print(f"scenarios: {len(grid)}  IS: <{pd.Timestamp(split_ts, unit='ms', tz='UTC')}  "
          f"OOS: >={pd.Timestamp(split_ts, unit='ms', tz='UTC')}")
    all_trades = []
    t0 = time.time()
    symbols = sorted(ds["symbol"].unique())
    for sym in symbols:
        d = ds[ds["symbol"] == sym].sort_values("ts").reset_index(drop=True)
        tr = simulate_asset(d, grid, split_ts)
        all_trades.append(tr)
        n_is = int((~tr["oos"]).sum()) if len(tr) else 0
        n_oos = int(tr["oos"].sum()) if len(tr) else 0
        print(f"{sym}: {len(d):,} bars, {n_is} IS + {n_oos} OOS trades ({time.time()-t0:.0f}s)")
    T = pd.concat(all_trades, ignore_index=True)
    T.to_parquet(f"{DATA}/aec_trades.parquet", index=False)
    # ---- per-scenario summary (IS and OOS)
    rows = []
    gi = {i: g for i, g in enumerate(grid)}
    for sid, grp in T.groupby("scen"):
        g = gi[int(sid)]
        s_is = summarize(grp[~grp["oos"]])
        s_oos = summarize(grp[grp["oos"]])
        rows.append({**g, "scen": int(sid),
                     "IS_" + "n": s_is.get("n", 0), **{f"IS_{k}": v for k, v in s_is.items()},
                     **{f"OOS_{k}": v for k, v in s_oos.items()}})
    S = pd.DataFrame(rows)
    S.to_csv(f"{DATA}/aec_scenarios.csv", index=False)
    print(f"\ntrades: {len(T):,} -> {DATA}/aec_trades.parquet")
    # quick view: top IS by tstat with n>=60, their OOS
    top = S[(S["IS_n"] >= 60)].sort_values("IS_tstat", ascending=False).head(12)
    cols = ["scen", "regime", "band", "ac", "stop_mult", "exit", "side",
            "IS_n", "IS_win%", "IS_avg_bps", "IS_pf", "IS_tstat",
            "OOS_n", "OOS_win%", "OOS_avg_bps", "OOS_pf", "OOS_tstat"]
    cols = [c for c in cols if c in top.columns]
    print("\nTOP-12 by IS t-stat (with OOS):")
    print(top[cols].to_string(index=False))


if __name__ == "__main__":
    main()
