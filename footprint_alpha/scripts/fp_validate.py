#!/usr/bin/env python3
"""Data integrity: aggTrades-derived footprint bars vs Binance 5m klines.
Joins on timestamp (robust to exchange downtime). Validates OHLC, total
volume and taker-buy volume - the last one proves our is_buyer_maker
interpretation (True -> aggressive SELL) is exactly Binance's."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fp_core import BASE, load_all  # noqa: E402

KL = os.path.join(BASE, "klines")


def rel(a, b_):
    a = np.asarray(a, np.float64)
    b_ = np.asarray(b_, np.float64)
    return np.max(np.abs(a - b_) / np.maximum(np.abs(b_), 1e-9))


def main():
    bars, _, _, days = load_all()
    kfs = sorted(os.listdir(KL))[: len(days)]
    kl = pd.concat([pd.read_parquet(os.path.join(KL, f)) for f in kfs],
                   ignore_index=True)
    kl = kl.rename(columns={c: f"k_{c}" for c in kl.columns if c != "open_ts"})
    m = bars.merge(kl, left_on="ts", right_on="open_ts", how="inner")
    n = len(m)
    print(f"bars={len(bars)} klines={len(kl)} matched on ts={n} ({n/len(bars)*100:.1f}%)")
    if n < len(bars) * 0.98:
        print("VALIDATION: FAIL (timestamp coverage below 98%)")
        return
    def p999(a, b_):
        return np.quantile(np.abs(np.asarray(a) - np.asarray(b_))
                           / np.maximum(np.abs(np.asarray(b_)), 1e-9), 0.999)
    print(f"OHLC p99.9 rel diff: open {p999(m.open, m.k_open):.2e} "
          f"high {p999(m.high, m.k_high):.2e} "
          f"low {p999(m.low, m.k_low):.2e} close {p999(m.close, m.k_close):.2e} "
          f"(max {rel(m.high, m.k_high):.1e})")
    bv = m.buy_v.to_numpy() + m.sell_v.to_numpy()
    print(f"volume        : corr={np.corrcoef(bv, m.k_vol.to_numpy())[0,1]:.6f} "
          f"p99.9 rel diff={p999(bv, m.k_vol.to_numpy()):.2e}")
    print(f"taker-buy vol : corr={np.corrcoef(m.buy_v.to_numpy(), m.k_tbv.to_numpy())[0,1]:.6f} "
          f"p99.9 rel diff={p999(m.buy_v.to_numpy(), m.k_tbv.to_numpy()):.2e}")
    print(f"TOTALS        : agg vol={bv.sum():.1f} BTC  kline vol={m.k_vol.sum():.1f} BTC")
    ok = (p999(m.open, m.k_open) < 1e-5 and p999(m.high, m.k_high) < 1e-5
          and p999(m.low, m.k_low) < 1e-5 and p999(m.close, m.k_close) < 1e-5
          and p999(bv, m.k_vol.to_numpy()) < 2.5e-2
          and p999(m.buy_v.to_numpy(), m.k_tbv.to_numpy()) < 2.5e-2
          and np.corrcoef(bv, m.k_vol.to_numpy())[0, 1] > 0.9999
          and np.corrcoef(m.buy_v.to_numpy(), m.k_tbv.to_numpy())[0, 1] > 0.9999)
    print("VALIDATION:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
