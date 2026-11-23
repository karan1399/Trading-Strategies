#!/usr/bin/env python3
"""
IV Wall strategy backtest engine.

Tests session-anchored expected-move envelopes ("IV walls") on the
karan1399/Trading-Strategies historical_parquet OHLCV store (151 MT5/Blueberry
symbols x 1M/5M/15M/1H/4H, 5000 bars each).

Because these instruments carry no options chains, the implied-vol input of the
classic IV-wall formula (expected move = S * IV * sqrt(T)) is proxied by each
instrument's own recent volatility (OmegaTools' documented approach for
non-options instruments): EWMA close-to-close, EWMA Parkinson, or Wilder ATR.

Strategies (all intrabar-sequenced stop-first, entry at next bar open, no
lookahead):
  fade_touch     touch of a wall with close back inside  -> fade toward reference
  rejection      bar closes back inside by a margin after touch          -> fade
  breakout       close beyond wall                       -> continuation to next wall
  exhaustion     >= m consecutive closes beyond wall     -> fade

Costs: broker spread column (points * 10^-digits) charged per side, optional
slippage fraction; everything reported in R (risk = entry-to-stop distance).
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

PARQUET_ROOT = os.environ.get("PARQUET_ROOT", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "historical_parquet")))
TF_MIN = {"1M": 1, "5M": 5, "15M": 15, "1H": 60, "4H": 240}


# ---------------------------------------------------------------- data ------
def list_symbols(tf: str) -> list[str]:
    d = os.path.join(PARQUET_ROOT, tf)
    return sorted(f[:-8] for f in os.listdir(d) if f.endswith(".parquet"))


def infer_digits(close: pd.Series) -> int:
    """MT5 digits = the dominant decimal count in close prices (mode, cap 8).

    Max-decimals is unsafe: a single float-repr artifact (1.0852300000000001)
    inflates digits and silently zeroes the spread cost model.
    """
    s = close.astype(str).str.extract(r"\.(\d+)")[0].fillna("")
    s = s.str.rstrip("0")
    dd = s.str.len()
    if not len(dd):
        return 2
    mode = int(dd.mode().iloc[0])
    return int(min(mode, 8))


def load_symbol(tf: str, symbol: str) -> pd.DataFrame:
    df = pd.read_parquet(os.path.join(PARQUET_ROOT, tf, symbol + ".parquet"))
    df = df[["datetime", "open", "high", "low", "close", "tick_volume", "spread"]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    df["digits"] = infer_digits(df["close"])
    return df


# ------------------------------------------------------------ indicators ----
def ewma_vol(logret: pd.Series, lam: float = 0.94) -> pd.Series:
    """RiskMetrics EWMA variance of log returns, per bar (causal)."""
    return np.sqrt(logret.pow(2).ewm(alpha=1 - lam, adjust=False).mean())


def parkinson_vol(df: pd.DataFrame, lam: float = 0.94) -> pd.Series:
    hl = np.log(df["high"] / df["low"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    var = (hl.pow(2) / (4.0 * math.log(2.0))).ewm(alpha=1 - lam, adjust=False).mean()
    return np.sqrt(var)


def wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def session_vwap(df: pd.DataFrame, sess: pd.Series) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv_sum = (tp * df["tick_volume"].replace(0, 1)).groupby(sess).cumsum()
    v_sum = df["tick_volume"].replace(0, 1).groupby(sess).cumsum()
    return pv_sum / v_sum


# ------------------------------------------------------------- engine -------
@dataclass
class Config:
    name: str
    strategy: str = "fade_touch"       # fade_touch | rejection | breakout | exhaustion
    sd_wall: float = 1.0               # which wall is the entry trigger (1 or 2 SD)
    vol_estimator: str = "ewma_cc"     # ewma_cc | parkinson | atr
    atr_mult: float = 1.0              # EM = ATR * atr_mult * sqrt(session hours * bph)  (atr mode)
    vol_lam: float = 0.94
    buffer_atr: float = 0.25           # stop beyond wall in ATR14 units
    target_mode: str = "open"          # open | vwap | opposite | next_wall | wall_m
    target_m: float = 0.0              # wall_m: target = ref +/- m * EM on the other side
    tp_rr: float = 0.0                 # if >0, TP = entry +/- tp_rr * R (overrides target_mode)
    min_rr: float = 0.0                # require |target-entry|/R >= this, else skip
    min_r_atr: float = 0.0             # skip trade if R < this * ATR14 (blocks tiny-R lottery fills)
    vol_q_min: float = 0.0             # if >0, require session-start sigma >= this expanding quantile of past sigma
    stop_cap_em: float = 1.2           # SKIP trade if stop distance > this fraction of full EM
    exhaustion_bars: int = 3           # for exhaustion strategy
    max_trades_session: int = 2
    cooldown_bars: int = 6
    min_session_bars: int = 8
    skip_first_bars: int = 1           # bars of session before signals allowed
    trend_filter: str = "none"         # none | fade_flat | align   (EMA slope regime gate)
    flat_threshold: float = 1.0        # fade_flat: require |6-bar EMA slope| < this (in ATR-sqrt units)
    ema_period: int = 50
    spread_frac_atr_max: float = 1.0   # skip entry if spread_price > frac * ATR14
    cost_mult: float = 1.0             # spread charged per side (entry+exit => 2x)
    risk_usd: float = 10.0             # notional risk per trade for $ accounting


def _expected_move(o: float, sig_bar: float, bph: float, hours: float, cfg: Config, atr: float) -> float:
    if cfg.vol_estimator == "atr":
        return atr * cfg.atr_mult * math.sqrt(max(hours, 0.25) * bph)
    return o * sig_bar * math.sqrt(max(hours, 0.25) * bph)


def backtest_symbol(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Run one config on one symbol; return trade list."""
    tfm = None  # derived from index spacing below
    if len(df) < 50:
        return pd.DataFrame()
    idx = df.index
    dt_min = int(np.median(np.diff(idx.values).astype("timedelta64[s]").astype(int) / 60))
    bars_per_hour = 60.0 / dt_min

    # crypto/24x7 detection -> session anchor (broker GMT+2 opens day at 22:00 UTC)
    weekend_frac = float((idx.dayofweek >= 5).mean())
    anchor_h = 0 if weekend_frac > 0.05 else 2

    # ---- causal indicators
    logret = np.log(df["close"] / df["close"].shift(1)).fillna(0.0)
    if cfg.vol_estimator == "parkinson":
        sig_bar = parkinson_vol(df, cfg.vol_lam)
    elif cfg.vol_estimator == "atr":
        sig_bar = None
    else:
        sig_bar = ewma_vol(logret, cfg.vol_lam)
    atr14 = wilder_atr(df, 14)

    sess = (idx + pd.Timedelta(hours=anchor_h)).floor("D")

    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    atr_v = atr14.values
    sig_v = sig_bar.values if sig_bar is not None else None
    spr_p = df["spread"].values * (10.0 ** -int(df["digits"].iloc[0]))
    vw = session_vwap(df, sess).values
    ema = df["close"].ewm(span=cfg.ema_period, adjust=False).mean().values
    sess_codes, sess_start = pd.factorize(sess)
    n = len(df)

    # session spans (hours) -> expanding median, shifted (causal)
    span_h = {}
    sess_ids = sess_codes
    first_pos, last_pos = {}, {}
    for i in range(n):
        s = sess_ids[i]
        if s not in first_pos:
            first_pos[s] = i
        last_pos[s] = i
    span_list = []
    for s in range(len(sess_start)):
        i0, i1 = first_pos[s], last_pos[s]
        span_h[s] = (idx[i1] - idx[i0]).total_seconds() / 3600.0 + dt_min / 60.0
    # causal expected hours: expanding median of *past* session spans
    exp_h = np.zeros(len(sess_start))
    hist = []
    for s in range(len(sess_start)):
        exp_h[s] = float(np.median(hist)) if len(hist) >= 3 else 12.0
        hist.append(span_h[s])
    sess_last = np.array([last_pos[s] for s in range(len(sess_start))])
    sess_first = np.array([first_pos[s] for s in range(len(sess_start))])
    sess_em_sigma = {}   # sigma at session start (from bar before first bar)
    sess_open = np.array([o[first_pos[s]] for s in range(len(sess_start))])
    for s in range(len(sess_start)):
        i0 = first_pos[s]
        j = max(i0 - 1, 0)
        sess_em_sigma[s] = (sig_v[j] if sig_v is not None else None, atr_v[j])

    trades = []
    cooldown_until = -1
    open_pos = None  # dict: side, entry_i, entry, stop, target, r, is_tp_rr, sess, wall_k, wall_side
    last_close_side = {1: 0, -1: 0}  # consecutive closes beyond wall per side

    def close_pos(i: int, price: float, reason: str):
        nonlocal open_pos
        p = open_pos
        r_gross = (p["entry"] - price) / p["r"] if p["side"] == -1 else (price - p["entry"]) / p["r"]
        cost_r = (spr_p[p["entry_i"]] * cfg.cost_mult + spr_p[i] * cfg.cost_mult) / p["r"]
        trades.append({
            "symbol": df.attrs.get("symbol", "?"),
            "sess": int(p["sess"]),
            "side": p["side"], "wall_k": p["wall_k"], "wall_side": p["wall_side"],
            "entry_time": idx[p["entry_i"]], "exit_time": idx[i],
            "entry": p["entry"], "stop": p["stop"], "target": p["target"],
            "r_gross": r_gross, "r_net": r_gross - cost_r, "reason": reason,
            "bars_held": i - p["entry_i"],
        })
        open_pos = None

    for i in range(1, n):
        s = sess_codes[i]

        # ---- manage open position with intrabar sequencing (stop first)
        if open_pos is not None:
            p = open_pos
            stopped = h[i] >= p["stop"] if p["side"] == -1 else l[i] <= p["stop"]
            hit_t = l[i] <= p["target"] if p["side"] == -1 else h[i] >= p["target"]
            if stopped:
                close_pos(i, p["stop"], "stop")
            elif hit_t:
                close_pos(i, p["target"], "tp")
            elif p["sess"] != s or i == sess_last[p["sess"]]:
                close_pos(i, c[i], "session_end")
            continue

        # ---- new entries
        if i - 1 < cfg.skip_first_bars or i <= cooldown_until or s != sess_codes[i - 1]:
            last_close_side = {1: 0, -1: 0}
            continue

        sig, atr_s = sess_em_sigma[s]
        if cfg.vol_estimator != "atr" and (sig is None or not np.isfinite(sig) or sig <= 0):
            continue
        if cfg.vol_q_min > 0 and sig_v is not None and i > 200:
            if sig < np.quantile(sig_v[max(0, i - 2000):i], cfg.vol_q_min):
                continue
        em = _expected_move(sess_open[s], sig, bars_per_hour, exp_h[s], cfg, atr_s)
        if not np.isfinite(em) or em <= 0:
            continue
        up_k = sess_open[s] + cfg.sd_wall * em
        dn_k = sess_open[s] - cfg.sd_wall * em
        n_trades_sess = sum(1 for t in trades if t["sess"] == s)

        spread_px = spr_p[i]
        if spread_px > cfg.spread_frac_atr_max * atr_v[i] or atr_v[i] <= 0:
            continue

        beyond_up = c[i] > up_k
        beyond_dn = c[i] < dn_k
        touched_up = h[i] >= up_k
        touched_dn = l[i] <= dn_k

        side = 0
        wall = 0.0
        wall_k = cfg.sd_wall
        wall_side = "upper"

        if cfg.strategy in ("fade_touch", "rejection"):
            prev_close_in_up = c[i - 1] > up_k if cfg.strategy == "rejection" else False
            if touched_up and not beyond_up:
                side, wall, wall_side = -1, up_k, "upper"          # fade short
            elif cfg.strategy == "rejection" and prev_close_in_up and c[i] <= up_k:
                side, wall, wall_side = -1, up_k, "upper"
            elif touched_dn and not beyond_dn:
                side, wall, wall_k = 1, dn_k, cfg.sd_wall
                wall_side = "lower"
            elif cfg.strategy == "rejection" and (c[i - 1] < dn_k) and c[i] >= dn_k:
                side, wall, wall_side = 1, dn_k, "lower"
        elif cfg.strategy == "breakout":
            if beyond_up:
                side, wall, wall_side = 1, up_k, "upper"
            elif beyond_dn:
                side, wall, wall_side = -1, dn_k, "lower"
        elif cfg.strategy == "exhaustion":
            last_close_side[1] = last_close_side[1] + 1 if beyond_up else 0
            last_close_side[-1] = last_close_side[-1] + 1 if beyond_dn else 0
            if last_close_side[1] >= cfg.exhaustion_bars:
                side, wall, wall_side = -1, up_k, "upper"
            elif last_close_side[-1] >= cfg.exhaustion_bars:
                side, wall, wall_side = 1, dn_k, "lower"

        if side == 0 or n_trades_sess >= cfg.max_trades_session:
            continue

        # regime gate
        if cfg.trend_filter != "none":
            j0 = max(i - 6, 0)
            slope_stat = (ema[i] - ema[j0]) / max(atr_v[i] * math.sqrt(6.0), 1e-12)
            if cfg.trend_filter == "fade_flat" and abs(slope_stat) > cfg.flat_threshold:
                continue
            if cfg.trend_filter == "align" and ((side == 1 and slope_stat < 0.5) or (side == -1 and slope_stat > -0.5)):
                continue

        entry = o[i + 1] if i + 1 < n and sess_codes[i + 1] == s else None
        if entry is None:
            continue
        buf = cfg.buffer_atr * atr_v[i]
        stop = wall + buf if side == -1 else wall - buf
        r = abs(entry - stop)
        if r <= 0:
            continue
        if side == -1 and entry >= stop:
            continue
        if side == 1 and entry <= stop:
            continue
        if r > cfg.stop_cap_em * em:      # risk exceeds the priced move -> skip, honest
            continue
        if cfg.min_r_atr > 0 and r < cfg.min_r_atr * atr_v[i]:
            continue

        # target
        if cfg.tp_rr > 0:
            target = entry - cfg.tp_rr * r if side == -1 else entry + cfg.tp_rr * r
        elif cfg.target_mode == "vwap":
            target = vw[i + 1] if side == -1 else vw[i + 1]
            if (side == -1 and target >= entry) or (side == 1 and target <= entry):
                target = sess_open[s]
        elif cfg.target_mode == "opposite":
            target = sess_open[s] - cfg.sd_wall * em if side == -1 else sess_open[s] + cfg.sd_wall * em
        elif cfg.target_mode == "next_wall":
            target = sess_open[s] + (cfg.sd_wall + 1.0) * em if side == 1 else sess_open[s] - (cfg.sd_wall + 1.0) * em
        elif cfg.target_mode == "wall_m":
            target = sess_open[s] - cfg.target_m * em if side == -1 else sess_open[s] + cfg.target_m * em
        else:  # open (session reference)
            target = sess_open[s]
        # target must be reachable in profit direction
        if (side == -1 and target >= entry - 0.1 * r) or (side == 1 and target <= entry + 0.1 * r):
            target = entry - r if side == -1 else entry + r  # fallback 1R
        if cfg.min_rr > 0 and abs(target - entry) / r < cfg.min_rr:
            continue

        open_pos = {
            "side": side, "entry_i": i + 1, "entry": entry, "stop": stop,
            "target": target, "r": r, "sess": s, "wall_k": wall_k,
            "wall_side": wall_side,
        }
        if open_pos["entry_i"] <= cooldown_until:
            pass
        cooldown_until = (i + 1) + cfg.cooldown_bars

    return pd.DataFrame(trades)


def run_config(tf: str, symbols: list[str], cfg: Config) -> pd.DataFrame:
    out = []
    for sym in symbols:
        try:
            df = load_symbol(tf, sym)
        except Exception:
            continue
        df.attrs["symbol"] = sym
        t = backtest_symbol(df, cfg)
        if len(t):
            out.append(t)
    if not out:
        return pd.DataFrame()
    tr = pd.concat(out, ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    return tr


def metrics(tr: pd.DataFrame) -> dict:
    if tr is None or len(tr) == 0:
        return {"n": 0}
    r = tr["r_net"].values
    wins = r[r > 0]
    losses = r[r <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() < 0 else float("inf")
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    return {
        "n": int(len(r)),
        "total_r": float(r.sum()),
        "win_rate": float((r > 0).mean()),
        "pf": pf,
        "avg_r": float(r.mean()),
        "max_dd_r": dd,
        "t_stat": float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 2 and r.std() > 0 else 0.0,
        "pnl_usd": float(r.sum() * 10.0),  # at $10 risk/trade
    }


if __name__ == "__main__":
    import sys
    cfg = Config(name=sys.argv[1] if len(sys.argv) > 1 else "smoke")
    syms = list_symbols("15M")
    tr = run_config("15M", syms[:20], cfg)
    print(metrics(tr))
    print(tr.head(8).to_string() if len(tr) else "no trades")
