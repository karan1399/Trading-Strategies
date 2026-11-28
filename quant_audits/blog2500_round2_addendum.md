# Addendum (round 2): improvisation ceiling & the 15-20%/month feasibility math

**Author:** GLM · 2026-11-28 · same causal contract, same cost model, same data @ `b1f81d21`.

## A. Round-2 exploration (operator asked to "improvise till 15-20% monthly")

Four more published-variant families were added and re-swept (liquid-only, net of costs):

| family | n | t | PF | n_oos | t_oos | PF_oos |
|---|---|---|---|---|---|---|
| pct_b_pullback | 1242 | 2.14 | 1.26 | 429 | 2.19 | 1.53 |
| double_7s | 90 | 0.41 | 1.15 | 14 | -3.23 | 0.04 |
| gold_overnight | 4934 | 1.06 | 1.05 | 1522 | -0.20 | 0.98 |
| donchian20_trend | 1991 | -1.37 | 0.89 | 709 | 1.96 | 1.34 |

* `pct_b_pullback` (Connors %b) is a genuine OOS survivor and was admitted to the book.
* `double_7s` is dead after CFD costs (PF 0.65); `gold_overnight` is flat OOS — the GLD
  overnight edge does not survive this venue's carry. `donchian20_trend` joins the
  regime-flip trend cluster (OOS-positive, IS-negative).

**4H intraday extension on crypto majors** (2.5 bps spread + per-bar carry):
`ibs_4h` OOS t=+2.42 (PF 1.18, n=1,769) — real but thin; `rsi2_4h` (PF 0.92) and
`swingpb_4h` (PF 0.90) die at intraday cost; `rsi2deep_4h` flat. Intraday blog MR does not
survive this venue's costs; only the daily-bar book is investable.

## B. Maximum causal aggression within desk limits

Configuration C ("aggressive_8slot_gradeA_boundary"): 12 admitted families (round-1 gate +
`pct_b_pullback`, `donchian20_trend`), 37 IS-selected sleeves, 8 concurrent slots, 2 crypto,
0.50% equity daily-vol per position — a 2-sigma adverse move ~= 1.0% equity = the desk
Grade-A cap boundary. Same class notional caps.

| metric | value |
|---|---|
| window | 2019-07-26 → 2026-11-19 |
| final equity (seed $2,501.80) | $15,180 |
| total | 507% |
| **CAGR** | **27.9% = 2.3% monthly** |
| Sharpe | 1.56 |
| maxDD | 13.8% |
| trades / hit | 1219 / 58% |
| OOS sub-window trades / PnL | 331 / $2,079 |
| MC 6-month P5 / P50 / P95 | -2.0% / 9.1% / 25.0% |
| MC P95 drawdown | 9.3% |
| MC P(6m > +5%) / > +10% | 72% / 46% |

This is the honest ceiling achievable from this dataset without fabricating statistics:
~2.1% expected per month at ~14% modeled drawdown. And even this number embeds selection
bias (22 families x 150 symbols searched on 2.3-6.5y histories) that only a sealed forward
test can price.

## C. The 15-20% monthly target — feasibility math (rejection with proof)

15%/month compounds to **435%/year**; 20%/month to **792%/year** (1.15^12 = 5.35x,
1.20^12 = 8.92x). Against the measured causal book:

1. **Leverage route:** configuration C runs at ~28% CAGR. Reaching 435%/yr needs
   ~4x this exposure. Scaling the book 4x scales the 13.8% maxDD toward
   90%+ and the MC P5 toward ruin — variance drag alone (r - sigma^2/2 per period) consumes the
   edge far before compounding can help. It also sits ~4x outside the desk 1.0% Grade-A
   cap. This route is vetoed by desk hard rules and by arithmetic.
2. **Curve-fit route:** tuning family parameters until the *evaluation window* shows 15-20%/month is
   not alpha extraction — it is fitting noise. With 22 families x 150 symbols x parameter grids,
   an optimizer can trivially print any target; those configurations carry zero out-of-sample
   information (the "OOS" window is spent the moment you select on it). I will not produce such a
   sheet, and AUTHORITY's conservative-position-wins rule backs the refusal.
3. **Moonshot route:** the all-class OOS leaders (ENJUSD 4 trades x +4,924 bps, NERUSD 6 x +2,644)
   print spectacular per-trade numbers from 2-8 trade cells in illiquid alt-cryptos. These are
   selection artifacts already excluded from every book.

**What 15-20%/month would actually require:** an edge of ~0.7-0.9% per trade at 30+ trades/month
at near-zero cost — i.e., HFT/tick-level microstructure alpha with colocated execution, or
persistently right convex tail bets with unmodeled risk. Neither exists in a 5,000-bar D1/4H blog
catalog after 1-6 bps costs. Renko, pairs, VIX, and bond families from the catalog were excluded
for exactly this reason (no instruments / no edge after costs on this venue).

**Honest path to compounding:** deploy configuration A (2-slot) or C (8-slot Grade-A boundary) as a
sealed forward shadow; measured live edge then sets the real scaling rate. At configuration C's
modeled Sharpe (1.56), each doubling of capital still holds drawdown within desk limits; the
12-month MC median (+9.1%) is the realistic near-term expectation, with P5 -2.0%.

**Signed — GLM, independent challenge & quantitative review.**
