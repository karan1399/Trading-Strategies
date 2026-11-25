# ROUND R25 · New-Trade Formulation From Fresh `origin/main` Telemetry

**Date:** 2026-11-25 · **Agent:** Arena.ai Agent Mode (formulate-and-report only; **no order submitted**) · **Status:** COMPLETE
**Authority:** `desk/LLM_TRADE_FORMULATION_PLAYBOOK.md` (SOP + §6 verdict shape) · `desk/CANONICAL_MASTER_SPECIFICATION.md` (gates) · `desk/log.md` R20–R24 (book) · **Account:** Blueberry MT5 #5126976, equity $2,587.15, risk cap 1.00% ($25.87)

---

## 1. Verdict

| Scope | Verdict |
|---|---|
| Actionable new trades | **NONE — 0 of 153 evaluable assets clear the full canonical stack** |
| Best candidate | **TRX short, confluence 6** — vetoed by §5 (R at T2 = 0.03R), GATE-02, GATE-04, GATE-07, and K-4/P5-02 |
| Second | **ATOM long, confluence 5** — vetoed by GATE-02, GATE-04, PIT-22, the §5 structural-stop limit, and K-4/P5-02 |
| Watch tier | 36 assets score 1–4 (below the grade threshold) — recorded in the CSV, not formulated |
| Coverage gap | 99 of 252 assets carry no `mt5` quote block; 1 (COPPER) has a quote/telemetry price-scale mismatch |
| Orders submitted | **0** — capacity is full (2/2 positions, 1/1 crypto) and no candidate passes the gates |

**No order was submitted, modified, or cancelled.** Every line below is a report to the desk operator.

---

## 2. What was used

Fresh JSON pulled from `origin/main` (commit `19ccdaa`), read directly — the canonical terminal tools (`scripts/trading/dump_for_llm.py`, `batch_formulate_playbook.py`) require `localhost:3000` and `node_modules`, neither of which is available offline, so the evaluator was written against the persisted telemetry directly.

| Source | Contents |
|---|---|
| `data/telemetry/assets/*.json` | 252 assets: MT5 quote, `market_structure` (auction, EMAs, VWAP), `narrative_engine`, footprint tail, liq levels, book walls, CVD, signals |
| `data/telemetry/footprint/<SYM>/summary_7d.json` | full 300-bar (75h) 15m OHLC series — used for ATR and Setup-A strike detection |
| `desk/audit_data/live_book_r24.json` | R24 book reconstruction (equity, risk cap, ticket geometry, ILK-01 halt) |

**Evaluator:** `scripts/ops/formulate_trades.py` → `desk/audit_data/trade_formulations.json` (playbook §6 shape) and `trade_formulations.csv` (triage). Both regenerate byte-identically apart from the `asOf` timestamp.

### 2.1 Coverage

| Bucket | Count |
|---|---|
| Total universe | 252 |
| Evaluable (quote present, scales consistent) | **153** |
| No `mt5` block at all (coverage gap — only the live quote is missing; all 99 do have a narrative regime) | 99 |
| Quote/telemetry price-scale mismatch (COPPER) | 1 |

---

## 3. Defects found and fixed in this round

Six defects in the evaluation path changed the result materially. Each is now reproducible.

### 3.1 T2 fallback pointed the wrong way for shorts (the most material)

For a short, the mechanical T2 fallback was `entry + (stop − entry) × 2` — **above** entry. That produced a fake "2.00R" on SOL (T2 72.73 against entry 68.28, stop 70.51) and a fake "2R" on every short whose targets were computed that way. Targets must sit in the trade direction: a short's targets sit **below** entry, a long's **above**.

With the direction fixed, TRX (confluence 6) drops to **0.03R** and fails the §5 "minimum R: 2 reward-to-risk at T2" rule outright. SOL's 2.00R survives only because it has no day-auction level below entry and T2 falls back to the mechanical 2R — that fallback is now **flagged in the evidence** (`t2_is_fallback`) rather than presented as a measured target.

### 3.2 §5 minimum R was never enforced

The playbook's §5 "Minimum R: 2 reward-to-risk at T2; skip otherwise" was not implemented at all — `rr` was computed and reported but never gated. It is now a hard veto evaluated before the canonical gates. **28 of the 38 assets with a setup fail this rule**; only TRX (confluence 6) does so at score >= 5.

### 3.3 §5 structural-stop limit was conflated with the PIT-16 cold-zone stop

§5 says the structural stop must fit inside 1.0% or *"the setup is invalid"*. The code had one `over_stop_limit` flag covering both the structural stop and the wider PIT-16 cold-zone stop, and reported the wider case as a watch item.

The two are now separated:

* `struct_stop_pct` — structural stop (structure + 5 ticks) beyond 1.0% → **hard no-trade** (§5, setup invalid). 12 assets, including ATOM (1.04%) and DOGE (1.14%).
* `over_stop_limit` — the PIT-16 cold-zone stop sits wider than the structural stop → **watch**, requiring explicit operator acceptance under PIT-21.

### 3.4 GATE-01 ignored its own ATR alternative

The canonical text is *"SL MUST be separated from Session/Day POC and Day VWAP by >= 0.5% cushion **(or 1.0x ATR)**"*. The code reported the ATR alternative in a note but passed GATE-01 **solely** on the 0.5% percentage cushion. Each reference level now independently satisfies (percentage cushion **OR** 1.0× ATR), and the artifact records which leg cleared (`GATE01_leg_poc` / `GATE01_leg_vwap`).

### 3.5 Setup-A close-back marker was never wired

`primary.closed_back` was read but never populated, so every qualifying strike reported "quality marker MISSING" even when the strike bar had closed back on the trend side. ATOM now correctly reports *"§3 quality marker: strike bar CLOSED back on the trend side of the zone"*.

### 3.6 Sixteen assets carried timestamps in seconds, not milliseconds

`summary_7d.json` for AMZN, BLESS, COPPER, CRCL, FIL, INTC, KSM, MU, PENGU, QTUM, SKHY, SOL, SOXL, SPCX, TSM and XLM store `ts` in **seconds** while the other 236 store milliseconds. The 300-bar SOL series spanned 0.1 hours instead of 74.8, which silently corrupted ATR, strike age and the Setup-A window. All persisted epochs are now normalised to milliseconds at load.

---

## 4. Data-integrity and instrument-quality findings

### 4.1 COPPER — 2,178× price-scale mismatch

`COPPER.p` quotes at **11,818.55** while its persisted footprint/auction telemetry sits at **~5.40**. Every level, stop and target derived from it was nonsense (this is the source of the negative stop and the 1,311% spread previously observed). A guard now rejects any asset whose `mt5.mid` and `market_structure` disagree by more than 2×. COPPER is the **only** asset in the universe with this defect; it is recorded as a coverage gap, not a trade.

### 4.2 The static contract spread is stale — live bid/ask is used instead

`pb.SPECS[...]['spread']` disagrees with the live quote for **14 assets**. COPPER's static spread is 9.05× its live bid/ask; ALUMINIUM 10.5×, LEAD 10.8×, NICKEL 10.8×, ZINC 9.4×, AAVE 13.4×. GATE-04 and PIT-22 now size spread from the **live bid/ask** in the asset JSON (present for all 153 evaluable assets), falling back to the static spec only if a quote is missing.

### 4.3 BNB's `$1.22` is locked profit, not risk

The R24 book recorded `desk_risk_usd: 1.22` for BNB `#20432336`. The stop is ratcheted to 554.50 **below** the 560.90 short entry, so that $1.22 is locked *profit* and the remaining downside risk from mid 542.00 is **$0**. The evaluator had been adding it to portfolio heat as if it were risk. The book JSON now carries an explicit `remaining_risk_usd: 0.0`.

Reconciliation after the fix matches the desk's own live-book line (`desk/log.md` R24: *"$5.36 open + $16.88 pending = $22.24"*) **exactly**:

| | Nominal | Stressed 1.2× |
|---|---|---|
| Open | $5.36 (EURUSD; BNB $0) | $6.43 |
| Pending | $16.88 (GBPNZD 6.00 + ETH 5.40 + NOKJPY 5.48) | $20.26 |
| **Total** | **$22.24 (0.860%)** | **$26.69 (1.032%)** |

Note the stressed figure **exceeds the $25.87 cap by $0.82**. R19 §5 already recorded that the desk's book-risk line uses nominal rather than the mandated 1.20× stressed risk; on the mandated basis the R24 book has **no heat headroom at all**, before any new order is considered.

### 4.4 Liquidation-level telemetry is too thin to compute the Setup-A dominance credit

Persisted footprint bars carry `buy_vol = sell_vol = 0` across the board, so `dominance_ratio` (§3's auction-quality leg) **cannot be computed for any asset**. The §4 "+3 Setup-A credit" is therefore never awarded, and every Setup-A record states that the leg is UNVERIFIED rather than silently scoring it as zero. A Setup-A setup without verified dominance is structurally incomplete.

---

## 5. The shortlist, in full

### 5.1 TRX — short, confluence 6 (the strongest candidate, and it fails)

| Field | Value |
|---|---|
| Setup | A (liq-level strike, 15m) |
| Entry | 0.3082 |
| Stop | 0.31991916 (3.80%, cold-zone) |
| T1 / T2 | 0.29883 / 0.307822 |
| **R at T2** | **0.03R — §5 minimum is 2.0R → skip** |
| Gates | GATE-01 PASS · GATE-02 **FAIL** · GATE-03 PASS · GATE-04 **FAIL** · GATE-07 **FAIL** · PIT-16 PASS · PIT-22 PASS |
| Portfolio | **FAIL** — 2/2 positions, 1/1 crypto, stressed heat $26.69 > $25.87 |

GATE-02: four liq hazard pools sit inside the risk path (0.31397 str 74.2 $4.22M; 0.316 str 100 $5.69M; 0.31819 str 63.6 $3.62M; 0.31944 str 64.2 $3.65M). GATE-04: live spread 0.0013 = 0.42% of entry against a 0.15% limit. GATE-07: minimum quantity 1.0 lot exceeds the lot cap of 0.0427.

### 5.2 ATOM — long, confluence 5

| Field | Value |
|---|---|
| Setup | A (liq-level strike, 15m, age 3 bars, closed back above the zone) |
| Entry | 1.252 |
| Stop | 1.206188 (3.66%, cold-zone); **structural stop 1.04% > the §5 1.0% limit** |
| T1 / T2 | 1.259 / 1.343624 (**T2 is the mechanical 2R fallback** — no day-auction level above entry) |
| Size | 0.1 lots, nominal $4.58, stressed $5.50 (cap $6.00) |
| Gates | GATE-01 PASS · GATE-02 **FAIL** · GATE-03 PASS · GATE-04 **FAIL** · GATE-07 PASS · PIT-16 PASS · PIT-22 **FAIL** |
| Portfolio | **FAIL** — 2/2 positions, 1/1 crypto, stressed heat $32.18 > $25.87 |

GATE-02: four hazard pools inside the risk path (1.208 str 64.2; 1.211 str 65.5; 1.218 str 100 $511k; 1.242 str 74.4). GATE-04: live spread 0.022 = 1.76% of entry against a 0.15% limit, and 0.48× the stop distance against a 0.20× limit. PIT-22: the stop distance (0.0458) is below 5× the spread (0.11) and below the 1.5% crypto minimum.

**This trade fails on five independent grounds** — GATE-02, GATE-04, PIT-22, the §5 structural-stop limit, and K-4/P5-02 capacity. It is not a marginal candidate that needs a re-check; it is out.

### 5.3 The watch tier (36 assets, score 1–4)

The two highest-scoring watch-tier names:

* **ALGO** short, 4 — GATE-03 BLOCKED (no 7d totals, so no LSR magnet ratio), GATE-04 FAIL (2.90% spread), PIT-16 BLOCKED, PIT-22 FAIL.
* **DOGE** short, 4 — GATE-02 FAIL, GATE-03 BLOCKED, GATE-04 FAIL, GATE-07 FAIL, PIT-22 FAIL.

Note that **every** watch-tier asset also fails at least one canonical gate. The score threshold is not the binding constraint on this universe — the gate stack is. Across the 38 assets with a setup, gate failures run GATE-04 ×35, PIT-22 ×25, GATE-02 ×20, GATE-07 ×8, GATE-01 ×3. Exactly **one** asset in the entire universe is gate-clean (no FAIL and no BLOCKED): **CL**, which scores 1 and independently fails §5 (R at T2 = 0.07R) and K-4/P5-02.

---

## 6. Why nothing can be traded even if the gates cleared

K-4 / P5-02 capacity is full on every axis:

| Constraint | Current | A new fill |
|---|---|---|
| Owned positions | 2 | 3 — **cap 2** |
| Owned crypto positions | 1 (BNB) | 2 — **cap 1** |
| Stressed heat | $26.69 (1.032%) | $32–38 — **cap $25.87 (1.00%)** |

The heat ceiling is breached **even before a new order**, because the desk's own risk line reports nominal rather than stressed risk. The remaining headroom is negative. Independently, three pending orders (ETH, NOKJPY, GBPNZD) already count against the pending side of P5-02.

---

## 7. Caveats and limits of this round

1. **No venue access.** Every ticket fact is a desk readback from `desk/log.md` R22/R24, not a live MT5 query. There is no independent MT5 connection in this environment, and the Delta route is blocked by ILK-01 (HTTP 401 `ip_not_whitelisted_for_api_key`).
2. **The trend gate is a proxy.** The playbook's §2 hard filter is the **5m** EMA-233. Only **15m** EMAs are persisted, so every regime call is an m15 proxy and is labelled as such in the evidence.
3. **GATE-03 is BLOCKED, not PASS, wherever 7d long/short liquidation totals are missing** — an unresolved magnet ratio is a coverage gap, never a pass.
4. **Setup A requires a key-level strike within three bars.** A 4–24 bar strike is reported as context only. Of the 38 assets with a setup, 11 had a strike within three bars, 5 had a 4–24 bar contextual strike, and 22 had no adverse-level strike at all in 24 bars.
5. **The +3 Setup-A dominance credit is unawarded everywhere** (see §4.4). No asset's score includes it.
6. **99 of 252 assets are unevaluable** for want of a live quote, and 1 more for the COPPER scale defect. The universe actually screened is 153 assets — roughly 61%.
7. **Footprint signals are sparse.** 4,294 persisted signals across 35 assets (`div` 1,364, `abs` 1,320, `exh` 764, `sdiv` 442, `trap` 404). ETH's `signals_7d.json` is empty. 41 assets carry a narrative plan but only 35 carry signals — an empty plan is not a setup.

---

## 8. Artifacts

| Path | Contents |
|---|---|
| `scripts/ops/formulate_trades.py` | the evaluator (deterministic; regenerates both artifacts byte-identically apart from `asOf`) |
| `desk/audit_data/trade_formulations.json` | 153 records in the playbook §6 shape, plus `canonical_gates`, `risk_detail` and `portfolio` blocks |
| `desk/audit_data/trade_formulations.csv` | 28-column triage table: all 153 records with entry/stop/targets/R/size, every gate, and the reason |
| `desk/audit_data/live_book_r24.json` | R24 book with the corrected BNB `remaining_risk_usd` |

**Validation performed:** every JSON record carries all 16 §6 fields; every record cites at least one field path in its evidence; the CSV and JSON symbol sets agree with no action/score mismatches; the run is reproducible.
