# R048 strategy, optimizer, M4 and Delta audit

**Arena.ai / Codex · AUDIT · 28 November 2026 IST · Base ac5aaae50**

**Verdict.** Accept the offline timing and hard-sizing corrections. Carry forward the desk-capped M4 book as a shadow control. Withhold the expanded prop baskets, Delta champions and M4 replacements from live promotion. The local ledgers contain no untouched forward holdout or complete point-in-time broker execution tape.

**VERIFIED.** Reviewed 16 numbered packages, 94 asset files, 728 Stage A strategy/asset cells, 16 M4 routes, the unified pilot, and 262 Delta contracts across the ten PA families. All replays and tests used local inputs. [Modular and M4 results](r048_verification_results.json) · [Stage A refit](r048_stage_a_results.json) · [Delta results](r048_delta_results.json) · [Shadow JSON diffs](r048_shadow_proposals.json).

**UNVERIFIED-BY-AGENT.** Actual venue fills, balances, broker-loss tick values and currency conversion, fee/funding debits, liquidation-shelf clearance, challenge rules and live farm reservation enforcement. The lot checks below model gross stop risk on saved inputs unless noted.

## 1. Expanded universe and all 16 strategy families

Of 94 files, 91 matched an exact local ledger recipe. The three exceptions are explicit D1-gated Initiative variants, which required fresh signal replays. Of 49 additions, 44 came from prop Stage A and five from MT5 Stage A. Every added recipe had positive net R in the same historical validation period. A fixed pre-2024 screen of at least 20 settled trades, PF ≥1.15 and positive net R admitted 62 of the 91 traceable recipes and 29 of the 49 additions. The all-added export shows +531.77R and PF 1.432. The 29 training-screened additions show +347.42R and PF 1.348. These are raw sums with unequal sample sizes and overlapping exposures.

The unscreened local winner universe contained 423 distinct pairs. Of the 144 historical IS passers, 81 were positive later. The export omitted 80 of those passers, of which 60 were negative later. Filtering within the 94-file export retains that upstream selection bias. The fresh purged refit changed **138 of 728** selected recipes. Its shadow Stage B produced five A, nine B and 12 retired labels. An intermediate settled-label-only run put S04 in A and S10 in B. Adding the closed tail, gap price and time-exit correction reversed both labels. This outcome sensitivity is a reason to keep both in research. [Stage A runner](r048_stage_a_refit.py) · [Full scope and per-file provenance](r048_verify.py).

**Donchian expansion.** ETH/SOL earned +38.84 raw R on 191 fills, with replay PF **1.51**, DD **5.47%** and Calmar **0.88**. The seven-name expansion produced +121.44 candidate raw R and 440 fills from 701 candidates, with replay PF **1.16**, DD **6.85%** and Calmar **0.45**. ETH admissions went from 79 to 31 and SOL from 112 to 50. BCH and ZEC lost money after admission. BNB had a 39.08 bp saved spread, XAU lacked fixed pre-2024 evidence, and the BTC/BNB files mixed a different source recipe family into the basket. The IS-only six-name and 35 bp snapshot variants returned Calmar 0.41 and 0.66. Slot competition diluted the incumbents. Retain the two-name control and test each proposed addition marginally at matched capacity.

The table reports Stage A training passers, the count positive later and their additive raw validation R. Cached feature arrays and snapshot costs were held fixed for this diagnostic.

| Package | IS passers / later positive | Validation R | Disposition |
| --- | ---: | ---: | --- |
| s01 Williams pullback | 6 / 2 | +19.41 | Keep the locked shadow control. Breadth is sparse. |
| s02 Donchian breakout | 7 / 4 | +22.60 | ETH/SOL control. Eleven fitted recipes changed. |
| s03 EMA233 imbalance rejection | 13 / 6 | −33.65 | Withhold breadth expansion. The R047 ETH trail challenger is narrow. |
| s04 Volatility compression | 8 / 2 | −16.29 | Standalone Calmar 0.84. Keep conditional. |
| s05 EMA zero order-flow | 17 / 12 | +68.78 | Strongest broad shadow family. Require genuine footprint inputs. |
| s06 Imbalance stack | 2 / 2 | +30.92 | Two qualified assets. Stress costs and concentration. |
| s07 Daily pivot breakout | 11 / 7 | +64.50 | Latest-bar signal fix verified. BTC lot feasibility remains binding. |
| s08 Volatility target overlay | — | — | Removing vOF reduced M4 validation Calmar from 1.03 to 0.89. |
| s09 Donchian CTA | 6 / 4 | +49.27 | Isolated standalone PF 1.07. Keep conditional. |
| s10 EMA pullback | 15 / 9 | +50.66 | Historical PF 1.46 and modeled breach 8.4%. Replicate under stressed costs. |
| s11 OF Initiative breakout | 5 / 3 | +18.43 | Legacy grid and explicit D1 recipes have separate evidence. |
| s12 OI liquidation breakout | 7 / 3 | −0.38 | Research only pending released OI and adapter parity. |
| s13 OI reclaim fade | 7 / 2 | −6.06 | Research only. Keep the promotion veto. |
| s14 OI squeeze momentum | 15 / 7 | +5.69 | Research only despite historical standalone tag. |
| s15 RSI2 mean reversion | 6 / 4 | +44.35 | Standalone Calmar 0.95 and validation PF 1.04. Keep conditional. |
| s16 Zero auction reversal | 13 / 8 | +21.23 | Shadow with source-quality gates for the footprint. |

**S07 prefix proof.** The cached BTC M4 run had 144 nonzero S07 signals. Thirteen prefixes ending at nonzero signals reproduced the full side vector on their shared prefix. The exact R046 case at Unix 1795233600 now emits a short from the latest closed bar. The proof concerns detection in cached data. A filled venue trade requires separate evidence.

**S11 UTC close and gap handling.** The committed plugin used the previous bar-open day even when there was no UTC closing bar and changed its EMA span with sample length. The patched plugin uses fixed EMA spans, explicit end timestamps, 200 observed D1 closes and the latest observed UTC midnight close younger than 24 hours. The midnight close is available to a signal closing at the same time. Weekend and holiday gaps create no artificial closes, and a stale Friday close expires. A TradFi session variant needs explicit session-close observations and a separately justified age limit. On 12,879 BTC bars, the corrected direction mask matches the R047 reference exactly. Fresh plugin replays confirm that the D1 gate raised BTC PF from 1.66 to 1.97 while reducing net R from +34.19R to +29.52R. ETH fell from +21.26R to −8.39R and SOL fell from +7.42R to −1.02R. [S11 code](../../strategies/s11_of_initiative_breakout/strategy.py).

**S12–S14 OI publication.** All three strategy entry points still return zero. Propose marking their 13 asset files research-only until a genuine adapter shares the optimizer's feature clock and formulas. Strict Stage A now requires recorded OI availability, ignores stale event times and refuses to let a late older backfill displace a newer observation. Strict mode abstains when local metrics lack availability timestamps. The existing event-time research replay remains useful as a diagnostic. [OI code](../../footprint_Chart/backtesting/oi_vwap_sweep_research.py) · [Status diffs](r048_shadow_proposals.json).

## 2. M4 V3B and the 2025–26 plateau

The supplied 16-pair control reproduced exactly at $2,501.80. Inspecting the producer showed that native MT5 H4 signals use an H1 execution tape. The R047 fallback released those exits at the H4 close. Corrected legacy inference and explicit metadata for new ledgers now use the H1 close. The short 2025–26 comparisons reset equity to the same start. Drawdown and ulcer below are realized-balance metrics.

| Scenario | Full end / Calmar | 2024–26 end / Calmar / ulcer | 2025–26 Calmar / ulcer |
| --- | ---: | ---: | ---: |
| Supplied V3B control | $14,822.72 / 2.21 | $3,933.60 / 1.05 / 10.40 | −0.25 / 10.32 |
| Correct H1 exit availability | $14,716.38 / 2.16 | $3,930.84 / 1.03 / 10.56 | −0.27 / 10.79 |
| H1 with frozen M4 weight/magic priority | $14,952.02 / 2.24 | $3,919.81 / 1.05 / 10.37 | −0.26 / 10.67 |
| Cap weights at 1.0 | $12,123.47 / 2.33 | $3,647.20 / 1.04 / 8.62 | −0.20 / 8.68 |
| Drop ETH diagnostic | $13,171.20 / 1.70 | $4,046.19 / 0.93 / 12.06 | −0.37 / 12.56 |
| ETH closed-D1 gate | $13,021.46 / 1.86 | $4,073.98 / 1.04 / 11.20 | −0.34 / 11.48 |
| ETH H4 vol ≤ trailing 60-bar median | $14,878.90 / 1.93 | $4,116.80 / 1.01 / 11.72 | −0.32 / 12.17 |

ETH added **+5.39R / +$94.86** in the corrected 2025–26 reset replay. SP500 contributed **−17.15R / −$318.85** and USDJPY **−2.72R / −$56.67**. ETH's individual RSI2 variant lost 5.19 raw R across 2024–26, while ETH VWAP made 31.29 raw R before admission. Removing ETH improved purged IS Calmar and deteriorated later Calmar and ulcer. The weight cap reduced ulcer with near-identical validation Calmar, while later-period equity still declined. Treat it as a risk challenger.

The fixed native H4 PA04 test for SP500, USDJPY, GAUUSD, GAUCNH and USWTI admitted **zero** replacements under the minimum 20 pre-2024 settled trade rule. GAUUSD and GAUCNH had zero pre-2024 observations. Their positive later tests remain descriptive. GAUCNH lacked a verified CNH loss conversion and USWTI lacked the required contract snapshot. GAUUSD sizing uses native stop distance. Historical XAUUSD P&L still comes from a different instrument.

### Whole-lot economics and conflict-free policy

Latest cached bars and saved contract terms admitted **11 of 16** pairs under the $20/$30 research caps and **5 of 16** under a $6 gross-stop cap. The BTC S07 minimum lot stress proxy was **$30.74 gross**, above the $30 research cap. Broker bid/ask, stops/freeze levels, loss tick and account-currency conversion must be captured at the decision. [MT5 symbol information](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py) · [MT5 account-currency P&L](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py).

| 2025–26 paired local coverage | Trades | End | CAGR | DD | Calmar | Ulcer |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fractional control | 488 | $2,426.24 | −1.6% | 11.57% | −0.14 | 7.23 |
| $20/$30 whole-lot caps | 461 | $2,716.68 | 4.4% | 22.22% | 0.20 | 12.60 |
| Whole lots, weights ≤1.0 | 461 | $2,777.80 | 5.7% | 20.46% | 0.28 | 11.51 |
| $6 stop cap | 222 | $2,767.32 | 5.5% | 2.43% | 2.26 | 1.04 |
| $6 cap, two total, one crypto, 1% heat | 182 | $2,789.59 | 5.9% | 1.75% | 3.40 | 0.66 |
| Same policy, +10 bp per round trip | 182 | $2,716.49 | 4.5% | 2.03% | 2.19 | 0.84 |
| Same policy, +20 bp per round trip | 182 | $2,643.39 | 3.0% | 2.53% | 1.17 | 1.12 |

Only **609 of 744** candidates had matching local bars and contract inputs for the paired comparison. The covered candidate tape was stronger. PF for the desk-capped arm was 1.68, 1.46 after +10 bp and 1.28 after +20 bp, using the causal decision close to translate each flat surcharge to R. The desk-capped row is the preferred shadow control and requires reproducible live-equivalent costs and fill evidence. Use one central serialized interlock and reservation ledger across M4, the unified pilot and Delta. Limit the farm to two total reserved/pending/open slots, one crypto and one canonical symbol exposure, with grade, $6 MT5, $0.50 Delta, $1.50 Delta margin, and 1% heat constraints. The M4 six-slot 0.80% growth model and $20/$30 caps remain personal research assumptions. They confer no venue override. [Desk authority](../CANONICAL_MASTER_SPECIFICATION.md#L119) · [M4 and unified proposed caps](r048_shadow_proposals.json).

### Remaining parity gaps

- M4 now takes H2 overlay context from H4 bars closed by the H2 decision. Its collector orders minute open/close by exchange event, deduplicates within minute, applies close grace, counts late events and preserves the forming-minute boundary. Neutral kline fallback still cannot reconstruct true footprint levels and incomplete coverage still requires a fail-closed source-quality gate before production.
- M4 stop amendments now use tick grids, stop/freeze distances and acknowledgment-aware retry. The simulator uses bar highs/lows. M4 uses sampled bid/ask MFE. True latency, cashflow and MFE can differ. Brackets need post-fill checks against actual risk, net target distance and shelf clearance. The repaired MT5 lot function enforces a hard dollar cap even above minimum size. [M4 code](../../engine/m4_live_engine.py) · [Unified pilot](../../engine/unified_alpha_pilot.py).
- The unified pilot only hydrates its configured sleeve fields. M4 uses its separate pair list and builders. The numbered asset JSONs remain research inputs. A local position count cannot account for other linked accounts or pending intents. Preserve reservations through uncertain submissions until readback resolves them.

## 3. Delta PA sweep and route audit

The old Delta optimizer fit straddling outcomes, chose risk on full history, resolved entry ties by future exit and credited P&L when a slot opened. The corrected source scores settled H1 labels against a fixed calendar cutoff, uses deterministic entry identity and close-time cash, chooses risk only in training, and reserves one position per product. Missing expected entry hours are skipped, gapped stops use the adverse open and unfinished positions contribute no final label. It excludes newly generated shadow candidates from the live recipe loader. The checked-in legacy report remains validation-selected.

The live adapter now floors size to its equity, $0.50 stressed stop and $1.50 margin budgets. It rejects stale entry candles and skips a cycle when positions or pending orders are unreadable. The existing market IOC route, prior-close bracket anchor, mark-price trigger, symbol-level stop lookup and lack of a common gateway still prevent calling it desk-ready. Venue-market rules, signed reservation, passive limits, actual fill and bracket readback, native funding and trigger behavior remain release dependencies. [Delta live adapter](../../Delta_Exchange_Data/delta_pa_live_trader.py) · [Delta fee reference](https://www.delta.exchange/support/solutions?articleId=80001177864).

All **262 local Delta contracts** ran through the ten-family scope, with **42 insufficient-history** classifications. The fixed **25 bp** cost model produced nine historical champions versus 24 legacy. Both counts use viewed outcomes. The portfolio used 12 symbols from 146 training-qualified contracts based only on settled observations before 1 June 2026. Delta histories are shorter than M4's and the June–August and September–November slices are historical evaluation. The fresh outer holdout starts after the analyzed data.

| Delta one-crypto book | Train PF | June–August PF | September–November PF | Later end / Calmar |
| --- | ---: | ---: | ---: | ---: |
| Lexical fractional | 1.50 | 0.66 | 0.61 | $4,557.26 / −1.88 |
| Frozen IS priority fractional | 1.54 | 0.81 | 0.70 | $4,763.22 / −1.20 |
| Frozen IS priority +10 bp | 1.41 | 0.77 | 0.56 | $4,703.05 / −1.38 |
| Frozen priority with hard contract caps | 1.53 | 0.80 | 0.80 | $4,992.39 / −1.45 |
| Hard caps +10 bp | 1.47 | 0.75 | 0.76 | $4,990.45 / −1.59 |

The IS-priority fractional book had 26.4% modeled 20K trailing breach, rising to 33.9% under +10 bp. The integer cap reduces dollar variance and retains negative expectancy. Reject the tested book for promotion. [Delta runner](r048_delta_replay.py) · [Optimizer source](../../scripts/delta_pa_trend_optimizer.py).

## 4. Causal controls and reproducibility

- Stage A, Stage 2 and Delta score settled labels before exclusive cutoffs. An unresolved trade can occupy capacity without contributing training P&L. Native cash and slot availability follow the execution candle. Stage A now includes the final closed signal bars, skips missing entry windows, prices gapped stops at the adverse opening price and leaves unfinished time exits open.
- Shared-account admission uses ascending `(ts_in, entry_priority, sleeve, symbol, mt5_symbol, tf, bar_i, side, entry_id)`. Priority is fixed before the decision. Duplicate keys require distinct entry IDs. Only the observed exit timestamp can release a slot or credit its P&L.
- The 26-strategy model now fixes its IS cutoff and fences research at **2026-12-01 UTC**. The WF pool requires a separate refit for each fold with matching training dates, settled training outcomes and skips folds without adequate evidence. Its sole registered legacy strategy is retired, so the fold path is verified with deterministic fixtures.
- The strict Stage A CLI accepts an offline execution tape and separate output directory. Each symbol supplies timestamped quotes, signed rollover debits and a complete cashflow coverage range. Quote fields are available_at, spread_bps, commission_bps, min_stop_distance, tick_value_loss, tick_size, vol_min, vol_max and vol_step. Rollover fields are cashflow_ts, buy_bps and sell_bps. Admission uses age, spread and stressed minimum-lot checks, followed by endpoint friction and carry. Strict Stage B floors actual lots to the per-entry grid, reserves stressed stop risk under 1% portfolio heat and computes realized P&L from gross stop risk. No real historical tape with that coverage was available. The full-grid run used saved feature arrays and snapshot proxy pricing as disclosed.
- Footprint tick floors use each bar’s observed open. Cache schema 3 invalidates the old chunk-wide price-median heuristic. The Stage A plateau term is constant within a strategy’s asset grid and therefore leaves trial ranks unchanged. Treat winner selection as a multiple-testing risk. The full-grid diagnostic deliberately read existing caches in isolation to preserve comparability. It did not overwrite the checked-in recipe or venue configs.
- The new holdout starts **2026-12-01 UTC** and remains pending by calendar. Freeze recipes, priority, risk and cost arms before that date. Compare the same eligible tape and the same risk budget. Proposed acceptance requires six months and at least 100 settled entries, net PF above 1 and Calmar at least 1 after +20 bp, lower daily-equity ulcer than the matched control and zero violations of reserved-risk or slot limits. A same-date block bootstrap should test the paired net daily performance difference. Unreconciled fills, missing cashflows, missing loss-currency terms or a breach of the written interlock disqualify the result.

Run from the repository root. Each report is emitted as JSON on standard output. The Stage A output directory must be new and separate from the tracked results.

```powershell
python -X utf8 -m desk.quant_audits.r048_verify
python -X utf8 -m desk.quant_audits.r048_stage_a_refit --output-dir "$env:TEMP/r048-stage-a-new" --workers 2
python -X utf8 -m desk.quant_audits.r048_delta_replay
```

**Verification.** The footprint suite passed 9 tests and the final focused suites passed 35. The M4 offline self-test and actual S07 cached prefixes passed. Regression fixtures cover unsettled labels, closed-tail replay, gap execution, lot grids, UTC day gaps, release-time OI, quote and rollover availability, WS event ordering, Delta gaps and incomplete labels, and failed position readback. All 19 JSON proposal sets and their 70 preconditions were checked against copies of the current files. [Causal regression fixtures](../../footprint_Chart/tests/test_r048_causal_inputs.py).

**Signed — Arena.ai / Codex, quantitative review.**
