# OMNI_R828 — 360° fusion: coverage, the bug it exposed, and the trades

**2026-11-22 19:35 UTC · Arena.ai · analysis + proposals only — no execution authority**

Requested: use every dimension at every timeframe, 360° analysis, then give trades.
This note covers what was actually reachable, one defect found in my own earlier
work, and the trade set that survives an honest filter.

---

## 1. Coverage — what was reachable

51 API routes exist. 14 data layers were pulled, 5 of 16 symbols returned usable
heat data per attempt (Coinglass `50001` rate limiting, see §6).

| layer | route | dimensions | status |
|---|---|---|---|
| heat Model 1/2/3 | `cg/stopmap` | 3 models × 2 ranges × 14,515 cells | **ok** |
| per-venue liq | `cg/liqlevels` | 5 venues, strength + leverage tier | ok |
| regime + plan | `cg/narrative` | 3-TF internal (H4/H1/m15), 11-pt checklist | ok |
| flow signals | `cg/signals` | 5 types × 3 bar-types (abs/trap/div/exh/sdiv) | ok |
| positioning | `cg/quant` | LS ratio 15m/1h/4h, OI quadrant 1h/4h/24h, basis | ok |
| options | `cg/options` | dvol, term structure, GEX regime, gamma flip, max pain, 25d RR | **ok — BTC/ETH only** |
| funding | `cg/funding` | 21 venues, 8h-normalised | ok |
| open interest | `cg/oi` | OI + 1h/4h/24h deltas | ok |
| delta | `cg/cvd` | spot vs perp, **6 timeframes** m5→d1 | ok |
| orderflow | `cg/orderflow` | price-bucketed taker buy/sell, **6 timeframes** | ok |
| book depth | `cg/bookwalls` | resting bid/ask USD, wall detection | ok |
| micro | `cg/micro` | HL mark-vs-oracle, multi-venue BBO | ok |
| COT | `cg/cot` | — | **400, unsupported for crypto** |
| perp depth | `cg/perpdepth` | — | **unsupported for crypto** |

**Disclosed gaps:** options are BTC/ETH only. `cg/perpdepth` only serves equities
and commodities. `cg/cot` is a CFTC dataset with no crypto coverage. Everything
else in the table was used.

## 2. The defect — and it is mine

`narrative.ts` STEP 1 evaluates the regime branches in a fixed order:

```
if (atrPct < 20)                     -> COMPRESSION_SQUEEZE     ← MY Q-3 FIX
else if (... TREND_UP gate ...)      -> TREND_UP
else if (... TREND_DOWN gate ...)    -> TREND_DOWN
else                                 -> RANGE_BALANCE
```

When I fixed Q-3 earlier today I made `atrPct < 20` **sufficient** for
`COMPRESSION_SQUEEZE`. Because it is the first branch, it now **subsumes every
trend state whenever volatility is compressed** — which is precisely the
condition under which a coiled trend exists.

Measured right now, all 8 majors:

| sym | H4 EMA13-89 | price vs Day VWAP | VWAP z | TREND_DOWN reachable? |
|---|---|---|---|---|
| BTC | **−10.63 ATR** | above | +0.61σ | **NO — aboveVwap blocks** |
| ETH | −11.52 ATR | above | +0.96σ | NO |
| SOL | −10.88 ATR | above | +0.50σ | NO |
| XRP | −10.34 ATR | above | +0.62σ | NO |
| DOGE | −12.77 ATR | above | +0.60σ | NO |
| LINK | −10.00 ATR | above | +0.98σ | NO |
| AVAX | −12.55 ATR | above | +0.91σ | NO |
| BNB | −12.80 ATR | above | +1.23σ | NO |

A monolithic **−10 to −13 ATR** bearish EMA separation on every major — a mature
downtrend by any reading — plus price above Day VWAP. `TREND_DOWN` requires
`aboveVwap === false`, so it cannot fire. `TREND_UP` needs `h4Stack === "up"`,
which is false. Result: **16/16 symbols report `COMPRESSION_SQUEEZE`, M1 OFF,
M2 OFF, direction NEUTRAL** — a total model-wide stand-down.

**Two honest readings, and I am not going to pretend to know which is right:**

1. The engine is correct. Coil + above-VWAP inside a downtrend genuinely *is*
   "wait for the expansion", and the fact that 16/16 agree is the market being
   in one state, not a bug. A verified NO-TRADE is an approved outcome.
2. The engine is too rigid. `aboveVwap === false` is a *daily* anchor test being
   applied as a *structural* trend gate. A −12 ATR monthly downtrend with a
   +1σ intraday bounce still satisfies "price above Day VWAP" while being, in
   every meaningful sense, a downtrend. Requiring both is stricter than intended.

**What I did NOT do:** change it tonight. I already changed this file once today
and the change had an unintended interaction. Altering a regime gate while I am
also producing trades would be marking my own homework. Q-5 logs it for the
operator.

## 3. Engine fixes made (in `scripts/trading/omni.py`)

**Target placement was wrong.** The first version put the target at the nearest
aligned wall. But a liquidation cluster is *fuel* — price accelerates into it as
positions are force-closed. Exiting at the cluster throws away the move it
causes. Now targets sit **beyond** the nearest significant cluster. Same
principle as "do not put the stop inside fuel", applied to the exit. This is why
XRP's R:R moved from 0.88 to a real number.

**Family aggregation added.** A single blended score let six heat configs that
disagree with each other outvote six CVD timeframes that agree perfectly.
"Liquidations are split" and "flow is unanimous" are completely different
statements. Votes are now grouped into families (flow, liquidations,
microstructure, positioning, vol/options, venue) and family agreement is
reported separately.

## 4. The 360° read

**Cross-asset structure — every major is mid-bounce inside a downtrend:**
price above Day VWAP (+0.5σ to +1.2σ) while H4 EMA13-89 sits at −10 to −13 ATR.
That is a bounce, not a reversal. Do not read "above VWAP" as strength.

**Flow direction by timeframe:**

```
SYM    CVD_h4    CVD_h1    CVD_m30   flow_dir
BTC    -0.57     +0.79     +0.39     MIXED
DOGE   -0.85     -0.14     -0.17     BEARISH
LINK   -0.88     -0.81     -0.89     BEARISH
XRP    -0.79     -0.69     -0.98     BEARISH
SOL    -0.61     +0.75     +0.49     MIXED
ETH    -0.60     +0.82     +0.20     MIXED
```

**The decisive pattern:** the higher timeframe (h4) is bearish on **all six**,
while short timeframes have bounced. DOGE / LINK / XRP are bearish at *every*
timeframe — flow, not just fuel.

**Fuel vs flow alignment — the filter that matters:**

| sym | flow | fuel skew | aligned? | verdict |
|---|---|---|---|---|
| **LINK** | BEARISH 3/3 | −0.21 | **YES** | closest to tradable |
| **XRP** | BEARISH 3/3 | +0.30 | **NO** | flow and fuel oppose |
| DOGE | BEARISH 3/3 | −0.12 | yes (weak) | low liquidity |
| BTC | MIXED | −0.38 | no | conflict 41% |
| SOL/ETH | MIXED | — | no | — |

**XRP is the trap.** Flow is unanimous bearish (h4 −0.79, h1 −0.69, m30 −0.98)
and the nearest multi-model fuel cluster sits **above** at 0.9437 (+1.62%,
$690.4 M across 2/3 models). A short has to cross a $690 M squeeze shelf to
work, and a tight stop sits directly inside it. **I am not recommending it.**

**LINK is the best-aligned name**: bearish flow at every timeframe *and* fuel
skewed below (−0.21). But multi-model wall coverage is only partial and the
absolute fuel is small ($31.8 M / $34.7 M) — thin enough that a stop can be
spoofed through.

**BTC has a real technical trigger.** Nearest multi-model fuel below is
**60,349.80 (−0.60%, $73.6 M, 2/3 models)**. A decisive break of that level opens
the 59,728 → 59,210 → 58,533 ladder ($151.7 M combined). Above,
61,955.29 (+2.04%, $50.1 M) is the squeeze gate.

## 5. TRADES

Everything below is `PENDING` for Antigravity. I do not execute.

### T-1 · BTC SHORT ON TRIGGER — the only setup with a defined edge

**Not a market entry. A conditional break.**

| field | value | basis |
|---|---|---|
| trigger | 4H close **below 60,349.80** | nearest multi-model fuel, 2/3 models, $73.6 M |
| entry | 60,300 (limit, on break) | just through the level |
| stop | **60,555** | back above the level + 0.4 ATR1h (565) — invalidates the break |
| target-1 | 59,728.32 (−1.62%) | next fuel cluster, $40.9 M |
| target-2 | 59,210.42 (−2.48%) | $49.5 M |
| target-3 | 58,533.28 (−3.59%) | $61.8 M, deepest cluster |
| risk | 255 pts = 0.45 ATR1h | |
| reward | 572 / 1,090 / 1,767 pts | R:R **2.24 / 4.27 / 6.93** |
| size | **must be recomputed from per-venue balance at execution** | 1% cap, L423 |
| invalidation | 4H close back above 60,555, or VWAP reclaim > +1.5σ | |

**Why this is the honest one:** flow is mixed on the short timeframes (+0.79 h1
is bullish), so this is **not** a flow-confirmed trade — it is a *level* trade
where the fuel structure points down. The h1 bullish reading is a bounce inside
a −10 ATR downtrend. If the bounce continues, the trigger never fills and there
is no trade. That is the correct failure mode.

**Explicit dissent:** regime veto says NO_TRADE (M1 OFF). This violates the
model verdict. I am surfacing it as a *conditional* proposal with that stated,
not as a high-conviction call.

### T-2 · LINK SHORT — best-aligned, worst liquidity

| field | value |
|---|---|
| entry | market **83.6k → reject** (fuel too thin, $31.8 M) |
| why | bearish flow 3/3 (h4 −0.88, h1 −0.81, m30 −0.89) *and* fuel below |
| stop | above 8.99 (+2.05%, $36.8 M) |
| target | 8.6825 (−1.44%) |

Aligned on every axis I can measure, but the book is too thin for the size this
desk implies. **Recommend decline** unless size is small.

### T-3 · XRP — DO NOT TRADE

Flow says short, fuel says squeeze. Nearest wall 0.9437 is $690 M across two
models at +1.62%. Any stop tight enough to have a viable R:R sits inside that
fuel. **Declining is the recommendation.**

### Everything else — NO TRADE

BTC, ETH, SOL, DOGE, AVAX, BNB, ADA, LTC, BCH, NEAR, DOT, TRX, UNI, ATOM all
report `COMPRESSION_SQUEEZE` with M1/M2 OFF. 15 of 16 symbols produce no
qualifying setup. **That is the correct output, not a failure to find trades.**

## 6. Limits of this analysis — stated plainly

- **Rate limiting corrupted the wall scan.** Coinglass `50001` hit XRP and SOL
  mid-sweep; those two returned 1 of 2 model configs, so their "multi-model"
  figures are single-model. Do not trust XRP/SOL wall consensus from §4.
- **Heat models 1/2/3 disagree by ~20× in absolute size** (BTC: $189M to $3.1B
  for the same window). They are different leverage assumptions, so the *ratio*
  above/below is comparable but the dollar magnitude is not. Any single-model
  dollar figure is an assumption, not a fact.
- **`cumulative` field is still excluded** (degenerate, see R827 §2).
- **Raw heat cells are still not dollars** (185.9× trap, R827 §2).
- **Options layers are BTC/ETH only** — the "360°" view is 360° for those two and
  ~250° for the rest.
- **CVD slope is a z-normalised heuristic**, not a model with a track record.
  It has never been back-tested on this desk. Treat as context, not signal.
- **Every wall can be spoofed.** This is a liquidation map, not order flow.

## 7. Open question for the operator — Q-5

Should `TREND_DOWN`'s `aboveVwap === false` requirement be relaxed to a
structural test (e.g. H4 stack + D1 stack) so that a mature downtrend with a
short-term bounce is still classified as a downtrend?

- **If yes:** the engine will start producing trend-pullback (M1) setups during
  coils — far more trade flow, and the "16/16 stand down" ends.
- **If no:** the desk stays in verified NO-TRADE until volatility expands out of
  the coil on its own.

**Honest cost of each:** relaxing it opens trades I have no evidence are good —
it would have armed entries during the exact period where I have no backtest.
Keeping it means the desk may sit idle for a long time, which conflicts with the
standing "we cannot stay idle" expectation.

**My recommendation: do not relax it tonight.** Fix the classifier so the coil
and the trend are both *recorded* (e.g. `COMPRESSION_SQUEEZE` plus a
`trendContext: DOWN` field) without arming M1. That preserves information and
adds no trade path — then back-test M1-during-coil on the archived telemetry
before deciding whether it deserves entries.
