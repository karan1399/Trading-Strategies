#!/usr/bin/env python3
"""Delta-Fibonacci validation: charts + $5K equity simulation for the best portfolio."""
import os
import sys

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
fm.fontManager.addfont('/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Noto Sans SC']
plt.rcParams['axes.unicode_minus'] = False

OUT = "/home/z/my-project/dfib_results"
DL = "/home/z/my-project/download/dfib"
CH = os.path.join(DL, "charts")
os.makedirs(CH, exist_ok=True)
GOLD, BLUE, RED, GREY = "#C9A84C", "#6B9DAD", "#B87472", "#9A9A9A"

# ---------------- data ----------------
ver = pd.read_csv(os.path.join(OUT, "champions_verification.csv"))
picks = pd.read_csv(os.path.join(OUT, "portfolio_picks.csv"))
led = pd.read_parquet(os.path.join(OUT, "portfolio_ledgers.parquet"))
led = led[led["cost_mult"] == "x1"].sort_values("entry_time").reset_index(drop=True)
sweep = pd.read_parquet(os.path.join(OUT, "sweep_stage1.parquet"))
sweep = sweep[sweep["spread"]]

# ---------------- $5K equity sim (concurrency-aware risk) ----------------
# 15 members trade concurrently: size each trade so total open heat stays sane.
# per-trade risk = min(1%, 2% portfolio heat / observed max concurrent positions)
events = []
for _, r in led.iterrows():
    events.append((r["entry_time"], 1)); events.append((r["exit_time"], -1))
events.sort()
cur = peak = 0
for _, d in events:
    cur += d
    peak = max(peak, cur)
per_trade_frac = min(0.01, 0.02 / max(peak, 1))
print(f"max concurrent positions: {peak} | per-trade risk: {per_trade_frac * 100:.3f}%")
eq = 5000.0
eqs, pnls = [], []
for r in led["r_net"]:
    rk = max(10.0, per_trade_frac * eq)
    pnl = r * rk
    eq += pnl
    eqs.append(eq); pnls.append(pnl)
led["equity_usd"] = eqs
led["pnl_usd"] = pnls
cut = led["entry_time"].quantile(0.70)
is_mask = led["entry_time"] <= cut
print(f"equity: start 5000 -> end {eqs[-1]:.0f} | IS end {led[is_mask]['equity_usd'].iloc[-1]:.0f} | "
      f"max DD {((led['equity_usd'].cummax() - led['equity_usd']) / led['equity_usd'].cummax()).max() * 100:.1f}%")

# ---------------- fig1: their claim vs corrected ----------------
fig, ax = plt.subplots(figsize=(9.5, 4.8), constrained_layout=True)
vals = [ver["their_net_r"].sum() / 1000.0, ver["ours_r_nospread"].sum() / 1000.0,
        ver["ours_r_spread"].sum() / 1000.0]
labels = ["Their audit claim\n(zero spread, full-sample pick)", "Corrected engine\n(spreads OFF)",
          "Corrected engine\n(real spreads)"]
colors = [GREY, BLUE, RED]
bars = ax.bar(labels, vals, color=colors, width=0.55)
ax.bar_label(bars, fmt="%+,.1fkR", fontsize=11, fontweight="bold", padding=6)
ax.margins(y=0.22)
ax.axhline(0, color="#888888", lw=1)
ax.set_ylabel("Total net R (thousands)")
ax.set_title("Delta-Fibonacci 362 champion setups: claimed vs corrected performance", fontsize=12)
ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "dfib_fig1_claim_vs_corrected.png"), dpi=200); plt.close(fig)

# ---------------- fig2: cost cliff scatter ----------------
fig, ax = plt.subplots(figsize=(7.2, 6.0), constrained_layout=True)
x = ver["ours_r_nospread"].clip(-200, 200)
y = ver["ours_r_spread"].clip(-3500, 100)
ax.scatter(x, y, s=14, alpha=0.55, color=BLUE, edgecolors="none")
ax.axhline(0, color=RED, lw=1.2); ax.axvline(0, color="#888888", lw=0.8, ls=":")
ax.set_xlabel("Net R per setup, spreads OFF")
ax.set_ylabel("Net R per setup, real spreads")
ax.set_title("The cost cliff: same 362 setups, spread-charged vs spread-free", fontsize=12)
ax.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "dfib_fig2_cost_cliff.png"), dpi=200); plt.close(fig)

# ---------------- fig3: pooled family health ----------------
summ = pd.read_csv(os.path.join(OUT, "pooled_refined_summary.csv"))
summ["uni"] = summ["config"].str.split("|").str[0]
summ["fam"] = summ["config"].str.split("|").str[1]
base = summ[(summ["config"].str.endswith("|gateoff"))]
fig, ax = plt.subplots(figsize=(9.5, 4.6), constrained_layout=True)
groups = [(f"{u} {f}", g["is_pf"]) for (u, f), g in base.groupby(["uni", "fam"])]
names = [k for k, _ in groups]
data = [v.values for _, v in groups]
bp = ax.boxplot(data, labels=[n.replace(" ", "\n") for n in names], showfliers=False,
                patch_artist=True, medianprops=dict(color="#333333"))
for patch in bp["boxes"]:
    patch.set_facecolor(BLUE); patch.set_alpha(0.55)
ax.axhline(1.0, color=RED, lw=1.4, ls="--")
ax.set_ylabel("In-sample profit factor (pooled)")
ax.set_title("Pooled same-config portfolios, 23 champions vs 59 FX: no family reaches PF 1.0", fontsize=12)
ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "dfib_fig3_pooled_health.png"), dpi=200); plt.close(fig)

# ---------------- fig4: portfolio equity + DD ----------------
fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10.5, 6.4), constrained_layout=True,
                              sharex=True, height_ratios=[2.4, 1])
x_t = pd.to_datetime(led["entry_time"])
ax.plot(x_t, led["equity_usd"], color=BLUE, lw=1.4)
ax.axhline(5000, color="#AAAAAA", lw=0.9, ls=":")
ax.axvline(cut, color=RED, lw=1.3, ls="--")
ax.text(cut, ax.get_ylim()[1] * 0.97, "  OOS begins (edge decays)", fontsize=9, color=RED, va="top")
ax.set_ylabel("Account equity (USD)")
ax.set_title("Best disciplined variant - 15-symbol plateau portfolio, $5,000 start, concurrency-sized risk", fontsize=12)
ax.grid(True, alpha=0.3)
dd = (led["equity_usd"].cummax() - led["equity_usd"]) / led["equity_usd"].cummax() * 100
ax2.fill_between(x_t, -dd, 0, color=RED, alpha=0.55, lw=0)
ax2.set_ylabel("Drawdown (%)"); ax2.set_xlabel("Date")
ax2.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "dfib_fig4_portfolio_equity.png"), dpi=200); plt.close(fig)

# ---------------- fig5: member OOS ----------------
fig, ax = plt.subplots(figsize=(10.5, 4.8), constrained_layout=True)
mem = led.groupby("portfolio_member").apply(
    lambda g: g[~is_mask.reindex(g.index, fill_value=False)]["r_net"].sum(),
    include_groups=False).sort_values()
cols = [GOLD if v > 0 else RED for v in mem.values]
ax.barh(range(len(mem)), mem.values, color=cols, height=0.65)
ax.set_yticks(range(len(mem)))
ax.set_yticklabels([m.replace(".pi", "").replace(".p", "") for m in mem.index], fontsize=8)
ax.axvline(0, color="#888888", lw=1)
ax.set_xlabel("Out-of-sample net R (never used for selection)")
ax.set_title("Portfolio members, out-of-sample: a coin-flip split, total -8.1R", fontsize=12)
ax.grid(True, axis="x", alpha=0.3)
fig.savefig(os.path.join(CH, "dfib_fig5_member_oos.png"), dpi=200); plt.close(fig)

# save equity ledger
led_out = led.copy()
led_out["entry_time"] = led_out["entry_time"].astype(str)
led_out["exit_time"] = led_out["exit_time"].astype(str)
led_out.to_csv(os.path.join(DL, "portfolio_equity_ledger.csv"), index=False)
print("charts + equity ledger saved")
for f in sorted(os.listdir(CH)):
    print(" ", f)
