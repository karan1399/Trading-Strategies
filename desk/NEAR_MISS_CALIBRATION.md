# NEAR-MISS CALIBRATION REPORT
> **Generated UTC**: 2026-11-22T20:49:29Z  |  **Engine**: `desk/LLM_TRADE_FORMULATION_PLAYBOOK.md` rejected candidates
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
| **3/10** | 13 | 0 | 3 | 5 | 4 | 0.0 | 0.385 | **-0.236** | 1.41 | 1.58 | INSUFFICIENT SAMPLE (n < 30) |

## Pending (captured, horizon not yet elapsed)

_None — every captured candidate has resolved._

## Calibration policy
- **No threshold changes from small samples.** Verdicts only at n ≥ 30 per bucket (est. 2–3 weeks of daily scans).
- If the 3-bucket shows EDGE REJECTED at scale: the playbook gate is confirmed correct — thresholds stay.
- If the 3-bucket shows EDGE SUPPORTED at scale AND MFE materially exceeds realized: re-test a B-grade-at-4 rule in paper mode before any live tiering change (Kaizen amendment per PRE_TRADE_INTERLOCK_RULE.md §6 authority chain).
- Ledger: `data/near_miss/candidates.jsonl` (capture log, append-only) · `data/near_miss/resolved.jsonl` (outcomes).
