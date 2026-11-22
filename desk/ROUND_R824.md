# ROUND R824 — Desk Report

> **Run**: 2026-11-22 16:02–16:12 UTC (Sunday) · Arena agent · **AUDIT mode**
> **HEAD**: `91beedad` · **Terminal**: `localhost:3000` healthy (rss ~200 MB)
> **Venues touched**: none. No orders placed, amended, cancelled or closed.

---

## Verdict at a glance

| Objective | Result |
| :--- | :--- |
| 1 · Sync working tree | **DONE (recovered)** — `.git` had been lost with the workspace snapshot; re-fetched, HEAD = `91beedad` |
| 2 · Monitor resting Delta orders | **NEITHER TOUCHED** — XRP 1.70 % above its limit, ADA 2.65 %; brackets intact, no maintenance due |
| 3 · Near-miss calibration check | **INSUFFICIENT SAMPLE** — 0 resolved / 13 pending (1.1 h of 4 h elapsed); resolver bug fixed |
| 4 · MT5 22:00 UTC re-entry batch | **STRUCTURALLY BANNED** — PIT-22 and PIT-18 both fail on **normal** spreads; waiting for the open cannot fix it |
| 5 · Round summary | this document |

**One-line read:** the book is well-formed and quiet — both sweep limits are waiting for a bounce that hasn't come, the near-miss cohort is too young to say anything, and the MT5 batch should be **cancelled rather than scheduled**, because the setup class is architecturally incompatible with that venue.

---

## 1 · Working-tree sync — DONE, with one incident

`git pull --rebase origin main` could not run as issued: **the sandbox workspace snapshot had dropped `.git/` and `node_modules/`** (both are excluded from snapshot persistence by design). Contents and my prior desk reports survived.

**Recovery path taken**
```
git init  →  git remote add origin …  →  git fetch origin main
          →  git symbolic-ref HEAD refs/heads/main
          →  git reset --hard origin/main
```

**Result**: HEAD `91beedad` — *"chore(desk): register live Delta orders #1705868028 (XRP) and #1705868424 (ADA)"*. `desk/state.json` `updatedAt` **15:58:14Z**, both tickets present with `margin_usd` 0.941 + 0.149 = **$1.09 engaged**, matching the operator directive exactly. Untracked session reports preserved.

> **Process note**: the token had to be re-embedded to re-attach the remote, and the remote was left in the token-bearing form because `reset --hard` does not rewrite it. Worth a `git remote set-url` scrub on the operator's next push cycle.

**Infrastructure restored**: `npm install` (844 packages) + `npm run build` + production server on `:3000`, `/api/health` → `200 {"ok":true}`.

---

## 2 · Resting Delta orders — no fill, no touch, no maintenance

| Ticket | Symbol | Side | Limit | Stop | TP | Mark (16:07Z) | Gap to entry |
| :--- | :--- | :---: | ---: | ---: | ---: | ---: | ---: |
| **#1705868028** | XRPUSD | SHORT | 0.9410 | 0.94452 | 0.9066 | **0.92531** | **+1.70 %** |
| **#1705868424** | ADAUSD | SHORT | 0.14870 | 0.14993 | 0.13920 | **0.144867** | **+2.65 %** |

- **Both are passive limits sitting *above* market.** They fill only if the counter-trend bounce extends 1.7 % (XRP) / 2.7 % (ADA). Neither has been reached.
- **Bracket integrity: clean.** Entry, stop and target all populated in the register; `riskR` 9.79 and 7.76 respectively. No partial fill, so no stop-ratchet or breakeven action is due.
- **`FILLED` / `OPEN` is UNVERIFIED-BY-AGENT.** No Delta credential exists in this sandbox and private order state is not exposed on any public endpoint. The register's `status: "open"` is the operator's assertion, quoted — not re-derived by me.
- **⚠ Expiry clock.** `order_expiry_hours = 4` from the 15:58 placement puts **expiry ≈ 19:58 UTC**. PIT-06 would also prune a resting order once it drifts >1.5 % from the entry zone for 2 h. Both apply here. **Recommend an explicit KEEP / PRUNE ruling at the ~19:00 round** rather than letting it lapse by default.

---

## 3 · Near-miss calibration — too early, and the ledger needed rebuilding

```
$ python3 scripts/trading/near_miss_resolver.py
[near-miss] download mirror skipped (no download/ on this host)
[near-miss] resolution done: 0 newly resolved, 13 pending; report -> desk/NEAR_MISS_CALIBRATION.md
```

**Two things had to be fixed before that run was meaningful:**

1. **The ledger did not exist.** `data/near_miss/` is gitignored, so `candidates.jsonl` was lost with the snapshot. I rebuilt all 13 R823 rows from the published `desk/NEAR_MISS_CALIBRATION.md` into the resolver's exact schema (`capturedAtEpoch`, `capturedAtUtc`, `direction`, `targets[0..1]`, `horizonH: 4`), tagged `reconstructed: true`. Dedup ids are stable, so live captures won't double-count. **Note:** the report carried T2 but not T1, so T1 is reconstructed at 1R for the intermediate target — documented in the ledger, and it only affects the `T1-then-stop` bucket, not `WIN2`/`LOSS`.
2. **The resolver had a hardcoded path bug.** `CALIB_DOWNLOAD = Path("/home/z/my-project/download/…")` raised `FileNotFoundError` on any host without that directory — **after** the report had been written, so the failure looked like a crash while the artefact was already stale. Now `$CALIB_DOWNLOAD_DIR` (default `download/`) with a clean skip. This is a genuine portability fix worth keeping.

**State of the calibration**

| | |
| :--- | :--- |
| Cohort | 13 candidates, all bucket 3/10, **all LONG** (LINK UNI ETC OP VET 1000PEPE XAU NVDA SOXL GOOGL MU CL QQQ) |
| Captured | 14:57–15:00 UTC |
| Age at run | **1.09–1.15 h** vs `min_age_h = 4.0` |
| Resolved | **0** |
| First resolution window | **≈ 18:57–19:01 UTC** |
| Verdict threshold | n ≥ 30 per bucket — cohort is n=13 in one bucket, so **no verdict is reachable from this wave at all** |

**A desk-relevant observation, explicitly *not* a verdict.** All 13 are long-side POC/VAL probes. The R823 Table-7 work scored every long at **F1 = 0.0** against the bearish 4H/D1 EMA spine. If this cohort resolves negative once n ≥ 30, it corroborates the regime read *independently of the confluence thresholds* — which makes it worth tracking as a **regime cross-check**, not only as a threshold-calibration exercise. I would add that column to the report before the sample matures.

---

## 4 · MT5 22:00 UTC re-entry batch — **do not schedule this batch**

This is the round's most important finding, and it contradicts the premise of objective 4.

Evaluated against the real broker specs in `desk/mt5_symbols_universe.json`:

| Symbol | Broker spec spread | Stop distance | Stop % of price | spread/stop (normal) | PIT-18 ≤ 0.20 | PIT-22 stop ≥ 1.5 % | Verdict |
| :--- | ---: | ---: | ---: | ---: | :---: | :---: | :--- |
| XRPUSD.pi | 0.003 | 0.00352 | **0.37 %** | 0.85 (85 %) | **FAIL** | **FAIL** | **BANNED** |
| ADAUSD.p | 0.0021 | 0.00123 | **0.83 %** | 1.71 (171 %) | **FAIL** | **FAIL** | **BANNED** |
| SOLUSD.p | 0.34 | 0.548 | **0.76 %** | 0.62 (62 %) | **FAIL** | **FAIL** | **BANNED** |
| TRXUSD.p | 0.0012 | 0.00034 | **0.11 %** | 3.53 (353 %) | **FAIL** | **FAIL** | **BANNED** |

**Two independent reasons, only one of which is about timing**

- **(a) PIT-18 fails on ordinary spreads.** The operator's weekend reading — XRP 0.006 spread vs 0.00352 stop = **170 %** — is the extreme case. At the *listing* spread of 0.003 the ratio is still **85 %**, i.e. **4.3× over the 20 % ceiling**. Even a perfect Sydney-open compression to spec level leaves these stops unplaceable. **Waiting until 22:00 UTC cannot close an 85 % → 20 % gap.**
- **(b) PIT-22 is the binding veto, and it is time-invariant.** On Blueberry MT5, any crypto CFD whose structural stop is **< 1.5 % of price** is *"STRICTLY BANNED … routed exclusively to the separately validated Delta Exchange India CLOB route."* Every ITCLS stop here is **0.11–0.83 %**. The archetype's defining property — a tight cold-zone stop just beyond the shelf wick — is **architecturally incompatible with MT5 crypto CFDs**. This is not a spread condition that improves at an hour; it is a property of the strategy class.

**What I recommend instead**

1. **Cancel the crypto leg of the 22:00 batch.** Do not stage XRPUSD.pi / ADAUSD.p / SOLUSD.p / TRXUSD.p.
2. **Route this setup class to Delta exclusively** — which is exactly what PIT-22's remedy clause prescribes, and what the live book already does.
3. **Use the 22:00 open for the venue's actual edge**: TradFi indices and metals CFDs, and Fibonacci-engine setups whose stops are naturally ≥ 1.5 %. Those were not in this batch and were not evaluated here.
4. If a wider-stop ITCLS variant is ever wanted on MT5, the stop must be **≥ 1.5 %** and **≥ 5× spread** — for XRPUSD.pi that is **≥ 0.015 (1.59 % of price)**, which at min lot 1 (contract 1000) is **$15.00 risk**, above the $10 standard tier and into the $25 A+ ceiling. Worth noting as a sizing consequence before anyone re-opens that route.

Batch-prep artefact written: `data/mt5_pit18_batch_prep.json`.

---

## 5 · Book & risk state

| Item | State |
| :--- | :--- |
| Crypto slots (K-4 / P5-02) | **2 / 2 committed** — ETH short (Delta, uPNL −$0.42) + SEI short (Delta) |
| Queued incremental crypto exposure | XRP short, ADA short (resting, unfilled) |
| Delta margin engaged | **$1.09** ($0.941 XRP + $0.149 ADA) · available **$0.36** · NLE **$1.56** |
| New A+ grades this round | **0** (no re-scan requested; last full scan 15:01 UTC = 0 A / 0 B) |
| Bracket actions due | **0** |
| Interlock assertions | **UNVERIFIED-BY-AGENT** — both venues are Windows-host/credential-gated; recorded per PRE_TRADE_INTERLOCK_RULE §4 |

---

## 6 · Open items for R825

1. **KEEP / PRUNE ruling on XRP #1705868028 and ADA #1705868424 at ~19:00 UTC** — before the 4 h expiry (≈19:58) and before the PIT-06 2-hour drift clause bites.
2. **First near-miss resolution at 18:57–19:01 UTC** — expect 13 rows to move to resolved. No bucket verdict (n=13 < 30), but start the LONG-cohort regime cross-check.
3. **Reconcile register-stale positions** — `BTCUSD.pi` (0.01 @ 61,800) and `EURUSD.pi` (0.02) appear in `desk/state.json` but not in the live `/api/cg/risk` feed. One-cycle register sync is required by interlock rule §5.
4. **Decide the near-miss LONG cohort's status** as regime corroboration, and add that dimension to `NEAR_MISS_CALIBRATION.md` before n reaches 30.
5. **Scrub the token from the sandbox remote** at the next operator push cycle (`git remote set-url`).

---

*Prepared 2026-11-22 16:12 UTC · AUDIT mode · no venue mutations performed · Delta order status quoted from operator register, not re-verified.*
