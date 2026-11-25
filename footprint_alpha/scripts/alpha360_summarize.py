#!/usr/bin/env python3
"""Alpha360: equity sim + charts + workbook (incl. MT5 calibration sheet)."""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/z/my-project/scripts")
sys.path.insert(0, "/home/z/my-project/skills/xlsx")
sys.path.insert(0, "/home/z/my-project/skills/xlsx/templates")
from dfib_engine import load_symbol, metrics, split_is_oos, wilder_atr, ema
from alpha360_common import classify_family
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

OUT = "/home/z/my-project/alpha360_results"
DL = "/home/z/my-project/download/alpha360"
CH = os.path.join(DL, "charts")
os.makedirs(CH, exist_ok=True)
GOLD, BLUE, RED, GREY, GREEN = "#C9A84C", "#6B9DAD", "#B87472", "#9A9A9A", "#7BA05B"

sweep = pd.read_parquet(os.path.join(OUT, "sweep_a.parquet"))
try:
    sweepb = pd.read_parquet(os.path.join(OUT, "sweep_b.parquet"))
except Exception:
    sweepb = pd.DataFrame()
summary_c = json.load(open(os.path.join(OUT, "stage_c_summary.json")))

# MT5 contract-size assumptions for lot sizing (VERIFY in MT5 symbol specs)
CONTRACT = {"gold": 100.0, "metals_other": 5000.0, "jpy": 100000.0, "crypto": 1.0,
            "usd_major": 100000.0, "energy": 1000.0, "index": 10.0, "fx_cross": 100000.0}


def fam(sym):
    return classify_family(sym)


def equity_sim(led1):
    events = []
    for _, row in led1.iterrows():
        events.append((row["entry_time"], 1)); events.append((row["exit_time"], -1))
    events.sort()
    cur = peak = 0
    for _, d in events:
        cur += d; peak = max(peak, cur)
    frac = min(0.01, 0.02 / max(peak, 1))
    r = led1["r_net"].to_numpy(float)
    eq = 5000.0; eqs = []
    for rr in r:
        eq += rr * max(10.0, frac * eq)
        eqs.append(eq)
    cut = led1["entry_time"].quantile(0.70)
    is_end = eqs[int((led1["entry_time"] <= cut).sum()) - 1]
    return {
        "start_usd": 5000.0, "end_usd": float(eqs[-1]), "is_end_usd": float(is_end),
        "max_concurrent": int(peak), "per_trade_risk_frac": frac,
        "max_dd_pct": float(((np.maximum.accumulate(eqs) - eqs) / np.maximum.accumulate(eqs)).max() * 100),
    }, eqs


def load_basket(tag):
    picks = pd.read_csv(os.path.join(OUT, f"basket_picks_{tag}.csv"))
    led = pd.read_parquet(os.path.join(OUT, f"basket_ledgers_{tag}.parquet"))
    led1 = led[led["cost_mult"] == "x1"].sort_values("entry_time").reset_index(drop=True)
    return picks, led, led1


picks_d, led_d, led1_d = load_basket("div")
picks_c, led_c, led1_c = load_basket("conc")
base = pd.read_parquet("/home/z/my-project/htf_results/basket_ledgers.parquet")
led1_b = base[base["cost_mult"] == "x1"].sort_values("entry_time").reset_index(drop=True)

eq_d, eqs_d = equity_sim(led1_d)
eq_c, eqs_c = equity_sim(led1_c)
led1_d["equity_usd"] = eqs_d
led1_c["equity_usd"] = eqs_c
led1_d["family"] = led1_d["symbol"].map(fam)

r_d = led1_d["r_net"].to_numpy(float)
is_d, oos_d = split_is_oos(led1_d)
mi_d, mo_d = metrics(is_d), metrics(oos_d)
oos_r = oos_d["r_net"].to_numpy(float)
oos_tstat = float(oos_r.mean() / (oos_r.std(ddof=1) / np.sqrt(len(oos_r)))) if len(oos_r) > 2 else 0.0

# ----------------------------------------------- MT5 calibration levels ------
# Real broker specs from the desk daemon's symbol universe (contract size,
# volume limits, digits, point, spread); quote->USD via latest parquet closes.
UNI = json.load(open("/home/z/my-project/repos/karan-ts/desk/mt5_symbols_universe.json"))
SPEC = {s["name"]: s for lst in UNI.values() for s in lst}
QUOTE_TO_USD_SYMBOL = {"USD": None, "AUD": "AUDUSD", "CAD": "USDCAD", "CHF": "USDCHF",
                       "CNH": "USDCNH", "EUR": "EURUSD", "GBP": "GBPUSD", "HKD": "USDHKD",
                       "JPY": "USDJPY", "NOK": "USDNOK", "NZD": "NZDUSD", "SEK": "USDSEK",
                       "SGD": "USDSGD"}
INVERTED = {"CAD", "CHF", "CNH", "HKD", "JPY", "NOK", "SEK", "SGD"}  # quoted as USDxxx


def quote_of(sym):
    base = sym.upper().split(".")[0]
    if sym.endswith(".p"):
        return "USD"
    return base[3:6] if len(base) == 6 else "USD"


_q2u_cache: dict = {}


def quote_to_usd(qc):
    if qc == "USD":
        return 1.0
    if qc in _q2u_cache:
        return _q2u_cache[qc]
    psym = QUOTE_TO_USD_SYMBOL.get(qc)
    val = 1.0
    if psym:
        try:
            d = load_symbol("4H", psym + ".pi")
            px = float(d["close"].iloc[-1])
            val = (1.0 / px) if qc in INVERTED else px
        except Exception:
            val = 1.0
    _q2u_cache[qc] = val
    return val


mt5_rows = []
for _, p in picks_d.iterrows():
    sym, tf = p["symbol"], p["tf"]
    try:
        d = load_symbol(tf, sym)
    except Exception:
        continue
    o = d["open"].to_numpy(float); h = d["high"].to_numpy(float)
    l = d["low"].to_numpy(float); c = d["close"].to_numpy(float)
    atr = wilder_atr(h, l, c, 14)
    ef, es = ema(c, int(p["fast"])), ema(c, int(p["slow"]))
    i = len(c) - 1
    zone_lo, zone_hi = min(ef[i], es[i]), max(ef[i], es[i])
    famname = fam(sym)
    spec = SPEC.get(sym, {})
    contract = float(spec.get("trade_contract_size", 0) or 0)
    if contract <= 0:
        contract = {"gold": 100.0, "metals_other": 5000.0, "jpy": 100000.0, "crypto": 1.0,
                    "usd_major": 100000.0, "energy": 1000.0, "index": 10.0,
                    "fx_cross": 100000.0}.get(famname, 100000.0)
    qc = quote_of(sym)
    q2u = quote_to_usd(qc)
    sl_dist = 1.5 * atr[i]          # sizing example: structural floor
    risk_usd = 25.0                  # 0.5% of $5,000
    lots_raw = risk_usd / (sl_dist * contract * q2u) if sl_dist > 0 else 0.0
    vmin = float(spec.get("volume_min", 0) or 0.01)
    vstep = float(spec.get("volume_step", 0) or 0.01)
    lots = max(vmin, round(lots_raw / vstep) * vstep) if lots_raw > 0 else 0.0
    spread_px = float(spec.get("spread", 0) or 0) * float(spec.get("point", 0) or 0)
    exit_desc = (f"TP {p['exit'].split('_')[1].replace('r', 'R ')}" if "fixed" in p["exit"]
                 else f"trail {p['exit'].split('_')[1].replace('r', 'R')} step, BE +1R")
    mt5_rows.append({
        "MT5 symbol": sym, "chart TF": tf, "pattern": p["pattern"],
        "EMA zone (fast/slow)": f"{p['fast']}/{p['slow']} {p['reject']}",
        "overlay": p["overlay"], "gates": f"slope={'on' if p['slope'] else 'off'}/regime={'on' if p['regime'] else 'off'}",
        "exit plan": exit_desc,
        "last close": round(float(c[i]), 3),
        "EMA fast now": round(float(ef[i]), 3),
        "EMA slow now": round(float(es[i]), 3),
        "ATR14 now": round(float(atr[i]), 3),
        "watch zone": f"{round(zone_lo, 3)} - {round(zone_hi, 3)}",
        "SL dist example (1.5 ATR)": round(float(sl_dist), 3),
        "spread now (price)": round(spread_px, 5),
        "contract size": contract,
        "quote ccy": qc,
        "lots @ $25 risk": round(float(min(lots, 500.0)), 2),
        "vol min/step": f"{vmin:g}/{vstep:g}",
    })
mt5 = pd.DataFrame(mt5_rows)

# ---------------- charts ----------------
q = sweep[(sweep["is_n"] >= 15) & (sweep["is_pf"] >= 1.15) & (sweep["is_r"] > 0)].copy()

# fig1: qualified configs by pattern x tf
fig, ax = plt.subplots(figsize=(9.5, 4.4), constrained_layout=True)
pt = q.groupby(["tf", "pattern"]).size().unstack(fill_value=0)
x = np.arange(len(pt.columns)); w = 0.38
ax.bar(x - w/2, pt.loc["1H"], w, label="1H", color=BLUE)
ax.bar(x + w/2, pt.loc["4H"], w, label="4H", color=GOLD)
ax.set_xticks(x); ax.set_xticklabels(pt.columns)
ax.set_ylabel("IS-qualified configs")
ax.set_title("Pattern health across the 360-degree grid (11 pairs x 3 reject modes)", fontsize=12)
ax.legend(); ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "a360_fig1_pattern_health.png"), dpi=200); plt.close(fig)

# fig2: EMA-pair heatmap (median IS PF of qualified, count annotated)
fig, ax = plt.subplots(figsize=(9.0, 5.2), constrained_layout=True)
piv = q.groupby(["fast", "slow"]).agg(cnt=("is_pf", "size"), mpf=("is_pf", "median")).reset_index()
pairs = sorted(q.groupby(["fast", "slow"]).size().index.tolist())
fasts = sorted({f for f, s in pairs}); slows = sorted({s for f, s in pairs})
mat = np.full((len(fasts), len(slows)), np.nan)
cntm = np.zeros((len(fasts), len(slows)), int)
for _, row in piv.iterrows():
    mat[fasts.index(row["fast"]), slows.index(row["slow"])] = row["mpf"]
    cntm[fasts.index(row["fast"]), slows.index(row["slow"])] = row["cnt"]
im = ax.imshow(mat, cmap="YlGnBu", aspect="auto", vmin=1.15, vmax=1.6)
ax.set_xticks(range(len(slows))); ax.set_xticklabels([f"EMA{s}" for s in slows])
ax.set_yticks(range(len(fasts))); ax.set_yticklabels([f"EMA{f}" for f in fasts])
for a in range(len(fasts)):
    for b in range(len(slows)):
        if cntm[a, b]:
            ax.text(b, a, f"{mat[a, b]:.2f}\n(n={cntm[a, b]})", ha="center", va="center", fontsize=7)
ax.set_title("Median IS profit factor by EMA pair (fast x slow) - qualified configs only", fontsize=12)
fig.colorbar(im, ax=ax, shrink=0.85, label="median IS PF")
fig.savefig(os.path.join(CH, "a360_fig2_ema_pair_heatmap.png"), dpi=200); plt.close(fig)

# fig3: equity curve (diversified primary)
fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10.5, 6.4), constrained_layout=True,
                              sharex=True, height_ratios=[2.4, 1])
xt = pd.to_datetime(led1_d["entry_time"])
cut = led1_d["entry_time"].quantile(0.70)
ax.plot(xt, led1_d["equity_usd"], color=BLUE, lw=1.4)
ax.axhline(5000, color="#AAAAAA", lw=0.9, ls=":")
ax.axvline(cut, color=RED, lw=1.3, ls="--")
ax.text(cut, ax.get_ylim()[1]*0.95, "  OOS begins", fontsize=9, color=RED, va="top")
ax.set_ylabel("Account equity (USD)")
ax.set_title(f"Alpha360 DIVERSIFIED basket - $5,000 start, concurrency-sized risk "
             f"({eq_d['per_trade_risk_frac']*100:.2f}%/trade, max {eq_d['max_concurrent']} open)", fontsize=12)
ax.grid(True, alpha=0.3)
dd = (led1_d["equity_usd"].cummax() - led1_d["equity_usd"]) / led1_d["equity_usd"].cummax() * 100
ax2.fill_between(xt, -dd, 0, color=RED, alpha=0.55, lw=0)
ax2.set_ylabel("Drawdown (%)"); ax2.set_xlabel("Date"); ax2.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "a360_fig3_equity.png"), dpi=200); plt.close(fig)

# fig4: member OOS by family (diversified)
mem = led1_d.groupby("member").apply(
    lambda g: g[g["entry_time"] > cut]["r_net"].sum(), include_groups=False).sort_values()
famcol = {"gold": GOLD, "jpy": BLUE, "usd_major": GREEN, "crypto": "#8B7BB8", "fx_cross": GREY}
fig, ax = plt.subplots(figsize=(10.5, 5.0), constrained_layout=True)
cols = [famcol.get(fam(m.split("|")[0]), GREY) for m in mem.index]
ax.barh(range(len(mem)), mem.values, color=cols, height=0.62)
ax.set_yticks(range(len(mem)))
ax.set_yticklabels([m.replace(".pi", "").replace(".p", "").replace("|", " ") for m in mem.index], fontsize=7.5)
ax.axvline(0, color="#888888", lw=1)
ax.set_xlabel("Out-of-sample net R (never used for selection)")
ax.set_title("Member OOS contribution - family-colored (gold/JPY/USD-major/crypto/cross/other)", fontsize=12)
ax.grid(True, axis="x", alpha=0.3)
fig.savefig(os.path.join(CH, "a360_fig4_member_oos.png"), dpi=200); plt.close(fig)

# fig5: IS vs OOS scatter (both baskets' members)
memrows = []
for m, g in pd.concat([led1_d, led1_c]).groupby("member"):
    ti, to = split_is_oos(g)
    memrows.append({"m": m, "is": metrics(ti)["total_r"], "oos": metrics(to)["total_r"],
                    "fam": fam(m.split("|")[0])})
ms = pd.DataFrame(memrows).drop_duplicates("m")
fig, ax = plt.subplots(figsize=(7.0, 6.0), constrained_layout=True)
for f_, colr in [("gold", GOLD), ("jpy", BLUE), ("usd_major", GREEN), ("crypto", "#8B7BB8"),
                 ("fx_cross", GREY), ("metals_other", "#B08968"), ("energy", "#CC8844"), ("index", "#7799AA")]:
    sub = ms[ms["fam"] == f_]
    if len(sub):
        ax.scatter(sub["is"], sub["oos"], s=42, color=colr, label=f_, alpha=0.85,
                   edgecolors="#333333", linewidths=0.5)
ax.axhline(0, color=RED, lw=1); ax.axvline(0, color="#888888", lw=0.8, ls=":")
ax.set_xlabel("In-sample net R (selection)"); ax.set_ylabel("Out-of-sample net R (validation)")
ax.set_title("Selection vs validation across all basket members", fontsize=12)
ax.legend(fontsize=8); ax.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "a360_fig5_is_vs_oos.png"), dpi=200); plt.close(fig)

# fig6: basket comparison (baseline vs conc vs div)
fig, axes = plt.subplots(1, 3, figsize=(11.5, 4.2), constrained_layout=True)
names = ["Baseline\n89/233", "Alpha360\nCONC", "Alpha360\nDIV"]
sets = []
for led1 in [led1_b, led1_c, led1_d]:
    ti, to = split_is_oos(led1)
    sets.append((metrics(ti)["pf"], metrics(to)["pf"], metrics(to)["total_r"]))
for k, (title, idx) in enumerate([("IS PF", 0), ("OOS PF", 1), ("OOS net R", 2)]):
    vals = [s[idx] for s in sets]
    bars = axes[k].bar(names, vals, color=[GREY, GOLD, BLUE], width=0.6)
    axes[k].set_title(title, fontsize=11)
    axes[k].grid(True, axis="y", alpha=0.3)
    for b_, v in zip(bars, vals):
        axes[k].text(b_.get_x() + b_.get_width()/2, v, f"{v:.1f}", ha="center",
                     va="bottom" if v >= 0 else "top", fontsize=9)
    axes[k].axhline(1.0 if k < 2 else 0, color=RED, lw=0.8, ls="--")
fig.suptitle("360-degree exploration vs the validated 89/233 baseline", fontsize=12)
fig.savefig(os.path.join(CH, "a360_fig6_basket_comparison.png"), dpi=200); plt.close(fig)

# ---------------- summary json ----------------
grid_stats = {
    "sweep_a_rows": int(len(sweep)),
    "sweep_b_rows": int(len(sweepb)),
    "qualified": int(len(q)),
    "by_pattern": {k: int(v) for k, v in q.groupby("pattern").size().items()},
    "by_reject": {k: int(v) for k, v in q.groupby("reject").size().items()},
    "by_tf": {k: int(v) for k, v in q.groupby("tf").size().items()},
    "by_pair": {f"{f}/{s}": int(v) for (f, s), v in q.groupby(["fast", "slow"]).size().items()},
    "symbols_with_qual": int(q["symbol"].nunique()),
}
if len(sweepb):
    grid_stats["overlays"] = {k: int(v) for k, v in sweepb.groupby("overlay").size().items()}

summary = {
    "n_trials": summary_c["n_trials"],
    "grid": grid_stats,
    "diversified": {**summary_c.get("DIVERSIFIED", {}), "equity": eq_d, "oos_t_stat": oos_tstat},
    "concentrated": {**summary_c.get("CONCENTRATED", {}), "equity": eq_c},
    "baseline": summary_c.get("baseline", {}),
    "div_members": picks_d.to_dict("records"),
    "conc_members": picks_c.to_dict("records"),
    "mt5_calibration": mt5.to_dict("records") if len(mt5) else [],
}
with open(os.path.join(DL, "alpha360_summary.json"), "w") as f:
    json.dump(summary, f, indent=1, default=str)

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


p2 = picks_d.copy()
p2["slope"] = p2["slope"].map({True: "on", False: "off"})
p2["regime"] = p2["regime"].map({True: "on", False: "off"})
put_df(wb.active, p2, "Alpha360 DIVERSIFIED Basket (family cap 3, IS-only plateau selection)",
       f"Grid: 11 EMA pairs x 3 reject modes x 6 patterns x 4 gates x exits; {summary_c['n_trials']:,} trials deflated",
       num_cols=["pick_is_r", "pick_is_pf", "pick_oos_r", "pick_oos_pf"], short="DIV Basket")

p3 = picks_c.copy()
p3["slope"] = p3["slope"].map({True: "on", False: "off"})
p3["regime"] = p3["regime"].map({True: "on", False: "off"})
put_df(wb.create_sheet(), p3, "Alpha360 CONCENTRATED Basket (no family cap)",
       "Same picks pool as diversified, uncapped - the max-alpha construction",
       num_cols=["pick_is_r", "pick_is_pf", "pick_oos_r", "pick_oos_pf"], short="CONC Basket")

batt_rows = []
for name, led1 in [("Baseline 89/233", led1_b), ("Alpha360 CONC", led1_c), ("Alpha360 DIV", led1_d)]:
    ti, to = split_is_oos(led1)
    mi, mo = metrics(ti), metrics(to)
    batt_rows.append([name, mi["n"], round(mi["total_r"], 1), round(mi["pf"], 2),
                      mo["n"], round(mo["total_r"], 1), round(mo["pf"], 2), round(mo["max_dd_r"], 1)])
batt = pd.DataFrame(batt_rows, columns=["Basket", "IS n", "IS R", "IS PF", "OOS n", "OOS R", "OOS PF", "OOS DD (R)"])
put_df(wb.create_sheet(), batt, "Basket Comparison (single-shot OOS, same window)",
       "OOS = last 30% by entry time; never used for selection",
       num_cols=["IS R", "IS PF", "OOS R", "OOS PF", "OOS DD (R)"], short="Comparison")

put_df(wb.create_sheet(), mt5, "MT5 Manual Execution Calibration (DIVERSIFIED basket)",
       "Watch the EMA zone on chart close; lots = risk_usd / (SL_dist x contract x quote_to_USD), snapped to volume step; specs from the desk symbol universe",
       num_cols=["last close", "EMA fast now", "EMA slow now", "ATR14 now",
                 "SL dist example (1.5 ATR)", "lots @ $25 risk"], short="MT5 Calibration")

led_out = led1_d[["symbol", "tf", "side", "setup", "entry_time", "exit_time", "entry", "exit",
                  "risk_dist", "r_net", "reason", "member", "family"]].copy()
led_out["entry_time"] = led_out["entry_time"].astype(str)
led_out["exit_time"] = led_out["exit_time"].astype(str)
put_df(wb.create_sheet(), led_out, f"Diversified Basket Trade Ledger ({len(led_out)} trades, real spreads)",
       "Chronological; OOS = last 30% by entry time", num_cols=["entry", "exit", "risk_dist", "r_net"],
       short="Ledger")

pair_health = q.groupby(["fast", "slow", "reject"]).agg(
    configs=("pf", "size"), med_is_pf=("is_pf", "median"),
    pos_oos=("oos_pf", lambda x: int((x > 1).sum()))).reset_index()
put_df(wb.create_sheet(), pair_health, "EMA Pair x Reject Mode Health (IS-qualified configs)",
       "Which combinations of the 360-degree grid survive the IS gates",
       num_cols=["med_is_pf"], short="Pair Health")

path = os.path.join(DL, "Alpha360_EMA_Rejection_Basket.xlsx")
wb.save(path)
led1_d[["entry_time", "exit_time", "symbol", "member", "r_net", "equity_usd"]].to_csv(
    os.path.join(DL, "basket_equity_ledger.csv"), index=False)
print("charts + workbook + summary saved")
print("DIV equity:", json.dumps(eq_d, indent=1))
print("CONC equity:", json.dumps(eq_c, indent=1))
print("OOS t-stat (div):", round(oos_tstat, 2))
