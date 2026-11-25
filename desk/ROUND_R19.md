# ROUND R19 · Independent Audit of the Desk Book and the EMA89 Pullback Template

**Date:** 2026-11-25 · **Auditor:** Arena.ai Agent Mode (read-only; no orders submitted) · **Status:** analysis COMPLETE · signed-evidence attestation INCOMPLETE (no venue access — see §10.2)
**Authority:** `desk/CANONICAL_MASTER_SPECIFICATION.md` · **Book source:** `desk/log.md` R18 (2026-11-25 07:47 UTC) · **Account:** Blueberry MT5 #5126976, equity $2,587.67, risk cap 1.00% ($25.88)

---

## 1. Verdict

| Scope | Verdict |
|---|---|
| ETH pending `#20498955` | **CANCEL** — P5-02/K-4 capacity is already full (2/2 positions, and a fill makes 2 crypto); also fails GATE-02, GATE-03, GATE-04, GATE-07, PIT-16 |
| USWTI pending `#20496682` | **CANCEL** — P5-02/K-4 capacity is already full (2/2 positions); also fails GATE-02, GATE-03, GATE-07, PIT-16 |
| BNB open `#20432336` | **MODIFY (hold, no add)** — stop already ratcheted to 554.50 locking +$1.22; fails GATE-02 and GATE-04 |
| EURUSD open `#20467916` | **MODIFY (hold, no add)** — fails GATE-07; GATE-02/GATE-03/PIT-16 BLOCKED (no liquidation telemetry) |
| New entries | **NO-TRADE** — 0 of 28 evaluable candidates clear the full gate stack |
| S10 EMA89 pullback template | **MODIFY** — the pullback *timing* edge is negative in both train and test; the test-window gain is regime drift, not timing alpha |

**No order was submitted, modified, or cancelled by this audit.** Every action above is a recommendation to the desk operator.

---

## 2. Engine defects found and fixed

Three defects in the durable backtest engine changed the result materially. Each is now reproducible.

### 2.1 H1 ATR look-ahead (the largest defect)

`historical_parquet` bars are **start-labelled**: the 1H bar labelled `T` covers `[T, T+1h)`. Proven by re-aggregating the 15M feed into 1H and comparing against the raw 1H parquet — `max |dHigh| = max |dClose| = 0.000000` over 1,251 overlapping ETH bars (and `0.000000` for BNB, USWTI, EURUSD), while close-labelling is off by up to 91.9 (ETH high) / 101.1 (ETH close).

The last **completed** H1 bar knowable at a 15M bar labelled `T` is therefore the one labelled `floor(T) − 1h`. The previous engine used `join + ffill + shift(1)`, which steps back only one 15M bar and therefore fed the **in-progress** H1 bar into every `:15`, `:30` and `:45` row.

| Symbol | rows leaked | max relative ATR error | median error |
|---|---|---|---|
| ETHUSD.pi | 74.8% | **122.3%** | 1.8% |
| EURUSD.pi | 74.8% | 74.0% | 2.3% |
| BNBUSD.p | 70.5% | 80.6% | 1.9% |
| XAUUSD.pi | 74.8% | 46.8% | 1.8% |
| USWTI.p | 74.9% | 32.6% | 2.5% |

Fixed in `scripts/ops/pullback_backtest.py::prep()`; proof script: `scripts/ops/verify_timestamp_semantics.py`.

### 2.2 Overlapping positions counted as independent trades

The engine allowed a new position in the same symbol while the previous one was still open. It now enforces strict non-overlap (one position per symbol at a time), matching desk practice.

This is the fix that collapses the sample, and it does so on its own. Measured on the 97 eligible symbols, counting those with ≥25 test trades:

| Engine state | symbols with ≥25 test trades |
|---|---|
| original (leaked ATR, overlapping) | 57 |
| corrected ATR, overlapping | 77 |
| corrected ATR, **non-overlapping** | **2** |

The ATR correction slightly *raises* the trade count; the non-overlap correction removes 75 of 77 symbols from the ≥25-trade bucket (93 → 71 at the ≥10 threshold). The earlier "57 eligible symbols with ≥25 test trades" figure was an artifact of counting concurrent positions in one symbol as independent trades.

### 2.3 GATE-07 lot sizing

`lots = volume_min + floor(lots/step)*step` **added** the broker minimum on top of the capped size, over-sizing the position (and so failing the cap). Corrected to `floor(lots/step)*step` with an explicit veto when the minimum quantity cannot fit. Effect: GATE-07 pass rate among the 28 mapped candidates moved from 2 → **18**.

Also corrected in this pass: PIT-22 now applies the stricter 5× PIT-18 rule named in its own text (`stop ≥ 5 × spread`); crypto-CFD detection uses `mt5.category` instead of a hard-coded symbol list; GATE-02 is implemented on the desk's operative thresholds with a strict reading reported alongside; adverse-slippage sign and the touch-null degeneracy were fixed and a **date-conditioned null** was added.

---

## 3. GATE-02: the specification is self-contradictory here

`[GATE-02]` says *zero liquidation hazard pools inside the risk path (P_entry → P_SL)*. `[PIT-16]` says the stop must sit *strictly beyond the entire multi-exchange 3D/7D/30D shelf*. Any stop placed beyond the whole shelf necessarily contains that shelf inside `P_entry → P_SL`. **The two clauses cannot both be satisfied**, so the literal reading fails by construction.

The desk's own log resolves it operationally:

- `desk/log.md:6658` — *"1 minor cluster at 9.589 ($173.6k USD, strength 59.8 < 60 threshold). Vector B Hazard = False."* → **strength ≥ 60**
- `desk/log.md:6804` — *"Vector B Hazard = $1,596,072 USD (< $2.0M threshold)"* → **notional ≥ $2,000,000**
- `desk/log.md:6684, 6750` — *"Vector B Risk Zone … is 100% clear with 0 adverse clusters"* (a zero-pool case that passes)

So GATE-02 is evaluated **both** ways below: `GATE02` (operative: strength ≥ 60 or notional ≥ $2.0M) and `GATE02_STRICT` (spec literal: any pool ≥ 40% strength, per PIT-16's definition of high-density).

**Vector A / Vector B assignment** (from `desk/log.md:6621`, `6684`, `6895`, `7677`): for a SHORT, Vector B is *above* the entry and the hazard is the **short-liquidation** pool set (`keyLevels.above`); for a LONG, Vector B is *below* the entry and the hazard is the **long-liquidation** pool set (`keyLevels.below`). The matrix uses exactly that mapping.

**Liquidation window semantics resolved:** the 3d/7d/30d windows are **cumulative snapshots**, not disjoint — 53 of 77 assets have nested 3d ⊆ 7d ⊆ 30d price sets and 67 of 77 have monotonically growing notionals (52 of 77 are byte-identical across all three windows). PIT-16 asks for the stop to clear the *entire* shelf across all three windows, so the union is the operative set and the cumulative/ambiguous reading changes no conclusion.

---

## 4. Template results (151 symbols, 97 eligible)

Eligibility = broker spread ≤ 20% of stop distance and stop ≥ 3× spread. Train = first 70% of each symbol's history, test = last 30%. Median symbol expectancy in R.

| Window | Universe | Raw expectancy | +0.10 ATR slippage | Edge vs **touch** null | Edge vs **date** null |
|---|---|---|---|---|---|
| Train (→ 2026-10-28) | eligible (97) | **−0.0328R** (40.2% pos) | −0.0995R (29.9% pos) | **−0.0048R** (45.4% pos) | **−0.0039R** (48.5% pos) |
| Test (2026-10-28 → 2026-11-20) | eligible (97) | **+0.1320R** (64.9% pos) | +0.0654R (59.8% pos) | **−0.0266R** (42.3% pos) | **+0.0641R** (62.9% pos) |
| Train | ineligible (54) | −1.1527R (1.9% pos) | — | +0.0013R | +0.0534R |
| Test | ineligible (54) | −1.1921R (7.4% pos) | — | −0.0171R | +0.0932R |

**Reading.** The two nulls answer different questions:

- The **touch-conditioned null** keeps the entry *level* and the date, and randomises only *which* touching bar fills the limit. The template loses to it in **both** train (−0.0048R) and test (−0.0266R). **The pullback timing itself carries no edge.**
- The **date-conditioned null** keeps the date but randomises the entry bar *and* level. The template beats it by +0.0641R in test and by −0.0039R in train — i.e. **all** of the residual is concentrated in the test window and has no training support.

Context: the test window is a bear tape (median universe drift −2.32%, 73.2% of eligible symbols fell) versus −1.41% / 67.0% in train. The train→test sign flip on raw expectancy (−0.033R → +0.132R) with no train support for either edge is the signature of **regime-contingent beta, not a stable edge**. Spread-to-risk gating is essential: the 54 ineligible symbols lose ≈1.2R per trade.

Sample caveat: under strict non-overlap only **2** eligible symbols reach 25 test trades (71 reach 10). Thin samples are the reason the median is the headline statistic.

---

## 5. Setup matrix — 0 of 28 clear the stack

152 telemetry assets were evaluated. 124 are **BLOCKED** (no liquidation shelf, so PIT-16/GATE-02 cannot be evaluated); 28 are fully mapped.

| Gate | PASS | FAIL | BLOCKED |
|---|---|---|---|
| PIT-16 cold-zone SL | 28 | 0 | 124 |
| GATE-01 auction clearance | 28 | 0 | 124 |
| GATE-02 (operative) | 2 | 26 | 124 |
| GATE-02 (strict) | 1 | 27 | 124 |
| GATE-03 LSR magnet | 12 | 0 | 16 |
| GATE-04 spread drag | 5 | 23 | 0 |
| GATE-07 $6 stressed risk | 18 | 10 | 0 |
| PIT-22 spread-to-stop | 12 | 16 | 0 |

**Full stack (GATE-01 + 02 + 03 + 04 + 07 + PIT-16 + PIT-22): no candidate passes.** The only two GATE-02 survivors, **MANA** and **COMP**, both fail GATE-04 (spread) and PIT-22, and both have catastrophic backtests (MANA −1.68R train / −2.05R test; COMP −0.90R / −1.10R). `G02s` = strict reading.

| Asset | Symbol | LSR | G02 | G02s | G03 | G04 | G07 | PIT22 | elig | train R | test R | test R slip | edge DATE | edge TOUCH | test n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| MANA | MANUSD.p | 2.27 | PASS | PASS | PASS | FAIL | PASS | FAIL | n | -1.683 | -2.049 | -2.116 | +0.231 | -0.121 | 36 |
| COMP | COMUSD.p | 0.82 | PASS | FAIL | PASS | FAIL | PASS | FAIL | n | -0.896 | -1.103 | -1.170 | +0.042 | +0.121 | 27 |
| XAU | XAUUSD.pi | 5.09 | FAIL | FAIL | BLOCKED | PASS | FAIL | PASS | Y | +0.014 | +0.390 | +0.324 | +0.998 | -0.077 | 13 |
| CL | USWTI.p | 3.14 | FAIL | FAIL | BLOCKED | PASS | PASS | PASS | Y | +0.086 | -0.011 | -0.077 | +0.494 | -0.175 | 18 |
| BTC | BTCUSD.pi | 5.63 | FAIL | FAIL | BLOCKED | PASS | FAIL | PASS | Y | +0.037 | +0.431 | +0.364 | +0.121 | -0.068 | 24 |
| XLM | XLMUSD.p | 8.81 | FAIL | FAIL | BLOCKED | PASS | FAIL | PASS | Y | +0.162 | -0.011 | -0.078 | -0.078 | -0.145 | 27 |
| XAG | XAGUSD.pi | 52.26 | FAIL | FAIL | BLOCKED | PASS | FAIL | PASS | Y | +0.028 | -0.012 | -0.078 | -0.610 | -0.182 | 13 |
| AVAX | AVXUSD.p | 4.67 | FAIL | FAIL | BLOCKED | FAIL | PASS | FAIL | n | -0.068 | +0.199 | +0.132 | +0.830 | +0.191 | 19 |
| VET | VETUSD.p | 1.08 | FAIL | FAIL | PASS | FAIL | PASS | PASS | Y | -0.036 | +0.032 | -0.035 | +0.470 | +0.148 | 20 |
| TRX | TRXUSD.p | 0.74 | FAIL | FAIL | PASS | FAIL | FAIL | PASS | n | -1.190 | -0.890 | -0.957 | +0.321 | +0.038 | 20 |
| AR | ARWUSD.p | 2.22 | FAIL | FAIL | PASS | FAIL | PASS | FAIL | n | -0.209 | -0.265 | -0.332 | +0.269 | +0.129 | 11 |
| DOGE | DOGUSD.p | 2.87 | FAIL | FAIL | BLOCKED | FAIL | FAIL | FAIL | n | -2.448 | -1.827 | -1.893 | +0.267 | -0.028 | 27 |
| CHR | CHRUSD.p | 1.21 | FAIL | FAIL | PASS | FAIL | PASS | FAIL | n | -7.024 | -8.000 | -8.067 | +0.248 | +0.010 | 22 |
| XRP | XRPUSD.pi | 3.73 | FAIL | FAIL | BLOCKED | FAIL | FAIL | PASS | n | -0.314 | +0.008 | -0.058 | +0.213 | +0.116 | 28 |
| LINK | LNKUSD.p | 1.88 | FAIL | FAIL | PASS | FAIL | PASS | FAIL | n | -0.506 | -0.370 | -0.436 | +0.201 | +0.055 | 28 |
| SUSHI | XSIUSD.p | 1.69 | FAIL | FAIL | PASS | FAIL | PASS | FAIL | n | -0.880 | -1.393 | -1.460 | +0.170 | -0.071 | 22 |
| ETH | ETHUSD.pi | 12.05 | FAIL | FAIL | BLOCKED | FAIL | PASS | PASS | Y | -0.033 | -0.084 | -0.150 | +0.133 | -0.038 | 23 |
| SOL | SOLUSD.p | 6.13 | FAIL | FAIL | BLOCKED | FAIL | PASS | PASS | n | -0.259 | -0.161 | -0.227 | +0.078 | +0.047 | 15 |
| FIL | FILUSD.p | 2.83 | FAIL | FAIL | BLOCKED | FAIL | PASS | FAIL | n | -2.332 | -2.561 | -2.628 | +0.018 | -0.012 | 26 |
| HBAR | BARUSD.p | 4.14 | FAIL | FAIL | BLOCKED | FAIL | PASS | PASS | n | +0.072 | +0.120 | +0.054 | +0.009 | -0.058 | 31 |
| LTC | LTCUSD.pi | 2.28 | FAIL | FAIL | PASS | FAIL | FAIL | FAIL | n | -0.149 | -0.341 | -0.407 | -0.017 | -0.018 | 24 |
| NEAR | NERUSD.p | 9.63 | FAIL | FAIL | BLOCKED | FAIL | FAIL | FAIL | n | -0.177 | -0.575 | -0.642 | -0.055 | -0.024 | 27 |
| DOT | DOTUSD.pi | 3.54 | FAIL | FAIL | BLOCKED | FAIL | PASS | FAIL | n | -0.918 | -1.386 | -1.453 | -0.089 | -0.166 | 27 |
| ADA | ADAUSD.p | 3.71 | FAIL | FAIL | BLOCKED | FAIL | FAIL | FAIL | n | -0.630 | -0.554 | -0.621 | -0.110 | -0.030 | 32 |
| BNB | BNBUSD.p | 1.86 | FAIL | FAIL | PASS | FAIL | PASS | PASS | n | -0.295 | -0.415 | -0.482 | -0.161 | -0.061 | 29 |
| UNI | UNIUSD.p | 6.69 | FAIL | FAIL | BLOCKED | FAIL | PASS | FAIL | n | -0.195 | -0.408 | -0.475 | -0.217 | -0.029 | 21 |
| IOTA | IOTUSD.p | 2.11 | FAIL | FAIL | PASS | FAIL | PASS | FAIL | n | -2.746 | -4.143 | -4.210 | -0.237 | -0.016 | 27 |
| ATOM | ATMUSD.p | 0.90 | FAIL | FAIL | PASS | FAIL | PASS | FAIL | n | -0.962 | -0.795 | -0.862 | -0.279 | -0.044 | 13 |

---

## 6. Live book audit — R18 tickets, actual geometry

Gates are evaluated on the **entry-time** geometry (a ratcheted stop is reported separately). Full detail: `desk/audit_data/live_book_audit.csv`.

| Ticket | Asset | State | Side / lots | Entry | SL | TP | Risk | ×1.2 stress | G01 | G02 | G03 | G04 | G07 | PIT-16 | PIT-22 | LSR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| #20498955 | ETH | PENDING | SELL_LIMIT 0.15 | 1671.50 | 1707.50 | 1599.50 | $5.40 | **$6.48** | PASS | **FAIL** | **BLOCKED** | **FAIL** | **FAIL** | **FAIL** | PASS | 12.05 |
| #20496682 | CL | PENDING | SELL_LIMIT 0.05 | 82.38 | 83.48 | 80.18 | $5.50 | **$6.60** | PASS | **FAIL** | **BLOCKED** | PASS | **FAIL** | **FAIL** | PASS | 3.14 |
| #20432336 | BNB | OPEN | SELL 0.19 | 560.90 | 554.50 | 503.43 | $4.78 (orig) | $5.74 | PASS | **FAIL** | PASS | **FAIL** | PASS | PASS | PASS | 1.86 |
| #20467916 | EURUSD | OPEN | SELL 0.01 | 1.09744 | 1.10280 | 1.08615 | $5.36 | **$6.43** | PASS | BLOCKED | BLOCKED | PASS | **FAIL** | BLOCKED | PASS | n/a |

**GATE-07 — the accounting defect.** GATE-07 caps *stressed* MT5 risk at **$6.00** per trade, i.e. nominal risk ≤ **$5.00** at the mandated 1.20× multiplier. The desk's book-risk line counted **nominal** risk (`$5.36 open + $10.90 pending = $16.26`, 0.628%). On the mandated stressed basis the three non-BNB tickets are $6.48 / $6.60 / $6.43 — **each individually breaches the $6.00 cap**. Aggregate stressed risk is $20.97 (0.810%) at the ratcheted BNB stop and $25.25 (0.976%) if BNB sits at its original stop, still under the 1.00% account cap but only because BNB has been ratcheted.

**GATE-04 on BNB.** BNB's broker spread is 220 points = $2.20 on a $560 instrument = **0.392%** of price, which fails the "< 0.15%" leg regardless of the stop. The spread/stop leg (2.20 / 25.18 = 0.087) passes on the original stop. This is a structural property of BNB on this venue, and it is why BNB is ineligible in the backtest (test expectancy −0.415R).

**BNB stop conflict resolved.** `586.08` is the **original** stop; `554.50` is the **ratcheted** protective stop. Both reconcile exactly against the desk's own numbers at contract size 1.00:
- `(586.08 − 560.90) × 0.19 × 1.00 = $4.784` → desk "$4.78 original risk"
- `(560.90 − 554.50) × 0.19 × 1.00 = $1.216` → desk "+$1.22 locked"

There is no contradiction; the two figures describe different moments.

### 6.1 P5-02 / K-4 portfolio capacity — both pendings are infeasible before any gate is applied

The previously open question ("do pending orders consume the two-position ceiling?") is **resolved by K-4**, which is the binding policy:

> **K-4 · Portfolio** — *"Keep the stricter P5-02 concurrency limit: up to two owned positions total and one crypto position across the desk. **Count proposed and pending orders in fill/capacity scenarios.** Open plus pending total terminal heat ≤ 1.0% of the relevant current equity. Reserve capacity before sending. **Cancel further owned candidate orders after the first two fills** when permitted; re-read all orders and fills first."*

P5-02 itself (`[P5-02] [MAX_POSITIONS]`) and `[PIT-19]` both say the same: *"Apply the K-4 ceilings of two owned positions total, at most one owned crypto position…"* and *"Apply P5-02/K-4 to active **and pending** orders."*

Applied to the R18 book:

| State | Asset | Class | Count |
|---|---|---|---|
| OPEN | BNB `#20432336` | crypto | 1 |
| OPEN | EURUSD `#20467916` | FX | 1 |
| **Owned positions** | | | **2 / 2 — full** |
| **Owned crypto positions** | | | **1 / 1 — full** |
| PENDING | ETH `#20498955` | crypto | — |
| PENDING | USWTI `#20496682` | energy | — |

- If **ETH `#20498955` fills** → 3 owned positions (> 2) **and** 2 owned crypto positions (> 1). **Both ceilings breached.**
- If **USWTI `#20496682` fills** → 3 owned positions (> 2). Ceiling breached.
- The 1.00% heat ceiling is **not** the binding constraint: all four tickets stressed together are $20.97 (0.810%) at the ratcheted BNB stop and $25.25 (0.976%) at its original stop, both under $25.88.

**So both pending orders are capacity-infeasible independently of every gate result.** K-4's own remedy is explicit — *"Cancel further owned candidate orders after the first two fills"* — and the two fills have already happened (BNB, EURUSD). This is the cleanest cancellation reason on the book, and it does not depend on the GATE-02 reading, the spread data, or the backtest.

**Risk-cap erratum.** K-4 records that the operator authorized a $10.00 stressed per-trade ceiling on 2026-11-20 but that *"the deployed bridge still enforces $6.00 — erratum A-24 — so $6.00 is the binding cap until the bridge is updated."* All GATE-07 tests above therefore use **$6.00**. If the bridge is ever updated to $10.00, ETH ($6.48), USWTI ($6.60) and EURUSD ($6.43) would clear the cap — **but the P5-02 count ceiling would still forbid both pendings**, and GATE-02/GATE-03/GATE-04 would still veto them.

---

## 7. Ticket ledger — closed and reconciled

The earlier gap (`desk/log.md` recorded only the *placement* of `#20458009` / `#20458013`) is now closed from `desk/COUNCIL.md`:

| Ticket | Asset | Final disposition |
|---|---|---|
| #20458009 | XAU | **FILLED** @ 3620.44; SL ratcheted to 3620.50 breakeven (+$7.89 at 3628.33); later absent from the blotter — the explicit close record is still not in the desk files (**only unresolved item**) |
| #20458013 | BTC | **CANCELLED** — `TRADE_ACTION_REMOVE`, counter-trend buy banned by TC-09 (H1 Close < EMA89 < EMA233) |
| #20469186 | NAS100 | **TP HIT** +$26.28 / +2.24R |
| #20471818 | BTC | **CANCELLED** (frees $5.30) |
| #20471819 | SOL | **CANCELLED** in the risk-cap reconciliation |
| #20474557 | LINK | **CANCELLED** in the risk-cap reconciliation |
| #20475921 | UNI | **TP CLOSED** — deal #18587579 @ 4.534, +$10.30 gross / −$0.03 swap = **+$10.27 net** |
| #20480855 | ETH | **CANCELLED** — SL 1714.50 was inside the 1713.89–1720.76 3d/7d/30d cluster |
| #20496681 | ETH | **CANCELLED** — spot swept 1634.9 through TP 1637.50 |
| #20496682 | USWTI | **PENDING** (live) |
| #20498955 | ETH | **PENDING** (live) |
| #20432336 | BNB | **OPEN** |
| #20467916 | EURUSD | **OPEN** |

**Cost calibration.** UNI's $10.30 gross reconciles to the desk figure with **zero commission** on crypto CFDs. Legacy FX replay uses $7/lot round turn.

---

## 8. Open items and data caveats

1. **XAU `#20458009` close** — no explicit close/exit line in `desk/log.md` or `desk/COUNCIL.md`; the position simply disappears from the blotter between the 15:42Z and 15:27Z readbacks. Most consistent with the 3620.50 breakeven stop being hit (price 3590.29 at 15:27Z), but this is inference, not a record.
2. **`historical_parquet/` is stale** — latest bars span 2026-11-20 20:45 to 2026-11-22 22:45 UTC, while the live book is dated 2026-11-25. Every result in this report carries that freshness gap.
3. **Telemetry is a desk readback**, not venue-verified state; there is no independent MT5 connection in this environment.
4. **GATE-02 wording** — the spec's literal clause contradicts PIT-16 (Section 3). It should be amended to state the operative thresholds (strength ≥ 60 / notional ≥ $2.0M) rather than leaving the desk to guess.
5. **GATE-07 accounting** — the desk's own book-risk line uses nominal risk, not the 1.20× stressed risk GATE-07 mandates.
6. **P5-02 is resolved by K-4** (Section 6.1): pending orders *do* count, and the count ceiling is breached by both pendings. What remains is enforcement — the desk's risk line tracks heat but not the position/crypto count.
7. The 1H feed's low differs from the 15M-aggregated low in 1 of 1,251 hours (finer-grained 1H source); high and close match exactly.

---

## 9. Recommendations

**CANCEL** — ETH `#20498955` and USWTI `#20496682`. The primary and most robust reason is **P5-02/K-4 portfolio capacity**: the desk already owns 2 of 2 permitted positions and 1 of 1 permitted crypto position, so *any* fill breaches the count ceiling (an ETH fill breaches it twice). Independently, both breach the $6.00 stressed per-trade cap, both sit in a GATE-03 LSR magnet (12.05 and 3.14 vs the 2.5 threshold) as passive limits, and both have Vector B hazard pools at strength 100 inside the risk path.

**MODIFY** — hold BNB `#20432336` at the ratcheted 554.50 (locks +$1.22; the ratchet cannot be widened under the cold-zone rule) and hold EURUSD `#20467916`. Do not add to either, and do not widen either stop. BNB passes PIT-16 on its **entry-time** stop (586.08, beyond the whole shelf) and PIT-22 (stop 25.18 ≥ 5 × spread 11.00); it fails GATE-02 (4 Vector B pools, max strength 84.6) and GATE-04 (spread 0.392% of price). EURUSD has no liquidation telemetry, so its GATE-02/GATE-03/PIT-16 are BLOCKED rather than PASS.

**ADD** — five controls, all cheap and all evidence-backed:
1. Apply the **1.20× stress** to every ticket *before* it enters the aggregate book-risk line.
2. **Enforce the P5-02/K-4 count ceiling against pending orders**, not only against fills: cancel further owned candidate orders after the first two fills. Today both pendings are capacity-infeasible and neither was caught by the risk line.
3. Gate on the **spread-percentage** leg of GATE-04 (0.15% of price), not only the spread/stop leg — BNB and ETH both fail it.
4. Treat a **GATE-02/LSR veto as final for passive limits**: GATE-03 bans the passive limit into the magnet path outright, so a pending order with LSR ≥ 2.5 should never be staged.
5. Re-run the template only with the **date-conditioned null** as the headline. The touch-conditioned null shows the timing edge is negative; the date null shows the rest is window drift. Until a template beats the date null in *both* train and test, it is not an edge.

**Do not scale.** No candidate clears the full gate stack, the template's test-window advantage has no training support and disappears at 0.25 ATR slippage, and the live book already sits at three individual GATE-07 breaches. The correct posture is **no new risk**.

---

## 10. Reproduction

```bash
python3 scripts/ops/verify_timestamp_semantics.py      # bar-label proof + ATR leak quantification
python3 scripts/ops/build_audit_artifacts.py            # ATR/EMA coverage + 15M correlation matrix
python3 scripts/ops/build_setup_matrix.py               # backtest + 152-asset gate matrix
python3 scripts/ops/audit_live_book.py                  # live-book gate audit
```

Artifacts (`desk/audit_data/`): `atr_15m_1h.csv` (151 symbols), `corr_15m_matrix.csv` (151×151, 2,117 aligned observations, 2026-10-02 → 2026-11-20), `corr_15m_meta.json`, `pullback_bt_results.csv` (151 symbols), `setup_matrix.csv` (152 assets), `live_book_audit.csv`, `live_book_r18.json`.

> Requires `pandas` + `pyarrow`. In this sandbox: `python3 -m venv /tmp/audvenv && /tmp/audvenv/bin/pip install pandas pyarrow`.

### 10.1 Placement

The report lives at `desk/ROUND_R19.md` (matching `desk/ROUND_R824.md` … `ROUND_R826.md`) and the artifacts at `desk/audit_data/` (matching the existing `htf_audit_cells.csv` / `ivwall_audit_summary.json`). The reproducer scripts are in `scripts/ops/`.

They were **not** promoted to `Delta-Fibonacci-Institutional-Business/validation/results/`: that directory holds artifacts of the Delta Exchange order-flow strategy validation (`dfib_stage3.py`, `dfib_sweep.py`, `portfolio_ledgers.parquet`), a different venue and a different strategy. Filing an MT5 desk-book audit there would conflate two unrelated evidence sets and corrupt that convention.

`.gitignore` gained one line — `!desk/audit_data/*.csv` — because the repo already tracks CSVs in that directory and the audit artifacts must survive a clone. Total added size ≈ 560 KB.

### 10.2 Attestation status: INCOMPLETE (attested path), COMPLETE (analysis)

`desk/ROUND_EVIDENCE_CONTRACT.md` requires a signed evidence bundle under `data/round-evidence/<round-id>/` with live captures from the CG terminal (`127.0.0.1:3000`), the Delta Exchange API and a MetaTrader5 terminal. **None of those are reachable from this environment**, so the attested bundle could not be produced and `scripts/verify_round_evidence.py` could not be run. The analysis in this report is therefore **unattested**: it is reproducible from repo data, but its inputs are desk readbacks rather than venue-captured payloads.

What that means concretely:

- Every live-book fact here comes from `desk/log.md` / `desk/COUNCIL.md` / `data/telemetry/assets/*.json`, **not** from a broker payload. The round contract is explicit that *"A catalog, ticker, or prior round cannot stand in for the broker book."*
- Per the contract, *"If the account audit shows a slot or risk limit breached, the gate cannot be PASS even when the order is already resting."* The P5-02 count breach (Section 6.1) is exactly that condition, and it is the reason both pendings are CANCEL rather than HOLD.
- `scripts/antigravity_selfcheck.py` passes unchanged (10 pass, 1 pre-existing warn about a missing `desk/mt5.json` path in the poller) and independently confirms **2 open MT5 positions**, consistent with the R18 book read here.
- `scripts/check_specs.py` cannot run here (no `MetaTrader5` module) — this is a pre-existing environment limitation, not a regression.

**A live operator must re-read the broker book and re-run the gates against live quotes before acting on any cancellation.** The cancellation recommendations rest on gate evidence that is internally consistent and reproducible, but is one readback old.

