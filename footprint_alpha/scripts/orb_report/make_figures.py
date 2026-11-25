#!/usr/bin/env python3
"""Report figures: basket OOS equity curve + monthly regime context bars."""
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

RES = "/home/z/my-project/repos/karan-ts/ORB/results"
OUT = "/home/z/my-project/scripts/orb_report/assets"
os.makedirs(OUT, exist_ok=True)
GOLD = "#C9A84C"; DARK = "#1A1A1A"; BLUE = "#6B9DAD"; RED = "#B87472"; GREEN = "#7BA68A"

# Figure 1: basket OOS equity curve
eq = pd.read_csv(os.path.join(RES, "evolved_basket_equity.csv"))
eq["entry_time"] = pd.to_datetime(eq["entry_time"])
fig, ax = plt.subplots(figsize=(10, 5.2), constrained_layout=True)
ax.plot(eq.entry_time, eq.cum_r, color=DARK, lw=1.8, marker="o", ms=4,
        mfc=GOLD, mec=DARK, label="Basket cumulative R (OOS)")
ax.axhline(0, color="#999999", lw=0.8, ls="--")
ax.fill_between(eq.entry_time, eq.cum_r, 0, where=eq.cum_r >= 0,
                color=GOLD, alpha=0.15, interpolate=True)
ax.fill_between(eq.entry_time, eq.cum_r, 0, where=eq.cum_r < 0,
                color=RED, alpha=0.15, interpolate=True)
ax.set_xlabel("Entry time (UTC, OOS window)")
ax.set_ylabel("Cumulative R")
ax.set_title("Pre-registered Alpha Basket - Out-of-Sample Equity (frozen configs, 2 sleeves)",
             fontsize=12, color=DARK)
ax.grid(True, alpha=0.3)
ax.legend(loc="upper left", frameon=False)
fig.savefig(os.path.join(OUT, "fig_basket_equity.png"), dpi=200)
plt.close(fig)

# Figure 2: monthly regime context (4H close-to-close % by month)
months = ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
data = {}
for sym in ["SP500.p", "NAS100.p", "BTCUSD.pi", "XAUUSD.pi"]:
    df = pd.read_parquet(f"/home/z/my-project/repos/karan-ts/historical_parquet/4H/{sym}.parquet")
    df["dt"] = pd.to_datetime(df["datetime"], utc=True)
    df["m"] = df.dt.dt.strftime("%Y-%m")
    ret = df.groupby("m").apply(lambda g: (g.close.iloc[-1] / g.close.iloc[0] - 1) * 100,
                                include_groups=False)
    data[sym] = [ret.get(m, np.nan) for m in months]
x = np.arange(len(months)); w = 0.2
fig, ax = plt.subplots(figsize=(10, 5.0), constrained_layout=True)
for k, (sym, vals) in enumerate(data.items()):
    ax.bar(x + (k - 1.5) * w, vals, w, label=sym,
           color=[BLUE, GREEN, GOLD, RED][k], alpha=0.9)
ax.axhline(0, color="#666666", lw=0.8)
ax.set_xticks(x); ax.set_xticklabels(months)
ax.set_ylabel("Month % change (4H closes)")
ax.set_title("Regime context: all four asset classes roll over into the November risk-off window",
             fontsize=12, color=DARK)
ax.grid(True, alpha=0.3, axis="y")
ax.legend(loc="lower left", frameon=False, ncols=4)
fig.savefig(os.path.join(OUT, "fig_regime.png"), dpi=200)
plt.close(fig)

from PIL import Image
for f in ["fig_basket_equity.png", "fig_regime.png"]:
    im = Image.open(os.path.join(OUT, f))
    print(f, im.size, os.path.getsize(os.path.join(OUT, f)) // 1024, "KB")
