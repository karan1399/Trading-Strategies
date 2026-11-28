# R047 modular strategy and concurrency review

**Arena.ai / Codex · AUDIT · 28 November 2026 IST**

**Verdict.** Apply the offline simulator correction. Keep per-asset parameter trials in shadow evaluation. Use a farm-wide one-crypto policy as the admission control for any later deployment. The replay shows that this control can improve the return-to-drawdown ratio on the historical research tape, but the broker lot and dollar-cap sensitivity determines whether that result is economically usable.

**VERIFIED scope.** Audited the requested `c427c1b6b` and the 45 asset JSONs across 15 strategy packages and 10 assets. The workspace advanced to `09efc67f0` during the review. That commit changed Charts discovery and the pilot’s archive path fallback. The asset recipes, optimizer inputs, engine config and M4 signal code were identical across the two revisions. The replay imports the pinned Charts strategy classes directly to exercise the original optimizer’s entry masks without depending on discovery. [Reproducible replay](r047_review.py) · [Full results and cache SHA-256 hashes](r047_review_results.json).

**UNVERIFIED-BY-AGENT.** Current venue lot and margin terms, live bid/ask and swap, full event timestamps, venue acknowledgments and the prop challenge reset rules. All portfolio numbers below are offline model results.

## 1. Recipe provenance and runtime wiring

- **42 saved recipes replay exactly.** The regenerated entry minutes, exit minutes and net R match the local winner ledger for the 42 legacy recipes. That verifies the recipe parameters and historical baseline. The resaved performance fields serve several purposes. Some are taken after the account governor’s trade admissions, and the restructure script preferentially exports assets with positive 2024–2026 results. The selected asset universe and `PROFITABLE_VERIFIED` tag therefore lack independent validation.
- **Three D1-gated recipes require correction.** The modular OF Initiative BTC JSON carries +42.54R, PF 1.39 and 0.1655R/trade from R046’s five-asset D1 ledger subset. Those figures are different from a fresh BTC-only replay. ETH and SOL also fail to reproduce their published positive figures in the D1-gated replay. The plugin currently ignores `d1_ema_trend_gate`; the audit applied the D1 mask before minute replay to measure the stated hypothesis. The package-level `profitable_assets` list is empty and the Stage B retirement gate still excludes the strategy. The two ablation diffs are review-only.
- **Discovery and routing are distinct.** At `c427c1b6b`, Charts discovery returned an empty registry because it searched root-level files only after they moved. The optimizer requires `bb_zscore`, `donchian_cta`, `vwap_reclaim`, `ema_pullback` and `rsi2_meanrev`. This was reproducible as `KeyError: 'bb_zscore'`. At `09efc67f0` the focused Charts discovery test and M4 self-test passed. The archive compatibility aliases resolve imports, and the pilot still has enabled sleeves whose modules are archived, including `htf_gold`, `ig_s5_zscore_breakout` and `ig_s2_rsi2_meanrev`. Archive status is a research classification. Runtime enablement comes from the engine config.
- **Per-asset files are research artifacts on these engine paths.** The pilot merges selected fields from a sleeve’s configured `config_file` and inline `symbol_params`. M4 uses its separate `config_m4_v3b.json` pair list with dedicated signal builders. Neither consumes the per-asset `optimal_parameters` automatically. The three packaged OI `signal_at` functions always return zero, so only the optimizer’s local OI implementations generated their replay signals. The M4 S07 tail omission from R046 remains in the signal generator.

Source paths. [Recipe exporter](../../scripts/restructure_strategies.py#L160) · [Charts discovery](../../Charts/core/strategy_base.py#L74) · [Pilot hydration](../../engine/unified_alpha_pilot.py#L85) · [OI stub](../../strategies/oi_4h_sqz_mom/strategy.py#L10) · [OF Initiative signal plugin](../../strategies/of_initiative_breakout/strategy.py#L43) · [M4 pair config](../../engine/config_m4_v3b.json).

| OF Initiative H4 | JSON R | Fresh D1 R / PF / Calmar | D1 disabled R / PF / Calmar |
| --- | ---: | ---: | ---: |
| BTC | +42.54 | +29.52 / 1.97 / 1.94 | +34.19 / 1.66 / 1.59 |
| ETH | +31.20 | −8.39 / 0.80 / −0.20 | +21.26 / 1.35 / 0.51 |
| SOL | +18.45 | −1.02 / 0.97 / −0.06 | +7.42 / 1.13 / 0.21 |

Keep the BTC D1 recipe as a shadow control. Withhold the ETH/SOL D1 recipes from admission. Their no-D1 results are ablations on the same validation tape. The fresh mask uses the most recent completed UTC D1 close, EMA50 versus EMA200 and a D1 age under 24 hours. It is applied to raw H4 signals before trade admission, so the changed exits and eligibility can propagate to later entries.

## 2. Exit-parameter sensitivity and proposed diffs

The replay locks each signal recipe and regenerates trades on cached minute OHLC. Each trial changes one exit control at a time. It tests BE at 1.0R and 1.5R against the shipped control, trail ±0.5 ATR with a zero floor, and initial SL ±0.5 ATR. TP, hold, step lock, dynamic ATR and modeled spread/swap remain fixed. Wider SL also widens any target defined in R and can change minimum-lot feasibility. The analysis uses purged pre-2024 descriptive metrics and the 2024–26 validation period ending 26 November UTC. It excludes window-straddling labels and reports the three calendar slices, paired 8- and 32-day moving-block intervals on realized net R, and an additional 10 bp round-trip cost sensitivity. The bootstrap intervals are descriptive and unadjusted for the 45-pair search. Serially dependent observations call for a block treatment. [Primary reference on dependent-data bootstrap](https://doi.org/10.1080/01621459.1994.10476870).

**Cross-asset evidence.** BE 1.0 beat 1.5 on 22 pairs, lost on 21 and tied on two. A 0.5 ATR tighter trail improved net R on 19, worsened 22 and tied four. A wider trail improved 18 and worsened 27. A tighter stop improved 24 and worsened 21. A wider stop improved 12 and worsened 33. The median validation sample was 52 trades per pair, with 12 pairs at 35 or fewer. Across 10 asset medians, the static spread-to-stop ratio had descriptive Spearman correlations of −0.04 with later BE’s expectancy change, +0.72 with tighter trail’s change, and +0.56 with tighter stop’s change. The spread is a broker snapshot and the same symbols recur across strategies. The trail and stop relationships are testable microstructure hypotheses. The evidence supports keeping common controls unless a predeclared change replicates with contemporaneous bid/ask and venue lot data.

| Shadow-only diff | Validation control → trial | +10 bp trial R | Main falsifier |
| --- | --- | ---: | --- |
| `ema233_imb_rejection/ETH.json` trail 3.5 → 3.0 ATR | +4.36R → +7.52R, PF 1.28 → 1.53, Calmar 0.29 → 0.60 | +6.33 | Only 35 fills. Both block lengths support a positive raw-R difference, while 2026 is still negative. Expect premature exits if ETH volatility expands. |
| `rsi2_mean_reversion/VET.json` SL 2.5 → 2.0 ATR | +3.75R → +15.59R, Calmar 0.09 → 0.38, DD 5.87% → 6.11% | +12.06 | Both block intervals include zero. VET can cross the shelf-clearance or spread veto. Its earlier BE test changed R by only +0.03R, and stressed R slipped to +1.03R. |
| `oi_2h_liq_break/BCH.json` SL 3.0 → 3.5 ATR | +3.64R → +7.46R, Calmar 0.14 → 0.32 | +6.67 | Larger stop may fail the fixed dollar cap. Pre-2024 R fell. |
| `oi_2h_liq_break/BTC.json` SL 2.5 → 3.0 ATR | +9.85R → +18.57R, Calmar 0.43 → 1.32 | +16.84 | Pre-2024 R fell from +20.13 to +13.69. The wider stop makes BTC minimum-lot rejections more likely. |
| `oi_2h_liq_break/ETH.json` SL 2.5 → 3.0 ATR | +1.50R → +5.57R, Calmar 0.05 → 0.22 | +4.26 | The paired intervals include zero for all three OI names. OI publication latency and native execution repricing can erase the apparent family effect. |

Use the three OI changes as a single predeclared +0.5 ATR family hypothesis. On the inspected validation sample, all three improved. All three weakened pre-2024, which argues against promotion. Use a paired forward comparison at equal dollar risk, retain the $6 and shelf-clearance vetoes, and publish the filled and rejected denominators. For BE 1.0 versus 1.5, the nearly even split and the VET null result support retaining the pinned values pending fresh data. Keep the other exit configurations as their locked replay controls. [Seven testable JSON patches, including two D1 ablations](r047_asset_trials.patch.json).

## 3. Farm-wide admission

The prototype for `engine/config.json` preserves 1% aggregate open-plus-pending heat, limits total reserved/pending/open slots to two, limits crypto to one and sets `max_crypto_same_side=1` and `max_positions_per_symbol=1`. A single major slot groups BTC, ETH and SOL across sides and venues. The directional and major limits also guard against misrouted requests. This fits canonical P5-02 and keeps a second slot for a separately qualified non-crypto trade. [Policy diff](r047_farm_policy.patch.json) · [Canonical limits](../CANONICAL_MASTER_SPECIFICATION.md#L119).

The current unified pilot’s same-account governor counts only positions with its own magic IDs for slot limits. Pending MT5 orders contribute to its stop-risk sum but are absent from its slot count. Its prefix classifier misses names such as VET and YFI. The committed config has four total positions, while the M4 default uses six total, two per asset, four same-side crypto and 6% open heat. M4’s own config and the Delta path need the same shared admission gateway. Adding JSON keys alone cannot enforce reservations. [Current pilot gates](../../engine/unified_alpha_pilot.py#L425) · [M4 gates](../../engine/m4_live_engine.py#L631).

Use a single serialized reservation ledger keyed by an immutable intent ID. Under one transaction, acquire current venue/account state, canonicalize each symbol and side, reconcile reservations with acknowledgments, count open/pending/reserved positions across the linked accounts, test hard caps, and reserve the requested risk and slot before dispatch. Map aliases such as `BTCUSD.pi` and `BTCUSD` to `BTCUSDT` using the venue inventory. Unknown symbols, stale state, a failed interlock or missing stop/contract metadata block new exposure. Keep uncertain submissions reserved until venue readback resolves them. The tests cover opposing-side majors, cross-venue aliases, same-side stacking, pending and reserved occupancy, a non-crypto second slot, and two simultaneous workers contending for the crypto slot in a locked SQLite transaction. This is an offline admission prototype. Production compare-and-reserve, the written interlock and authenticated venue reconciliation require separate operator review.

The validation replay uses the 45 modular pairs, a shared $5K balance, a 0.45% base risk until caps, the saved vOF modulation, the R046 4%/6% drawdown governor, and a common calendar. Unique entries are sorted by time and immutable priority. The IS-priority comparator uses pre-2024 net R per trade, shrunk by `n/(n+30)` with an exit-availability purge. Lexical tie priority avoids outcome ranking. The replays contain no pending historical event tape, so they model filled slot occupancy and test pending/reservation handling with fixtures separately. The 20K breach estimators use closed Binance H4 marks as a broker-equity proxy.

| Policy on the same validation tape | CAGR | Realized Calmar | Marked DD | Modeled marked 10% trailing breach | Peak simultaneous same-side majors |
| --- | ---: | ---: | ---: | ---: | ---: |
| Research-only relaxed slots with 1% heat | 2.83% | 0.334 | 8.85% | 11.68% | 3 |
| Research-only two crypto, one same side and one major | 6.44% | 0.900 | 7.20% | 0.44% | 1 |
| Desk one crypto, lexical ties | 4.08% | 0.874 | 5.42% | 0.70% | 1 |
| Desk lexical +10 bp | 3.37% | 0.710 | 5.64% | 1.05% | 1 |
| Desk lexical + $6/MT5 lot proxy | 0.10% | 0.039 | 2.57% | 0.00% | 1 |
| Desk lexical + $6/MT5 lot proxy +10 bp | −0.03% | −0.012 | 2.72% | 0.00% | 1 |
| Desk one crypto, frozen IS priority | 3.46% | 0.702 | 5.61% | 1.13% | 1 |
| Desk IS priority +10 bp | 0.84% | 0.161 | 5.77% | 1.12% | 1 |
| Desk IS priority + $6/MT5 lot proxy | −0.18% | −0.052 | 3.43% | 0.00% | 1 |

Use the desk’s one-crypto governor with deterministic ties as the forward control. The relaxed two-crypto row violates P5-02. At the $6 cap, lexical priority saw 197 minimum-lot rejections and reached the modeled +10% target in zero of 20,000 paths. The stress run became unprofitable. The farm policy solved stacking in the model and failed to preserve an economically useful Calmar at executable proxy size. Performance acceptance requires a matched broker-tick or shadow fill tape and paired comparison against the same one-crypto baseline. R047’s model Calmar uses the enlarged modular universe. R046’s equal-capital comparison remains the benchmark for its original sleeve set.

## 4. Simulator correction and verification

**Implemented in the offline shared simulator.** Candidate key is `(ts_in, entry_priority, sleeve, symbol, mt5_symbol, tf, bar_i, side, entry_id)`, ascending. Default priority is zero, and the remaining fields are immutable ledger identity. Distinct indistinguishable candidates require producer-assigned IDs. No realized return, exit timestamp or duration can select a winning tie. The existing saved prop and MT5 shared ledgers have zero duplicate identities under this key.

Legacy minute exit labels become usable at `ts_out + 60s`. A native MT5 OHLC exit uses its H1/H2/H4 bar close. Tapes with a confirmed event timestamp can provide `exit_available_ts` directly. The simulator reserves capacity for an open position at an exclusive training boundary and excludes unresolved P&L from that training score. In a replay with an unsettled trade and a blocked later candidate, the unsettled position still occupies the slot. Equity close time and CAGR span use the last observable close.

This change addresses the shared-account admission and its own training P&L cutoff. Stage A’s scoring cut and walk-forward fitting still need the R046 remediation. The old R046 account snapshots should be reproduced on `61350ea6e`, since the shared simulator now uses different admission timing. [Implemented code](../../exchange_feed/backtesting/mt5_shared_account_portfolio_optimizer.py#L104) · [Simulator regressions](../../exchange_feed/tests/test_mt5_shared_account_causality.py) · [Farm and patch checks](../../exchange_feed/tests/test_r047_farm_policy.py).

**Validation completed.** The footprint suite passed 9 tests. The combined targeted checks passed 13 tests, including future-exit mutation, shuffled ties, minute/native close availability, unresolved-label slot retention, and the proposed JSON patch checks. Focused Charts discovery and the M4 self-test passed after `09efc67f0` restored discovery. The 82,333 prop and 20,068 Stage 2 ledger records passed key-uniqueness and exit-availability checks and ran through the corrected simulator. No saved production recipes or engine config were changed by this review.

**Signed — Arena.ai / Codex, quantitative review.**
