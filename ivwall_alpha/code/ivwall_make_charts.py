#!/usr/bin/env python3
"""Build summary.json (single source of truth) + 6 report charts."""
import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivwall_engine import Config, run_config, list_symbols, metrics
from ivwall_robustness import mc_iid, block_bootstrap, dsr, rolling_thirds
from ivwall_runner import classify

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
fm.fontManager.addfont('/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Noto Sans SC']
plt.rcParams['axes.unicode_minus'] = False

OUT = "/home/z/my-project/ivwall_results"
DL = "/home/z/my-project/download/ivwall"
CH = os.path.join(DL, "charts")
os.makedirs(CH, exist_ok=True)

ACCENT = "#C9A84C"   # IG-1 ink gold accent
MORANDI = ["#6B9DAD", "#C49B72", "#7BA68A", "#B87472", "#9687A8", "#C8B87C"]
GREY = "#B0B0B0"

FX = [s for s in list_symbols("15M") if classify(s) == "fx"]
FX_1H = [s for s in list_symbols("1H") if classify(s) == "fx"]
FX_4H = [s for s in list_symbols("4H") if classify(s) == "fx"]
FX_5M = [s for s in list_symbols("5M") if classify(s) == "fx"]

flag = pd.read_parquet(os.path.join(OUT, "trades_flagship.parquet"))
bask = pd.read_parquet(os.path.join(OUT, "trades_basket.parquet"))
_b = bask.copy()
_qb = _b["entry_time"].quantile(0.70)
_g = _b[_b.entry_time <= _qb].groupby("symbol")["r_net"].sum().sort_values(ascending=False)
syms_basket = _g.index.tolist()


def split(tr):
    q = tr["entry_time"].quantile(0.70)
    return tr[tr.entry_time <= q], tr[tr.entry_time > q]


summary = {}
summary["flagship"] = {"full": metrics(flag), "is": metrics(split(flag)[0]), "oos": metrics(split(flag)[1])}
summary["basket"] = {"full": metrics(bask), "is": metrics(split(bask)[0]), "oos": metrics(split(bask)[1])}
summary["basket_symbols"] = syms_basket

rf, rb = flag["r_net"].values, bask["r_net"].values
srs = []
for f in glob.glob(os.path.join(OUT, "trades_fade*.parquet")):
    t = pd.read_parquet(f, columns=["r_net"])
    if len(t) > 30:
        srs.append(t["r_net"].mean() / (t["r_net"].std(ddof=1) + 1e-12))
v_sr, n_trials = float(np.var(srs)), max(96, len(srs))
summary["dsr_params"] = {"v_sr": v_sr, "n_trials": n_trials, "n_sr_configs": len(srs)}
summary["flagship"]["mc"] = mc_iid(rf, n=10000)
summary["flagship"]["bb"] = block_bootstrap(rf, n=5000)
summary["flagship"]["dsr"] = dsr(rf, n_trials=n_trials, v_sr=v_sr)
summary["basket"]["mc"] = mc_iid(rb, n=10000)
summary["basket"]["bb"] = block_bootstrap(rb, n=5000)
summary["basket"]["dsr"] = dsr(rb, n_trials=n_trials, v_sr=v_sr)

for vn, tr in [("flagship", flag), ("basket", bask)]:
    thirds = rolling_thirds(tr)
    summary[vn]["thirds"] = [{"pf": t["pf"], "total_r": t["total_r"], "n": t["n"]} for t in thirds]

# per-class breakdown of the Stage A pooled 2SD fade (all classes)
trA = pd.read_parquet(os.path.join(OUT, "trades_fade_touch_sd2_ewma_cc.parquet"))
trA["iclass"] = trA["symbol"].map(classify)
summary["class_breakdown"] = {c: metrics(g) for c, g in trA.groupby("iclass")}

# cost stress + timeframe checks (re-run engine)
cs = run_config("15M", FX, Config(name="cs", strategy="fade_touch", vol_estimator="ewma_cc",
              target_mode="wall_m", sd_wall=2.0, target_m=0.5, buffer_atr=1.0,
              min_rr=0.3, cost_mult=2.0))
summary["cost_stress_2x"] = metrics(cs)
tfres = {}
for tf, syms in [("1H", FX_1H), ("4H", FX_4H), ("5M", FX_5M)]:
    t = run_config(tf, syms, Config(name="tf", strategy="fade_touch", vol_estimator="ewma_cc",
                target_mode="wall_m", sd_wall=2.0, target_m=0.5, buffer_atr=1.0, min_rr=0.3))
    tfres[tf] = metrics(t)
summary["timeframes"] = tfres

# stage tables
stA = pd.read_csv(os.path.join(OUT, "stage_a_15M.csv"))
stB = pd.read_csv(os.path.join(OUT, "stage_b_15M.csv"))
summary["stageA_top"] = stA.sort_values("is_total_r", ascending=False).head(8).to_dict("records")
summary["stageB_top"] = stB[stB["is_n"] >= 80].sort_values("oos_pf", ascending=False).head(8).to_dict("records")
summary["grid_counts"] = {"stageA_configs": len(stA), "stageB_configs": len(stB), "symbols_total": 151,
                          "symbols_fx": len(FX), "trials_total": len(stA) + len(stB)}

with open(os.path.join(DL, "summary.json"), "w") as f:
    json.dump(summary, f, indent=1, default=str)
print("summary.json written")

# ---------------- charts ----------------
def eq(tr):
    return np.cumsum(tr["r_net"].values)

# Fig 1: equity curves flagship vs basket
fig, ax = plt.subplots(figsize=(10, 5.2), constrained_layout=True)
for tr, lab, col in [(flag, "Flagship — all 59 FX symbols (523 trades)", MORANDI[0]),
                     (bask, "Basket overlay — top-20 IS-selected (189 trades)", ACCENT)]:
    ax.plot(range(1, len(tr) + 1), eq(tr), color=col, lw=1.8, label=lab)
qf = (flag["entry_time"] <= flag["entry_time"].quantile(0.70)).sum()
ax.axvline(qf, color="#888888", ls="--", lw=1.0)
ax.text(qf + 6, ax.get_ylim()[0] + 4, "IS | OOS split (70%)", fontsize=9, color="#666666")
ax.set_xlabel("Trade number (chronological)"); ax.set_ylabel("Cumulative net P&L (R)")
ax.set_title("IV Wall Fade — cumulative equity by trade, FX universe, 15M bars", fontsize=12)
ax.grid(True, alpha=0.3); ax.legend(loc="upper left", fontsize=9)
ax.axhline(0, color="#AAAAAA", lw=0.8)
fig.savefig(os.path.join(CH, "fig1_equity.png"), dpi=200); plt.close(fig)

# Fig 2: MC histograms
fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), constrained_layout=True, sharey=False)
rng = np.random.default_rng(7)
for axi, r, lab, col in [(axes[0], rf, "Flagship (all-FX)", MORANDI[0]), (axes[1], rb, "Basket overlay", ACCENT)]:
    idx = rng.integers(0, len(r), size=(10000, len(r)))
    tot = r[idx].sum(axis=1)
    axi.hist(tot, bins=48, color=col, alpha=0.75, edgecolor="white", linewidth=0.2)
    axi.axvline(r.sum(), color="#333333", lw=1.4)
    axi.axvline(0, color="#B87472", lw=1.2, ls="--")
    axi.set_title(f"{lab}\nP(profit)={(tot > 0).mean():.3f}   5th pct={np.percentile(tot, 5):.0f}R", fontsize=10)
    axi.set_xlabel("Bootstrap total net P&L (R)")
axes[0].set_ylabel("Frequency")
fig.suptitle("Monte Carlo iid resampling of trade sequence (10,000 draws)", fontsize=12)
fig.savefig(os.path.join(CH, "fig2_mc.png"), dpi=200); plt.close(fig)

# Fig 3: class breakdown
cb = summary["class_breakdown"]
order = sorted(cb.keys(), key=lambda k: -cb[k]["total_r"])
vals = [cb[k]["total_r"] for k in order]
cols = [ACCENT if k == "fx" else GREY for k in order]
fig, ax = plt.subplots(figsize=(8, 4.4), constrained_layout=True)
ax.bar(order, vals, color=cols, width=0.55)
for i, v in enumerate(vals):
    ax.text(i, v + (6 if v >= 0 else -14), f"{v:.0f}R\n(n={cb[order[i]]['n']})", ha="center", fontsize=9)
ax.axhline(0, color="#888888", lw=1)
ax.set_ylabel("Net P&L (R)"); ax.set_title("Stage A pooled 2SD wall-fade — P&L by asset class", fontsize=12)
ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "fig3_class.png"), dpi=200); plt.close(fig)

# Fig 4: rolling thirds PF
fig, ax = plt.subplots(figsize=(8, 4.4), constrained_layout=True)
labels = ["Third 1 (oldest)", "Third 2", "Third 3 (newest)"]
x = np.arange(3)
for i, (vn, col) in enumerate([("flagship", MORANDI[0]), ("basket", ACCENT)]):
    pfs = [t["pf"] for t in summary[vn]["thirds"]]
    ax.bar(x + (i - 0.5) * 0.36, pfs, width=0.34, color=col, label=vn)
ax.axhline(1.0, color="#B87472", ls="--", lw=1.2)
ax.text(2.42, 1.03, "break-even", fontsize=8, color="#B87472")
ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=9)
ax.set_ylabel("Profit factor"); ax.set_title("Walk-forward stability — profit factor by chronological third", fontsize=12)
ax.legend(fontsize=9); ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "fig4_thirds.png"), dpi=200); plt.close(fig)

# Fig 5: underwater curve flagship
eqf = eq(flag)
runmax = np.maximum.accumulate(eqf)
dd = eqf - runmax
fig, ax = plt.subplots(figsize=(10, 3.6), constrained_layout=True)
ax.fill_between(range(1, len(dd) + 1), dd, 0, color=MORANDI[3], alpha=0.6, lw=0)
ax.set_xlabel("Trade number (chronological)"); ax.set_ylabel("Drawdown (R)")
ax.set_title("Flagship underwater equity (peak-to-trough drawdown in R)", fontsize=12)
ax.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "fig5_dd.png"), dpi=200); plt.close(fig)

# Fig 6: timeframe comparison
tf = summary["timeframes"]
names = ["15M\n(chosen)", "1H", "4H", "5M"]
pfs = [summary["flagship"]["full"]["pf"], tf["1H"]["pf"], tf["4H"]["pf"], tf["5M"]["pf"]]
ns = [summary["flagship"]["full"]["n"], tf["1H"]["n"], tf["4H"]["n"], tf["5M"]["n"]]
cols = [ACCENT, GREY, GREY, GREY]
fig, ax = plt.subplots(figsize=(8, 4.4), constrained_layout=True)
ax.bar(names, pfs, color=cols, width=0.5)
for i, (p, n_) in enumerate(zip(pfs, ns)):
    ax.text(i, p + 0.02, f"PF {p:.2f}\nn={n_}", ha="center", fontsize=9)
ax.axhline(1.0, color="#B87472", ls="--", lw=1.2)
ax.set_ylabel("Profit factor"); ax.set_ylim(0, 1.7)
ax.set_title("Same strategy across timeframes — the edge is 15M-specific", fontsize=12)
ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "fig6_tf.png"), dpi=200); plt.close(fig)

for f in sorted(glob.glob(os.path.join(CH, "*.png"))):
    from PIL import Image
    im = Image.open(f); print(os.path.basename(f), im.size)
print("charts done")
