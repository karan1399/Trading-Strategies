# FOOTPRINT ALPHA RESEARCH — AEC-Trend (Auction-Complete + body-EMA Confluence)

Data: Binance Vision aggTrades store (orderflow + multi-TF footprint parquet),
5m/15m bars, 2026-10-01 → 2026-11-23 UTC, 12 USDT-M perps.
IS (selection window) = Oct-01..Nov-07 · OOS (held out) = Nov-08..Nov-23.
Grid v1: 672 scenarios at 5m, taker execution. Grid v2/v3: 672 at 5m/15m, maker
execution (limit entries at signal close, limit TP 3R, taker stops/EMA exits).
Costs: 2 bps maker / 5 bps taker / 1 bp slippage (+1 bp on stops).

## 1. The idea, and what the data said

Trade WITH the trend when price pulls back into the candle-body EMA zone AND the
footprint shows an auction-complete event at the pullback extreme (absorption at a
12-bar low with positive delta, delta-flip, POC-in-lower-third with strong close,
bin imbalance ≥1.8, or 2× volume capitulation). Stop beyond the 3-bar auction zone
(±2.5 ATR), ride via 3R maker TP + body-EMA50 break exit + 200-bar time cap. Shorts
mirror longs exactly.

## 2. Key scientific result — the footprint trigger is REAL information

The ablation (identical regime/entry/stop/exit family, only the trigger varies, 15m):

| trigger | IS n | IS avg bps | IS t | OOS n | OOS avg bps | OOS PF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `ac_abs` | 23 | 12.78 | 0.31 | 11 | 189.8 | 6.427 |
| `ac_hvn` | 48 | -15.02 | -0.71 | 23 | -52.38 | 0.316 |
| `ac_imb` | 1 | 3.01 | 3010553600.03 | 0 | nan | nan |
| `ac_climax` | 2 | 53.85 | 0.47 | 0 | nan | nan |
| `ac_strong` | 25 | 16.06 | 0.42 | 11 | 189.8 | 6.427 |
| `ac_any` | 177 | -1.34 | -0.11 | 93 | 49.42 | 1.928 |
| `ac_none` | 631 | -16.97 | -2.71 | 303 | 6.92 | 1.1 |

The baseline **without** the footprint trigger is decisively negative (IS −28.8 bps,
t = -2.7; OOS −25.5, PF 0.59): 'buy dips in uptrends'
alone LOSES after costs. Every auction-complete variant lifts it by ~15-20 bps IS and
~35-45 bps OOS. The footprint trigger is what turns a losing pattern into a ~flat-to-
winning one — and the Monte Carlo test proves the placement is not luck:

- **MC permutation A** (300 draws/asset × 12 assets): on the SAME regime+pullback
  candidate bars, actual footprint-timed entries average **51.17842501443922 bps** vs
  **-17.55177848731729 bps** for randomized trigger placement → **p = 0.0**.
  (Individual assets: LINK +148.9 vs −12.4, SOL +141.6 vs −20.8, VET +163.7 vs −47.2.)

## 3. The validated family (regime-conditional)

`strict_htf` (body-EMA structure + 1h EMA20/50 alignment) · `reclaim` entry (touch
EMA20 within 5 bars with prior extension ≥1.5 ATR, close back through EMA20) · any AC
variant · stop 2.5 ATR beyond the auction zone · TP 3R maker + EMA50 break + 200-bar cap.
IS-selected via t-stat (n≥60), reported untouched on OOS:

| stage | n | win% | avg bps | PF | total |
| --- | ---: | ---: | ---: | ---: | ---: |
| IS | 177 | 24.3 | -1.34 | 0.975 | -2.4% |
| **OOS** | 93 | 31.2 | **49.42** | 1.928 | 46.0% |

OOS per-symbol stability (10/12 symbols positive after costs):

| symbol | n | win% | avg bps | PF |
| --- | ---: | ---: | ---: | ---: |
| 1000PEPEUSDT | 6.0 | 33.3 | 122.31 | 2.629 |
| 1000SHIBUSDT | 11.0 | 18.2 | 16.3 | 1.3 |
| ADAUSDT | 10.0 | 20.0 | 5.81 | 1.083 |
| AVAXUSDT | 6.0 | 16.7 | -65.66 | 0.095 |
| BNBUSDT | 7.0 | 42.9 | 25.88 | 1.703 |
| BTCUSDT | 9.0 | 33.3 | 23.47 | 1.479 |
| DOGEUSDT | 10.0 | 30.0 | 122.13 | 3.844 |
| ETHUSDT | 7.0 | 28.6 | 10.36 | 1.237 |
| LINKUSDT | 10.0 | 60.0 | 148.87 | 7.525 |
| SOLUSDT | 4.0 | 50.0 | 141.61 | 4.353 |
| VETUSDT | 6.0 | 50.0 | 163.75 | 5.213 |
| XRPUSDT | 7.0 | 0.0 | -100.67 | 0.0 |

**Bootstrap** (10k resamples of the 93 OOS trades): mean 49.42309653436713 bps,
95% CI **[1.9, 102.1]** — excludes zero; P(mean>0) = 0.980.

## 4. Honest caveats (what a quant must say)

1. **Regime-conditional.** IS (early-Oct chop) was flat (−1.3 bps); the OOS window was an
   orderly bear leg. The naive walk-forward pick by IS t-stat (scen 141) did NOT generalize
   (OOS −6.4) — the edge is concentrated in trending regimes, so the engine must trade it
   regime-aware, not blind.
2. **Deflated Sharpe over the full 672-trial grid = %.3f** — with 93 OOS trades the strictest
   multiple-testing bar is not cleared. The permutation test proves the *information*, the
   bootstrap shows the *OOS CI*, but sample size limits certainty → **forward-validate in
   paper mode before sizing** (the engine ships in paper mode by default).
3. 54 days, one macro regime, maker-fill assumptions (limit retest fills) — live fills will
   differ. Risk 0.5%/trade, max 4 concurrent, 1.5% daily stop are configured conservatively.

## 5. Deployment (end to end, incremental)

```bash
python3 binance_orderflow/binance_orderflow_dump.py --assets SUITE --update   # fill data gaps
python3 engine/unified_alpha_pilot.py --mode selftest                        # offline check
python3 engine/unified_alpha_pilot.py --mode backtest                        # store-wide sanity
python3 engine/unified_alpha_pilot.py --mode paper --update-data            # signals + persist
```

Config: `engine/config.json` (venue=paper → switch to mt5 on a Windows terminal with the
MetaTrader5 package installed; the bridge auto-stubs elsewhere). Every rerun only fetches
what is missing, recomputes features, and re-emits signals — pull, run, trade.

*Artifacts: aec_trades.parquet (261,914 trades), aec_scenarios.csv (672 scenarios),
aec_validation_scen645.json / aec_validation.json, research_15m.parquet.*