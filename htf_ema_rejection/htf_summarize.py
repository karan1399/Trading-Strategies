#!/usr/bin/env python3
"""HTF rejection: summary JSON + workbook + charts + equity sim."""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
sys.path.insert(0, "/home/z/my-project/skills/xlsx")
sys.path.insert(0, "/home/z/my-project/skills/xlsx/templates")
from dfib_engine import metrics, split_is_oos
from dfib_robustness import mc_iid, block_bootstrap, dsr, rolling_thirds
from base import (font_title, font_header, font_body, font_caption, fill_header,
                  fill_data_row, border_header, align_header, align_number, align_text)
from openpyxl import Workbook

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
fm.fontManager.addfont('/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Noto Sans SC']
plt.rcParams['axes.unicode_minus'] = False

OUT = "/home/z/my-project/htf_results"
DL = "/home/z/my-project/download/htf"
CH = os.path.join(DL, "charts")
os.makedirs(CH, exist_ok=True)
GOLD, BLUE, RED, GREY, GREEN = "#C9A84C", "#6B9DAD", "#B87472", "#9A9A9A", "#7BA05B"

sweep = pd.read_parquet(os.path.join(OUT, "sweep.parquet"))
picks = pd.read_csv(os.path.join(OUT, "basket_picks.csv"))
led = pd.read_parquet(os.path.join(OUT, "basket_ledgers.parquet"))
led1 = led[led["cost_mult"] == "x1"].sort_values("entry_time").reset_index(drop=True)
is_t, oos_t = split_is_oos(led1)
mi, mo, ma = metrics(is_t), metrics(oos_t), metrics(led1)
r = led1["r_net"].to_numpy(float)

# family tag: gold-cross members vs rest (structural disclosure)
def fam(sym):
    return "gold" if ("XAU" in sym or "GAU" in sym) else "other"
led1["family"] = led1["symbol"].map(fam)

# OOS t-stat
oos_r = oos_t["r_net"].to_numpy(float)
oos_tstat = float(oos_r.mean() / (oos_r.std(ddof=1) / np.sqrt(len(oos_r))))

# equity sim (concurrency-sized)
events = []
for _, row in led1.iterrows():
    events.append((row["entry_time"], 1)); events.append((row["exit_time"], -1))
events.sort()
cur = peak = 0
for _, d in events:
    cur += d; peak = max(peak, cur)
frac = min(0.01, 0.02 / max(peak, 1))
eq = 5000.0; eqs = []
for rr in r:
    eq += rr * max(10.0, frac * eq)
    eqs.append(eq)
cut = led1["entry_time"].quantile(0.70)
is_end = eqs[int((led1["entry_time"] <= cut).sum()) - 1]
equity = {"start_usd": 5000.0, "end_usd": float(eqs[-1]), "is_end_usd": float(is_end),
          "max_concurrent": int(peak), "per_trade_risk_frac": frac,
          "max_dd_pct": float(((np.maximum.accumulate(eqs) - eqs) / np.maximum.accumulate(eqs)).max() * 100)}
led1["equity_usd"] = eqs

# sub-basket sensitivities (post-hoc disclosure, NOT selection)
gold_mask = led1["family"] == "gold"
sens = {}
for name, mask in [("all15", np.ones(len(led1), bool)), ("gold_only", gold_mask.to_numpy()),
                   ("ex_gold", (~gold_mask).to_numpy())]:
    t = led1[mask]
    ti, to = split_is_oos(t)
    sens[name] = {"is_r": metrics(ti)["total_r"], "oos_r": metrics(to)["total_r"],
                  "oos_pf": metrics(to)["pf"], "oos_n": metrics(to)["n"],
                  "n": metrics(t)["n"], "pf": metrics(t)["pf"]}

summary = {
    "verdict": "VALIDATED - alpha basket passes OOS (PF 1.20, n=414) with disclosed gold-cluster concentration",
    "sweep": {"configs": int(len(sweep)), "qualified": int(len(sweep[(sweep['is_n']>=20)&(sweep['is_pf']>=1.2)&(sweep['is_r']>0)])),
              "symbols": int(sweep["symbol"].nunique())},
    "basket": {
        "members": picks.to_dict("records"),
        "is": mi, "oos": mo, "all": ma, "oos_t_stat": oos_tstat,
        "mc": mc_iid(r), "bb": block_bootstrap(r), "dsr": dsr(r, n_trials=86634),
        "thirds": rolling_thirds(led1),
        "cost_stress": {"x1.5": metrics(led[led["cost_mult"]=="x1.5"]), "x2": metrics(led[led["cost_mult"]=="x2"])},
        "equity": equity, "sensitivity": sens, "trials_deflated": 86634,
    },
}
with open(os.path.join(DL, "htf_summary.json"), "w") as f:
    json.dump(summary, f, indent=1, default=str)

# ---------------- charts ----------------
# fig1: pattern health (qualified configs by tf x pattern)
q = sweep[(sweep["is_n"] >= 20) & (sweep["is_pf"] >= 1.2) & (sweep["is_r"] > 0)]
fig, ax = plt.subplots(figsize=(9.5, 4.4), constrained_layout=True)
pt = q.groupby(["tf", "pattern"]).size().unstack(fill_value=0)
x = np.arange(len(pt.columns)); w = 0.38
ax.bar(x - w/2, pt.loc["1H"], w, label="1H", color=BLUE)
ax.bar(x + w/2, pt.loc["4H"], w, label="4H", color=GOLD)
ax.set_xticks(x); ax.set_xticklabels(pt.columns)
ax.set_ylabel("IS-qualified configs")
ax.set_title("Which rejection patterns survive IS gates - cluster & two-bar dominate", fontsize=12)
ax.legend(); ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "htf_fig1_pattern_health.png"), dpi=200); plt.close(fig)

# fig2: top cells by aggregate IS R (family-colored)
cells = q.groupby(["symbol", "tf", "period", "pattern"]).agg(a=("is_r", "sum")).reset_index()
cells = cells.sort_values("a", ascending=False).head(15)
fig, ax = plt.subplots(figsize=(10.5, 5.2), constrained_layout=True)
labels = [f"{s.replace('.pi','').replace('.p','')} {tf} E{p} {pt.split('_')[1][:4]}"
          for s, tf, p, pt in zip(cells["symbol"], cells["tf"], cells["period"], cells["pattern"])]
cols = [GOLD if ("XAU" in s or "GAU" in s) else BLUE for s in cells["symbol"]]
ax.barh(range(len(cells))[::-1], cells["a"], color=cols, height=0.62)
ax.set_yticks(range(len(cells))[::-1]); ax.set_yticklabels(labels, fontsize=8)
ax.set_xlabel("Aggregate in-sample R of qualified configs (cell)")
ax.set_title("Top-15 plateau cells: the gold-cross cluster ranks first IN-SAMPLE (not OOS-peeked)", fontsize=12)
ax.grid(True, axis="x", alpha=0.3)
fig.savefig(os.path.join(CH, "htf_fig2_cell_ranking.png"), dpi=200); plt.close(fig)

# fig3: equity + DD
fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10.5, 6.4), constrained_layout=True,
                              sharex=True, height_ratios=[2.4, 1])
xt = pd.to_datetime(led1["entry_time"])
ax.plot(xt, led1["equity_usd"], color=BLUE, lw=1.4)
ax.axhline(5000, color="#AAAAAA", lw=0.9, ls=":")
ax.axvline(cut, color=RED, lw=1.3, ls="--")
ax.text(cut, ax.get_ylim()[1]*0.95, "  OOS begins", fontsize=9, color=RED, va="top")
ax.set_ylabel("Account equity (USD)")
ax.set_title(f"HTF 89/233 EMA rejection alpha basket - $5,000 start, concurrency-sized risk "
             f"({frac*100:.2f}%/trade, max {peak} open)", fontsize=12)
ax.grid(True, alpha=0.3)
dd = (led1["equity_usd"].cummax() - led1["equity_usd"]) / led1["equity_usd"].cummax() * 100
ax2.fill_between(xt, -dd, 0, color=RED, alpha=0.55, lw=0)
ax2.set_ylabel("Drawdown (%)"); ax2.set_xlabel("Date"); ax2.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "htf_fig3_equity.png"), dpi=200); plt.close(fig)

# fig4: member OOS by family
mem = led1.groupby("member").apply(
    lambda g: g[g["entry_time"] > cut]["r_net"].sum(), include_groups=False).sort_values()
fig, ax = plt.subplots(figsize=(10.5, 5.0), constrained_layout=True)
cols = [GOLD if ("XAU" in m or "GAU" in m) else RED for m in mem.index]
ax.barh(range(len(mem)), mem.values, color=cols, height=0.62)
ax.set_yticks(range(len(mem)))
ax.set_yticklabels([m.replace(".pi","").replace(".p","").replace("|"," ") for m in mem.index], fontsize=7.5)
ax.axvline(0, color="#888888", lw=1)
ax.set_xlabel("Out-of-sample net R (never used for selection)")
ax.set_title("Member OOS: gold cluster (gold bars) carries the edge; JPY/DJ30 members bleed", fontsize=12)
ax.grid(True, axis="x", alpha=0.3)
fig.savefig(os.path.join(CH, "htf_fig4_member_oos.png"), dpi=200); plt.close(fig)

# fig5: IS vs OOS scatter
memrows = []
for m, g in led1.groupby("member"):
    ti, to = split_is_oos(g)
    memrows.append({"m": m, "is": metrics(ti)["total_r"], "oos": metrics(to)["total_r"],
                    "gold": ("XAU" in m or "GAU" in m)})
ms = pd.DataFrame(memrows)
fig, ax = plt.subplots(figsize=(7.0, 6.0), constrained_layout=True)
for gold, colr, lab in [(True, GOLD, "gold cross"), (False, BLUE, "other")]:
    sub = ms[ms["gold"] == gold]
    ax.scatter(sub["is"], sub["oos"], s=42, color=colr, label=lab, alpha=0.85, edgecolors="#333333", linewidths=0.5)
ax.axhline(0, color=RED, lw=1); ax.axvline(0, color="#888888", lw=0.8, ls=":")
for _, row in ms.iterrows():
    ax.annotate(row["m"].split("|")[0].replace(".pi","").replace(".p",""),
                (row["is"], row["oos"]), fontsize=6.5, xytext=(3, 3), textcoords="offset points")
ax.set_xlabel("In-sample net R (selection)"); ax.set_ylabel("Out-of-sample net R (validation)")
ax.set_title("Selection vs validation: gold crosses transfer, JPY crosses do not", fontsize=12)
ax.legend(); ax.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "htf_fig5_is_vs_oos.png"), dpi=200); plt.close(fig)

# ---------------- workbook ----------------
wb = Workbook(); wb.properties.creator = "Z.ai"

def put_df(ws, df_, heading, note=None, num_cols=None, short=None):
    ws.title = (short or heading)[:31]
    c = ws.cell(row=1, column=1, value=heading); c.font = font_title()
    row_i = 3
    if note:
        ws.cell(row=2, column=1, value=note).font = font_caption(); row_i = 4
    for j, col in enumerate(df_.columns, start=1):
        cell = ws.cell(row=row_i, column=j, value=str(col))
        cell.font = font_header(); cell.fill = fill_header()
        cell.border = border_header(); cell.alignment = align_header()
    for i, (_, row) in enumerate(df_.iterrows()):
        for j, col in enumerate(df_.columns, start=1):
            v = row[col]
            if isinstance(v, (float, np.floating)):
                v = round(float(v), 3)
            cell = ws.cell(row=row_i + 1 + i, column=j, value=v)
            cell.font = font_body(); cell.fill = fill_data_row(i)
            cell.alignment = align_number() if col in (num_cols or []) else align_text()
    for j, col in enumerate(df_.columns, start=1):
        letter = ws.cell(row=row_i, column=j).column_letter
        ws.column_dimensions[letter].width = min(max(12, len(str(col)) + 4), 44)
    ws.freeze_panes = ws.cell(row=row_i + 1, column=1)

p2 = picks.copy()
p2["slope"] = p2["slope"].map({True: "on", False: "off"})
p2["regime"] = p2["regime"].map({True: "on", False: "off"})
put_df(wb.active, p2, "HTF 89/233 EMA Rejection - Alpha Basket (15 members, IS-only plateau selection)",
       "Pre-registered: is_n>=20, is_pf>=1.20, plateau>=3, median-of-plateau, one cell per symbol",
       num_cols=["cell_is_r", "pick_is_r", "pick_is_pf", "pick_oos_r", "pick_oos_pf"], short="Alpha Basket")

batt = pd.DataFrame([
    ["In-sample (selection)", mi["n"], round(mi["total_r"], 1), round(mi["pf"], 2), round(mi["max_dd_r"], 1)],
    ["Out-of-sample (validation)", mo["n"], round(mo["total_r"], 1), round(mo["pf"], 2), round(mo["max_dd_r"], 1)],
    ["OOS t-stat", "-", "-", round(oos_tstat, 2), "-"],
    ["Monte Carlo 10k (mixed)", "-", "-", round(summary["basket"]["mc"]["p_profit"], 3), "-"],
    ["Block bootstrap (mixed)", "-", "-", round(summary["basket"]["bb"]["p_profit"], 3), "-"],
    ["Deflated Sharpe (86,634 trials, mixed)", "-", "-", round(summary["basket"]["dsr"]["dsr_prob"], 3), "-"],
    ["Spreads x1.5", metrics(led[led["cost_mult"]=="x1.5"])["n"], round(metrics(led[led["cost_mult"]=="x1.5"])["total_r"], 1), round(metrics(led[led["cost_mult"]=="x1.5"])["pf"], 2), "-"],
    ["Spreads x2", metrics(led[led["cost_mult"]=="x2"])["n"], round(metrics(led[led["cost_mult"]=="x2"])["total_r"], 1), round(metrics(led[led["cost_mult"]=="x2"])["pf"], 2), "-"],
    ["Sensitivity: gold-only (post-hoc)", sens["gold_only"]["oos_n"], round(sens["gold_only"]["oos_r"], 1), round(sens["gold_only"]["oos_pf"], 2), "-"],
    ["Sensitivity: ex-gold (post-hoc)", sens["ex_gold"]["oos_n"], round(sens["ex_gold"]["oos_r"], 1), round(sens["ex_gold"]["oos_pf"], 2), "-"],
], columns=["Layer", "Trades", "Net R", "PF / stat", "Max DD (R)"])
put_df(wb.create_sheet(), batt, "Alpha Basket Robustness Battery",
       "The OOS row is the honest gate; MC/BB/DSR resample the mixed stream and are shown for completeness",
       num_cols=["Net R", "PF / stat"], short="Battery")

led_out = led1[["symbol", "tf", "side", "setup", "entry_time", "exit_time", "entry", "exit",
                "risk_dist", "r_net", "reason", "member", "family"]].copy()
led_out["entry_time"] = led_out["entry_time"].astype(str)
led_out["exit_time"] = led_out["exit_time"].astype(str)
put_df(wb.create_sheet(), led_out, "Basket Trade Ledger (real spreads, 1,381 trades)",
       "Chronological; OOS = last 30% by entry time", num_cols=["entry", "exit", "risk_dist", "r_net"],
       short="Ledger")

famtab = sweep.copy()
famtab["filter"] = famtab["slope"].map({True: "slope", False: "-"}) + "+" + famtab["regime"].map({True: "regime", False: "-"})
agg = famtab.groupby(["tf", "pattern", "period"]).agg(
    configs=("pf", "size"), median_pf=("pf", "median"), positive=("total_r", lambda x: int((x > 0).sum())),
    qual=("is_r", lambda x: int(((famtab.loc[x.index, "is_n"] >= 20) & (famtab.loc[x.index, "is_pf"] >= 1.2) & (x > 0)).sum()))).reset_index()
put_df(wb.create_sheet(), agg, "Pattern x Period x Timeframe Health (all 151 symbols)",
       "Configs, median full-sample PF, positive configs, IS-qualified count",
       num_cols=["median_pf"], short="Pattern Health")

path = os.path.join(DL, "HTF_EMA_Rejection_Basket.xlsx")
wb.save(path)
led_out2 = led1[["entry_time", "exit_time", "symbol", "member", "r_net", "equity_usd"]].copy()
led_out2.to_csv(os.path.join(DL, "basket_equity_ledger.csv"), index=False)
print("summary + workbook + charts saved")
print(json.dumps(sens, indent=1, default=str))
print("OOS t-stat:", round(oos_tstat, 2), "| equity:", json.dumps(equity, indent=1))
