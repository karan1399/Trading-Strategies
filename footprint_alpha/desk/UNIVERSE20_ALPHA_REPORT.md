# UNIVERSE-20 ALPHA REPORT — per-asset optimization + sleeve upgrades

Universe: the top-20 most liquid Binance USDT-M perps that ALSO exist as MT5 CFDs at the
broker in `repos/karan-ts/historical_parquet` (verified per-TF). ADA/PEPE/SHIB are liquid on
Binance but have no MT5 CFD at this broker — excluded, exactly as requested.

| # | Binance | MT5 | # | Binance | MT5 |
| --- | --- | --- | --- | --- | --- |
| 1 | BTCUSDT | `BTCUSD.pi` | 11 | DOTUSDT | `DOTUSD.pi` |
| 2 | ETHUSDT | `ETHUSD.pi` | 12 | BCHUSDT | `BCHUSD.p` |
| 3 | SOLUSDT | `SOLUSD.p` | 13 | XLMUSDT | `XLMUSD.p` |
| 4 | XRPUSDT | `XRPUSD.pi` | 14 | UNIUSDT | `UNIUSD.p` |
| 5 | BNBUSDT | `BNBUSD.p` | 15 | ATOMUSDT | `ATMUSD.p` |
| 6 | DOGEUSDT | `DOGUSD.p` | 16 | FILUSDT | `FILUSD.p` |
| 7 | LINKUSDT | `LNKUSD.p` | 17 | NEARUSDT | `NERUSD.p` |
| 8 | AVAXUSDT | `AVXUSD.p` | 18 | XMRUSDT | `XMRUSD.p` |
| 9 | LTCUSDT | `LTCUSD.pi` | 19 | ALGOUSDT | `ALGUSD.p` |
| 10 | TRXUSDT | `TRXUSD.p` | 20 | VETUSDT | `VETUSD.p` |

Data flow stays exactly as designed: **Binance Vision/API footprint+orderflow drives all
signals; execution targets the MT5 candle for the mapped CFD** (`mt5_symbol` is stamped on
every emitted signal; the paper/MT5 bridges use it).

## 1. Per-asset optimization (AEC-Trend, 15m maker grid, 2R minimum enforced)

448-scenario grid per asset (regime × entry × 7 footprint triggers × stop × TP{2R,3R,5R}
× side); selection IN-SAMPLE ONLY with IS n≥30 and TP≥2R hard constraints; OOS untouched.

**11/20 assets OOS-verified** (11 engine overrides written; 1000PEPE OK but not in the
MT5 universe). Nulls are reported as nulls — no forced configs.

| asset | best config (IS) | IS n / t | OOS n | OOS avg bps | OOS PF | OOS avg R |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| ALGOUSDT | strict/reclaim/ac_any/stop2.5/2R/short | 41 / +0.03 | 14 | 53.23 | 1.93 | 0.379 |
| AVAXUSDT | strict_htf/reclaim/ac_none/stop2.5/3R/short | 42 / -0.96 | 21 | 10.92 | 1.128 | -0.008 |
| BNBUSDT | strict_htf/reclaim/ac_none/stop3.5/3R/short | 41 / +0.17 | 23 | 33.66 | 1.57 | 0.17 |
| DOGEUSDT | strict/reclaim/ac_any/stop2.5/2R/short | 39 / -0.17 | 22 | 37.19 | 1.731 | 0.064 |
| ETHUSDT | strict/reclaim/ac_any/stop2.5/3R/short | 30 / +0.98 | 14 | 18.92 | 1.539 | 0.227 |
| FILUSDT | strict/touch_b2/ac_any/stop3.5/5R/short | 54 / -0.07 | 27 | 19.63 | 1.34 | 0.045 |
| LINKUSDT | strict/touch_b2/ac_any/stop3.5/3R/short | 49 / +0.28 | 26 | 19.32 | 1.392 | 0.141 |
| LTCUSDT | strict/reclaim/ac_any/stop3.5/3R/short | 34 / +0.46 | 17 | 40.16 | 1.664 | 0.249 |
| NEARUSDT | strict/touch_b2/ac_any/stop2.5/2R/short | 51 / -0.40 | 19 | 61.45 | 1.83 | 0.273 |
| UNIUSDT | strict_htf/reclaim/ac_none/stop3.5/2R/short | 30 / +0.54 | 19 | 21.3 | 1.208 | 0.08 |
| VETUSDT | strict_htf/reclaim/ac_none/stop2.5/3R/short | 57 / +0.44 | 18 | 10.92 | 1.128 | 0.071 |

Non-qualifying (honest): BTC (1 OOS trade — no config clears the n-bar), ATOM, BCH, DOT,
SOL, TRX, XLM, XMR, XRP negative OOS. **Do not trade the nulls** — the engine's override
map simply omits them (they still get the pooled AEC default in paper mode).

## 2. Sleeve upgrades with footprint/orderflow confluence

**ORB (crypto)** — UTC-day 60-min opening range; breakout bar must CLOSE through the edge
with aligned delta (|delta%|>2%) and ≥1.2× avg volume; stop = opposite range edge capped at
1.5× range; TP 2R/3R; flat by day end. Pooled over the universe (IS → untouched OOS):

- tp=2.0R, OI-filter=False: IS n=1490 win 69.3% avg 29.07 bps PF 2.886 t 13.69 → OOS n=548 win 71.7% avg 34.55 bps PF 3.583 t 9.86 (avg 0.415R)
- tp=3.0R, OI-filter=False: IS n=1466 win 63.6% avg 36.09 bps PF 2.879 t 13.26 → OOS n=547 win 67.3% avg 39.0 bps PF 3.243 t 9.32 (avg 0.753R)
- tp=2.0R, OI-filter=True: IS n=1487 win 69.3% avg 29.27 bps PF 2.915 t 13.79 → OOS n=541 win 71.7% avg 33.37 bps PF 3.476 t 9.71 (avg 0.412R)
- tp=3.0R, OI-filter=True: IS n=1463 win 63.6% avg 36.3 bps PF 2.903 t 13.33 → OOS n=540 win 67.2% avg 37.79 bps PF 3.154 t 9.16 (avg 0.749R)

**EMA-89/233 rejection (crypto)** — the prior MT5 sleeve ported to Binance data with the
validated footprint trigger added: wick-kiss/pierce-fail patterns + auction-complete event
at the rejection extreme, maker limit entry, stop beyond rejection extreme ±1.5 ATR:

- ac_confirm=False, OI-filter=False, tp=2.0R: IS n=831 avg 33.83 PF 1.568 → OOS n=288 avg 66.43 PF 2.009 t 5.16 (avg 0.483R)
- ac_confirm=False, OI-filter=False, tp=3.0R: IS n=737 avg 31.29 PF 1.445 → OOS n=254 avg 90.94 PF 2.241 t 5.19 (avg 0.673R)
- ac_confirm=False, OI-filter=True, tp=2.0R: IS n=830 avg 34.13 PF 1.575 → OOS n=288 avg 67.28 PF 2.033 t 5.24 (avg 0.483R)
- ac_confirm=False, OI-filter=True, tp=3.0R: IS n=736 avg 31.63 PF 1.451 → OOS n=254 avg 91.17 PF 2.244 t 5.19 (avg 0.673R)
- ac_confirm=True, OI-filter=False, tp=2.0R: IS n=318 avg 49.86 PF 2.086 → OOS n=141 avg 61.78 PF 2.048 t 3.7 (avg 0.494R)
- ac_confirm=True, OI-filter=False, tp=3.0R: IS n=294 avg 51.68 PF 1.922 → OOS n=127 avg 70.2 PF 1.98 t 3.07 (avg 0.6R)
- ac_confirm=True, OI-filter=True, tp=2.0R: IS n=318 avg 49.86 PF 2.086 → OOS n=140 avg 58.74 PF 1.99 t 3.55 (avg 0.484R)
- ac_confirm=True, OI-filter=True, tp=3.0R: IS n=294 avg 51.68 PF 1.922 → OOS n=126 avg 64.94 PF 1.9 t 2.89 (avg 0.581R)

Read: AC-confirm raises IS quality (fewer, better trades) but the OOS edge concentrated in
the orderly bear leg favored the broader pattern; 3R variants monetize it best (+0.75R avg).
The OI-wall proxy filter (metrics-store OI 30-min change) is roughly neutral here — kept as
an optional flag, off by default. The FX IV-Wall package (OOS PF 2.00, DSR 0.88) is untouched;
its FX store lives with the MT5 deployment package.

## 3. Deployment

`engine/config.json` now carries: the 20-symbol MT5 universe map, `sleeves` =
`[aec_trend, ema_rej, orb]`, per-sleeve 2R defaults, and per-asset overrides auto-loaded
from `research/aec_asset_overrides.json`. Every signal is stamped `mt5_symbol` — Binance
footprint drives it, MT5 candle executes it:

```bash
python3 binance_orderflow/binance_orderflow_dump.py --assets SUITE --update
python3 engine/unified_alpha_pilot.py --mode selftest
python3 engine/unified_alpha_pilot.py --mode paper --update-data   # signals -> desk/signals.json
```

## 4. Honest caveats

- 54 days, one macro regime. OOS (Nov 8-23) was an orderly bear leg — short-side results
  benefited; the long mirrors are unproven in this window. Re-validate monthly as data lands.
- ORB's wide-stop/high-win-rate structure means tail risk lives in violent reversal days;
  the EOD-flat + 5% risk-distance guard caps it. Paper-validate forward before sizing.
- Deflated-Sharpe was computed for the pooled AEC family last phase; the per-asset/sleeve
  results here are reported with raw IS/OOS + t-stats and must clear the same forward bar.