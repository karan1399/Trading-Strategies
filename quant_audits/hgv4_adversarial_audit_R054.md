# R054 — Independent Adversarial Quant + Systems Reliability Audit of `HGv4 v4.0-LIVE-DUAL` (`4_HGv4_Core+TRSI+TF_CS16_R1.00`)

**Auditor:** GLM (tricouncil reviewer — propose & audit only; no execution, no force-push)
**Date:** 2026-11-28 · **Repo HEAD audited:** `4a69b9783` (parent of `8c8b40cb6` = current remote `main`; `git diff 4a69b9783 -- engine/ strategies/s1[789]_*/ exchange_feed/tests/test_holy_grail_engine.py` = empty → identical files)
**Scope:** `engine/holy_grail_engine.py` (1,735 L), `engine/config_holy_grail.json` (537 L), `strategies/s17_kl_triple_rsi/strategy.py`, `strategies/s18_kl_flush_reclaim/strategy.py`, `strategies/s19_cs_carry_momentum/strategy.py`, `exchange_feed/tests/test_holy_grail_engine.py` (373 L), plus dependency read of `m4_live_engine.py`, `sovereign_core_engine.py`, `unified_alpha_pilot.py`, `delta_direct.py`, `strategies/core/indicators.py`.

**Run verification (executed in this session):**
- `python engine/holy_grail_engine.py --selftest` → **`HOLY_GRAIL_ENGINE HGv4 SELFTEST PASS (7/7 invariants verified)`**
- `python engine/holy_grail_engine.py --status` → prints `Version=4.0-LIVE-DUAL | Locked=4_HGv4_Core+TRSI+TF_CS16_R1.00`, `Risk=1.0% | MaxPos=16 | PodCaps={'CORE': 8, 'KL_MR': 4, 'TRADFI': 4} | MT5Pairs=31`, OOS `Tr=2034 PF=1.38 CAGR=+55.9% MaxDD=26.6%`, `Pod 4 Mode=NOT_RUN Avail=$0.00/$250`, `InterlockRearm=False`.
- `python -m pytest strategies/tests/ exchange_feed/tests/test_holy_grail_engine.py -v` → **19 passed, 14 warnings, 4.70s** (7 fp_steroids + 3 reels/AGI + 12... exactly: 3 fp_steroids, 4 reels_and_agi, 12 holy_grail).

**VERDICT: CONDITIONAL PASS.** No P0 found. Two P1 (live-vs-backtest drift), five P2. Tests are green; safety rails are real; the drift items mean the live book will not reproduce the 16-fold OOS profile until fixed and re-validated.

---

## D1. Bar-Completion Causality & Clock Skew — CLEAN (one nuance)

| Check | Result | Evidence (all `VERIFIED` by code read + probes) |
|---|---|---|
| `fetch_binance_klines` forming-bar exclusion | ✅ CLEAN | `close_ms > cutoff_ms` skips forming bars (L156-158); unit test `test_fetch_binance_klines_excludes_forming_bar` passes: bar 2 at close 1005_000 with cutoff 1002_000 excluded, returns 1 bar. |
| `-3000ms` skew guard | ✅ CLEAN | `cur_ms = (cur_ts - 3) * 1000` at L213 (4H sync) and L711 (1D sync) and L1198 (pod4). A bar must be closed for 3+ seconds to be ingested — strictly conservative vs a forming bar at boundary. Note: the mandate text calls it `-3000ms`; the code applies −3s to the *cutoff*, equivalent intent, plus WS collector has its own `close_grace_seconds=3`. |
| MT5 clock normalization | ✅ CLEAN | `_mt5_server_offset_sec` (L749-763) probes tick time, rounds offset to nearest hour (EET +7200 / +10800 DST), `sync_mt5_bars` subtracts it from bar times and uses `start_pos=1` (skip forming bar), then filters `end_ts <= cur_ts` (L799). `test_mt5_server_timezone_offset_alignment` passes: last closed H4 bar end lands 5 s before now → zero 2 h lag, latest closed bar kept. |
| Signal-lag gates | ✅ CLEAN | `max_signal_lag_4h_sec=1800`, `vc=900`, `1d=43200` enforced in `step_once` L1539-1547; a signal older than its lag budget is skipped, not traded stale. |
| Double-punch across restarts | ✅ CLEAN | `bars_seen` keyed by `sym:sleeve` → `ts_sig` (L1535-1537, persisted via StateStore JSON with atomic `os.replace`); restart re-loads and skips already-consumed signal timestamps. Dry-mode also stamps `bars_seen` (L1572). |
| Daily-bar causality for S17 | ✅ CLEAN | `sync_binance_daily_closed` filters `end_ts <= cur_ts` twice (L728, L742); `eval_kl_triple_rsi_latest` evaluates only `iloc[-1]` of the closed frame; `s17_signal_at`/`exit_signal_at` compute RSI strictly on `c[:i+1]` prefixes (verified by `test_s17_triple_rsi_trigger_and_zero_lookahead`). |
| **Nuance (P2-7)** | ⚠️ | `sync_live_symbol_feeds` merge `rest4[rest4["ts"] >= old_max_ts - 86400]` then `drop_duplicates("ts", keep="first")` (L257-258): REST rows appended after base rows keep **base** values on overlap — including stale footprint `buy_imb`/`sell_imb` (see D2). Only bars strictly newer than `old_max_ts` are refreshed (L260-262). Correct for prices; embeds the imbalance parity issue. |
| `is_in_macro_blackout` | ✅ | `[ev-1800, ev+900]` inclusive bounds match config `pre_event_blackout_sec=1800 / post_event_blackout_sec=900`; FOMC `B` family exempt; selftest asserts A-blocked/B-allowed. |

**Conclusion D1:** No lookahead, no off-by-one, no double-punch found. The MT5 UTC+2/+3 normalization is genuinely clean (the test proves the latest closed H4 bar survives with 5 s latency, not 2 h).

## D2. Strategy Coverage & Live-vs-Backtest Parity — **P1 drifts here**

Coverage inventory (all present and gate-wired): `A_4H_L`, `A_4H_S`, `A_RECL` (Pod 1), `B_FOMC_15m_ALN` (BTC-only), `C1_VC_PURE_L`, `C3_VC_PURE_S` (Volume-Clock), `D_TRSI_KL` (Pod 2 S17), `TF_TREND_L` + `TF_TRSI_KL` (Pod 3, 15 TradFi symbols: 7 indices, 2 metals, 2 energy, 4 FX — 13 `trsi_eligible`, XAG/USWTI/UKBRENT/FX legs trend-only), `S19` (Pod 4, 27-coin universe, config lists 27). Total 31 MT5 pairs (14 crypto + CLUSDT/USWTI + XAUUSDT/GAUUSD + 15 TradFi).

**P1-1. `buy_imb=0` / `sell_imb=0` on live REST 4H bars kills pillars p3/s3.**
`sync_live_symbol_feeds` L232-233 hardcodes `rest4["buy_imb"] = 0; rest4["sell_imb"] = 0` because Binance REST klines carry no footprint imbalance. Probe A (runtime, synthetic identical frames, real vs zero imbalances): with real imbalances, p3 fires on 99/320 bars; with REST zeros, p3 fires on **0/320**. Since `sc_long >= 3` requires 3 of 5 pillars and `sc_short >= 4` requires 4 of 5, live shorts must fire p1+s2+p4+s5 **all four simultaneously** (p3 permanently 0) — live entry frequency is strictly lower than the backtest's. WS collector aggTrades could reconstruct imbalances, but the splice only feeds the 1m frame (L310-335), never the 4H frame. **This is a genuine live-vs-backtest distribution drift on Sleeves A.**
*Fix (proposal):* either (a) compute `buy_imb/sell_imb` from the WS collector's 1m buy/sell trade counts aggregated to 4H when available, else set both `NaN` and make `compute_4h_physics_features` treat NaN as "pillar abstain" (renormalize thresholds 3→2.5 for long / 4→3 for short), or (b) recalibrate the backtest with imbalances zeroed for the REST-only path. Do **not** ship (a) without re-running the 16-fold OOS with the same abstention logic.

**P1-2. Live `A_RECL` is the HGv3-style tight-stop variant, not `s18_kl_flush_reclaim`.**
Probe B (runtime): forcing `A_RECL` conditions yields a signal with `kl_nosl` ABSENT (→ False), `sl_dist = 2.5×ATR`, no `max_hold_days`. The engine **never imports s18** (`grep s18 holy_grail_engine.py` → only s17/s19 imported, L55-57). Meanwhile `strategies/s18_kl_flush_reclaim/strategy.py` documents itself as the fix for exactly this sleeve: "Fixes the `HGv3 A_RECL` tight-stop whipsaw (`PF=0.93`... replacing `SL=2.5 ATR + BE@1.5R` with VWAP/POC reversion exit + `T=18` bars + wide `6.0x ATR` catastrophe cap scaled at `0.40x` lifts OOS to PF=2.46." The live engine's A_RECL therefore trades the *documented-bad* variant (tight 2.5 ATR stop + T4_Convex trailing + BE@1.5R) rather than the *validated-good* s18 spec (kl_nosl, 6 ATR catastrophe cap, VWAP/POC exit, 0.40 risk scale, 18-bar hold). **Whatever OOS numbers were attributed to `A_RECL` in the 16-fold run, live will not reproduce them.**
*Fix (proposal):* route A_RECL through `s18_kl_flush_reclaim.signal_at` mirroring the S17 wiring: `kl_nosl=True`, `risk_scale=0.40`, `max_hold=18`, `sl_dist=cat_rd` (6 ATR), exit via `s18.exit_signal_at` (vwz48>=0 or c>=poc) in `_manage_hg_open_positions` (add to the `kl_nosl` branch's exit map alongside the RSI(5) map), and disable T4_Convex for it (already handled by the `kl_nosl` branch).

**P2-1. S19 `fund_bp` REST backfill stamps today's single funding rate on all fetched bars.**
`_step_pod4_cs_delta` L1199-1203 fetches one `premiumIndex` value and writes it into every REST 4H bar; `fund_72h = nanmean(fb[-18:])` (L1224) then equals *today's spot rate repeated 18 times*, not a trailing 72 h average (probe D). The carry factor degenerates to a constant across coins sampled at the same sync → cross-sectional rank of a constant, i.e. that factor contributes ~nothing freshly; worse, splicing with historical footprint bars (which do hold per-bar rates) mixes regimes. The `s19` backtest used real per-8h funding from the parquet footprint.
*Fix (proposal):* fetch the funding-rate *history* endpoint (`/fapi/v1/fundingRate`, up to 100 records of 8h rates) and `merge_asof` per-bar by `end_ts`, or default `fund_bp=NaN` and rank only on momentum+delta factors when unavailable. Fresh 4H cache parquets exist under `historical_data/binance_footprint/*_funding_8h.parquet` (33 coins) — use them for the trailing window instead of the spot value.

**P2-2. Single-tranche 72 h rebalance vs 18-tranche backtest.**
Config `rebalance_hours: 72` and `_step_pod4_cs_delta` rebalance the whole book when `(latest_bar_ts - last_rebal_ts) >= 72h` (L1243). The `_meta` claims an "18-Phase Tranche Audit" (`tranche18_*` metrics). A single-shot rebalance of the full target weight has different turnover, slippage and timing-luck profile than tranched execution (1/18th per interval); neither is validated against the other here. **Drift: turnover and rebalancing noise in live Pod 4 will not match the tranche-18 backtest.**
*Fix (proposal):* implement tranched moves (target 1/18 of full reweight per 4 h step, or at minimum spread across `rebalance_hours/18` sub-steps), or re-run the OOS with single-shot rebalancing to re-baseline.

**P2-3. `T4_Convex` on 30 s tick polls vs 4H-bar checks.**
`_manage_hg_open_positions` runs each 30 s `step_once` and trails on live tick (`cur_px`), using `cur_atr` from the last closed 4H bar. The backtest would have updated once per closed bar. Trailing on every tick with bar-level ATR ≈ a *tighter* effective trail (BE@1.5R locks sooner at tick granularity). Not wrong, but it is a behavioral difference vs bar-close backtests — direction favors risk reduction, magnitude unquantified.
*Fix (proposal):* restrict SL modification to once per closed 4H bar (track last-modify bar `end_ts` in position meta), or accept and document the tick-level tightening.

**P2-4 (nuance, informational).** `eval_fomc_15m_aligned` uses `i0 = searchsorted(ts_a, fomc_ts)` — if a 1m bar at exactly `fomc_ts` is absent (exchange gap), `i0` lands on the next available bar and the 15-minute window silently shifts. Minor; the release-minute bar exists on Binance with near-certainty. No action needed beyond awareness.

## D3. S17 Kaminsky-Lo & Asymmetric Exit Safety — CLEAN, one nuance

| Mandate item | Result | Evidence |
|---|---|---|
| Catastrophe SL still attached when `kl_nosl=True` | ✅ VERIFIED | `_punch_hg_signal` L1455-1459 computes `sl_px = entry - side*sl_dist` where `sl_dist = cand["sl_dist"] * scale` and for KL candidates `sl_dist` comes from `sig["cat_rd"]` (6×ATR, capped at 28% of price, L62 in s17) — then `send_bracket(..., sl_px, tp_px, ...)` **always** sends server-side SL in the same atomic order. `kl_nosl` only changes the *trailing* behavior, never the SL attachment. The 28%-of-price cap on `cat_rd` is *tighter* than 6×ATR for high-ATR assets — bounded tail, documented in s17 (LUNA/FTX rationale). |
| `<= 1.0% / $25.02` single-trade ceiling | ✅ VERIFIED | `target_risk = equity * risk_pct(1.0%) * dd_mult * risk_scale` and `max_single = equity * 1.0%`; `venue_risk_size` → `lots_for_risk` **rejects** (never compresses) when min-lot risk exceeds the cap (pilot L273-277); config `max_lot_risk_usd: 25.02` on every pair. Unit test `test_venue_sizing_non_compression...` asserts `lots_bad is None` at $18.75 target/$25 cap with a 3000-pt stop. |
| T4_Convex skipped for `kl_nosl` | ✅ VERIFIED | `_manage_hg_open_positions` L1376-1382: `if meta.get("kl_nosl"): if d_exits...close_position(...); continue` — the `continue` skips the entire trailing block below. Test `test_hgv4_pods...` asserts `closed_tickets == [8811] and mod_sls == []`. |
| Exit strictly on closed Daily RSI(5)>50 or 12d cap | ✅ VERIFIED | `d_exits` map comes from `eval_kl_triple_rsi_latest` → `s17_exit_at` on the last **closed** daily bar; `_manage` closes when flag True (L1377-1381); time cap `tf_sec = max_hold_sec = 12*86400` (L1363) fires `HG_TIME_EXIT` (L1369-1374). Bounce test flips RSI>50 → exit fires. |
| TP for KL positions | ⚠️ P2-5 | `tp_mult = max(6.0, safety_tp_r/2)` for `kl_nosl` → 6R TP (L1456-1458). Harmless (RSI exit will fire long before), but the TP is a hardcoded 6.0 magic number not derived from s17 params — informational. |

## D4. Venue Separation & Delta Safety — CLEAN with two flags

Pod 4 routes strictly to `delta_exchange_india` because MT5 crypto CFDs pay **two-way swap carry** on both sides (config note L121: "so MT5 CFDs are never harmed by negative two-way swaps"), while Delta perps pay/receive the funding rate itself — a market-neutral carry strategy is only coherent on the venue where funding is the P&L line, not a fixed swap debit. `min_delta_equity_usd=250` gates real execution; below it the pod runs `SHADOW_WAIT_FUNDING` with a shadow equity ledger persisted in `cs_pod4_state.json` (test asserts mode + $3.22 balance read). Rationale is sound.

`_rebalance_delta_pod4` verified by probe E (mock Delta API, runtime):
- **Contract math ✅:** `tgt_sz = round(w * eq * gross_scale / (cval * mark))` — BTC @ $50k, cval 0.001, w=0.30, eq $250 → 1.5 → round → 2 contracts ($100 notional). Rounding is integer contracts; `round(...)==0` → `diff==0` → **no order sent** (checked: zero-diff symbols are skipped, L1147).
- **Reduce-only ordering ✅:** `orders_to_send.sort(key=lambda x: x[0])` with reduce-only first (L1153) — verified in probe: shrinking order (3→2) went out as `reduce_only=True` ahead of the new short.
- **Rounding to zero ⚠️:** with $250 equity, small weights (|w|·250 / (cval·mark) < 0.5) round to 0 contracts — the leg silently drops out of the dollar-neutral book (neutral-ity drift), no error raised. Acceptable at $250 (it's the reason the pod is gated to shadow below $250... it is *at* $250 that this starts), worth logging.
- **Flip path ⚠️ P2-6:** long→short flip sends ONE non-reduce-only market order crossing zero (probe: cur=+3 → tgt=-2 sent as single `sell` size 5, `reduce_only=False`). Margin impact is full new gross; Delta accepts it if margin suffices, but this concentrates flip risk in one fill and skips the safer reduce-then-open sequence. Also `gross_scale` caps `target_ann_vol/0.16 * dd_mult` at ≤1.0 but there is **no explicit gross-notional/leverage cap** per order or per book on the venue (config has none; the strategy's weights sum |w|≤1.0 → ~1x gross at full weight, which is the implicit cap).
*Fix (proposal):* split flips into `reduce_only` close-to-zero then open remainder; log zero-rounded legs with `note=zero_rounded` in the journal for neutral-drift monitoring.

## D5. Portfolio Caps, Risk Limits, Friday Flatten — mostly CLEAN, one P1 and one P2

| Item | Result | Evidence |
|---|---|---|
| `pod_caps` non-blocking | ✅ | Gate counts `pod` membership only when the pod is at cap (L903); cross-pod trades unaffected. `CORE:8 / KL_MR:4 / TRADFI:4`, `max_global:16` (L905), `max_per_asset:2`, `max_per_asset_sleeve:1`, `max_same_mt5_symbol:2`, `max_same_side:6` all enforced L902-917. Selftest asserts non-blocking (C and D allowed alongside active A; second A on same asset rejected `MAX_PER_ASSET_SLEEVE`). |
| `max_same_mt5_symbol=2` for CLUSDT/USWTI.p and XAUUSDT/XAUUSD.pi | ✅ | Gate L911-915 keyed on `mt5_symbol` (not source symbol): CORE `CLUSDT→USWTI.p` (magic 46102) and TRADFI `USWTI.p` (magic 46210) share the MT5 symbol → capped at 2 concurrent; same for `XAUUSDT→GAUUSD.pi` vs `XAUUSD.pi`. Verified by code read; no unit test covers this specific pair (gap noted, probe not needed — logic is 4 lines). |
| `max_total_open_risk_pct=8.0` | ✅ | `committed_risk + target_risk > max_total` → `BOOK_RISK_CAP_REACHED` (L1445-1447); `committed_risk` from `total_sl_risk_usd` which sums SL distance × volume × tick-value/tick-size across **positions and pending orders** (pilot L361-385). |
| Friday 20:30 UTC forced flatten | ✅ | `step_once` L1514-1517: `is_friday_close_ts(cur_ts)` **or** `non_crypto_schedule_state in {FRIDAY_CLOSE, WEEKEND_BLACKOUT}` → `_flatten_non_crypto_for_friday(positions)` closes every non-crypto-category position (sovereign L152-181, skips `is_crypto_pair`), then re-reads positions. Covers COMMODITY (CLUSDT/XAUUSDT), INDEX, METALS, ENERGY, FOREX pods. Journal `FRIDAY_CLOSE` per ticket. Test asserts 20:35 UTC dry step journals `FRIDAY_COMMODITY_BLACKOUT` veto for new entries. |
| `trade_allowed` interlock | ⚠️ **P1-3** | When `terminal_info().trade_allowed` is False: records failure, sets `interlock_rearm_required=True`, saves state, returns early (L1498-1502). Good. **But no code path anywhere ever sets it back to False** (verified: `grep` finds only `= True` at L1500 and sovereign L127/250; StateStore persists it to `holy_grail_state.json`). A single AlgoTrading toggle-off blip (a reboot, a terminal restart, a weekend platform update) permanently halts new entries, position trailing and Pod 4 live mode (`not self.state.get("interlock_rearm_required")` gates L1251, L1521, L1556) **until a human hand-edits the state file**. Conservative direction (safe), but it converts a transient platform blip into a persistent silent halt — a reliability P1, not a safety P0. |
| Friday 19:00 entry cutoff | ⚠️ P2-7 | `session_policy.friday_entry_cutoff_utc: "19:00"` declares entries halt 19:00-20:30, but the gate only checks `is_friday_close_ts` (≥20:30). Probe C: 19:15 UTC Friday `US2000.p` entry → **(True, PASS)** while schedule state = `FRIDAY_CUTOFF`. A TRADFI position opened 19:15 Friday gets force-flattened 75 min later at 20:30 by design — losing spread+slippage with near-zero expected holding time. Config-vs-code policy drift. |

*Fix for P1-3 (proposal):* in `step_once`, after the live interlock check succeeds, add a re-arm branch: when `trade_allowed=True` **and** `interlock_rearm_required` is True, write a re-arm journal entry (`INTERLOCK_REARM`), clear the flag and save — optionally requiring N consecutive healthy polls or operator confirmation via a marker file if the desk wants manual re-arm. The conservative-auto option keeps the halt bounded to the outage duration while preserving the evidence trail in `desk/events/`.

*Fix for P2-7 (proposal):* in `holy_grail_portfolio_gate`, replace `is_friday_close_ts(now_ts)` with `hg.sc.non_crypto_schedule_state(now_ts, self.cfg) in {"FRIDAY_CUTOFF", "FRIDAY_CLOSE", "WEEKEND_BLACKOUT"}` for the non-crypto entry veto, matching the declared 19:00 cutoff (the schedule fn already exists and is used in `step_once` for flattening — one-line change, plus a selftest assertion at 19:15 Friday).

## D6. Run Verification — outputs above. Environment caveats (honest): MT5 package unavailable in this sandbox, so all MT5-touching tests run their mock paths (tests are designed for this: `_FakeMT5`, `monkeypatch`), and no live venue state exists here (`Pod 4 Mode=NOT_RUN`, balances $0). All pass/fail claims above come from the commands I ran in this session.

---

## Consolidated findings

| ID | Sev | Finding | Dimension | Fix sketch |
|---|---|---|---|---|
| P1-1 | P1 | Live REST bars zero `buy_imb/sell_imb` → pillars p3/s3 dead live; `sc_short` needs all 4 remaining pillars. Live entry distribution ≠ backtest. | D2 | WS-collector-derived 4H imbalances or pillar-abstain logic + OOS re-run |
| P1-2 | P1 | Live `A_RECL` = tight-stop HGv3 variant (PF=0.93 whipsaw per s18's own docstring); `s18_kl_flush_reclaim` never imported; live sleeve ≠ validated spec (PF=2.46, kl_nosl, 6ATR cap, VWAP/POC exit). | D2 | Wire A_RECL through s18 like S17 is wired |
| P1-3 | P1 | `interlock_rearm_required` set on one `trade_allowed=False` blip, never cleared by code → persistent silent halt of entries/trailing/Pod4-live until manual state edit. | D5 | Re-arm on healthy `trade_allowed` poll (+journal), or explicit operator marker |
| P2-1 | P2 | S19 `fund_bp` spot-stamped on all REST bars → `fund_72h` = today's rate, carry factor degenerate vs backtest's real 8h history. | D2 | `/fapi/v1/fundingRate` history merge or footprint parquet + NaN abstention |
| P2-2 | P2 | Single-tranche 72 h rebalance vs `_meta`'s 18-tranche audit — turnover/timing-luck drift. | D2 | Tranched execution or re-baseline OOS single-shot |
| P2-3 | P2 | T4_Convex trails every 30 s tick vs backtest's per-closed-bar updates → tighter effective trail, unquantified. | D2 | Bar-gated SL modification |
| P2-4 | P2 | FOMC `searchsorted` silent window shift if release-minute 1m bar missing. | D1 | Assert bar presence or fall back to next bar with journal note |
| P2-5 | P2 | KL TP hardcoded `max(6.0, 12/2)` magic number, not s17-derived. | D3 | Derive from params (informational) |
| P2-6 | P2 | Delta position flips sent as one non-reduce-only crossing order; no explicit gross/leverage cap beyond weights; zero-rounded legs drop silently from neutral book. | D4 | Split flips reduce→open; log zero-rounds |
| P2-7 | P2 | Friday 19:00-20:30 entry cutoff declared in `session_policy` but not enforced (gate blocks only ≥20:30); 19:15 Friday entries pass then flatten 75 min later. | D5 | Use `non_crypto_schedule_state` in the gate |

**Positive verification highlights** (things that are genuinely solid): zero-lookahead prefix-invariance tests for both 4H physics and the Volume-Clock bar builder; forming-bar exclusion incl. the −3s margin; MT5 server-clock normalization with zero H4 lag; atomic parquet/state writes (`os.replace`); never-compress sizing with outright rejection; always-attached server-side SL incl. for `kl_nosl`; DD hysteresis throttle; reduce-only-first Delta ordering; non-blocking pod caps; state-persisted signal dedup across restarts; `--live` requiring `--i-understand-live-risk`; strict-live corrupt-state hard-fail.

**Bottom line:** architecture and safety rails are sound; the 16-fold OOS numbers (PF 1.38, CAGR 55.9%) are **not** what live will deliver until P1-1/P1-2 (sleeve parity) and P1-3 (re-arm) are addressed and re-validated. Recommend: fix P1s → re-run the 16-fold OOS with live-parity data (zeroed imbalances, s18-wired A_RECL, funding history) → only then compare live journal vs re-baselined backtest.

**Confidence labels:** everything above marked from code-read + session-run probes is `VERIFIED`. Anything about live venue behavior (fills, swaps, Delta margin at flips, real `trade_allowed` blips) is `UNVERIFIED-BY-AGENT` from this sandbox.

— GLM · 2026-11-28 · R054 · propose & audit only
