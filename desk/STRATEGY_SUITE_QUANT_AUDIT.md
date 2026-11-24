# STRATEGY SUITE QUANT AUDIT — Red-Team Review of the 4-Strategy Portfolio

**Author:** GLM independent red-team audit
**Date:** 2026-11-24
**Scope:** `htf_ema_rejection/` and `ivwall_alpha/` (fresh audits) + carried verdicts for
`Volatility_compression/` and `ORB/` (audited 2026-11-23, frozen configs re-checked here).
**Data:** `historical_parquet/` — rolling 5,000-bar MT5 exports, NOT "Jan 2023–Nov 2026":
4H ≈ 3.0–3.3 years, 1H ≈ 10 months, 15M ≈ 53 calendar days, 5M ≈ 25 days, 1M ≈ 4 days.
**Account:** Blueberry Markets MT5 #5126976, equity ≈ $2,555.

Audit scripts (self-contained, no dependency on the strategy suites' own code except
where stated): `scripts/suite_audit_htf.py`, `scripts/suite_audit_htf_stress.py`,
`scripts/suite_audit_ivwall.py`. Machine outputs: `scripts/suite_audit_out/`.

---

## 0. Methodology (identical to the ORB / VC audits)

1. **Digits** inferred per symbol (mode of decimal count — never a hardcoded 2-decimal
   fallback; JPY pairs resolve to 3 digits, FX to 5, crypto/indices to their own modes).
2. **Two-sided spread accounting**: the parquet `spread` column (points × 10^-digits)
   charged on **both** the entry bar and the exit bar of every trade.
3. **Enforced non-zero spreads**: every zero-spread bar replaced by that symbol's median
   **non-zero** spread before simulation (the raw export contains 0–63% zero-spread bars —
   see §2). Stress runs at ×1.5 / ×2 / ×3.
4. **Degenerate bars** (zero range) excluded from signal generation.
5. **Chronological 70/30 split by session days.** All parameter/variant selection used the
   in-sample leg only; the out-of-sample leg was evaluated exactly once, after freezing.
6. **Deflated t** = pooled OOS t minus the Harvey haircut √(2·ln N_trials), computed for
   both the suite's own trial count and my audit grid.
7. Selection-integrity test: an independent IS-only selection (plateau/median rules) is run
   alongside the suite's frozen picks; if the independent selection fails to transfer OOS
   while the frozen picks succeed, the edge is hypothesis-specific, not grid-generic —
   reported as such.

---

## 1. Data coverage disclosure (the reports' blind spot)

| TF | Bars/symbol | Actual window | Consequence |
| --- | --- | --- | --- |
| 4H | ~4,200–5,000 | 2020-08 → 2026-11 (XAUUSD), 2023-01+ (gold crosses), 2023-08+ (indices), 2023-09+ (FX majors) | Legit IS/OOS |
| 1H | 5,000 | 2026-01/02 → 2026-11 | ~10 months only |
| 15M | 5,000 | 2026-09-07/09 → 2026-11-20 | **53 calendar days — every 15M statistic is thin** |
| 5M | 5,000 | 2026-10-27 → 2026-11-20 | Day-trading research only |
| 1M | 5,000 | 2026-11-17 → 2026-11-20 | Microstructure only |

Any backtest claimed to span "Jan 2023 – Nov 2026" is only true for 4H. The IV-Wall
15M "validation" rests on 53 days of data; its OOS leg is **16 calendar days, 13 days
with actual trades**.

---

## 2. Cost survey (what the data actually says)

| Symbol class | zero-spread bars | median non-zero | note |
| --- | --- | --- | --- |
| XAU-family 4H (XAUUSD/XAUAUD/XAUEUR/…) | 15–31% | 0.06–0.24 quote-ccy units (~6–24 cents/oz) | ~3× tighter than retail — stress run compensates |
| JPY crosses 4H (USDJPY/EURJPY/GBPJPY/…) | 25–63% | 0.001–0.007 (~0.1–0.7 pip) | **understated vs retail (~1.0–2.5 pip)** |
| BTCUSD 4H | 5.1% | ~$19 | realistic |
| FX 15M basket | 0% | 1–13 pips; exotics: USDZAR 125, USDNOK 81 | enforced as-is; exotics are genuinely wide |
| DJ30.p 1H | 0% | 1.4 pts | realistic |

**Robustness note:** the JPY-major spreads in the export are materially tighter than
live Blueberry retail spreads. Any sleeve whose verdict *depends* on JPY-cross costs is
therefore judged on optimistic data; this is why the ex-gold HTF failure is decisive —
it fails *even at optimistic costs*.

---

## 3. HTF EMA Rejection (P1–P4, EMA 89/233) — fresh audit

### 3.1 Their frozen 15-cell basket, re-run under my cost/split protocol

| Symbol | TF | IS R / PF / n | **OOS R / PF / n** | Verdict |
| --- | --- | --- | --- | --- |
| XAUAUD.pi | 4H | +33.7 / 2.52 / 36 | **+24.9 / 3.25 / 20** | PASS |
| XAUCNH.pi | 4H | +32.6 / 2.24 / 75 | **+23.0 / 2.09 / 57** | PASS |
| XAUUSD.pi | 4H | +16.7 / 1.57 / 52 | **+40.7 / 1.90 / 88** | PASS |
| XAUEUR.pi | 4H | +19.9 / 1.52 / 77 | **+19.2 / 1.71 / 58** | PASS |
| XAUSGD.pi | 4H | +21.2 / 1.88 / 37 | **+19.0 / 2.19 / 26** | PASS |
| XAUGBP.pi | 4H | +20.0 / 1.66 / 68 | **+25.6 / 2.34 / 48** | PASS |
| CHFJPY.pi | 4H | +23.5 / 1.57 / 100 | **−8.0 / 0.68 / 44** | FAIL |
| GBPJPY.pi | 4H | +20.2 / 1.91 / 39 | **−1.6 / 0.88 / 19** | FAIL |
| USDJPY.pi | 4H | +20.5 / 2.02 / 48 | **−3.1 / 0.69 / 19** | FAIL |
| EURJPY.pi | 4H | +23.2 / 1.64 / 53 | **−13.2 / 0.35 / 22** | FAIL |
| CADJPY.pi | 4H | +20.6 / 2.54 / 36 | **−3.1 / 0.70 / 15** | FAIL |
| BTCUSD.pi | 4H | +25.9 / 1.44 / 114 | +2.0 / 1.07 / 47 | FAIL (no edge) |
| GAUUSD.pi | 4H | +18.0 / 1.62 / 59 | +5.1 / 1.56 / 28 | weak pass |
| GAUCNH.pi | 4H | +10.3 / 1.49 / 29 | +8.3 / 1.67 / 18 | weak pass |
| DJ30.p | 1H | +21.4 / 1.91 / 38 | **−7.1 / 0.29 / 11** | FAIL |

Pooled OOS by day: **gold family +165.8R, t = +3.29 (147 days)**; ex-gold **−34.2R,
t = −1.97**. The suite's own disclosure is confirmed and quantified: **the edge is 100%
a gold-cluster phenomenon.** The 10/15 gold members are highly correlated (same trade in
different quote currencies) — effectively **one independent bet**, not ten.

### 3.2 Cost stress on the gold family (their picks, OOS pooled)

| Spread model | OOS sum | t |
| --- | --- | --- |
| ×1 enforced | +165.8R | +3.29 |
| ×2 | +163.8R | +3.25 |
| ×3 | +161.8R | +3.21 |

Cost-robust: 4H gold stop distances (~1.2×ATR) dwarf the spread. The verdict survives
any plausible retail spread.

### 3.3 Selection integrity

My own IS-only conservative selection (288-config grid per symbol, plateau + median rules)
**failed to transfer OOS** on 9/15 symbols (gold pooled +32.9R, t=+1.17). The OOS success
is specific to the pre-specified **P4_cluster + EMA233 + 4H + gold** hypothesis (which the
user's original charts suggested), not to the grid in general. Under worst-case
multiple-testing (86,634 trials, haircut 4.77), gold's raw t=3.29 → **deflated −1.48**;
under the honest prior (the pattern family was specified from 4 charts before the sweep),
the effective trial count is a few hundred and the deflated t is ≈ +1.3…+1.8. Truth is in
between: **real but fragile edge, one bet, regime-dependent.**

### 3.4 HTF verdict

- **ARM (small):** gold-cluster sleeve = XAUUSD.pi, XAUAUD.pi, XAUEUR.pi, XAUSGD.pi,
  XAUGBP.pi, XAUCNH.pi, GAUUSD.pi, GAUCNH.pi on 4H P4_cluster EMA233 — **treated as ONE
  risk unit** (max 1 concurrent gold position in the governor, regardless of symbol).
- **DROP:** CHFJPY, GBPJPY, USDJPY, EURJPY, CADJPY, BTCUSD, DJ30 members (all fail OOS).
- Kill criteria: rolling 30-trade PF < 1.1, cumulative < −10R, or gold below EMA233 4H
  with sleeve net-negative for 20 trading days.

---

## 4. IV-Wall Alpha (20-pair M15 wall-fade) — fresh audit

Their engine, my enforced costs, day-split over the true window
(2026-09-09 → 2026-11-20: IS 37 days / **OOS 16 days, 13 with trades**).

| Leg | n | ΣR | PF | t |
| --- | --- | --- | --- | --- |
| Basket IS | 137 | +56.6R | 1.81 | +2.55 |
| **Basket OOS** | 55 | **+29.8R** | **2.16** | +2.15 (trade-level) |

- **Pooled by day** (the honest unit — 20 FX pairs move on the same macro tape):
  t = **+1.34** on 13 days; **deflated vs their 96-trial search: −1.69 → FAIL** strict accounting.
- **Block bootstrap (5-day blocks): P(profit) = 0.78, 5th pct = −14.6R** — the iid-MC
  numbers in their report (P=0.9999) are optimistic because they ignore cross-pair
  same-day correlation.
- **Spread stress survives**: ×1.5 → +27.9R (PF 2.06); ×2 → +24.1R (PF 1.89). Stop
  distances (wall ± 1×ATR14) are large relative to M15 spreads. Cost model is NOT the
  problem — **sample size and clustering are.**
- **Per-pair selection does not transfer**: IS screen (PF≥1.3, n≥8) picked 7 pairs →
  their OOS = +3.9R, PF 1.37, **t = 0.50**. The edge is basket-level, not pair-level.
- Exotics: USDZAR (median 125 pips), USDNOK (81), EURNZD (13) are wide and add little
  OOS (+1.9R combined) — excluded from the live subset.

### 4.1 IV-Wall verdict

- **ARM (paper / micro-risk only):** majors-and-crosses subset only — CADJPY, EURUSD,
  EURGBP, NZDCAD, AUDSGD, CADCHF, USDCHF, NZDCHF, USDSGD, GBPAUD, GBPNZD, NZDSGD,
  AUDNZD, NZDCAD (exotic trio excluded: USDZAR, USDNOK, EURHKD).
- Live-spread guard in the engine: skip entry if live spread > 1.0×ATR14 at signal (already
  in their rules) **and** if live spread > 1.5× the enforced-model median for that pair.
- Kill criteria: 20-session rolling PF < 1.0 or cumulative < −8R → OFF. Re-audit when
  15M history ≥ 200 sessions.

---

## 5. Carried verdicts — Volatility Compression and ORB (frozen configs re-checked)

- **VC (`vc_evolved_frozen.json`, 192 trials):** XAUUSD.pi|4H and XAUAUD.pi|4H are
  `qualified: false` under the deflated OOS gate; NAS100.p|4H is `qualified: true`
  (OOS +11.97R… IS PF 2.0) but with only ~63 OOS days and negative deflated t in the
  earlier audit. The **only deployable VC sleeve remains the XAUUSD 4H blueprint**
  (BB-in-KC ≥3 bars, close beyond KC band + EMA200 + direction, next-4H-open entry,
  SL = basis distance floored 1.2×ATR, **fixed 4R TP — no trailing**, cooldown 6 bars):
  OOS +25.96R, PF 2.85, t 2.10; paper-trade with the §3.4-style kill criteria.
- **ORB (`evolved_frozen.json`, 704 trials):** NAS100.p OR30@14:30 passed the OOS gate
  (n=13, +21.4R, PF 3.63, t=1.31) but **deflated_t = −2.31**; no ORB configuration has a
  positive deflated t. BTCUSD 15M Asian-range remains unvalidated. ORB is **paper/monitor
  only** at micro risk; do not size it up on 13 OOS trades.

---

## 6. Unified portfolio decision (what the engine arms)

| Sleeve | Magic | Status | Symbols | Risk |
| --- | --- | --- | --- | --- |
| HTF EMA gold cluster | 42003 | **ARMED — live-eligible, $5–6 fixed risk, ONE unit** | XAUUSD.pi + gold crosses (priority order), 4H P4 EMA233 | fixed $5.50 |
| IV-Wall majors | 42002 | **ARMED — micro-risk pilot, 14 majors, exotics excluded** | see §4.1, M15 | fixed $5.00 |
| VC XAUUSD 4H blueprint | 42004 | **ARMED — paper-eligible micro-risk** | XAUUSD.pi 4H | fixed $5.50 |
| ORB NAS100 OR30 | 42005 | **MONITOR — smallest size, 13-trade OOS base** | NAS100.p 15M OR30 @14:30 UTC | fixed $5.00 |
| CG order-flow (external) | 42001 | reserved — not in this engine | — | — |

Global governor: **max 2 concurrent positions portfolio-wide**, max 1 per sleeve, max 1
gold-cluster position (HTF and VC both touch gold), server-side SL/TP on every order,
BE lock at +1R, all signals on closed bars with next-bar-open entries.

**Everything above is still a research pilot.** With ≈$2,555 equity and $5–6 fixed risk
(≈0.22%/trade), a −15R cold streak costs ≈ $85 — survivable, which is the point of the
fixed-risk design. No sleeve graduates to larger risk without: (a) 30+ live trades at
PF > 1.2, (b) realized spread ≤ 1.5× model, (c) re-audit on extended data.
