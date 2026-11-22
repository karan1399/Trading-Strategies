# Data Coverage Audit — what the formulation actually consumed

> **Question asked**: "are you using the entire data from the terminal to build a trade formulation?"
> **Answer**: **No.** The terminal exposes **37 `/api/cg/*` routes**; the formulation pipeline bound to roughly **8 of them**, and two of the omissions are material — one of them puts a live order's stop inside a 30-day liquidation cluster.
> Audited 2026-11-22 16:20 UTC against the running terminal.

---

## 1. Route-by-route accounting

### 1a. What the dumps / screener / my Table-7 actually consumed

| Route | Used? | Where |
| :--- | :---: | :--- |
| `market` | ✅ | dumps, Table-7 F1/F2 |
| `signals` | ✅ | dumps, F3 |
| `footprint` | ✅ | dumps, tracker |
| `bookwalls` | ✅ | dumps, F4 |
| `liqlevels` | ⚠️ **3d only** | dumps, screener — see §3 |
| `cvd` | ✅ | dumps, F5 |
| `ticker` | ✅ | spot references |
| `kline` | ✅ | ATR, near-miss resolver |
| `screener` | ✅ | regime read (231 rows) |
| `micro` | ◐ | spread + funding proxy (Hyperliquid only) |

### 1b. **Available, working, and never used** — the gap

| Route | Status | Spec gate it serves | Consequence of omission |
| :--- | :--- | :--- | :--- |
| **`oracle`** | ✅ OK | **§15 ATHENA ORACLE** — weighted 8-factor verdict | Never consulted. For XRP it returns **39/100 "Cautious bearish lean — flow fading, whales offering"**, including a **+$5.0 B ask-side whale stack** that my F4 (book walls, $24 M scale) could not see. **Directional corroboration of my short, but I did not have it when I formed the trade.** |
| **`liq-live`** | ⚠️ partial | **PIT-11** `Liq 5m ≤ $500 k` cascade ban | `ok:true`, 87 events stored — but per-venue `events: 0` for binance/bybit, only `okx` reporting (last event `16:21Z`). **The cascade gate was never actually evaluated on any candidate.** |
| **`stopmap`** | ❌ blocked | **PIT-16** cold-zone SL beyond 3D/7D/30D hazard | **`40000` — requires `CG_SESSION`.** The full heat-map hazard check was impossible in this environment, and I substituted the 3-day key-level proxy **without saying so in the trade cards**. |
| **`funding`** | ✅ OK | Table-7 F7 | I used a **Hyperliquid proxy**, then Delta. The terminal's own multi-venue funding (XRP: Binance +0.0015 %, OKX +0.0049 %/8h) was never pulled. |
| **`oi`** | ✅ OK | Table-7 F7 alt-flow, S4 rising-OI | **Never used.** No open-interest confirmation on any candidate. |
| **`largeorder`** | ✅ OK | **PIT-15**, Tab 11 executed size, VPOF-01 big-trades convergence | 296 rows available for XRP. **Never used** — so "institutional distribution confirmation" in my write-up rests on footprint *signals*, not actual large executions. |
| **`orderflow-agg`** | ✅ OK | Coinglass aggregated method (multi-venue, the lead method) | **Never used.** My footprint was Binance-led single-source. |
| **`fp`** | ✅ OK | footprint buckets | Never used |
| **`paperbook`** | ✅ OK | desk Virtual Book — agent-setup paper outcomes | **Never used.** Would have given a *forward* test of my own formulations instead of theory. |
| **`backtest`** | ✅ OK | route exists | Never used |
| **`cot`** | ✅ OK | XAU COT positioning (§TradFi) | Never used — relevant to XAU |
| **`cnbc`** | ⚠️ needs `syms=` | **±2-min news freeze** (MT5-01) | I never verified the news freeze on any candidate. |
| **`options`** | ✅ OK | Table-7 **F7 GEX** (BTC/ETH only) | Never used. BTC DVOL 55.7, 25Δ RR −10.46 — would have informed the BTC short I listed at 7.88. |
| **`classic`** | ⚠️ needs `sym` | SPX500/UKO/NDX100 | N/A — outside the 58 universe |
| **`alert-rules`, `narrative`, `notifications`, `engine`, `stream`, `debug`, `assets`, `perpdepth`, `livebook`, `live`, `paperbook`** | ✅ | ops/plumbing | Not analysis inputs by design |
| `risk` | ✅ | portfolio/correlation/beta | Used only for position inventory; **correlation & beta arrays came back empty** |

**Score: ~10 of 37 routes used; of the 13 analysis-bearing routes I skipped, 9 were live and healthy.**

---

## 2. Spec sections I did not execute

| Spec | Requirement | Status |
| :--- | :--- | :--- |
| §3 · 8-voice desk council | Eight independent persona positions + Arbiter ruling per candidate | **Not run** — my Table-7 was effectively a single-voice score |
| §8 · 6-step DECIDE framework | Formal 6-step decision record | **Not run** |
| §10 · 11 Socratic gates + 10 pitfall inquests | Must enumerate | **Partially** — I applied 5 PIT rules (05/06/11-partial/16-partial/22), not the full 22 |
| §14 · 16-point telemetry audit sheet | Per-candidate D01–D16 | **Not run** |
| §15 · Athena Oracle | Consult `oracle` route | **Not run** ← available, see §1b |
| §21/§22 · SSRN order-flow archetypes EA-1…EA-6 | Map candidates to archetypes | **Not run** |
| PIT-15 · zero tab omission | Audit *all* footprint combos (time/volume/range/diagonal), LTO intensity, **3D/7D/30D**, CVD | **Failed** — 3D only, one footprint bar type |
| PIT-16 · cold-zone SL across 3D/7D/30D | Stop beyond *all* shelves | **Failed** — see §3 |
| §16 · 40/40/20 tranche ladder | T1/T2/T3 with BE at +1R | **Partial** — I specified T1/T2 + runner but the live tickets carry **a single TP** (`targets: [0.9066]`), not a tranche ladder |
| §11 · P5-11 running-trade 360° | Re-audit SL against 3D/7D/30D shelves | **Failed** — same root cause |

---

## 3. ⚠️ MATERIAL — the stop sits inside the multi-range shelf

`scripts/desk_screen_itcls.py` line 98 hardcodes `liqlevels?...&range=3d`. My formulation inherited it. Re-pulling **7d and 30d** changes the picture:

### XRP — stop 0.94452, entry 0.9410

| Range | Short-liq levels between entry and stop+2.5 % | Nearest hard level |
| :--- | :--- | :--- |
| 3d | 0.9410, 0.9431, 0.9488, 0.9503 | **0.9431 · $25.95 M · strength 100** |
| 7d | 0.9418, 0.9454, 0.9497, 0.9511 | **0.9454 · $36.49 M · strength 100** |
| **30d** | 0.9399, 0.9456, 0.9500, 0.9514, 0.9644 | **0.9456 · $102.7 M · strength 100** |

- The stop **0.94452** sits **0.09 % below a $102.7 M, strength-100 short-liquidation level at 0.9456** on the 30-day map, and **0.05 % below the $36.5 M 7-day level at 0.9454**.
- That is precisely the configuration **PIT-21** was written from: *"Directional correctness is completely futile if stop-loss placement is trapped within the liquidity sweep hunt radius."* The SOLUSD case in the ledger was swept by **0.08 %** before collapsing to target — this is a **0.05–0.09 %** margin. Same anatomy.
- **PIT-16 requirement**: stop must be beyond the *entire multi-exchange shelf across 3D, 7D, 30D*. On XRP that means beyond **0.9514–0.9644**, i.e. a stop of ~0.9520 at the nearest — which widens risk from 0.37 % to **1.17 %** and collapses R from 9.79 to **~2.9** (still passes ≥2R, but the headline number changes materially).

### ADA — stop 0.14993, entry 0.14870

| Range | Levels between entry and stop+2.5 % | Nearest |
| :--- | :--- | :--- |
| 3d | 0.1487, 0.1488, 0.1506, 0.1509 | **0.1509 · $26.25 M · str 100** |
| 7d | 0.1488, 0.1493 | 0.1493 · $20.34 M · str 53 |
| **30d** | 0.1490, 0.1504, **0.1509** | **0.1509 · $68.07 M** and **0.1504 · $77.14 M** |

- ADA's stop 0.14993 is **0.31 % below the 30-day 0.1504 level ($77.1 M)** — better than XRP but still **inside** the sweep radius by the PIT-21 standard.
- The 3-day map actively *encouraged* this stop (nearest level 0.1509 appeared safely beyond) while the 30-day map shows **two** hard levels in the gap.

**This is the single most important output of the audit.** Both live tickets were sized on a 3-day view of a 30-day hazard field. Before either fills, the stops should be re-derived against the 7d/30d shelves — or the orders stood down.

---

## 4. What I would change (concrete)

| # | Change | Why |
| :-- | :--- | :--- |
| 1 | **Loop 3d/7d/30d** in `desk_screen_itcls.py`; stop = beyond **max** shelf across all three | Fixes PIT-16/PIT-21 on both live tickets |
| 2 | **Add `oracle`** to the candidate bundle | 8-factor weighted verdict + whale magnitudes 100× larger than book-wall scale |
| 3 | **Pull `liq-live` per candidate** and record `Liq 5m` explicitly | PIT-11 is currently unfalsifiable |
| 4 | **Pull `funding` + `oi` multi-venue** instead of proxies | F7 done properly; OI gives S4/S10 confirmation |
| 5 | **Pull `largeorder`** for executed big-trade convergence | VPOF-01 requires actual executions, not just footprint signals |
| 6 | **Pull `cnbc?syms=`** before any order | MT5-01/Delta news freeze is unverified |
| 7 | **Wire `paperbook`** to log every formulation | Turns formulations into a forward-test scoreboard |
| 8 | Set `CG_SESSION` or explicitly record `stopmap` as **UNAVAILABLE** in trade cards | Currently a silent substitution |
| 9 | Emit **T1/T2/T3 tranche brackets**, not a single TP | §16 40/40/20 |

Routes 2–7 are all live and unblocked — this is an *effort* gap, not a data-availability gap.

---

## 5. Straight answer

> Did I use the entire terminal? **No — roughly a quarter of it, and I omitted the multi-range liquidation check that the spec's own PIT-16/PIT-21 make mandatory.** Nine analysis routes were healthy and unused. One of them (`oracle`) independently corroborates my direction. Two of them (`liqlevels` 7d/30d) show both live stops sitting inside 30-day liquidation clusters by 0.05–0.31 %.

The direction call was right and the process around it was **incomplete in a way that specifically compromises stop placement** — which is the exact failure mode the PIT-21 forensic case was written about.

---

## 6. trading-skills integration — what it caught immediately

Installed 19 `SKILL.md` packages at `skills/` (see `skills/INSTALLED.md` for the gate mapping).
Ran its dependency-free helpers (`skills/_lib/calculations.py`) against my six candidates.

### 6a. ⚠️ **Both live Delta tickets breach the 1.0% equity cap**

TAB7-GATE: Grade A authorises risk **≤ 1.0% of Net Liquid Equity**, which §9 itself illustrates as
*"≤ $0.028 USD on ≈ $2.77 equity"*. At NLE **$1.63** the cap is **$0.0163**.

| Ticket | Contracts | Risk/contract | **Total risk** | **% of $1.63 NLE** | Verdict |
| :--- | ---: | ---: | ---: | ---: | :--- |
| XRP #1705868028 | 10 | 0.00352 | **$0.0352** | **2.16 %** | **OVER CAP (2.2×)** |
| ADA #1705868424 | 20 | 0.00123 | **$0.0246** | **1.51 %** | **OVER CAP (1.5×)** |
| SOL (if routed) | 1 | 0.5482 | $0.5482 | 33.6 % | far over |

**Root cause of my error**: I verified risk *per contract* against the **$0.50 per-entry ceiling**
(all passed easily) and never multiplied by contract count nor divided by equity. The skill's
`fixed_fractional_position_size()` does exactly that division and fails loudly. This is a genuine
process defect in my R821/R824 formulation — the direction call is unaffected, the **size is wrong**.

### 6b. Friction haircut is small but non-zero

| Sym | R@T2 gross | R@T2 net of 1-tick slippage | Haircut |
| :--- | ---: | ---: | ---: |
| XRP | 9.77 | 9.48 | −3.05 % |
| LINK | 6.57 | 6.34 | −3.58 % |
| ADA | 7.72 | 7.65 | −0.91 % |
| DOGE / SOL / AVAX | 5.02 / 7.02 / 11.81 | ≈ unchanged | <0.5 % |

Confirms the R-multiples survive maker fees + one tick; the *stop distance* is the fragile part, not the ratio.

### 6c. Break-even win rates — a sanity anchor

| R@T2 | Break-even win rate |
| ---: | ---: |
| 9.79 (XRP) | **9.3 %** |
| 7.76 (ADA) | **11.4 %** |
| 5.02 (DOGE) | **16.6 %** |
| 3.39 | 22.8 % |

The published R-multiples tolerate a terrible hit rate — which is precisely why the
**stop-inside-shelf** problem from §3 is the real risk. A stop that gets swept turns a 9.3 %-needed
win rate into a certainty of loss; the ratio cannot rescue bad stop geometry.

### 6d. Kelly, honestly applied

At an *assumed* 40 % win rate and 5R average win, raw Kelly is **+0.280**, half-Kelly **+0.140**.
But I have **no measured win rate** for this archetype on this desk — the near-miss calibration
(§1 of R824) is the only forward evidence and it has **n = 0 resolved**. So Kelly is context, not a
sizing instruction; the binding constraint remains the 1.0 % cap in 6a.

### 6e. Verdict on the skills repo

- **Useful, keep.** It is *process*, not signals — it will not find a trade, but it caught a live
  sizing breach on its first run, which our own spec arithmetic missed.
- **Best fits**: `position-sizing` (6a), `risk-reward-sanity-check` (6b), `post-trade-review` +
  `journal-pattern-analyzer` (our near-miss ledger is exactly a journal), `execution-plan-check`
  (PIT-18/22 restated cleanly), `market-regime-analysis` (adds breadth/leadership dimensions we
  do not measure).
- **Not useful here**: `live-trade/etoro`; the `FMP`/`Polygon`/`TradingEconomics` provider refs
  (need keys — point them at our own routes instead).
- **Does not replace** Table-7, PIT-16/21/22, or the ITCLS archetype. It is a second opinion layer.

## 7. Corrected action list (supersedes §4)

| # | Action | Priority |
| :-- | :--- | :--- |
| 1 | **Reduce or cancel XRP/ADA size** to ≤1 % NLE ($0.0163) — currently 2.16 % / 1.51 % | **BLOCKING** |
| 2 | **Re-derive stops against 7d/30d shelves** — XRP stop is 0.05–0.09 % under a $102.7 M level | **BLOCKING** |
| 3 | Loop `liqlevels` 3d/7d/30d in `desk_screen_itcls.py` (line 98 hardcodes 3d) | High |
| 4 | Add `oracle`, `liq-live`, `funding`, `oi`, `largeorder`, `cnbc` to the candidate bundle | High |
| 5 | Record `stopmap` as UNAVAILABLE (needs `CG_SESSION`) instead of silent substitution | High |
| 6 | Wire `paperbook` to log every formulation as a forward test | Medium |
| 7 | Emit T1/T2/T3 tranche brackets (live tickets carry a single TP) | Medium |
