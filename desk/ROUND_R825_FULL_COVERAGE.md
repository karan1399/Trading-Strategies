# R825 — Full-Dataset Reformulation

**Run:** 2026-11-22 · 16:20–16:42 UTC · engine `scripts/trading/full_coverage_formulate.py` (new, 470 lines)
**Scope:** all 231 terminal instruments → 57 deep-pulled × 15 routes = 855 evidence calls
**Verdict:** ⛔ **NO NEW TRADES. Both live tickets carry a stop-death defect.**

---

## 1. What "restart with all data" actually changed

The previous formulation used ~10 of 37 routes. This run consumed **15 routes per symbol** and,
for the first time, recorded per-route success so nothing could hide.

| Route | Coverage | What it contributed |
| :--- | ---: | :--- |
| `narrative` (§8 DECIDE) | **100 %** | **regime + activeModel + direction + grade** — the gate I never had |
| `oracle` (§15 Athena) | **100 %** | 8-factor score 0–100 |
| `bookwalls` | **100 %** | L2 imbalance + ask/bid walls |
| `signals` / `fp` | 100 % / 42 % | footprint signal balance |
| `funding` | **100 %** | crowded-long test (multi-venue) |
| `oi` | **100 %** | venue concentration |
| `micro` | **100 %** | Hyperliquid BBO, mark vs oracle |
| `quant` | **100 %** | long/short ratios |
| `market` | 72 % | EMA / RSI / VWAP / ATR / auction |
| `cvd` | 42 % | cumulative delta |
| **`liqlevels` 3d/7d/30d** | **23–25 %** | liquidation shelves — **stop geometry** |
| `largeorder` | 12 % | market-wide whale executions (not per-symbol) |

**Still unavailable, now recorded rather than substituted:**

- `stopmap` — needs `CG_SESSION`; **PIT-16 heat-map remains UNAVAILABLE**
- `perpdepth` — registry is index-only; no verified crypto venues
- `cot` — no COT contract mapped for crypto
- `options` — BTC/ETH only
- `liq-live` — per-venue `events:0` except OKX ⇒ **PIT-11 gate UNEVALUABLE**
- `live` / `engine` / `debug` / `footprint` — HTML pages, not JSON APIs

**Why liqlevels caps at ~24 %:** the terminal's `MAX_CG_CONCURRENCY = 6` upstream gate plus a
30 s host-level 429 cooldown. Pulling 171 forced liquidation ranges collides with the background
engine. **The route is not broken — it is rate-limited.** The honest number is 23–25 %, not 100 %.

---

## 2. ⛔ THE FINDING: my direction call fights the terminal's own models

Across all 14 scored candidates:

| Conflict | Count | Detail |
| :--- | ---: | :--- |
| **Terminal direction LONG vs my SHORT geometry** | **6 / 14** | KORU, BTC, BNB, LTC, AVAX, TRX — all grade **B**, model `WATCH_EXHAUSTION` |
| Terminal NEUTRAL / `NO_TRADE` / grade **SKIP** | 8 / 14 | DOGE, XRP, ADA, ETH, SOL, LINK, NEAR, DOT |
| Terminal agrees with SHORT | **0 / 14** | — |

**Not one candidate is confirmed short by the terminal.**

### Why — and it is not a modelling subtlety, it is the tape

| Measure | Value | Reading |
| :--- | :--- | :--- |
| Symbols **green** on the day | **36 / 41** | broad risk-on bounce |
| Median 24h change | **+1.06 %** | — |
| Median **D1 RSI** | **32.0** | oversold-to-neutral |
| Symbols **D1 RSI < 30** | **18 / 41** | deeply stretched down |
| Price **above VWAP** (median z) | **+1.1 σ** | bouncing |
| ATR state | **squeeze on 39 / 41** | compression — moves not yet resolved |

This is a **compression bounce inside a daily downtrend**. H4/D1 EMAs are still bear-stacked
(hence Stage-A found 54 short-bias names), but price is mean-reverting up off oversold into a
squeeze. Every mechanical "shelf-fade short" I generated is shorting directly into that bounce —
which is exactly what `WATCH_EXHAUSTION` is flagging as a *long* setup and what `NO_TRADE` is
telling me to skip.

**This was invisible in the old formulation because I never pulled `narrative`.**

---

## 3. The stop-death defect on both live tickets

Re-pulling all three liquidation ranges shows the stops sit inside shallow clusters with only
**0.25–0.36 % of clearance** — narrower than one 15m candle in this regime.

| Ticket | Entry | Stop | Nearest shelf above | Clearance | ATR (H1) | Clearance in ATR |
| :--- | ---: | ---: | :--- | ---: | ---: | ---: |
| **XRP #1705868028** | 0.9410 | 0.94452 | **0.9479** ($7.4M, str 46) | **+0.36 %** | 0.00923 (1.0 %) | **0.36** |
| **ADA #1705868424** | 0.14870 | 0.14993 | **0.1503** ($13.0M, str 36) | **+0.25 %** | 0.00165 (1.1 %) | **0.23** |

A stop sitting **0.23–0.36 ATR** beyond a live liquidation cluster will be swept by noise before
the thesis resolves. This is the PIT-21 anatomy (the SOLUSD case was swept by 0.08 %) at 3–4× the
penetration.

**Both positions are also 1.22 % and 2.55 % away from entry** and have been untouched — the market
is not coming to them.

### Sizing — still over the 1 % cap

`fixed_fractional_position_size` at the 1 % cap ($0.0163 on $1.63 NLE):

| | Current | Compliant | Excess |
| :--- | :--- | :--- | :--- |
| **XRP** | 10 contracts, $0.0352 risk (**2.16 %**) | **4 contracts**, $0.0144 risk (0.88 %) | **2.4×** |
| **ADA** | 20 contracts, $0.0246 risk (**1.51 %**) | **12 contracts**, $0.0151 risk (0.92 %) | **1.6×** |

---

## 4. Friction-adjusted economics of the 14 candidates

Break-even win rate = 1/(1+R) after 0.20 % round-trip fees. Stops are the *new* wide stops that
clear the shelf clusters.

| Sym | Risk % | Stop | T1 | T3 | T3 net fees | **BE win %** | Terminal |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| ETH | 0.41 | 1783.8 | 9.4R | 14.1R | 13.6R | **6.6 %** | SKIP |
| KORU | 1.13 | 5.5017 | 2.8R | 5.8R | 5.7R | 14.6 % | **LONG B** |
| LINK | 0.79 | 9.0496 | 2.5R | 5.6R | 5.4R | 15.1 % | SKIP |
| SOL | 0.56 | 71.657 | 2.0R | 4.5R | 4.2R | 18.1 % | SKIP |
| AVAX | 0.94 | 6.9046 | 2.3R | 4.5R | 4.3R | 18.1 % | **LONG B** |
| DOGE | 1.48 | 0.05754 | 1.4R | 4.0R | 3.8R | 20.2 % | SKIP |
| ADA | 1.08 | 0.14748 | 1.0R | 3.7R | 3.6R | 21.1 % | SKIP |
| BTC | 1.43 | 63638 | 2.2R | 3.5R | 3.4R | 22.2 % | **LONG B** |
| TRX | 1.17 | 0.30957 | 1.1R | 3.1R | 3.0R | 24.1 % | **LONG B** |
| XRP | 0.66 | 0.94203 | 2.0R | 2.7R | 2.4R | 27.3 % | SKIP |
| LTC | 1.49 | 41.511 | 1.4R | 2.5R | 2.4R | 28.3 % | **LONG B** |
| BNB | 1.26 | 569.7 | 1.3R | 2.4R | 2.2R | 29.7 % | **LONG B** |
| NEAR | 2.24 | 3.1338 | 1.3R | 2.4R | 2.3R | 29.7 % | SKIP |
| DOT | 1.68 | 0.68073 | 1.8R | 1.8R | 1.7R | 35.3 % | SKIP |

**Read this table honestly:** the geometry is mathematically fine (break-evens of 6–35 %), but
**the direction is unsupported by the terminal on all 14**. High R-multiples do not rescue a
trade pointed the wrong way — §6c of the audit already showed that; this is the live version.

---

## 5. Recommendation

1. **Do not add the six-short arm queue.** It was built on 3d-only shelves and now conflicts with
   the terminal's own direction on 6 names and grade SKIP on the other 8.
2. **Cancel or re-derive XRP and ADA.** Their stops have 0.23–0.36 ATR of clearance over live
   liquidation clusters — structurally inadequate, independent of direction. If held, resize to
   **4 / 12 contracts** to respect the 1 % cap.
3. **Do not flip to long yet either.** `WATCH_EXHAUSTION` at grade B with `NO_TRADE` on 8 of 14 and
   an ATR squeeze on 39/41 means the bounce is unconfirmed. Wait for squeeze resolution.
4. **Fix the pipeline before the next formulation** — these are the actual defects:
   - `desk_screen_itcls.py` L98 hardcodes `range=3d` (**PIT-16/21 silently violated**)
   - no direction cross-check against `narrative` before geometry is drawn
   - size never checked against total risk vs NLE %
   - `liqlevels` must be pulled sequentially with the 6-slot gate respected, not in parallel

---

## 6. Coverage statement — what full dataset does and does not mean here

**Consumed (100 % of symbols):** narrative, oracle, bookwalls, signals, funding, oi, micro, quant.
**Consumed partially:** market 72 %, cvd 42 %, fp 42 %, liqlevels ~24 %.
**Recorded unavailable:** stopmap, perpdepth, cot, options, liq-live (PIT-11 UNEVALUABLE).
**Spec sections still unexecuted:** §3 8-voice council, §14 D01–D16, §21/22 EA archetypes,
PIT-15 single footprint bar type.

So: **more of the terminal than ever before, but not the entire terminal.** The binding gap is
`liqlevels` at 24 % — and that is the one route the stop geometry depends on. It is rate-limited,
not broken, and a slower dedicated pass can lift it.

---

## Files

- `scripts/trading/full_coverage_formulate.py` — engine (rate-limit aware, 3 passes, per-route coverage)
- `data/restart/formulation_full.json` — 14 scored candidates + coverage table
- `data/restart/raw/<SYM>.json` — full 15-route evidence bundle per symbol
- `data/restart/screener.json` — 231-instrument universe snapshot
- `desk/DATA_COVERAGE_AUDIT.md` — route-by-route audit (unchanged)
