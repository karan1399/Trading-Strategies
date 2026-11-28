"""Compare Sovereign's 0.25% and 0.50% sizing on the same offline marked equity tape."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, PercentFormatter
import numpy as np
import pandas as pd

from desk.quant_audits import sovereign_engine_step_replay as native


SHADOW = native.ROOT / "engine" / "config_sovereign_shadow.json"
COLORS = {"prior": "#718097", "current": "#137B72", "shadow": "#8E5BBC"}
LABELS = {"prior": "Incumbent · 0.25% prior", "current": "Incumbent · 0.50% configured",
          "shadow": "ETH Donchian shadow · 0.25%"}


def curve_frame(result):
    curve = result["curve"]
    frame = pd.DataFrame({"equity": curve["equity"]},
                         index=pd.to_datetime(curve["ts"], unit="s", utc=True))
    frame["drawdown_pct"] = (frame["equity"] / frame["equity"].cummax() - 1) * 100
    return frame


def render(results, target):
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 1, figsize=(11.4, 6.6), sharex=True,
                             gridspec_kw={"height_ratios": [2.2, 1]})
    fig.patch.set_facecolor("#ffffff")
    for key in ("prior", "shadow", "current"):
        frame = curve_frame(results[key])
        color = COLORS[key]
        style = "-" if key == "current" else "--"
        width = 1.95 if key == "current" else 1.25
        axes[0].plot(frame.index, frame["equity"], style, color=color, linewidth=width, label=LABELS[key])
        axes[1].plot(frame.index, frame["drawdown_pct"], style, color=color, linewidth=width, alpha=.90)
    axes[0].axhline(native.EQUITY, linewidth=.8, color="#bcc4ce", linestyle=":")
    axes[0].set_ylabel("Marked equity (USD)", color="#28374a")
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda value, _: "$" + f"{value:,.0f}"))
    axes[0].legend(loc="upper left", frameon=False, fontsize=9.1)
    axes[1].set_ylabel("Drawdown", color="#28374a")
    axes[1].yaxis.set_major_formatter(PercentFormatter(xmax=100, decimals=1))
    axes[1].xaxis.set_major_locator(mdates.MonthLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    for ax in axes:
        ax.grid(axis="y", color="#e4e9ee", linewidth=.8)
        ax.tick_params(labelsize=9, colors="#40536a", length=3)
        ax.spines[["bottom", "left"]].set_color("#ccd5e1")
        ax.set_axisbelow(True)
    fig.text(.11, .954, "Sovereign Core equity replay", fontsize=21, weight="bold", color="#172437")
    fig.text(.11, .911, "1 May–20 November 2026 UTC   |   Archived market data   |   +20 bp per round trip",
             fontsize=10.5, color="#4c6076")
    fig.text(.11, .035, "Sampled mark-to-market equity. All three runs enforce a $6 stressed cap per trade.",
             fontsize=9, color="#4c6076")
    fig.subplots_adjust(left=.11, right=.975, top=.865, bottom=.105, hspace=.16)
    fig.savefig(target, dpi=165, facecolor=fig.get_facecolor())
    plt.close(fig)


def compact(result):
    result = {k: v for k, v in result.items() if k in {
        "start", "end", "start_equity", "end_equity", "ending_balance", "net_pct", "trades", "entries",
        "still_open", "pf", "max_marked_dd_pct", "max_entry_stress_usd", "by_symbol", "exposure_peaks",
        "curve", "monthly_realized_pnl",
    }}
    frame = curve_frame(result)
    drawdown = frame["drawdown_pct"]
    trough = int(np.argmin(drawdown.to_numpy()))
    peak = int(np.argmax(frame["equity"].to_numpy()[:trough + 1]))
    result["max_drawdown_peak"] = frame.index[peak].isoformat()
    result["max_drawdown_trough"] = frame.index[trough].isoformat()
    result["daily_equity"] = {t.strftime("%Y-%m-%d"): round(v, 2)
                              for t, v in frame["equity"].resample("D").last().ffill().items()}
    del result["curve"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    incumbent = json.loads(native.sov.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert incumbent["governor"]["risk_pct"] == .5
    prior = deepcopy(incumbent)
    prior["_meta"]["base_risk_pct"] = .25
    prior["governor"].update(risk_pct=.25, max_risk_single_trade_pct=.25)
    shadow = json.loads(SHADOW.read_text(encoding="utf-8"))
    payloads, _, _, _ = native.prepare(shadow)
    opens, closes, _ = native.load_execution_tapes()
    observations = {}
    for symbol, alias in native.SYMBOLS.items():
        path = native.ROOT / "data" / "telemetry" / "assets" / f"{alias}.json"
        item = json.loads(path.read_text(encoding="utf-8"))["mt5"]
        assert item["symbol"] == symbol
        observations[symbol] = item
    start, end = native.utc("2026-05-01"), native.utc("2026-11-20 23:45")
    arms = {}
    for key, profile, bp in (("prior", prior, 20.), ("current", incumbent, 20.),
                             ("shadow", shadow, 20.), ("current_base_cost", incumbent, 0.)):
        arms[key] = native.replay(profile, payloads, opens, closes, start, end, observations,
                                  extra_bps=bp, include_curve=True)
        assert arms[key]["max_entry_stress_usd"] <= 6.
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    render(arms, out / "sovereign_equity_curve.png")
    report = {key: compact(result) for key, result in arms.items()}
    report["risk_at_seed_usd"] = {"prior_pct": native.EQUITY * .0025, "current_pct": native.EQUITY * .005,
                                 "binding_desk_cap": 6., "desk_cap_pct": 600. / native.EQUITY}
    report["basis"] = "Archived CFD price and spread proxies, sampled equity, no broker access."
    (out / "sovereign_equity_results.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                                                      encoding="utf-8")
    print(json.dumps({k: {f: report[k][f] for f in ("end_equity", "net_pct", "trades", "pf",
                                                  "max_marked_dd_pct", "max_entry_stress_usd")}
                      for k in arms}, indent=2))


if __name__ == "__main__":
    main()
