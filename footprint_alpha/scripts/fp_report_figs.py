#!/usr/bin/env python3
"""Report figures: forward-return profile by horizon (grouped bars with
t-stats annotated) from fp_events.json - English labels, palette-consistent."""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
fm.fontManager.addfont('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

RES = "/home/z/my-project/footprint/results"
ev = json.load(open(os.path.join(RES, "fp_events.json")))
fwd = ev["forward_bps"]
horizons = [1, 3, 6, 12, 24]
variants = [("strict", "strict 0-print", "#1A1A1A"),
            ("z2", "z2 (top-2 aggregated)", "#C9A84C"),
            ("mirror", "mirror (continuation)", "#B0B0B0")]

fig, ax = plt.subplots(figsize=(9, 4.6), constrained_layout=True)
x = np.arange(len(horizons))
w = 0.26
for i, (key, label, color) in enumerate(variants):
    vals = [fwd[key][str(h)][0] for h in horizons]
    ts = [fwd[key][str(h)][1] for h in horizons]
    bars = ax.bar(x + (i - 1) * w, vals, w, label=label, color=color)
    for b, v, t in zip(bars, vals, ts):
        ax.annotate(f"t={t:+.1f}", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 3 if v >= 0 else -11),
                    ha="center", fontsize=7.5, color="#404040")
ax.axhline(0, color="#606060", lw=0.8)
ax.axhline(10, color="#C0392B", lw=1.0, ls="--")
ax.text(4.32, 10.6, "round-trip taker fees ~10bps", fontsize=8, color="#C0392B", ha="right")
ax.set_xticks(x)
ax.set_xticklabels([f"{h} bar{'s' if h > 1 else ''}\n({h*5} min)" for h in horizons])
ax.set_ylabel("side-adjusted forward return (bps)")
ax.set_title("Auction-complete event: forward returns vs holding horizon (11 months)")
ax.grid(True, axis="y", alpha=0.3)
ax.legend(loc="upper left", bbox_to_anchor=(0.0, 1.0), fontsize=9)
dst = os.path.join(RES, "fig_forward.png")
fig.savefig(dst, dpi=150)
im_h, im_w = plt.imread(dst).shape[:2]
print("saved", dst, "px:", im_w, im_h)
