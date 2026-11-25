#!/usr/bin/env python3
"""Assemble the portable footprint_auction_ml/ deliverable:
  - copies all pipeline scripts into the repo folder (self-contained)
  - patches the MT5 desk-universe lookup to work in the repo layout
  - copies model artifacts + result files
  - writes README.md (usage + honest verdict)
  - zips the folder into download/footprint/ and copies key artifacts."""
import os
import shutil
import zipfile

SRC = "/home/z/my-project/scripts"
FP = "/home/z/my-project/footprint"
DEST = "/home/z/my-project/repos/karan-ts/footprint_auction_ml"
DL = "/home/z/my-project/download/footprint"
os.makedirs(DEST, exist_ok=True)
os.makedirs(DL, exist_ok=True)

SCRIPTS = ["fp_download.py", "fp_core.py", "fp_events.py", "fp_rr_sweep.py",
           "fp_train.py", "fp_backtest.py", "fp_watch.py", "fp_validate.py",
           "fp_diag.py"]
for f in SCRIPTS:
    s = open(os.path.join(SRC, f)).read()
    # make the MT5 desk-universe lookup work both in the sandbox and in a clone
    s = s.replace(
        'uni = os.path.join(os.path.dirname(HERE), "repos", "karan-ts", "desk",\n'
        '                       "mt5_symbols_universe.json")',
        'uni = None\n'
        '    for cand in (os.path.join(os.path.dirname(HERE), "desk",\n'
        '                              "mt5_symbols_universe.json"),\n'
        '                 os.path.join(os.path.dirname(HERE), "repos", "karan-ts",\n'
        '                              "desk", "mt5_symbols_universe.json")):\n'
        '        if os.path.exists(cand):\n'
        '            uni = cand\n'
        '            break')
    open(os.path.join(DEST, f), "w").write(s)

shutil.copytree(os.path.join(FP, "model"), os.path.join(DEST, "model"),
                dirs_exist_ok=True)
res_out = os.path.join(DEST, "results")
os.makedirs(res_out, exist_ok=True)
for f in ["fp_events.json", "fp_rr_sweep.json", "fp_backtest.json",
          "oof.parquet", "trades_ml.csv", "equity_curve.png", "fig_forward.png"]:
    src = os.path.join(FP, "results", f)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(res_out, f))

README = """# Footprint Auction ML — BTCUSDT order-flow research & live tooling

Live footprint tracking from Binance Vision / Binance REST tick data, the
"auction complete" (0-print at bar extreme) signal reverse-engineered at desk
standard, an ML grading model, a fee-aware backtester, and a production
watcher that prints executable order tickets (Binance BTC + MT5 BTCUSD.p lots).

## Verdict (2026-11-23, 326 days)

The raw 0-print rule is a **coin flip pre-fee and negative post-fee** in all
75 tested configurations; ML grading halves the loss (win rate 34.4% -> 44.5%,
PF 0.95 -> 1.04) but the untouched final fold stays negative
(-0.30R/trade fee-adjusted, P(profit)=4%). **Do not route live risk to it in
this configuration** — see Footprint_Auction_ML_Report.docx for the full
quantified study and the three evidence-backed paths forward (maker-fee
arbitrage research, overlay for the alpha360 desk, cross-symbol extension).

## Layout

    fp_download.py   Binance Vision aggTrades/klines -> bars/edges/grid parquet
    fp_validate.py   footprint vs klines integrity (PASS: taker-buy corr 1.000000)
    fp_events.py     event study: 4 variants x 2 entries x 3 stops x 2 horizons
    fp_rr_sweep.py   RR sweep locating the fee-viable base config
    fp_train.py      purged walk-forward LightGBM + isotonic calibration
    fp_backtest.py   fee-aware backtest: raw vs ML, bootstrap, deflated Sharpe
    fp_watch.py      live / --replay / --selftest watcher -> order tickets
    model/           lgbm_deploy.txt, isotonic.pkl, meta.json (config+threshold)
    results/         all study outputs (JSON grids, OOF trades, equity chart)

`FP_ROOT` (default: `<repo_root>/footprint`) holds processed/ and the model
store; scripts are also runnable from this folder with the store beside it.

## Quick start

    pip install pandas pyarrow numpy scipy matplotlib lightgbm scikit-learn joblib
    python3 fp_download.py full          # ~11 months from data.binance.vision
    python3 fp_validate.py               # must print VALIDATION: PASS
    python3 fp_watch.py --selftest       # aggregator parity + latency
    python3 fp_watch.py --replay 2026-11-20   # bar-by-bar ticket demo
    python3 fp_watch.py --live           # production (your IP; REST fromId)

Set risk per trade with `FP_RISK_USD` (default $25). Signals are CLOSED-bar
only; one position at a time; journal at footprint/watch_log.csv.

## Retrain cadence

    python3 fp_train.py z2 market 4.0 96 4.0   # or any config under test
    python3 fp_backtest.py
    python3 fp_watch.py --selftest

Monthly, with a rolling 100-trade PF>=0.9 kill switch before live routing.
"""
open(os.path.join(DEST, "README.md"), "w").write(README)

# zip + download copies
zpath = os.path.join(DL, "footprint_auction_ml_repo_folder.zip")
if os.path.exists(zpath):
    os.remove(zpath)
zf = zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED)
for root, _, files in os.walk(DEST):
    for f in files:
        p = os.path.join(root, f)
        zf.write(p, os.path.relpath(p, os.path.dirname(DEST)))
zf.close()
shutil.copy2(os.path.join(DEST, "README.md"), os.path.join(DL, "README_footprint.md"))
for f in ["fp_events.json", "fp_rr_sweep.json", "fp_backtest.json",
          "equity_curve.png", "fig_forward.png", "trades_ml.csv"]:
    src = os.path.join(FP, "results", f)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(DL, f))
shutil.copy2(os.path.join(FP, "processed", "feats_all.parquet"),
             os.path.join(DL, "feats_all.parquet"))
print("assembled", DEST)
for f in sorted(os.listdir(DEST)):
    print("  ", f)
print("zip:", zpath, os.path.getsize(zpath) // 1024, "KB")
print("download dir:", sorted(os.listdir(DL)))
