# Trading Alpha Suite — Binance footprint/orderflow signals, MT5 execution

Live signals are computed from **Binance USDT-M footprint + orderflow** (Vision monthly
dumps, incremental API gap-fill); every emitted signal carries the matching **MT5 CFD
symbol** (`mt5_symbol`) so execution targets the MT5 candle. Three sleeves ship wired:

| sleeve | entry logic | default target |
| --- | --- | --- |
| `aec_trend` | pullback/reclaim in EMA-regime with auction-complete (footprint) trigger | per-asset, TP ≥ 2R enforced |
| `ema_rej` | EMA 89/233 wick-kiss / pierce-fail rejection + AC-event confirm | 2R |
| `orb` | UTC 60-min opening-range breakout, close-through + delta>2% + volume ≥1.2× | 2R |

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
python3 binance_orderflow/binance_orderflow_dump.py --assets BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,LTCUSDT,TRXUSDT,DOTUSDT,BCHUSDT,XLMUSDT,UNIUSDT,ATOMUSDT,FILUSDT,NEARUSDT,XMRUSDT,ALGOUSDT,VETUSDT \
    --from 2026-10-01 --to 2026-11-23
# incremental afterwards (fetches only missing days, repairs gaps):
python3 binance_orderflow/binance_orderflow_dump.py --assets BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,LTCUSDT,TRXUSDT,DOTUSDT,BCHUSDT,XLMUSDT,UNIUSDT,ATOMUSDT,FILUSDT,NEARUSDT,XMRUSDT,ALGOUSDT,VETUSDT --update

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

Runs all three sleeves against your real MT5 terminal every 15 minutes (bar close +20s,
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

## Repo layout

- `binance_orderflow/binance_orderflow_dump.py` — the data engine (single file)
- `engine/` — unified signal engine + config (universe map, sleeves, overrides, risk)
- `scripts/` — research stack (per-asset optimizer, sleeve research, validation, reporting)
- `desk/` — research reports
- `binance_orderflow/data/` — created at runtime by the dump engine (gitignored)
