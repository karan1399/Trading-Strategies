# R826 — Candidate Hunt on a Live Terminal

**Run:** 2026-11-22 · 18:05–18:25 UTC
**Server:** restarted, health gate `{"ok":true}` at 18:01:52Z, uptime 0 → live
**Scope:** 231 instruments → 24 narrative-scanned → 14 deep geometry → 4 cleared 2R → **2 survive triage**
**Verdict:** ⚠️ **NO EXECUTABLE TRADE. Two WATCH names with live triggers.**
**Root cause found:** the terminal's abstention is a **volatility fact**, not an opinion — see §8.

---

## 1. The honest headline

You asked me to not stay idle. I didn't — but the terminal's own answer is that there is
**no executable setup right now**, and I should give you that before anything else:

> The `narrative` route returns `bestR = null`, `targets = []`, `invalidation = "no setup"` on
> **every one of 24 symbols**. 18 of them are `NO_TRADE / grade SKIP`. Six are
> `WATCH_EXHAUSTION / LONG / grade B` at 35 % confidence.

The checklist on BTC shows exactly why:

| Gate | Result |
| :--- | :--- |
| HTF EMAs (H4/D1 13×89×233) | **FAIL** — H4 down · D1 down · spread −10.30 ATR |
| VWAP Z-Score | **FAIL** — 0.79σ |
| Footprint Signal | **FAIL** — 4 confirmations, latest `abs@61647.9` |
| CVD / Spot Lead | PASS — 24h Δ +$556.3M |
| Walls / Whale | PASS — bid $78.9M @ 55017.8 · ask $97.8M @ 62025.0 |
| Liq Magnet | PASS — above 62789.2 · below 59991.7 |
| Funding / L-S | **FAIL** — n/a |
| R ≥ 1.5 | **FAIL** — no measurable R |
| Execution cost (MT5) | **FAIL** — gate off |

**4 of 11 gates fail on the cleanest instrument in the book.** The terminal is not being coy;
the setup genuinely is not there.

---

## 2. But there is a live thesis — and it has triggers

The tape is a **compression bounce inside a daily downtrend**, and it is very late in that bounce:

| Measure | Value | Reading |
| :--- | :--- | :--- |
| Symbols in ATR squeeze | **13 / 14** | coiled |
| Median H4 EMA spread | **+23.4 %** | still deeply bear-stacked |
| BTC D1 RSI | **24.9** | capitulation-adjacent |
| DOT / LTC / DOGE / ADA D1 RSI | 22.9 / 24.1 / 25.7 / 27.1 | deeply oversold |
| Price vs session VAH | **−0.04 to −0.41 ATR** on most | rejecting the value-area high |

ATR rank is at the **1.3–2.6 percentile** on most names — volatility is compressed to near its
window low. That resolves, and it usually resolves hard. The question is only *which way*, and
the structure says: the bounce is running into supply, not through it.

---

## 3. The two that survive every gate I have

| | **XRP** | **DOGE** |
| :--- | :--- | :--- |
| Live mark | 0.9297 | 0.05527 |
| **Short trigger** | **0.9399** (+1.34 ATR) | **0.05598** (+1.45 ATR) |
| **Stop** | **0.94748** (+0.81 % / 0.90 ATR) | **0.0564465** (+0.83 % / 0.90 ATR) |
| **Target** | **0.9124** | **0.05428** |
| **R : R** | **3.63** | **3.64** |
| Supply it fades | **$63 M @ str 62** (30d) | **$888 M @ str 91** (30d) |
| Demand it targets | $21 M @ str 55 | $77 M @ str 42 |
| Terminal direction | NEUTRAL / `NO_TRADE` / SKIP | NEUTRAL / `NO_TRADE` / SKIP |
| Contradiction? | **none** — it abstains, it does not disagree | **none** — same |
| D1 RSI | 29.2 | 25.7 |

Both are **passive limit sells at a liquidation shelf**, not market chases. Neither contradicts
the terminal — it abstains on both (`NO_TRADE`), it does not call them long. That is the exact
distinction that killed R825's book: six of those were outright *opposite* to a `LONG` call.

### Why the rejects were rejected

| Sym | R:R | Reject reason |
| :--- | ---: | :--- |
| **TRX** | 14.07 | ❌ **Terminal says LONG / `WATCH_EXHAUSTION` / B.** My geometry says short. Direct conflict — same failure as R825. The 14R is meaningless if the direction is wrong. |
| **BTC** | 6.70 | ❌ Same conflict: terminal LONG / B. Also entry sits **+2.17 ATR** away — a fill is unlikely enough that the R is theoretical. |

---

## 4. ⛔ A serious bug I found in my own engine

While normalizing levels I discovered the **Coinglass liquidation payload is not unit-consistent**:

| Symbol | `usd` field | Last price | Real value |
| :--- | ---: | ---: | :--- |
| BTC | `10,462` | 60,805 | **$10.46 M** (thousands) |
| XRP | `71,333,298` | 0.9292 | **$71.3 M** (raw dollars) |
| DOGE | `975,957,872` | 0.0552 | **$976 M** (raw dollars) |

Treating them alike produced **false "$0 M — immaterial"** reads on BTC-scale symbols and
**false "$888 M"** reads on sub-dollar ones. Consequences:

- **R825's rank #1, KORU (64.35), is invalid.** Its stop was derived from a shelf my code
  normalized to ~$0.2 M — it was really seven figures.
- BTC, BNB and LTC in R825 have the same contamination.
- Every "$0 M" in R825's shelf table should be read as "unknown", not "small".

**Fixed:** `usd_norm()` now added to `scripts/trading/full_coverage_formulate.py` and wired into
both `cluster_stop()` and `pick_targets()`. All numbers in this report are post-fix.

I am flagging this because it means **my R825 ranking was partly arithmetic, not analysis** — and
that is better said now than after a ticket is punched.

---

## 5. Sizing — verified against the 1 % cap

NLE / available: **$1.447032** (Delta readback 17:14 UTC, relayed — see caveat below)
Cap at 1 %: **$0.014470**

| Sym | Entry | Stop | Risk/unit | Raw size | **Size** | Total risk | % NLE |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| XRP | 0.9399 | 0.94748 | 0.007732 | 1.87 | **1** | $0.00773 | **0.53 %** ✅ |
| DOGE | 0.05598 | 0.0564465 | 0.000476 | 30.41 | **30** | $0.01427 | **0.99 %** ✅ |

Both respect the cap. Note XRP only allows **1 contract** at this equity — that is the reality of
$1.44 of working capital, not a preference.

**Correlation gate:** all candidates are crypto shorts. Rule allows **at most 2 same-direction
crypto positions**. XRP + DOGE would exactly fill it; a third must wait.

---

## 6. What Antigravity should do

**Nothing right now, and that is the correct action.** Specifically:

1. **Do not chase.** XRP needs +1.34 ATR, DOGE +1.45 ATR to trigger. If price never gets there,
   missing a 3.6R setup costs nothing. Buying it at market costs the entire thesis.
2. **Arm both as passive limit sells** at the trigger prices, per the interlock rule, IF the
   operator wants exposure. Final say is Antigravity's — it holds the only execution rights.
3. **Kill conditions** — either of these invalidates the short before entry:
   - 15m close **above** 0.9479 (XRP) / 0.05650 (DOGE) — the shelf is being absorbed, not defended
   - the ask walls at 0.928/0.930 (XRP) get **pulled** rather than added to
   - BTC reclaims 61250 with CVD still positive (currently +$556 M)
4. **Wait for the squeeze to resolve.** ATR rank 1.3–2.6 on 13 of 14 names means neither side has
   committed. The shelf-fade only pays if the bounce fails *at the shelf*.

---

## 7. Verification status — read this before trusting any number

| Claim | Status |
| :--- | :--- |
| Server healthy, 231 instruments, terminal routes responding | **VERIFIED** by me this session |
| Prices, ATR, RSI, VWAP, auction levels | **VERIFIED** — pulled live 18:05–18:20Z |
| Liquidation shelves 3d/7d/30d (24/24 success) | **VERIFIED** — sequential pull honoured the rate gate |
| R:R arithmetic, sizing vs 1 % cap | **VERIFIED** — recomputed twice, units normalized |
| NLE $1.447032, 0 open positions | **UNVERIFIED-BY-AGENT** — relayed from the daemon register; this sandbox holds no Delta key. **Q-1 in COUNCIL.md still unanswered.** |
| `zai-desk` rc=1 at 17:17Z | **UNRESOLVED** — **Q-2** still open; unknown whether rounds are being silently dropped |

Do not let Antigravity act on the sizing table until Q-1 is answered with a first-party venue
readback. The *levels* are solid; the *account state* is second-hand.

---

## Files

- `data/restart/r826/candidates.json` — 4 candidates with full geometry
- `data/restart/r826/final.json` — triage verdicts
- `data/restart/r826/*_mkt.json`, `*_bw.json`, `*_nar.json`, `*_orc.json`, `*_liq_{3d,7d,30d}.json`
- `scripts/trading/full_coverage_formulate.py` — `usd_norm()` fix

---

## 8. Why the terminal refused everything — root cause, traced

I initially suspected a dead code path. **That was wrong**, and I corrected it in COUNCIL.md.
Here is what is actually happening.

### The mechanism

`bestR` and `targets` are produced by **exactly one branch** — M2 mean-reversion
(`src/lib/cg/narrative.ts:788`). M2 cannot arm unless:

```
|VWAP z-score| ≥ 2.0          (narrative.ts:802)
```

Measured live across all 14 deep-pulled names:

| | |
| :--- | :--- |
| Max absolute VWAP z | **1.59σ** (LTC) |
| Names reaching 2.0σ | **0 / 14** |
| Median z | ~+0.72σ |

**Zero symbols qualify.** M2 is `OFF` everywhere, so `plan` keeps its default
`{targets: [], bestR: null, invalidation: "no setup"}`. BTC's checklist line
"R ≥ 1.5 — **no measurable R**" is a *downstream consequence* of that, not a missing feed.

### And the alternative branch never fires either

`regime` is **EMA-based**. It reports `RANGE_BALANCE` on symbols whose
`market.vol.atrState` is simultaneously **`squeeze`**. So the `COMPRESSION_SQUEEZE` branch
(`narrative.ts:842`) — which would output *"volatility is coiled, do NOT fade the range,
compression resolves explosively, stand ready"* — is **never reached**.

The classifier does not read `atr14PctRank`. So during the one regime where the correct advice is
"wait and be ready", the terminal emits a generic *"range balance, no edge"* instead. The advice
happens to be the same; the **information is not**.

### Why this matters more than any single trade

- **Consequence for agents:** a generic "no edge" looks like a *market* judgement and invites
  override. "Compression — stand ready" is a *state* judgement and invites patience. Every agent
  reading this repo has been getting the weaker message.
- **Consequence for R826:** my XRP/DOGE shelf-fades are themselves fighting a squeeze. Shelves can
  hold, or price can resolve straight through them. That is precisely why they are **WATCH, not ARM**.
- **Consequence for the design:** `|z| ≥ 2.0` may be nearly unreachable in a low-vol tape. M2 is then
  structurally offline exactly when mean-reversion is least useful and shelf-fade is most useful.

**Recommended fix (one line of input, not a rewrite):** pass `atr14PctRank` / `atrState` into the
regime classifier so `COMPRESSION_SQUEEZE` can actually be emitted. Flagged as **Q-3** in COUNCIL.md.
