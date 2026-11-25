#!/usr/bin/env python3
"""Footprint core: loaders, 'auction complete' event definitions, features,
triple-barrier labels. Shared by events study, training, backtest, watcher.

Semantics (all closed-bar, no lookahead):
  zero_top : taker-buy volume == 0 at the bar's TOP traded tick level
             -> no one was willing to pay the offer at the high
             -> 'auction complete up' -> SELL candidate
  zero_bot : taker-sell volume == 0 at the bar's BOTTOM tick level
             -> 'auction complete down' -> BUY candidate
Feature vector is computed at bar close; entry is the NEXT bar's open.
"""
import os
import glob

import numpy as np
import pandas as pd

BASE = os.environ.get(
    "FP_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "footprint"))  # repo-root/footprint when this file sits in a
                                # project subfolder, e.g. footprint_auction_ml/
PROC = os.path.join(BASE, "processed")
BARS_PER_DAY = 288  # 5m bars, crypto trades 24/7

FEE_BPS_SIDE = 5.0        # Binance um futures taker, conservative VIP0
FEE_MAKER_BPS = 2.0       # maker (limit) fee per side, um futures VIP0
TICK = 0.10               # BTCUSDT futures tick size
N_EMA_FAST, N_EMA_SLOW = 96, 288   # 8h / 24h EMA on 5m


def day_files(kind):
    fs = sorted(glob.glob(os.path.join(PROC, f"{kind}_BTCUSDT_*.parquet")))
    days = [os.path.basename(f).split("_")[-1][:10] for f in fs]
    return fs, days


def load_all(max_days=None):
    """Concat bars/edges/grid in date order; add global bar index bar_g.
    Days may have <288 bars when the exchange pauses (e.g. maintenance);
    offsets use TRUE per-day bar counts from the bars files."""
def load_all(max_days=None, load_grid=True):
    """Concat bars/edges in date order; global bar index bar_g. Days may
    have <288 bars when the exchange pauses; offsets use TRUE per-day bar
    counts. load_grid=False skips the large grid concat - stream it per
    day with grid_feats_stream() when memory is tight."""
    bfs, days = day_files("bars")
    efs, _ = day_files("edges")
    gfs, _ = day_files("grid")
    if max_days:
        bfs, efs, gfs, days = bfs[:max_days], efs[:max_days], gfs[:max_days], days[:max_days]
    per_day = [pd.read_parquet(f, columns=["ts"]) for f in bfs]
    bars = pd.concat([d.reset_index(drop=True) for d in
                      [pd.read_parquet(f) for f in bfs]], ignore_index=True)
    bars["bar_g"] = np.arange(len(bars))
    counts = [len(d) for d in per_day]
    assert sum(counts) == len(bars)
    edges_parts = []
    off = 0
    for f, cnt in zip(efs, counts):
        e = pd.read_parquet(f).rename(columns={"bar_i": "bar_g"})
        e["bar_g"] += off
        edges_parts.append(e)
        off += cnt
    edges = pd.concat(edges_parts, ignore_index=True)
    grid = None
    if load_grid:
        grid_parts = []
        off = 0
        for f, cnt in zip(gfs, counts):
            g = pd.read_parquet(f).rename(columns={"bar_i": "bar_g"})
            g["bar_g"] += off
            grid_parts.append(g)
            off += cnt
        grid = pd.concat(grid_parts, ignore_index=True)
    short = [(d, c) for d, c in zip(days, counts) if c != BARS_PER_DAY]
    if short:
        print(f"note: {len(short)} days with !={BARS_PER_DAY} bars: "
              + ", ".join(f"{d}({c})" for d, c in short[:5]))
    return bars, edges, grid, days


def grid_feats_stream(bars, days):
    """grid_feats computed day-by-day (exact: no cross-bar aggregation) and
    concatenated - peak memory stays at one day's grid."""
    parts = []
    off = 0
    for d in days:
        g = pd.read_parquet(os.path.join(
            PROC, f"grid_BTCUSDT_{d}.parquet")).rename(columns={"bar_i": "bar_g"})
        b = bars[(bars.ts >= pd.Timestamp(d, tz="UTC"))
                 & (bars.ts < pd.Timestamp(d, tz="UTC") + pd.Timedelta(days=1))]
        b = b.reset_index(drop=True)
        b["bar_g"] = np.arange(len(b))   # per-day 0-based, matches grid bar_g
        gf = grid_feats(g, b)
        gf.index += off          # grid_feats output is indexed 0..n-1/bar
        off += len(b)
        parts.append(gf)
    return pd.concat(parts)       # consecutive positional blocks = 0..N-1


def edges_wide(edges):
    """Top/bottom rank-1..3 levels -> wide per-bar columns."""
    e = edges[edges["rank"] <= 3]
    top = e[e.side == 1].pivot_table(index="bar_g", columns="rank",
                                      values=["buy_v", "sell_v", "n_tr"])
    bot = e[e.side == -1].pivot_table(index="bar_g", columns="rank",
                                      values=["buy_v", "sell_v", "n_tr"])
    out = pd.DataFrame(index=top.index)
    for r in (1, 2, 3):
        out[f"t{r}_buy"] = top[("buy_v", r)]
        out[f"t{r}_sell"] = top[("sell_v", r)]
        out[f"t{r}_n"] = top[("n_tr", r)]
        out[f"b{r}_buy"] = bot[("buy_v", r)]
        out[f"b{r}_sell"] = bot[("sell_v", r)]
        out[f"b{r}_n"] = bot[("n_tr", r)]
    return out


def grid_feats(grid, bars):
    """POC / absorption / imbalance features from the 1-USD bucket grid."""
    g = grid.copy()
    g["tot"] = g.buy_v + g.sell_v
    g["delta"] = g.buy_v - g.sell_v
    g["absd"] = g.delta.abs()
    bmax = bars.set_index("bar_g")[["high", "low"]].reindex(g.bar_g).to_numpy()
    rng = np.maximum(bmax[:, 0] - bmax[:, 1], 1e-9)
    # price zone within bar: 1 = at top of range, 0 = at bottom
    g["z"] = (g.bucket_px.to_numpy() - bmax[:, 1]) / rng
    hi = g.z >= 0.8
    lo = g.z <= 0.2
    tot = g.tot.to_numpy()
    g["_tot"] = tot
    per = g.groupby("bar_g")
    feats = pd.DataFrame(index=g.bar_g.unique())
    # POC: max-volume bucket
    poc_rows = g.loc[per["tot"].idxmax()]
    feats["poc_pos"] = poc_rows.set_index("bar_g")["z"]
    feats["poc_delta"] = poc_rows.set_index("bar_g")["delta"] / poc_rows.set_index("bar_g")["tot"]
    # delta concentration in the 3 heaviest buckets
    g3 = g.sort_values(["bar_g", "tot"], ascending=[True, False]).groupby("bar_g").head(3)
    absd_sum = per["absd"].sum()
    feats["d_conc3"] = g3.groupby("bar_g")["absd"].sum() / absd_sum
    # aggressive volume shares near extremes
    feats["hi_buy_sh"] = g[hi].groupby("bar_g")["buy_v"].sum() / per["buy_v"].sum()
    feats["hi_sell_sh"] = g[hi].groupby("bar_g")["sell_v"].sum() / per["sell_v"].sum()
    feats["lo_buy_sh"] = g[lo].groupby("bar_g")["buy_v"].sum() / per["buy_v"].sum()
    feats["lo_sell_sh"] = g[lo].groupby("bar_g")["sell_v"].sum() / per["sell_v"].sum()
    # stacked delta in extreme zones
    feats["hi_d"] = g[hi].groupby("bar_g")["delta"].sum()
    feats["lo_d"] = g[lo].groupby("bar_g")["delta"].sum()
    feats["n_levels"] = per.size()
    feats["max_lvl_sh"] = poc_rows.set_index("bar_g")["tot"] / per["tot"].sum()
    return feats.sort_index().reindex(range(len(bars)))


def build_features(bars, ew, gf):
    """Full per-bar feature frame; rows are bars, index bar_g."""
    n = len(bars)
    o, h, l, c = (bars[k].to_numpy(np.float64) for k in ("open", "high", "low", "close"))
    bv, sv = bars.buy_v.to_numpy(), bars.sell_v.to_numpy()
    totv = bv + sv
    delta = (bv - sv) / np.maximum(totv, 1e-9)
    rng = np.maximum(h - l, 1e-9)
    ret1 = c / np.roll(c, 1) - 1
    ret1[0] = 0
    ema = lambda x, k: pd.Series(x).ewm(span=k, adjust=False).mean().to_numpy()
    ema_f, ema_s = ema(c, N_EMA_FAST), ema(c, N_EMA_SLOW)
    # ATR(14) on 5m bars
    tr = np.maximum.reduce([h - l, np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))])
    tr[0] = h[0] - l[0]
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    med_rng = pd.Series(rng).rolling(96, min_periods=1).median().to_numpy()
    vol_med = pd.Series(totv).rolling(96, min_periods=1).median().to_numpy()
    mom6 = np.zeros(n); mom6[6:] = c[6:] / c[:-6] - 1
    mom24 = np.zeros(n); mom24[24:] = c[24:] / c[:-24] - 1

    f = pd.DataFrame(index=bars.bar_g)
    f["ts"] = bars.ts.to_numpy()
    f["open"] = o; f["high"] = h; f["low"] = l; f["close"] = c

    # ---- auction-complete events (the core hypothesis) ----
    t1b, t1s = ew.t1_buy.to_numpy(), ew.t1_sell.to_numpy()
    b1b, b1s = ew.b1_buy.to_numpy(), ew.b1_sell.to_numpy()
    zero_top = (t1b == 0) & (t1s > 0)      # high printed, all aggressive sells
    zero_bot = (b1s == 0) & (b1b > 0)
    weak_top = t1b / np.maximum(t1b + t1s, 1e-9) <= 0.05   # ~no aggressive buys at high
    weak_bot = b1s / np.maximum(b1b + b1s, 1e-9) <= 0.05   # ~no aggressive sells at low
    zt2 = ((ew.t1_buy + ew.t2_buy).to_numpy() == 0)
    zb2 = ((ew.b1_sell + ew.b2_sell).to_numpy() == 0)
    f["zero_top"], f["zero_bot"] = zero_top, zero_bot
    f["weak_top"], f["weak_bot"] = weak_top, weak_bot
    f["zt2"], f["zb2"] = zt2, zb2
    # inverse hypothesis: buyers STILL paying the offer at the high
    f["unfinished_top"] = (t1b / np.maximum(t1b + t1s, 1e-9) >= 0.8) & (t1b > 0)
    f["unfinished_bot"] = (b1s / np.maximum(b1b + b1s, 1e-9) >= 0.8) & (b1s > 0)

    # ---- edge micro-structure ----
    t1t = np.maximum(t1b + t1s, 1e-9)
    f["t1_buy_sh"] = t1b / t1t
    f["t1_vol"] = (t1b + t1s) / np.maximum(vol_med / 30, 1e-12)
    f["b1_buy_sh"] = b1b / np.maximum(b1b + b1s, 1e-9)
    f["t13_buy_sh"] = (ew.t1_buy + ew.t2_buy + ew.t3_buy).to_numpy() / \
        np.maximum((ew.t1_sell + ew.t2_sell + ew.t3_sell).to_numpy()
                   + (ew.t1_buy + ew.t2_buy + ew.t3_buy).to_numpy(), 1e-9)
    f["b13_sell_sh"] = (ew.b1_sell + ew.b2_sell + ew.b3_sell).to_numpy() / \
        np.maximum((ew.b1_sell + ew.b2_sell + ew.b3_sell).to_numpy()
                   + (ew.b1_buy + ew.b2_buy + ew.b3_buy).to_numpy(), 1e-9)
    f["edge_ratio_top"] = (t1b + t1s) / np.maximum((ew.t1_buy + ew.t1_sell + ew.b1_buy + ew.b1_sell).to_numpy(), 1e-9)

    # ---- bar shape / flow ----
    f["delta"] = delta
    f["body"] = (c - o) / rng
    f["cpos"] = (c - l) / rng
    f["upwick"] = (h - np.maximum(o, c)) / rng
    f["lowick"] = (np.minimum(o, c) - l) / rng
    f["range_atr"] = rng / np.maximum(atr, 1e-9)
    f["range_med"] = rng / np.maximum(med_rng, 1e-9)
    f["n_tr_log"] = np.log1p(bars.n_tr.to_numpy())
    f["vol_med"] = np.log(totv / np.maximum(vol_med, 1e-12))

    # ---- context / regime ----
    f["atr_pct"] = atr / c
    f["trend_f"] = c / ema_f - 1
    f["trend_s"] = c / ema_s - 1
    f["mom6"] = mom6
    f["mom24"] = mom24
    hi24 = pd.Series(h).rolling(288, min_periods=1).max().to_numpy()
    lo24 = pd.Series(l).rolling(288, min_periods=1).min().to_numpy()
    f["pos24"] = (c - lo24) / np.maximum(hi24 - lo24, 1e-9)
    hr = bars.ts.dt.hour.to_numpy() + bars.ts.dt.minute.to_numpy() / 60
    f["hr_sin"] = np.sin(2 * np.pi * hr / 24)
    f["hr_cos"] = np.cos(2 * np.pi * hr / 24)

    # ---- grid features ----
    for k in ("poc_pos", "poc_delta", "d_conc3", "hi_buy_sh", "hi_sell_sh",
              "lo_buy_sh", "lo_sell_sh", "hi_d", "lo_d", "n_levels", "max_lvl_sh"):
        f[k] = gf[k].to_numpy()

    # ---- event side & dataset flags ----
    side = np.zeros(n, np.int8)
    side[(zero_top) & (~zero_bot)] = -1
    side[(zero_bot) & (~zero_top)] = 1
    both = zero_top & zero_bot
    side[both] = np.where(f["cpos"].to_numpy()[both] > 0.5, -1, 1)
    f["side"] = side
    side_w = np.zeros(n, np.int8)
    side_w[(weak_top) & (~weak_bot)] = -1
    side_w[(weak_bot) & (~weak_top)] = 1
    bw2 = weak_top & weak_bot
    side_w[bw2] = np.where(f["cpos"].to_numpy()[bw2] > 0.5, -1, 1)
    f["side_weak"] = side_w
    f["ev_strict"] = zero_top | zero_bot
    f["ev_weak"] = weak_top | weak_bot
    f["ev_z2"] = zt2 | zb2
    # strong mirror: buyers STILL paying the offer at the high AND close near high
    cpos = f["cpos"].to_numpy()
    f["unf_strong_top"] = f["unfinished_top"].to_numpy(bool) & (cpos >= 0.75)
    f["unf_strong_bot"] = f["unfinished_bot"].to_numpy(bool) & (cpos <= 0.25)
    return f


def triple_barrier(o, h, l, i_sig, side, rng_sig, rr=1.5, horizon=12,
                   sl_mult=1.0, entry="market", fill_win=6):
    """Entry 'market': open of bar i_sig+1.
    Entry 'limit': RESTEST of the signal-bar extreme (sell at the bar high /
    buy at the bar low) within fill_win bars; if never touched, no trade
    (win=-1 exit_r=nan marker). SL = sl_mult * signal-bar range from entry.
    TP = rr * SL distance. Pessimistic when both barriers hit in one bar.
    Returns (win: 0/1, exit_r: signed R, n_bars) - win=-1 means unfilled."""
    d = max(rng_sig * sl_mult, 1e-9)
    end = min(i_sig + 1 + horizon, len(o))
    if entry == "limit":
        lvl = h[i_sig] if side == -1 else l[i_sig]
        for j in range(i_sig + 1, min(i_sig + 1 + fill_win, end)):
            filled = h[j] >= lvl if side == -1 else l[j] <= lvl
            if filled:
                w, r, nb = _barrier_from(o, h, l, j, lvl, side, d, rr, end, i_sig)
                return w, r, nb
        return -1, np.nan, 0
    entry_px = o[i_sig + 1]
    return _barrier_from(o, h, l, i_sig + 1, entry_px, side, d, rr, end, i_sig)


def _barrier_from(o, h, l, j0, entry_px, side, d, rr, end, i_sig):
    if side == 1:
        sl, tp = entry_px - d, entry_px + rr * d
        for j in range(j0, end):
            if l[j] <= sl:
                return 0, -1.0, j - i_sig  # pessimistic: SL first
            if h[j] >= tp:
                return 1, rr, j - i_sig
        exit_r = (o[end - 1] - entry_px) / d
        return int(exit_r > 0), exit_r, end - 1 - i_sig
    else:
        sl, tp = entry_px + d, entry_px - rr * d
        for j in range(j0, end):
            if h[j] >= sl:
                return 0, -1.0, j - i_sig
            if l[j] <= tp:
                return 1, rr, j - i_sig
        exit_r = (entry_px - o[end - 1]) / d
        return int(exit_r > 0), exit_r, end - 1 - i_sig


def label_events(feats, bars, event_mask, rr=1.5, horizon=12,
                 sl_mult=1.0, entry="market", fill_win=6):
    """Labels for all event bars (precomputed arrays; small loop per event).
    Limit-mode unfilled events are dropped from the result."""
    o = bars.open.to_numpy()
    h = bars.high.to_numpy()
    l = bars.low.to_numpy()
    rng = (bars.high - bars.low).to_numpy()
    side_arr = feats["side"].to_numpy(np.int8)
    idx = np.where(event_mask)[0]
    wins, exrs, nbs, ents, sls, src = [], [], [], [], [], []
    for i in idx:
        w, r, nb = triple_barrier(o, h, l, i, int(side_arr[i]), rng[i], rr,
                                  horizon, sl_mult, entry, fill_win)
        if w == -1:
            continue  # limit order never filled
        wins.append(w); exrs.append(r); nbs.append(nb)
        ents.append(o[i + 1] if entry == "market" else
                    (h[i] if side_arr[i] == -1 else l[i]))
        sls.append(max(rng[i] * sl_mult, 1e-9)); src.append(i)
    out = feats.iloc[np.array(src, dtype=int)].copy()
    out["win"], out["exit_r"], out["n_bars"] = wins, exrs, nbs
    out["entry"], out["sl_dist"] = ents, sls
    out["filled"] = True
    return out
