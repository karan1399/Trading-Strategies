#!/usr/bin/env python3
"""$5K equity simulation for the IV Wall alpha basket, split at 2025/2026,
plus MT5 lot-size calibration for the 20 alpha FX pairs.

Legs:
  A  4H alpha basket, 2025-01-01 .. 2025-12-31   (only timeframe covering 2025)
  A' 4H alpha basket, 2026-01-01 .. data end     (continuity)
  B  15M flagship basket, first 15M trade .. end (deployable config, Sep-Nov 2026)
Risk model: 1% of current equity per trade (compounding), $10 floor, start $5,000.
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivwall_engine import Config, run_config, list_symbols, load_symbol, infer_digits, metrics
from ivwall_runner import classify

import matplotlib
matplotlib.use("Agg")
try:
    import matplotlib.font_manager as fm
    fm.fontManager.addfont('/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf')
except Exception:
    pass
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Noto Sans SC']
plt.rcParams['axes.unicode_minus'] = False

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PARQUET_ROOT = os.environ.get("PARQUET_ROOT", os.path.abspath(os.path.join(ROOT, "..", "historical_parquet")))
OUT = os.environ.get("OUT", ROOT)
DL = os.environ.get("DL", ROOT)
CH = os.path.join(DL, "charts")
os.makedirs(CH, exist_ok=True)

GOLD, BLUE, RED, GREY = "#C9A84C", "#6B9DAD", "#B87472", "#B0B0B0"
START_EQUITY = 5000.0
RISK_FRAC = 0.01
RISK_FLOOR = 10.0
LOT_SIZE = 100000.0

summary = json.load(open(os.path.join(DL, "summary.json")))
BASKET = list(summary["basket_symbols"])
FX_ALL = [s for s in list_symbols("4H") if classify(s) == "fx"]
print(f"alpha basket: {len(BASKET)} pairs | FX universe on 4H: {len(FX_ALL)}")

# ---------------- 1) 4H alpha-basket trades ----------------
cfg = Config(name="alpha4h", strategy="fade_touch", vol_estimator="ewma_cc",
             target_mode="wall_m", sd_wall=2.0, target_m=0.5, buffer_atr=1.0,
             min_rr=0.3)
tr4 = run_config("4H", FX_ALL, cfg)
tr4 = tr4[tr4["symbol"].isin(BASKET)].sort_values("entry_time").reset_index(drop=True)
tr4["iclass"] = "fx"
tr4.to_parquet(os.path.join(OUT, "trades_4h_alpha.parquet"))
print("4H alpha trades total:", len(tr4), "| span", tr4["entry_time"].min(), "->", tr4["entry_time"].max())

b15 = pd.read_parquet(os.path.join(OUT, "trades_basket.parquet")).sort_values("entry_time").reset_index(drop=True)
print("15M basket trades:", len(b15), "| span", b15["entry_time"].min(), "->", b15["entry_time"].max())


# ---------------- 2) equity simulation ----------------
def sim(trades: pd.DataFrame, start_equity: float = START_EQUITY,
        risk_frac: float = RISK_FRAC, risk_floor: float = RISK_FLOOR) -> pd.DataFrame:
    tr = trades.sort_values("entry_time").reset_index(drop=True)
    eq, rows = start_equity, []
    for _, t in tr.iterrows():
        risk = max(risk_floor, risk_frac * eq)
        pnl = t["r_net"] * risk
        eq += pnl
        rows.append({**{k: t[k] for k in ("symbol", "entry_time", "exit_time", "side", "wall_side",
                                          "entry", "stop", "target", "r_net", "reason")},
                     "risk_usd": risk, "pnl_usd": pnl, "equity_usd": eq})
    return pd.DataFrame(rows)


def leg_metrics(curve: pd.DataFrame, label: str) -> dict:
    if len(curve) == 0:
        return {"label": label, "n": 0}
    eq = curve["equity_usd"].values
    pnl = curve["pnl_usd"].values
    runmax = np.maximum.accumulate(eq)
    dd = runmax - eq
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    return {
        "label": label, "n": len(curve),
        "start": float(eq[0] - pnl[0]), "end": float(eq[-1]),
        "net_usd": float(pnl.sum()), "ret_pct": float(pnl.sum() / (eq[0] - pnl[0]) * 100),
        "win_rate": float((pnl > 0).mean()),
        "pf_usd": float(wins.sum() / abs(losses.sum())) if losses.sum() < 0 else float("inf"),
        "max_dd_usd": float(dd.max()), "max_dd_pct": float(dd.max() / runmax[np.argmax(dd)] * 100),
        "first": str(curve["entry_time"].iloc[0])[:10], "last": str(curve["entry_time"].iloc[-1])[:10],
    }


curve4 = sim(tr4[tr4["entry_time"] >= "2025-01-01"])   # $5K starts at 2025-01-01, per spec
c25 = curve4[curve4["entry_time"].dt.year == 2025].reset_index(drop=True)
c26 = curve4[curve4["entry_time"].dt.year >= 2026].reset_index(drop=True)
# 2025 leg starts at $5,000; the 2026 leg continues the 2025 ending equity
if len(c26):
    cont_start = c25["equity_usd"].iloc[-1] if len(c25) else START_EQUITY
    c26c = sim(tr4[tr4["entry_time"].dt.year >= 2026], start_equity=cont_start)
curve15 = sim(b15)  # standalone $5K deployment of the validated 15M config

legs = pd.DataFrame([
    leg_metrics(c25, "A  2025 leg  (4H alpha basket, $5K start)"),
    leg_metrics(c26c if len(c26) else c26, "A' 2026 leg (4H alpha basket, continues 2025 equity)"),
    leg_metrics(curve4, "A  full 4H curve 2025 -> 2026-11 (continuous)"),
    leg_metrics(curve15, "B  15M flagship basket, Sep-Nov 2026 (standalone $5K)"),
])
print(legs.round(2).to_string(index=False))

# ---------------- 3) MT5 lot calibration ----------------
QUOTED_IN_USD = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"}
USD_QUOTE_PAIRS = {"JPY": "USDJPY", "CHF": "USDCHF", "CAD": "USDCAD", "SGD": "USDSGD",
                   "HKD": "USDHKD", "NOK": "USDNOK", "SEK": "USDSEK", "MXN": "USDMXN",
                   "ZAR": "USDZAR", "CNH": "USDCNH", "HUF": "USDHUF", "TRY": "USDTRY",
                   "CZK": "USDCZK", "PLN": "USDPLN", "DKK": "USDDKK"}
eur_cache = {}


def usd_per_quote(quote: str, tf_files) -> float | None:
    """USD price of 1 unit of `quote` currency, from median close of a data file."""
    if quote == "USD":
        return 1.0
    if quote in eur_cache:
        return eur_cache[quote]
    px = None
    pair = USD_QUOTE_PAIRS.get(quote)          # USDJPY: price = JPY per USD
    if pair and os.path.exists(os.path.join(PARQUET_ROOT, "4H", pair + ".pi.parquet")):
        jpy_per_usd = load_symbol("4H", pair + ".pi")["close"].median()
        px = 1.0 / jpy_per_usd                  # USD per JPY
    else:
        eurusd = load_symbol("4H", "EURUSD.pi")["close"].median()   # USD per EUR
        eurq_path = os.path.join(PARQUET_ROOT, "4H", f"EUR{quote}.pi.parquet")
        if os.path.exists(eurq_path):
            eurq = load_symbol("4H", f"EUR{quote}.pi")
            q_per_eur = eurq["close"].median()                      # QUOTE per EUR
            usd_per_eur = eurusd
            px = usd_per_eur / q_per_eur                            # USD per QUOTE
    eur_cache[quote] = px
    return px


def pip_size_from_digits(d: int) -> float:
    point = 10.0 ** -d
    return point * 10 if d in (5, 3) else point


lots_rows = []
st15 = b15.groupby("symbol").apply(lambda g: (g["entry"] - g["stop"]).abs().median(), include_groups=False)
st4 = tr4.groupby("symbol").apply(lambda g: (g["entry"] - g["stop"]).abs().median(), include_groups=False)
for sym in BASKET:
    base = sym.split(".")[0][:3]
    quote = sym.split(".")[0][3:]
    dfp = load_symbol("15M", sym)
    d = infer_digits(dfp["close"])
    pip = pip_size_from_digits(d)
    stop15 = float(st15.get(sym, np.nan))
    stop15_pips = stop15 / pip if np.isfinite(stop15) else np.nan
    stop4 = float(st4.get(sym, np.nan))
    stop4_pips = stop4 / pip if np.isfinite(stop4) else np.nan
    if base == "USD":
        pv_usd = LOT_SIZE * pip / (dfp["close"].median())      # quote ccy per pip -> USD
    else:
        upq = usd_per_quote(quote, None)
        pv_usd = LOT_SIZE * pip * upq if upq else np.nan
    lots = math.floor((50.0 / (stop15_pips * pv_usd)) * 100) / 100 if np.isfinite(pv_usd) and stop15_pips > 0 else np.nan
    lots = max(lots, 0.01) if np.isfinite(lots) else np.nan
    lots_rows.append({
        "symbol": sym, "digits": d, "pip_size": pip,
        "median_stop_15M_pips": round(stop15_pips, 1) if np.isfinite(stop15_pips) else None,
        "median_stop_4H_pips": round(stop4_pips, 1) if np.isfinite(stop4_pips) else None,
        "pip_value_usd_per_lot": round(pv_usd, 3) if np.isfinite(pv_usd) else None,
        "lots_at_50usd_risk": lots,
        "usd_per_pip_at_that_lot": round(lots * pv_usd, 2) if np.isfinite(lots) else None,
    })
lots_df = pd.DataFrame(lots_rows)
print(lots_df.to_string(index=False))
lots_df.to_csv(os.path.join(DL, "mt5_lot_table.csv"), index=False)

# ---------------- 4) charts ----------------
def dd_curve(eq):
    runmax = np.maximum.accumulate(eq)
    return (eq - runmax) / runmax * 100.0

fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10.5, 6.4), constrained_layout=True,
                              sharex=True, height_ratios=[2.4, 1])
x4 = pd.to_datetime(curve4["entry_time"])
ax.plot(x4, curve4["equity_usd"], color=BLUE, lw=1.5,
        label="4H alpha basket (2025 -> 2026-11, continuous)")
if len(c26):
    ax.plot(pd.to_datetime(c26c["entry_time"]), c26c["equity_usd"], color=GOLD, lw=1.5,
            label="2026 leg continuing 2025 ending equity")
ax.axhline(START_EQUITY, color="#AAAAAA", lw=0.9, ls=":")
ax.axvline(pd.Timestamp("2026-01-01"), color="#888888", ls="--", lw=1.0)
ax.text(pd.Timestamp("2026-01-08"), ax.get_ylim()[1] * 0.97, "2026 leg", fontsize=9, color="#666666")
ax.set_ylabel("Account equity (USD)")
ax.set_title(f"IV Wall alpha basket - $5,000 start, 1% compounding risk per trade", fontsize=12)
ax.legend(loc="upper left", fontsize=9)
ax.grid(True, alpha=0.3)
ax2.fill_between(x4, dd_curve(curve4["equity_usd"].values), 0, color=RED, alpha=0.55, lw=0)
ax2.set_ylabel("Drawdown (%)"); ax2.set_xlabel("Date")
ax2.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "fig7_equity_5k.png"), dpi=200); plt.close(fig)

# monthly P&L (continuous 4H curve)
m = curve4.copy()
m["month"] = pd.to_datetime(m["entry_time"]).dt.to_period("M").astype(str)
mo = m.groupby("month")["pnl_usd"].sum()
fig, ax = plt.subplots(figsize=(10.5, 4.2), constrained_layout=True)
cols = [GOLD if v >= 0 else RED for v in mo.values]
ax.bar(range(len(mo)), mo.values, color=cols, width=0.7)
ax.set_xticks(range(len(mo)))
ax.set_xticklabels(mo.index, rotation=60, fontsize=7)
ax.axhline(0, color="#888888", lw=1)
ax.set_ylabel("Net P&L (USD)")
ax.set_title("Monthly net P&L - 4H alpha basket, 2025 -> 2026-11 (1% compounding risk)", fontsize=12)
ax.grid(True, axis="y", alpha=0.3)
fig.savefig(os.path.join(CH, "fig8_monthly.png"), dpi=200); plt.close(fig)

fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10.5, 6.0), constrained_layout=True,
                              sharex=True, height_ratios=[2.4, 1])
x15 = pd.to_datetime(curve15["entry_time"])
ax.plot(x15, curve15["equity_usd"], color=GOLD, lw=1.7)
ax.axhline(START_EQUITY, color="#AAAAAA", lw=0.9, ls=":")
ax.set_ylabel("Account equity (USD)")
ax.set_title(f"15M flagship alpha basket - $5,000 start (deployable config, 15M data window "
             f"{curve15['entry_time'].iloc[0]:%Y-%m-%d} -> {curve15['entry_time'].iloc[-1]:%Y-%m-%d})", fontsize=11)
ax.grid(True, alpha=0.3)
ax2.fill_between(x15, dd_curve(curve15["equity_usd"].values), 0, color=RED, alpha=0.55, lw=0)
ax2.set_ylabel("Drawdown (%)"); ax2.set_xlabel("Date"); ax2.grid(True, alpha=0.3)
fig.savefig(os.path.join(CH, "fig9_equity_15m.png"), dpi=200); plt.close(fig)

# ---------------- 5) save ledgers + summary ----------------
curve4_out = curve4.copy()
curve4_out["entry_time"] = curve4_out["entry_time"].astype(str)
curve4_out["exit_time"] = curve4_out["exit_time"].astype(str)
curve4_out.to_csv(os.path.join(DL, "equity_curve_4h_alpha_2025_2026.csv"), index=False)
c15_out = curve15.copy()
c15_out["entry_time"] = c15_out["entry_time"].astype(str)
c15_out["exit_time"] = c15_out["exit_time"].astype(str)
c15_out.to_csv(os.path.join(DL, "equity_curve_15m_alpha.csv"), index=False)

legs.to_csv(os.path.join(DL, "equity_legs_summary.csv"), index=False)
mo_df = mo.reset_index(); mo_df.columns = ["month", "net_pnl_usd"]
mo_df.to_csv(os.path.join(DL, "monthly_pnl_4h_alpha.csv"), index=False)

sim_summary = {
    "legs": legs.to_dict("records"),
    "monthly": mo_df.to_dict("records"),
    "params": {"start_equity": START_EQUITY, "risk_frac": RISK_FRAC, "risk_floor": RISK_FLOOR,
               "basket": BASKET, "risk_model": "1% of current equity per trade, $10 floor, compounding"},
}
with open(os.path.join(DL, "equity_sim_summary.json"), "w") as f:
    json.dump(sim_summary, f, indent=1, default=str)
print("equity sim artifacts saved")
