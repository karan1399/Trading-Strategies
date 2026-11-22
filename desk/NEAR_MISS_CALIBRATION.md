# NEAR-MISS CALIBRATION REPORT
> **Generated UTC**: 2026-11-22T17:18:54Z  |  **Engine**: `desk/LLM_TRADE_FORMULATION_PLAYBOOK.md` rejected candidates
> **Question**: do rejected 3–6/10 formulations carry positive expectancy? If yes, grade thresholds are too tight; if no, the gate is saving the desk from noise.

## Method (pessimistic baseline)
- Entry assumed filled at capture (market-reference). Live playbook entries are limit-at-zone retests — strictly better-priced, so this simulation understates live edge.
- Same-candle stop/target ambiguity resolved **stop-first** (worst case).
- Horizon: 4h from capture, 5m candles from the terminal's own `/api/cg/kline` route (same data path as the dumps).
- realizedR: WIN2 = full move to T2; T1 = half at T1 + runner marked at T1; LOSS = −1R; OPEN = marked at horizon close.
- Verdict requires n ≥ 30 resolved rows per bucket. Below that: INSUFFICIENT SAMPLE — no rule changes.

## Resolved outcomes by confluence bucket

| Bucket | n | WIN2 | T1-then-stop | LOSS | OPEN | Win rate | Loss rate | avg realized R | avg MFE R | avg MAE R | Verdict |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| — | 0 | — | — | — | — | — | — | — | — | — | no resolved rows yet |

## Pending (captured, horizon not yet elapsed)

| Symbol | Captured UTC | Bucket | Direction | Entry | Stop | T2 | Evidence (top) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| LINK | 2026-11-22T14:57:13Z | 3/10 | long | 8.783 | 8.7303 | 9.0465 | market.auction.day.poc=8.78775 (+2) |
| UNI | 2026-11-22T14:57:42Z | 3/10 | long | 4.707 | 4.6788 | 4.8482 | market.auction.day.poc=4.702 (+2) |
| ETC | 2026-11-22T14:57:58Z | 3/10 | long | 5.569 | 5.5356 | 5.7361 | market.auction.day.vah=5.556 (+2) |
| OP | 2026-11-22T14:58:35Z | 3/10 | long | 0.072 | 0.0716 | 0.0742 | market.auction.day.poc=0.0719 (+2) |
| VET | 2026-11-22T14:59:12Z | 3/10 | long | 0.004628 | 0.0046 | 0.0048 | market.auction.day.poc=0.0046 (+2) |
| 1000PEPE | 2026-11-22T14:59:18Z | 3/10 | long | 0.0034004 | 0.0034 | 0.0035 | market.auction.day.poc=0.0034 (+2) |
| XAU | 2026-11-22T14:59:28Z | 3/10 | long | 3567.25 | 3545.85 | 3674.27 | market.auction.day.poc=3565.57 (+2) |
| NVDA | 2026-11-22T14:59:36Z | 3/10 | long | 133.18 | 132.38 | 137.18 | market.auction.day.poc=132.90 (+2) |
| SOXL | 2026-11-22T14:59:46Z | 3/10 | long | 33.95 | 33.75 | 34.97 | market.auction.day.poc=33.98 (+2) |
| GOOGL | 2026-11-22T14:59:55Z | 3/10 | long | 275.01 | 273.36 | 283.26 | market.auction.day.poc=274.58 (+2) |
| MU | 2026-11-22T14:59:59Z | 3/10 | long | 619.14 | 615.43 | 637.71 | market.auction.day.poc=617.88 (+2) |
| CL | 2026-11-22T15:00:20Z | 3/10 | long | 84.04 | 83.54 | 86.56 | market.auction.day.poc=84.04 (+2) |
| QQQ | 2026-11-22T15:00:49Z | 3/10 | long | 627.4 | 623.64 | 646.22 | market.auction.day.poc=628.42 (+2) |

## Calibration policy
- **No threshold changes from small samples.** Verdicts only at n ≥ 30 per bucket (est. 2–3 weeks of daily scans).
- If the 3-bucket shows EDGE REJECTED at scale: the playbook gate is confirmed correct — thresholds stay.
- If the 3-bucket shows EDGE SUPPORTED at scale AND MFE materially exceeds realized: re-test a B-grade-at-4 rule in paper mode before any live tiering change (Kaizen amendment per PRE_TRADE_INTERLOCK_RULE.md §6 authority chain).
- Ledger: `data/near_miss/candidates.jsonl` (capture log, append-only) · `data/near_miss/resolved.jsonl` (outcomes).
