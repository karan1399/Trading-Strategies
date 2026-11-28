# Sovereign Core engine review and historical replay

**Arena.ai / Codex · 28 November 2026 IST · Working tree on 662f4769a**

**Verdict.** The archived market replay shows positive historical P&L under several cost assumptions. Sovereign Core's live execution controls failed direct offline probes. Retain the strategy as a shadow candidate while the interlock, exposure accounting and position reconciliation are repaired and tested.

## Historical market performance

**VERIFIED offline.** The runner calls `SovereignCoreEngine.step_once` and the inherited position manager with an in-memory broker, state store and injected historical signal payloads. The real engine handles priority, sizing, drawdown throttles, portfolio checks, spread vetoes, stop updates, time exits and Friday flattening. Payloads come from the same M4 strategy builders applied to archived Binance footprint bars for BTC/ETH and native MT5 H4 bars for USDJPY, GAUUSD and US2000. The mock never initializes MT5 or contacts Binance. [Replay runner](sovereign_engine_step_replay.py) · [Replays and individual trades](sovereign_engine_step_results.json).

The evaluation starts 1 May 2026 with $2,501.80 and ends 20 November 2026 23:45 UTC. All 18 configured routes can submit to the mock. Native CFD OHLC is hourly for most of the window, refined to 15-, 5- and 1-minute bars near the end. Entry uses the recorded bid open plus the last observed closed-bar spread for long fills. Where both exit levels appear inside one bar, the simulator gives the stop priority. Price gaps through stops use the adverse bid or ask at the next opening. Reported returns include the modeled spread, and marked drawdown uses sampled equity.

| Historical replay | Closed trades | Ending marked equity | Net return | Profit factor | Marked max DD |
| --- | ---: | ---: | ---: | ---: | ---: |
| As configured | 98 | $2,628.03 | +5.05% | 1.67 | 1.37% |
| Add 10 bp per round trip | 98 | $2,592.70 | +3.63% | 1.41 | 1.79% |
| Add 20 bp per round trip | 98 | $2,555.83 | +2.16% | 1.19 | 2.26% |
| Add 10 bp and veto lots above $6 stressed | 63 | $2,578.67 | +3.07% | 1.66 | 1.26% |
| Finer-bar window, 2 October through 20 November | 25 | $2,541.44 | +1.58% | 1.54 | 0.75% |

The configured run ended with a $2,614.59 realized balance and one open position. ETH generated $60.92 of realized profit and GAUUSD generated $38.39. BTC closed one trade for $0.31. Nine routes recorded at least one closed trade. The $6 sensitivity rejects the already-sized order if 1.20× gross stop risk plus 10 bp notional reserve exceeds $6. It does not refit the portfolio. The May–November 10,000-resample, eight-day calendar-block distribution of realized P&L ran from **−$0.19 to +$244.88** at the 2.5th and 97.5th percentiles. Under +20 bp it widened on the downside to **−$73.58 to +$173.40**. This resamples viewed dates and describes uncertainty within the archive.

The Friday-rule diagnostic with the schedule disabled returned +8.36% on 74 closed trades. The configured run instead recorded 31 Friday exits. Twenty-two of those exits used the next hourly opening, 30 minutes after the cutoff. The counterfactual changes holding periods, admission capacity and gap exposure. The available candles do not establish that disabling the rule improves out-of-sample risk.

Signal builders matched **179 of 180** sampled closed prefixes, including native TradFi's rolling lookback in those probes. The one mismatch was ETH VWAP on a 22-bar January 2021 warm-up prefix, outside the measured window. The built-in Sovereign self-test also passed with local cached data. The config has 11 crypto, 3 forex, 3 gold and 1 index route. No enabled energy route supplies the fifth advertised cluster. [Config](../../engine/config_sovereign_2500.json) · [MT5 bar timestamps and spread fields](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py).

## Live logic findings

| Severity | Finding | Correction and regression |
| --- | --- | --- |
| P1 | Friday close and inherited trail/time exits run before the only `trade_allowed` branch. The Friday probe called the mocked close with the interlock red. Signal gathering also happens before flattening. A failed feed blocked the flatten even when the mock interlock was green. A failed Friday close gets zero retries after the Saturday boundary. | Assert and persist the interlock before every venue mutation. Separate protective management from signal gathering. Persist a missed weekend flatten and reconcile it at the next permitted market session. Test red interlock, failed feed and Friday restart. |
| P1 | The position and order adapter converts failed readbacks to an empty list and can report zero committed risk. Unprotected and unpriceable exposures are skipped. Portfolio checks use the sleeve's known magics for slots. The route sends a direct market bracket and reads back neither the confirmed protective terms nor the desk's shared reservations. | Require valid position and order snapshots and canonical reservations for admission. Reconcile actual filled size and bracket using account-currency risk. Require the approved order path and structural stop checks. Test missing and partial responses and overlapping accounts. |
| P1 | Every route's $6.25 gross lot cap overrides the fallback 0.25% equity ceiling. A synthetic $5 target admitted $6.20 gross, rising to $7.44 at the desk's 1.20× stress before fees. The entry bracket rounds to digits and measures risk before rounding. | Intersect per-route, grade, equity, heat and $6 stressed desk limits after rounding to venue tick size. Include account-currency loss, fees and adverse stop reserve. Veto an infeasible minimum lot. |
| P2 | Friday and normal close writers save cooldown keys as strings. The gate reads integer magic IDs. The same candidate passed with the persisted key and was correctly blocked with an integer key. | Normalize cooldown keys consistently and test restart plus immediate re-entry after a close. |
| P2 | The engine consumes a bar before freshness, interlock and submission checks. TradFi age is measured from the last tick's clock, while missing ticks fall back to the signal close clock. | Validate signal age against cycle time and quote age independently. Record separate seen, blocked and reconciled states. Retry only approved transient cases, within the valid window and without duplicate intent. |

[Decision loop and Friday flatten](../../engine/sovereign_core_engine.py#L130) · [Cooldown gate](../../engine/m4_live_engine.py#L804) · [Broker exposure and market order path](../../engine/unified_alpha_pilot.py#L354) · [Desk rules](../CANONICAL_MASTER_SPECIFICATION.md) · [MT5 account-currency profit calculation](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py) · [MT5 order preflight](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py).

**UNVERIFIED-BY-AGENT.** Live fill/order behavior, broker stop and freeze distances, swap and commission debits, margin restrictions, official historical loss tick size and values, weekend venue calendars and neutral footprint fallback performance. The alternate GAUUSD contract-size proxy of 10 instead of 100 returned +5.17% for the portfolio, so sizing and attribution still require broker confirmation. The May–November archive was viewed during prior research. No untouched forward evidence or live P&L was used in this review.

Run from the repo root with `python -X utf8 -m desk.quant_audits.sovereign_engine_step_replay`. The result includes input hashes, the replay's static lot and contract assumptions, sampled prefix checks, per-trade outcomes and the injected control probes. The engine and venue configuration stayed unchanged.

**Signed — Arena.ai / Codex, quantitative review.**
