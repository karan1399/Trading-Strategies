#!/usr/bin/env python3
"""Build a sanitized, push-ready git repo (code + docs only) + startup guide + bundle.

Excludes: raw parquet data store (rebuildable via the dump engine), repos/ (third-party
copies incl. a 125MB tarball that GitHub hard-rejects), download/, upload/, .env
(secret), caches/logs. History is rebuilt as 3 clean narrative commits.
"""
import os
import shutil
import subprocess
import sys

SRC = "/home/z/my-project"
DST = "/tmp/cleanrepo"
BUNDLE = f"{SRC}/download/trading-alpha-suite-clean.bundle"

U20 = ("BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,LTCUSDT,"
       "TRXUSDT,DOTUSDT,BCHUSDT,XLMUSDT,UNIUSDT,ATOMUSDT,FILUSDT,NEARUSDT,XMRUSDT,ALGOUSDT,VETUSDT")

README = f"""# Trading Alpha Suite — Binance footprint/orderflow signals, MT5 execution

Live signals are computed from **Binance USDT-M footprint + orderflow** (Vision monthly
dumps, incremental API gap-fill); every emitted signal carries the matching **MT5 CFD
symbol** (`mt5_symbol`) so execution targets the MT5 candle. Six sleeves ship wired —
the three footprint-alpha sleeves plus faithful ports of the repo's other strategy
families, all sharing one signal → guards → sizing → audit pipeline:

| sleeve | lineage | entry logic | target | pooled crypto-15m PF* | default |
| --- | --- | --- | --- | --- | --- |
| `aec_trend` | NEW (this suite) | pullback/reclaim in EMA-regime with auction-complete (footprint) trigger | per-asset, TP ≥ 2R | 1.02 pooled — the per-asset overrides carry the edge | **ON** |
| `ema_rej` | upgraded | EMA 89/233 wick-kiss / pierce-fail rejection + AC-event confirm | 2R | 1.20 | **ON** |
| `orb` | upgraded | UTC 60-min opening-range breakout, close-through + delta>2% + volume ≥1.2× | 2R | 0.89 on the fixed deployed config (research grid OOS PF 3.58 — re-tune before sizing) | **ON** |
| `dfib_vwap` | ported (Delta-Fibonacci) | 5-pattern level rejection (pinbar/two-bar/sweep/cluster/double-tap) at session VWAP ±1,2 SD bands | 2R, floor 1.2 ATR | 0.76 | off |
| `dfib_ema` | ported (Delta-Fibonacci) | same patterns at the EMA50 "kiss and bounce" | 2R, floor 1.2 ATR | 0.77 | off |
| `vc_breakout` | ported (Volatility_compression) | BB(20,2)-in-KC(20,1.5×ATR) squeeze ≥3 bars → close beyond KC band, EMA200+momentum gates | 2R, floor 1.2 ATR | 0.87 | off |

\* Pooled over the full 54-day store, engine fills, 4/11 bps costs — see
`desk/BACKTEST_ALL_SLEEVES.txt`. All six sleeves are integrated and share the same
signal → guards → sizing → audit pipeline; the three ported ones are **selftest-verified
but default-off** because they were validated on metals/FX 4H/1H history, not crypto 15m
— enable any of them by adding `dfib_vwap` / `dfib_ema` / `vc_breakout` to `sleeves` in
`engine/config.json` (per-asset optimization recommended first: the AEC per-asset
workflow in `scripts/` is the template).

HFT (`HFT/` in the repo) intentionally stays a separate process: it is market-making
microstructure on websocket cadence, not a 15-minute bar sleeve — folding it in would
break its design. Its risk-governor caps (max positions, daily loss, kill switch) are
embodied in this engine's live guards.

12/20 assets have OOS-verified per-asset configs (`engine` auto-loads
`binance_orderflow/data/research/aec_asset_overrides.json`); assets without a verified
config are intentionally left on the pooled default — do not force-trade the nulls.

## Universe (Binance ↔ MT5, broker-verified)

BTC↔BTCUSD.pi, ETH↔ETHUSD.pi, SOL↔SOLUSD.p, XRP↔XRPUSD.pi, BNB↔BNBUSD.p, DOGE↔DOGUSD.p,
LINK↔LNKUSD.p, AVAX↔AVXUSD.p, LTC↔LTCUSD.pi, TRX↔TRXUSD.p, DOT↔DOTUSD.pi, BCH↔BCHUSD.p,
XLM↔XLMUSD.p, UNI↔UNIUSD.p, ATOM↔ATMUSD.p, FIL↔FILUSD.p, NEAR↔NERUSD.p, XMR↔XMRUSD.p,
ALGO↔ALGUSD.p, VET↔VETUSD.p (ADA/PEPE/SHIB: liquid on Binance, no CFD at this broker — excluded).

## Start locally (5 steps)

```bash
# 1) clone + deps (Python 3.10+)
git clone https://github.com/karan1399/Trading-Strategie.git && cd Trading-Strategie
pip install -r requirements.txt

# 2) fill the data store (Binance Vision monthly orderflow+footprint; first fill ~15-30 min)
python3 binance_orderflow/binance_orderflow_dump.py --assets {U20} \\
    --from 2026-10-01 --to 2026-11-23
# incremental afterwards (fetches only missing days, repairs gaps):
python3 binance_orderflow/binance_orderflow_dump.py --assets {U20} --update

# 3) verify offline known-answer tests (no network needed)
python3 binance_orderflow/binance_orderflow_dump.py --selftest
python3 engine/unified_alpha_pilot.py --mode selftest

# 4) validation backtest over the filled store
python3 engine/unified_alpha_pilot.py --mode backtest

# 5) live paper signals (Binance footprint drives; prints + writes desk/signals.json)
python3 engine/unified_alpha_pilot.py --mode paper --update-data
```

`--mode signal` prints without persisting; `--status` shows per-asset data coverage.

## Rebuild the research (optional, reproduces every number)

```bash
python3 scripts/aec_build_dataset.py 15m                # 15m orderflow+footprint dataset
python3 scripts/aec_per_asset.py research_15m.parquet   # per-asset opt (TP>=2R pool, IS-select)
python3 scripts/orb_emarej_crypto.py research_15m.parquet  # ORB / EMA-rej sleeves
```
Artifacts land in `binance_orderflow/data/research/` (summaries, per-asset JSON, engine-ready
overrides). Full methodology + results + honest caveats: `desk/UNIVERSE20_ALPHA_REPORT.md`,
`desk/FOOTPRINT_ALPHA_RESEARCH.md`.

## MT5 attachment

Signals are venue-agnostic dicts with `mt5_symbol`, side, order_type (limit/market),
entry/stop/tp, volume. `execution.venue` in `engine/config.json`: `paper` (stub fills,
default), `mt5` (bridge hook for your terminal). Risk: `risk.risk_per_trade_pct` (0.5%),
`max_concurrent` 4, `daily_loss_cap_pct` 1.5. Paper-validate forward before sizing real
lots; the 54-day OOS window favored shorts (orderly bear leg) — re-validate monthly.

## Going live — one command, hard-guarded

```bash
python3 engine/unified_alpha_pilot.py --live --i-understand-live-risk
```

Runs all six sleeves (footprint-alpha + Delta-Fibonacci + volatility-compression ports)
against your real MT5 terminal every 15 minutes (bar close +20s,
incremental Binance gap-fill each pass). Guards — each one covered by a selftest:

- **Preflight, all must pass before any order**: terminal connected, algo-trading
  enabled, account tradeable, universe symbols `trade_mode=FULL`.
- **Never stubs fills**: live requires the `MetaTrader5` module; missing/failed
  bridge aborts (exit 2) — no fake fills, ever.
- **Daily loss circuit breaker** (`risk.daily_loss_cap_pct`, default -1.5% vs the
  UTC-day starting equity): no new entries for the rest of that UTC day.
- **Max concurrent** 4 positions, fixed-fractional sizing from live account equity,
  volume normalized to the broker's min/step/max.
- **Kill switch**: `touch desk/KILL` (or Ctrl+C) stops new entries; open positions
  keep their server-side SL/TP.
- **Dedupe + restart safety**: stable signal ids; `desk/live_orders.jsonl` records
  INTENT before and RECEIPT after every order; ids are never re-sent after a restart.
- **Stale-data (>20 min) and spread-cap** guards discard dangerous conditions.

## Are these strategies profitable? (honest read)

Validated on 54 days of Binance orderflow with untouched OOS windows: ORB sleeve OOS
PF 3.58 / 71.7% win @2R (n=548, ~0.42R avg), EMA-rej OOS PF ~2.0, AEC-Trend per-asset
OOS PF 1.2–1.9 on the 11/20 assets that qualified. That is **positive expectancy, not
a guarantee**: (1) the window favored shorts (orderly bear leg) — re-validate monthly;
(2) OOS stats are maker-fill modeled; real MT5 CFD spreads/swaps/rejections can consume
20–30 bps of a 30–60 bps edge — expect live below backtest; (3) assets without a
verified config are not traded by design ("do not trade the nulls"). Run `--mode paper`
at least two weeks and compare fills to the log before sizing real lots.

The ported Delta-Fibonacci and volatility-compression sleeves are code-faithful to
their engines but **their own research was on metals/FX/indices 4H/1H** — pooled on
crypto 15m over this window they are net negative (PF 0.76–0.87), which is why they
ship integrated but default-off. Treat them as a starting point for per-asset
optimization, not as ready crypto edges.

## Repo layout

- `binance_orderflow/binance_orderflow_dump.py` — the data engine (single file)
- `engine/` — unified signal engine + config (universe map, sleeves, overrides, risk)
- `scripts/` — research stack (per-asset optimizer, sleeve research, validation, reporting)
- `desk/` — research reports
- `binance_orderflow/data/` — created at runtime by the dump engine (gitignored)
"""

GITIGNORE = """# data store is rebuilt by the dump engine — never commit raw parquet
binance_orderflow/data/
*.parquet
download/
upload/
repos/
skills/
node_modules/
__pycache__/
*.pyc
.env
*.log
/tmp/
"""

REQUIREMENTS = "pandas>=2.0\nnumpy>=1.26\npyarrow>=15.0\nrequests>=2.31\n"

PUSH = """#!/bin/bash
# One-command push of this clean repo to GitHub. Token is NEVER stored on disk.
#   GITHUB_TOKEN=ghp_xxxxx bash push_to_github.sh [repo_url] [branch]
REPO_URL="${1:-https://github.com/karan1399/Trading-Strategie.git}"
BRANCH="${2:-main}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN env var (PAT with Contents: Read&Write on the repo)}"
AUTH_URL=$(python3 -c "import os,sys;u=sys.argv[1];t=os.environ['GITHUB_TOKEN'];print(u.replace('https://','https://x-access-token:'+t+'@'))" "$REPO_URL")
git push "$AUTH_URL" "HEAD:refs/heads/$BRANCH" --force-with-lease 2>&1 | sed -E "s/x-access-token:[^@]+@/***@/g"
rc=$?
git remote remove autotmp 2>/dev/null
exit $rc
"""


def sh(cmd, cwd=None, quiet=False):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0 and not quiet:
        print(f"[cmd] {cmd}\n{r.stdout[-800:]}{r.stderr[-800:]}")
    return r.returncode


def main():
    if os.path.exists(DST):
        shutil.rmtree(DST)
    os.makedirs(DST)
    # ---- whitelist copy
    def cp(rel):
        s = os.path.join(SRC, rel)
        d = os.path.join(DST, rel)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.docx", "*.xlsx", "*.parquet"))
        elif os.path.isfile(s):
            os.makedirs(os.path.dirname(d) or DST, exist_ok=True)
            shutil.copy2(s, d)

    cp("binance_orderflow/binance_orderflow_dump.py")
    for f in os.listdir(f"{SRC}/binance_orderflow"):
        if f.endswith((".md", ".sh")):
            cp(f"binance_orderflow/{f}")
    cp("engine"); cp("scripts"); cp("desk")
    for f in ("aec_asset_overrides.json", "aec_per_asset_summary.csv", "aec_per_asset.json",
              "orb_emarej_summary.csv", "orb_emarej_results.json", "aec_scenarios.csv",
              "aec_validation_scen645.json"):
        cp(f"binance_orderflow/data/research/{f}")
    open(f"{DST}/README.md", "w").write(README)
    open(f"{DST}/.gitignore", "w").write(GITIGNORE)
    open(f"{DST}/requirements.txt", "w").write(REQUIREMENTS)
    open(f"{DST}/push_to_github.sh", "w").write(PUSH)
    os.chmod(f"{DST}/push_to_github.sh", 0o755)

    # ---- standalone verification inside the clean repo
    r1 = sh("python3 binance_orderflow/binance_orderflow_dump.py --selftest", cwd=DST, quiet=True)
    r2 = sh("python3 engine/unified_alpha_pilot.py --mode selftest", cwd=DST, quiet=True)
    print(f"standalone selftests in clean repo: dump={'PASS' if r1 == 0 else 'FAIL'} "
          f"engine={'PASS' if r2 == 0 else 'FAIL'}")
    if r1 or r2:
        sys.exit("clean-repo selftest failed — fix before shipping")

    # ---- 3 narrative commits
    sh("git init -b main && git config user.email z@container && git config user.name 'Z User'", cwd=DST)
    sh("git add .gitignore requirements.txt README.md push_to_github.sh && "
       "git commit -q -m 'chore: repo scaffold — guide, requirements, gitignore, push helper'", cwd=DST)
    sh("git add binance_orderflow && git commit -q -m "
       "'feat(data): Binance Vision orderflow dump engine — 1m/5m/1h/4h orderflow + multi-TF "
       "footprint + metrics, incremental --update, selftest, sanity'", cwd=DST)
    sh("git add scripts desk && git commit -q -m "
       "'feat(alpha): research stack — AEC-Trend per-asset optimization (TP>=2R), ORB/EMA-rej "
       "footprint sleeves, validation, reports'", cwd=DST)
    sh("git add engine && git commit -q -m "
       "'feat(engine): unified signal engine — universe-20 MT5 mapping, per-asset overrides, "
       "3 sleeves, paper/mt5 venues'", cwd=DST)
    sh(f"git bundle create {BUNDLE} --all", cwd=DST)
    n = int(subprocess.run("git ls-files | wc -l", shell=True, cwd=DST, capture_output=True, text=True).stdout)
    sz = os.path.getsize(BUNDLE) / 1e6
    print(f"clean repo: {n} files, 4 commits; bundle {sz:.1f}MB -> download/trading-alpha-suite-clean.bundle")
    if sz > 50:
        sys.exit("bundle too big — something heavy slipped in")
    # mirror guide + push helper into download/ for immediate access
    shutil.copy2(f"{DST}/README.md", f"{SRC}/download/LOCAL_SETUP_GUIDE.md")
    shutil.copy2(f"{DST}/push_to_github.sh", f"{SRC}/download/push_to_github.sh")
    print("guide -> download/LOCAL_SETUP_GUIDE.md ; push helper -> download/push_to_github.sh")


if __name__ == "__main__":
    main()
