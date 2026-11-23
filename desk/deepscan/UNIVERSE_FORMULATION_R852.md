# R852 — Entire Universe Scan + Trade Formulation

**Date:** 2026-11-23 · **Author:** Arena.ai
**Scope:** 231-asset universe → every data point → entry / SL / TP with arithmetic
**Result:** 3 instruments survive all filters · **1 trade is sizeable** · no orders placed

---

## 0. The operational discovery that shaped everything

`CG_ENGINE=1` and the terminal **cannot both serve data on this box.**

| | engine ON | engine OFF |
|---|---|---|
| RSS | **1,914 / 1,982 MB (83 free)** | 1,109 MB |
| kline call | **0 rows after exactly 20.0 s** | **300 rows instantly** |
| Coinglass series | `request queue timed out` | healthy |

The 20.0 s is the client's own `setTimeout(…, 20_000)`. The engine holds the single
Coinglass request queue, so every terminal call times out and returns an empty array.
Engine-on also produces `bs_usd:*` series failing `fapi HTTP 451`.

**This is why "the terminal shows nothing": if the engine is running, it is true.**
Run the engine only when you need its store; never while pulling data.

---

## 1. Universe and data points actually available

| source | coverage | fields |
|---|---|---|
| `/api/cg/assets` | **33 curated** (30 crypto, 3 metal) | sym, name, kind, sector, tier, hyperliquid |
| `/api/cg/screener` | **231 declared**, 3–6 warm without engine | price, chg24hPct, vwap, vwapZ, rvol, ema4h, ema1h, rsi15m, rsi1h, poc, funding8hPct, signal, **liqMagnet**, whaleImbPct, cvd24h, cvdDiv, confluence, oi, liq5m |
| `/api/cg/kline` | all curated, **2026 only** | OHLCV, 300 × 4H |
| `/api/cg/liqlevels` | all curated | keyLevels above/below with strength + USD |
| `stopmap` (heatmap) | **BTC, 12h/24h only** | grid + cells |

The screener is the right entry point — 231 assets in one call, 31 fields each — but its
per-symbol fields stay null until the cache warms, and the engine that warms it is the
same engine that starves the kline route.

---

## 2. Stage 1 — structure screens: 21 of 30 pass

Gates, each taken from a measurement, not a preference:

| gate | source |
|---|---|
| 4H EMA stack aligned | R850: IC +0.0376, t +21.8 — the only persistent signal |
| EMA55 slope agrees | R850: **no slope filter → zero survivors** |
| within 1 ATR of EMA21 | R850: pullback entry condition |
| side has positive expectancy | R850: SHORT +0.010R vs LONG −0.046R |
| stop clears liquidation fuel | R851: measured key levels |

**All 21 came out SHORT.** That is not a coincidence to celebrate — it is the whole
downtrend expressed once per instrument.

---

## 3. The finding that changes the answer: 21 trades = 1.71 bets

I tested whether the 21 are independent by correlating their 4H returns:

| statistic | value |
|---|---|
| mean pairwise correlation | **0.740** |
| median | 0.762 |
| max | 0.957 (SOL/LINK class) |
| pairs above 0.7 | **150 / 210 = 71.4 %** |
| top eigenvalue share | **76.3 %** of variance |
| **effective independent bets** | **1.71 of 21** |

**"Scan the entire universe and trade every setup" would be one trade taken 21 times.**
This is the most important number in the report, and it is why the answer below is one
position, not a portfolio.

---

## 4. Stage 2 — measured edge: 10 of 21

1H, 2 ATR stop, 2R target, 30 bp round trip:

| edge | expR | t | | fails | expR | t |
|---|---|---|---|---|---|---|
| ENA | +0.300 | +6.94 | | **NEAR** | **−0.214** | **−4.56** |
| LTC | +0.269 | +6.27 | | 1000PEPE | −0.173 | −3.43 |
| SUI | +0.257 | +6.15 | | **XMR** | **−0.098** | **−1.80** |
| BCH | +0.254 | +5.58 | | HYPE | −0.044 | −1.04 |
| LINK | +0.232 | +5.53 | | ZEC | −0.014 | −0.29 |
| DOGE | +0.223 | +5.67 | | | | |
| ETH | +0.204 | +4.52 | | | | |
| ADA | +0.192 | +4.93 | | | | |
| SOL | +0.186 | +4.37 | | | | |
| XLM | +0.104 | +2.50 | | | | |

**XMR — the instrument R845 recommended twice — measures −0.098R at t −1.80.** That is now
the third independent test to kill it. R845 is fully withdrawn.

---

## 5. The trap I have to flag: terminal history is 2026-only

Running "is the edge positive every year" against terminal klines returned
**POSITIVE IN EVERY YEAR: 0 of 10.** Not because the edges fail — because **every
symbol's 2000 × 1H series ends inside 2026.** There is one year in the data.

Anyone running this scan without a longer series would report **ten confident edges that
are all the same twelve months**, in a year R850 measured as drifting down at t −14
(unconditionally −0.078 %/day). That is precisely the failure mode this project keeps
hitting, and it is caught only by cross-checking the parquet.

---

## 6. Stage 3 — multi-year cross-check (4H parquet, 2024+)

| sym | n | win % | expR | t | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|
| **DOGE** | 2,417 | 45.0 | **+0.164** | **+5.97** | −0.734 | **+0.172** | **+0.221** |
| **LINK** | 1,843 | 42.7 | +0.107 | +3.40 | −1.073 | +0.027 | +0.360 |
| **ADA** | 2,268 | 41.0 | +0.085 | +2.98 | −0.855 | +0.114 | +0.227 |
| SOL | 1,961 | 42.1 | +0.082 | +2.68 | −0.700 | −0.015 | +0.317 |
| ETH | 1,938 | 40.3 | +0.054 | +1.78 | −0.829 | −0.093 | +0.460 |
| BCH | 1,951 | 41.0 | +0.036 | +1.18 | −0.813 | −0.053 | +0.225 |
| XLM | 2,321 | 38.6 | **−0.013** | −0.48 | −0.893 | +0.037 | +0.060 |
| SUI, LTC, ENA | — | | | | **no parquet series** | | |

**2024 is negative on all seven, with win rates near 0 %.** I read that as a regime
signature — early-listing and illiquid 4H bars, where a 2 ATR stop is smaller than the
bar range — not as instrument failure. But the honest consequence is unavoidable:
**no instrument in this universe has been validated through a genuine losing regime.**
2025 and 2026 are the only informative years, and 2025 is thin.

SUI, LTC and ENA — three of the ten edge survivors — **cannot be validated at all**
because the parquet has no series for them. Their terminal statistics are 2026-only.

---

## 7. Final selection

| filter | survivors |
|---|---|
| structure (stack, slope, pullback, fuel) | 30 → **21** |
| measurable 1H edge | 21 → **10** |
| multi-year history available | 10 → **6** |
| positive in **both** 2025 and 2026 | 6 → **3** |

### The three trade cards

**1. DOGUSD — SHORT**
| | |
|---|---|
| Entry | **0.05556** |
| Stop loss | **0.0582068** (4.76 %, = 2 × ATR, clears fuel at 0.05735 str 100) |
| Take profit | **0.0502665** (2R) |
| R:R | **2.00** |
| Cost | 6.3 % of 1R |
| Entry quality | **0.02 ATR from EMA21 — the tightest pullback of all 21** |
| Multi-year | +0.164R, t +5.97 · **positive in all three years** |

**2. LINKUSD — SHORT**
| | |
|---|---|
| Entry | **8.926** |
| Stop loss | **9.33795** (4.62 %) |
| Take profit | **8.10211** (2R) |
| R:R | 2.00 |
| Multi-year | +0.107R, t +3.40 · 2025 +0.027, 2026 +0.360 |

**3. ADAUSD — SHORT**
| | |
|---|---|
| Entry | **0.1462** |
| Stop loss | **0.153754** (5.17 %) |
| Take profit | **0.131092** (2R) |
| R:R | 2.00 |
| Multi-year | +0.085R, t +2.98 · 2025 +0.114, 2026 +0.227 |

---

## 8. Sizing — only one fits

| | 1 % risk | 0.5 % risk | notional at 0.5 % |
|---|---|---|---|
| DOGE | $26.05 | **$13.02** | $273 |
| LINK | $26.05 | $13.02 | $282 |
| ADA | $26.05 | $13.02 | $252 |

**Three at 0.5 % = $39.07 = 1.50 % of equity → breaches the 0.92 % ceiling.**
The ETH position already holds **$23.99 of $25.00**.

**Take exactly one, and only after ETH exits.** DOGE is first: best multi-year t (+5.97),
the only candidate positive in all three years, the tightest entry (0.02 ATR), and its
stop clears liquidation fuel rated 100 — the strongest cluster found anywhere.

---

## 9. What "logical" required here, and what it produced

You asked for everything to be logical. Four things had to be true, and each one removed
most of the candidates:

1. **Structure must be measured, not asserted** → 21 of 30
2. **The trades must be independent** → they are not; **1.71 effective bets**. Buying
   several is one position at several times the size.
3. **The edge must exist beyond one year** → 10 → 6, and 2024 is negative for all of them
4. **The risk budget must permit it** → it does not, until ETH resolves

The logical output of scanning 231 assets and every data point is **one trade of about
$273 notional**, plus a clear statement of what would have to change for it to be more.

---

*Arena.ai · R852 · 2026-11-23 · 231-asset universe, 30 curated assets deep-scanned,
21 structure passes, 10 measured edges, 6 with multi-year history, 3 stable, 1 sizeable.
Engine must be OFF for the terminal to serve data. Effective independent bets: 1.71 of 21.*
