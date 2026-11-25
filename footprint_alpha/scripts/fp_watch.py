#!/usr/bin/env python3
"""Footprint Auction ML - live watcher (the order-punching engine).

Modes
  --live          poll Binance USDT-M aggTrades REST (fromId pagination),
                  maintain a rolling raw-trade buffer, close a footprint bar
                  every 5 minutes, rebuild features on a 576-bar (48h) window,
                  detect the auction-complete event, grade it with LightGBM +
                  isotonic calibration, print an ORDER TICKET (Binance BTC
                  size + MT5 BTCUSD.p lots). Warm-starts from the local
                  Vision store (fp_download.py) when available.
  --replay DATE   identical pipeline over a historical day (verification +
                  demo; tickets carry the timestamps they would have printed).
  --selftest      synthetic trade stream: incremental vs batch parity and a
                  per-bar-close latency benchmark.

Rate budget: aggTrades weight 20/req, 1 poll per 2s = 600 weight/min
(Binance cap 2400/min). Signals only on CLOSED bars; one position at a time.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import joblib  # noqa: E402
import lightgbm as lgb  # noqa: E402

from fp_core import TICK, edges_wide, grid_feats, build_features  # noqa: E402

FP_ROOT = os.environ.get("FP_ROOT",
                         os.path.join(os.path.dirname(HERE), "footprint"))
PROC = os.path.join(FP_ROOT, "processed")
# bundled model ships inside this folder; a retrained one can live in the
# store or be pointed to with FP_MODEL_DIR
MODEL_DIR = (os.environ.get("FP_MODEL_DIR")
             or (os.path.join(HERE, "model")
                 if os.path.exists(os.path.join(HERE, "model"))
                 else os.path.join(FP_ROOT, "model")))
JOURNAL = os.path.join(HERE, "watch_log.csv")

BAR_MS = 300_000
WINDOW_BARS = 576           # 48h = 2x EMA span -> converged rolling features
RISK_USD = float(os.environ.get("FP_RISK_USD", 25.0))
REST = "https://fapi.binance.com/fapi/v1/aggTrades?{}"

_meta = json.load(open(os.path.join(MODEL_DIR, "meta.json")))
_booster = lgb.Booster(model_file=os.path.join(MODEL_DIR, "lgbm_deploy.txt"))
_iso = joblib.load(os.path.join(MODEL_DIR, "isotonic.pkl"))


def mt5_lots(sl_dist):
    """BTCUSD.p lots from the desk universe if present (default contract 1)."""
    contract = 1.0
    uni = os.path.join(os.path.dirname(HERE), "repos", "karan-ts", "desk",
                       "mt5_symbols_universe.json")
    try:
        for _, lst in json.load(open(uni)).items():
            for s in lst:
                if s["name"] == "BTCUSD.p":
                    contract = float(s.get("trade_contract_size") or 1.0)
    except Exception:
        pass
    return max(0.01, round(RISK_USD / max(sl_dist * contract, 1e-9), 2))


class FootprintEngine:
    """Incremental footprint: raw trades in -> closed bars/edges/grid out."""

    def __init__(self):
        self.ts, self.px, self.qty, self.buys = [], [], [], []
        self.bars = self.edges = self.grid = None

    # ---------- live path: raw trades ----------
    def add_trades(self, ts_ms, price, qty, taker_buys):
        if len(ts_ms):
            self.ts.extend(np.asarray(ts_ms).tolist())
            self.px.extend(np.asarray(price, float).tolist())
            self.qty.extend(np.asarray(qty, float).tolist())
            self.buys.extend(np.asarray(taker_buys, bool).tolist())
        self._close_completed_bars()

    def _close_completed_bars(self):
        if not self.ts:
            return
        ts = np.asarray(self.ts)
        bids = ts // BAR_MS
        uniq = np.unique(bids)
        if len(uniq) <= 1:
            return  # still forming the current bar
        forming = uniq[-1]
        for cid in uniq[:-1]:
            m = bids == cid
            self._append_bar(cid, ts[m], np.asarray(self.px, float)[m],
                             np.asarray(self.qty, float)[m],
                             np.asarray(self.buys, bool)[m])
        keep = bids == forming
        self.ts = list(np.asarray(self.ts)[keep])
        self.px = list(np.asarray(self.px, float)[keep])
        self.qty = list(np.asarray(self.qty, float)[keep])
        self.buys = list(np.asarray(self.buys, bool)[keep])

    def force_close(self):
        """Close the forming bar too (used in selftest/replay tails)."""
        if self.ts:
            ts = np.asarray(self.ts)
            cid = ts[-1] // BAR_MS
            self._append_bar(cid, ts, np.asarray(self.px, float),
                             np.asarray(self.qty, float),
                             np.asarray(self.buys, bool))
            self.ts, self.px, self.qty, self.buys = [], [], [], []

    # ---------- shared bar assembly ----------
    def _append_bar(self, bar_id, ts, px, q, buys):
        nb = 0 if self.bars is None else len(self.bars)
        bar = pd.DataFrame({
            "ts": [pd.Timestamp(bar_id * BAR_MS, unit="ms", tz="UTC")],
            "open": [px[0]], "high": [px.max()], "low": [px.min()],
            "close": [px[-1]], "n_tr": [len(px)],
            "buy_v": [q[buys].sum()], "sell_v": [q[~buys].sum()],
            "bar_g": [nb]})
        tick = np.rint(px / TICK).astype(np.int64)
        uk, inv = np.unique(nb * 10_000_000 + tick, return_inverse=True)
        n = len(uk)
        lv = pd.DataFrame({
            "bar_g": uk // 10_000_000, "price": (uk % 10_000_000) * TICK,
            "buy_v": np.bincount(inv, weights=q * buys, minlength=n),
            "sell_v": np.bincount(inv, weights=q * (~buys), minlength=n),
            "n_tr": np.bincount(inv, minlength=n).astype(np.int32)})
        rows = []
        lp, bv, sv, nt = (lv.price.to_numpy(), lv.buy_v.to_numpy(),
                          lv.sell_v.to_numpy(), lv.n_tr.to_numpy())
        k = min(12, n)
        for r in range(k):
            j = n - 1 - r
            rows.append((nb, r + 1, 1, lp[j], bv[j], sv[j], nt[j]))
            rows.append((nb, r + 1, -1, lp[r], bv[r], sv[r], nt[r]))
        eg = pd.DataFrame(rows, columns=["bar_g", "rank", "side", "price",
                                          "buy_v", "sell_v", "n_tr"])
        bk = np.rint(px / 1.0).astype(np.int64) + 500_000
        uk2, inv2 = np.unique(nb * 2_000_000 + bk, return_inverse=True)
        m = len(uk2)
        gd = pd.DataFrame({
            "bar_g": uk2 // 2_000_000,
            "bucket_px": (uk2 % 2_000_000 - 500_000) * 1.0,
            "buy_v": np.bincount(inv2, weights=q * buys, minlength=m),
            "sell_v": np.bincount(inv2, weights=q * (~buys), minlength=m),
            "n_tr": np.bincount(inv2, minlength=m).astype(np.int32)})
        self._merge(bar, eg, gd)
        self._trim()

    def append_prepared(self, bar, eg, gd):
        """Replay path: append one already-built bar (renumbered)."""
        nb = len(self.bars)
        bar = bar.copy(); bar["bar_g"] = nb
        eg = eg.copy(); eg["bar_g"] = nb
        gd = gd.copy(); gd["bar_g"] = nb
        self._merge(bar, eg, gd)
        self._trim()

    def _merge(self, bar, eg, gd):
        self.bars = bar if self.bars is None else pd.concat(
            [self.bars, bar], ignore_index=True)
        self.edges = eg if self.edges is None else pd.concat(
            [self.edges, eg], ignore_index=True)
        self.grid = gd if self.grid is None else pd.concat(
            [self.grid, gd], ignore_index=True)

    def _trim(self):
        """Keep WINDOW_BARS bars and RENUMBER bar_g to 0..W-1 everywhere."""
        if self.bars is None or len(self.bars) <= WINDOW_BARS:
            return
        old_lo = int(self.bars["bar_g"].iloc[-WINDOW_BARS])
        self.bars = self.bars.iloc[-WINDOW_BARS:].reset_index(drop=True)
        self.bars["bar_g"] = np.arange(len(self.bars))
        e = self.edges[self.edges.bar_g >= old_lo].copy()
        e["bar_g"] -= old_lo
        g = self.grid[self.grid.bar_g >= old_lo].copy()
        g["bar_g"] -= old_lo
        self.edges, self.grid = e, g

    def prime(self, bars, edges, grid):
        """Warm-start from stored parquet (last WINDOW_BARS)."""
        old_lo = int(bars["bar_g"].iloc[-min(len(bars), WINDOW_BARS)])
        b = bars.iloc[-WINDOW_BARS:].reset_index(drop=True)
        b["bar_g"] = np.arange(len(b))
        e = edges[edges.bar_g >= old_lo].copy(); e["bar_g"] -= old_lo
        g = grid[grid.bar_g >= old_lo].copy(); g["bar_g"] -= old_lo
        self.bars, self.edges, self.grid = b, e, g


# ---------------- event -> model -> ticket ----------------
def evaluate(last, bar_row):
    variant = _meta["variant"]
    side = int(last["side"])
    if variant == "strict" and not bool(last["ev_strict"]):
        return None
    if variant == "weak" and not bool(last["ev_weak"]):
        return None
    if variant == "z2" and not bool(last["ev_z2"]):
        return None
    if side == 0:
        return None
    x = last[_meta["features"]].to_numpy(float).reshape(1, -1)
    p = float(_booster.predict(x, num_iteration=-1)[0])
    # NOTE: the threshold was selected on RAW out-of-fold probabilities, so
    # we gate on the same scale. Isotonic is not used at deploy time (it was
    # fit on OOF fold models; the deploy model's prob scale differs).
    if p < _meta["threshold"]:
        return None
    rng = float(bar_row.high - bar_row.low)
    sl_mult, rr = float(_meta["sl_mult"]), float(_meta["rr"])
    if _meta["entry"] == "limit":
        e_px = float(bar_row.high if side == -1 else bar_row.low)
    else:
        e_px = float(bar_row.close)
    return {"ts": str(bar_row.ts), "side": "BUY" if side == 1 else "SELL",
            "entry_mode": _meta["entry"], "entry": round(e_px, 1),
            "sl": round(e_px - sl_mult * rng * side, 1),
            "tp": round(e_px + side * rr * sl_mult * rng, 1),
            "risk_dist": round(sl_mult * rng, 1), "p": round(p, 3),
            "variant": variant, "horizon_bars": _meta["horizon"]}


def print_ticket(t):
    sz_btc = RISK_USD / max(t["risk_dist"], 1e-9)
    lots = mt5_lots(t["risk_dist"])
    print("=" * 78, flush=True)
    print(f"ORDER TICKET  {t['side']} BTCUSDT   [{t['ts']}]", flush=True)
    print(f"  signal : auction complete ({t['variant']}) - 0-print at "
          f"{'high -> SELL' if t['side'] == 'SELL' else 'low -> BUY'}", flush=True)
    print(f"  model  : P(win)={t['p']:.2f} >= thr {_meta['threshold']:.2f} -> TAKE", flush=True)
    if t["entry_mode"] == "limit":
        print(f"  entry  : LIMIT {t['entry']} (retest of signal-bar extreme; "
              f"cancel after {min(6, t['horizon_bars'])} bars if unfilled)", flush=True)
    else:
        print(f"  entry  : MARKET at bar close ~{t['entry']}", flush=True)
    print(f"  stop   : {t['sl']}   target: {t['tp']}   risk dist: {t['risk_dist']}", flush=True)
    print(f"  size   : {sz_btc:.4f} BTC (Binance)  |  MT5 BTCUSD.p: {lots} lots", flush=True)
    print(f"  plan   : time stop {t['horizon_bars']} bars; SL/TP per ticket; "
          "one position at a time", flush=True)
    with open(JOURNAL, "a") as f:
        f.write(",".join(str(v) for v in
                         (t["ts"], t["side"], t["entry"], t["sl"], t["tp"],
                          t["risk_dist"], t["p"], t["variant"])) + "\n")


def process_bar_close(eng, verbose=False):
    """After a bar closes: features -> event -> model -> ticket."""
    t0 = time.perf_counter()
    if eng.bars is None or len(eng.bars) < 300:
        return None
    ew = edges_wide(eng.edges)
    gf = grid_feats(eng.grid, eng.bars)
    feats = build_features(eng.bars, ew, gf)
    last = feats.iloc[-1]
    bar = eng.bars.iloc[-1]
    t = evaluate(last, bar)
    dt_ms = (time.perf_counter() - t0) * 1000
    if verbose:
        print(f"[{bar.ts}] close={bar.close:.1f} delta={last['delta']:+.2f} "
              f"ev={int(last['ev_strict'])} proc={dt_ms:.0f}ms", flush=True)
    if t:
        print_ticket(t)
    return dt_ms


# ---------------- data loading ----------------
def load_local(day):
    b = pd.read_parquet(os.path.join(PROC, f"bars_BTCUSDT_{day}.parquet"))
    e = pd.read_parquet(os.path.join(PROC, f"edges_BTCUSDT_{day}.parquet")).rename(
        columns={"bar_i": "bar_g"})
    g = pd.read_parquet(os.path.join(PROC, f"grid_BTCUSDT_{day}.parquet")).rename(
        columns={"bar_i": "bar_g"})
    return b, e, g


def concat_days(days):
    bs, es, gs, cum = [], [], [], 0
    for d in days:
        b, e, g = load_local(d)
        bs.append(b)
        e["bar_g"] += cum
        g["bar_g"] += cum
        es.append(e)
        gs.append(g)
        cum += len(b)
    bars = pd.concat(bs, ignore_index=True)
    bars["bar_g"] = np.arange(len(bars))
    return bars, pd.concat(es, ignore_index=True), \
        pd.concat(gs, ignore_index=True)


# ---------------- modes ----------------
def replay(day):
    eng = FootprintEngine()
    warm = [(pd.Timestamp(day) - pd.Timedelta(days=k)).date() for k in (3, 2, 1)]
    try:
        b_all, e_all, g_all = concat_days([str(d) for d in warm])
    except FileNotFoundError:
        b_all, e_all, g_all = concat_days([day])
    eng.prime(b_all, e_all, g_all)
    b_day, e_day, g_day = load_local(pd.Timestamp(day).date())
    print(f"replay {day}: warmup bars={len(eng.bars)}, streaming {len(b_day)} bars",
          flush=True)
    for i in range(len(b_day)):
        eng.append_prepared(b_day.iloc[i:i + 1],
                            e_day[e_day.bar_g == i],
                            g_day[g_day.bar_g == i])
        process_bar_close(eng, verbose=True)


def live():
    eng = FootprintEngine()
    last_id = None
    files = sorted(f for f in os.listdir(PROC) if f.startswith("bars_BTCUSDT"))
    if files:
        last_day = files[-1][-14:-8]
        print(f"warm-starting from local store: {last_day}", flush=True)
        try:
            b, e, g = concat_days([str((pd.Timestamp(last_day) - pd.Timedelta(days=k)).date())
                                   for k in (1, 0)])
            eng.prime(b, e, g)
            print(f"primed {len(eng.bars)} bars; REST tail will fill the gap",
                  flush=True)
        except FileNotFoundError:
            print("warmup parquet missing - features need ~25h before tickets",
                  flush=True)
    print("live: polling fapi/v1/aggTrades every 2s", flush=True)
    while True:
        try:
            args = "symbol=BTCUSDT&limit=1000"
            if last_id is not None:
                args += f"&fromId={last_id + 1}"
            with urllib.request.urlopen(REST.format(urllib.parse.quote(args)),
                                        timeout=10) as r:
                data = json.load(r)
            if data:
                last_id = int(data[-1]["a"])
                eng.add_trades(np.array([t["T"] for t in data], np.int64),
                               np.array([t["p"] for t in data], float),
                               np.array([t["q"] for t in data], float),
                               np.array([not t["m"] for t in data], bool))
        except Exception as e:
            print(f"poll error: {e}; retry in 5s", flush=True)
            time.sleep(5)
        time.sleep(2)


def selftest():
    rng = np.random.default_rng(7)
    n = 600_000
    ts = np.sort(rng.integers(0, 48 * 3600 * 1000, n))  # 48h of trades
    steps = rng.normal(0, 2.0, n)
    px = np.maximum(np.rint((43_000 + np.cumsum(steps)) / TICK), 1).astype(np.int64) * TICK
    q = rng.exponential(0.02, n)
    buys = rng.random(n) > 0.5
    eng = FootprintEngine()
    t0 = time.perf_counter()
    eng.add_trades(ts, px, q, buys)
    eng.force_close()
    build = time.perf_counter() - t0
    print(f"incremental build: {len(eng.bars)} bars from {n/1e3:.0f}k trades "
          f"in {build:.2f}s")
    ref = pd.DataFrame({"ts": ts, "px": px, "q": q, "buys": buys})
    ref["b"] = ref.ts // BAR_MS
    ok = True
    for bi in rng.integers(0, len(eng.bars), 25):
        row = eng.bars.iloc[bi]
        seg = ref[ref.b == bi]
        if (abs(seg.px.max() - row.high) > 1e-6 or abs(seg.px.min() - row.low) > 1e-6
                or abs(seg.q[seg.buys].sum() - row.buy_v) > 1e-6
                or abs(seg.px.iloc[0] - row.open) > 1e-6):
            ok = False
            print("MISMATCH at bar", bi)
    print("aggregator parity:", "PASS" if ok else "FAIL")
    times = []
    for _ in range(10):
        times.append(process_bar_close(eng, verbose=False))
    times = [t for t in times if t is not None]
    if times:
        print(f"bar-close pipeline: mean {np.mean(times):.0f}ms "
              f"p95 {np.quantile(times, 0.95):.0f}ms  (budget 300_000ms/bar)")


def main():
    if "--selftest" in sys.argv:
        selftest()
    elif "--replay" in sys.argv:
        replay(sys.argv[sys.argv.index("--replay") + 1])
    else:
        live()


if __name__ == "__main__":
    main()
