#!/usr/bin/env python3
"""Count signal rejection reasons for ivwall/orb replay to find the n=0 cause."""
import sys

import numpy as np

sys.path.insert(0, "/home/z/my-project/engine")
import unified_alpha_pilot as u

# --- ivwall EURUSD
df = u._load_parquet("EURUSD.pi", "15M")
o, h, l, c, tm = u._closed_arrays(df)
n = len(c)
sig = u.ewma_sigma(c, 0.94)
atr = u.wilder_atr(h, l, c, 14)
reasons = {}
sigs = 0
for i in range(60, n - 2):
    spans = u.session_spans_from(tm[:i + 1], 2)
    s = u.sig_ivwall_at(o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1], tm[:i + 1], atr,
                        sig[:i + 1], i, {"broker_utc_offset": 2, "sd_wall": 2.0,
                                          "target_m": 0.5, "buffer_atr": 1.0,
                                          "skip_first_bars": 1}, spans)
    if s is None:
        # why? probe internals
        offset = 2
        sid = u._broker_day(tm[i], offset)
        start = i
        while start > 0 and u._broker_day(tm[start - 1], offset) == sid:
            start -= 1
        if i == start or i - start < 1:
            reasons["session_start/skip"] = reasons.get("session_start/skip", 0) + 1
            continue
        sb = sig[start - 1] if start - 1 >= 0 else 0.0
        if not np.isfinite(sb) or sb <= 0:
            reasons["sigma<=0"] = reasons.get("sigma<=0", 0) + 1
            continue
        sess_open = o[start]
        exp_h = float(np.median(spans)) if len(spans) >= 3 else 24.0
        em = sess_open * sb * np.sqrt(max(exp_h, 0.25) * 4.0)
        up, dn = sess_open + 2 * em, sess_open - 2 * em
        if h[i] >= up and c[i] <= up:
            sigs += 1
        elif l[i] <= dn and c[i] >= dn:
            sigs += 1
    else:
        sigs += 1
print("ivwall EURUSD: touch+closebackinside signals:", sigs, "| rejection reasons:", reasons)

# sample EM magnitudes vs bar range
spans = u.session_spans_from(tm, 2)
print("session spans (h):", [round(s, 1) for s in spans[:6]])
i = n - 3
start = i
while start > 0 and u._broker_day(tm[start - 1], 2) == u._broker_day(tm[i], 2):
    start -= 1
sb = sig[start - 1]
em = o[start] * sb * np.sqrt(24.0 * 4.0)
print(f"sample session {u._broker_day(tm[start],2)}: sigma={sb:.6f} EM={em:.5f} ATR={atr[i]:.5f} "
      f"bar_range={h[i]-l[i]:.5f} wall_dist={2*em:.5f}")

# --- orb NAS100
df = u._load_parquet("NAS100.p", "15M")
o, h, l, c, tm = u._closed_arrays(df)
n = len(c)
df4 = u._load_parquet("NAS100.p", "H4")
htf_c4 = df4["close"].to_numpy(float)
params = {"anchor_utc": "14:30", "or_minutes": 30, "signal_window_hours": 6.0,
          "htf_trend": True, "htf_ema": 200, "exit_r": 2.0, "sl_mode": "midpoint"}
breaks = 0
gate_kills = 0
no_anchor = 0
from collections import Counter
hrs = Counter()
for i in range(200, n - 2):
    s = u.sig_orb_at(o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1], tm[:i + 1], None, htf_c4, i, params)
    if s:
        breaks += 1
# inspect bar coverage around anchor
t0 = tm[200] // 1440
tmod = tm % 1440
anchor_bars = ((tmod >= 870) & (tmod < 900)).sum()
print(f"orb NAS100: signals={breaks} over {n-202} bars; bars opening in [14:30,15:00) UTC: {anchor_bars}")
print("hours histogram sample:", sorted(Counter((tm[200:n] // 60) % 24).items())[:24])
