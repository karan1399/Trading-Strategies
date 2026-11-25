#!/usr/bin/env python3
"""Report figures for the VC audit: gold OOS equity (a-priori blueprint spec)
and the exit-lab asset-conditionality bars."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
fm.fontManager.addfont('/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf')
fm.fontManager.addfont('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Noto Sans SC']
plt.rcParams['axes.unicode_minus'] = False
import numpy as np
import pandas as pd
import os
import sys

ROOT = "/home/z/my-project"
sys.path.insert(0, os.path.join(ROOT, "Volatility_compression"))
from volatility_compression_evolved import (SymbolFeatures, EvolvedConfig, run_split,
                                            metrics)

OUT = os.path.join(ROOT, "scripts/vc_report/assets")
os.makedirs(OUT, exist_ok=True)
GOLD = "#C9A84C"; DARK = "#1A1A1A"; BLUE = "#6B9DAD"; RED = "#B87472"; GREEN = "#7BA68A"

# ---- Figure 1: XAUUSD 4H a-priori blueprint spec, OOS equity by exit mode
f = SymbolFeatures("XAUUSD.pi", "4H")
fig, ax = plt.subplots(figsize=(10, 5.2), constrained_layout=True)
for ex, color in [("fixed_3.0r", BLUE), ("fixed_4.0r", GOLD), ("chandelier", RED)]:
    cfg = EvolvedConfig(symbol="XAUUSD.pi", tf="4H", exit_mode=ex)
    tr_all, tr_is, tr_oos = run_split(f, cfg)
    tr_oos = sorted(tr_oos, key=lambda t: t["entry_time"])
    cum = np.cumsum([t["r_net"] for t in tr_oos])
    times = [pd.Timestamp(t["entry_time"]) for t in tr_oos]
    m = metrics(tr_oos)
    ax.plot(times, cum, lw=1.8, color=color,
            label=f"{ex}  (OOS {m['net_r']:+.1f}R, PF {m['pf']:.2f}, n={m['n']})")
ax.axhline(0, color="#999999", lw=0.8, ls="--")
ax.set_title("XAUUSD.pi 4H - a-priori blueprint spec (k=3, EMA200, no tuning), "
             "out-of-sample equity, both-fill spread costs", fontsize=12, color=DARK)
ax.set_ylabel("Cumulative R")
ax.grid(True, alpha=0.3)
ax.legend(loc="upper left", frameon=False)
fig.savefig(os.path.join(OUT, "fig_gold_oos_equity.png"), dpi=200)
plt.close(fig)

# ---- Figure 2: exit lab grouped bars (all-sample R)
lab = pd.read_csv(os.path.join(ROOT, "Volatility_compression/results/vc_evolved_exitlab.csv"))
sel = lab[lab.sym.isin(["XAUUSD.pi", "XAUAUD.pi", "BTCUSD.pi"]) & (lab.tf == "4H")]
exits = ["fixed_2.0r", "fixed_3.0r", "fixed_4.0r", "trail_1.0r", "chandelier"]
fig, ax = plt.subplots(figsize=(10, 5.0), constrained_layout=True)
x = np.arange(len(exits)); w = 0.26
for k, (sym, color) in enumerate([("XAUUSD.pi", GOLD), ("XAUAUD.pi", BLUE), ("BTCUSD.pi", RED)]):
    vals = [float(sel[(sel.sym == sym) & (sel.exit == e)].all_r.iloc[0]) for e in exits]
    ax.bar(x + (k - 1) * w, vals, w, label=f"{sym} 4H", color=color, alpha=0.9)
ax.axhline(0, color="#666666", lw=0.8)
ax.set_xticks(x)
ax.set_xticklabels([e.replace("_", " ") for e in exits])
ax.set_ylabel("All-sample net R (both-fill spreads)")
ax.set_title("Exit lab: identical signals, exit varied - the Chandelier is a crypto tool, "
             "not a gold tool", fontsize=12, color=DARK)
ax.grid(True, alpha=0.3, axis="y")
ax.legend(loc="upper right", frameon=False, ncols=3)
fig.savefig(os.path.join(OUT, "fig_exit_lab.png"), dpi=200)
plt.close(fig)

from PIL import Image
for fn in ["fig_gold_oos_equity.png", "fig_exit_lab.png"]:
    p = os.path.join(OUT, fn)
    im = Image.open(p)
    print(fn, im.size, os.path.getsize(p) // 1024, "KB")
