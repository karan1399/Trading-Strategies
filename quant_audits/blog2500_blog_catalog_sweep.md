# Blog-Catalog Strategy Sweep & $2,500 Portfolio Proposal (GLM audit `b1f81d21`)

**Author:** GLM (independent quantitative reviewer — propose & audit only, no execution authority)
**Date:** 2026-11-28 · **Base:** `karan1399/Trading-Strategies` main @ `b1f81d21` · **Data:** `historical_data/mt5_parquet` (4H+1H, 151 symbols, 5,000 bars/TF)

## 1. Scope & method

The operator supplied 456 quantifiedstrategies.com article URLs and the MT5 historical store, asking which
blog strategies still generate alpha on this broker's instruments and how to combine survivors into one
$2,501.80 account portfolio (MT5 `#5126976`).

The 456 URLs were clustered by keyword into canonical families: **167 articles map to 18 testable
families**; 214 are non-actionable on this inventory (VIX, bonds, single-stock/ETF-specific, seasonals,
commodity-specific, meta/how-to); 75 slugs are variant duplicates of tested families (verified by
inspection). All 18 canonical families were implemented once, exactly as published, with **zero parameter
search** — no optimization was performed in this sweep.

Causal contract (hard): signals evaluated at bar-t close, entries fill at bar t+1 open, exits at t+k open
(or the defined close of the holding bar); bracket exits use worst-case ordering (stop beats TP on ties);
costs = class spread + 1 bp slippage + nightly carry. D1 bars are resampled from 4H (UTC). Per-symbol IS/OOS
split: first 70% / last 30%.

**Data honesty:** every symbol holds exactly 5,000 4H bars — 2.3y (crypto), ~2.3-3y (majors/indices),
up to 6.5y (some FX/metals). The blog's 20-year equity curves are NOT reproducible here; these are
short-history validations with wide confidence intervals.

**Cost assumptions (UNVERIFIED-BY-AGENT, calibrated from the 1H spread column):** round-trip spread bps —
fx_major 1.0, fx_cross 2.0, fx_exotic 6.0, precious 1.2, industrial 4.0, index 3.0, energy 5.0,
crypto_major 2.5, alt 6.0; carry bps/night 0.1-0.3 (FX/gold) to 2-3 (crypto CFDs); +1 bp slippage per
round trip. The 4H spread column is 0 on many FX rows (known repo defect), so costs are class-level
assumptions, not per-bar data.

## 2. Family results (liquid classes only, net of costs)

Liquid universe = fx majors/crosses, indices, precious metals, crypto majors, energy (excludes 43
alt-cryptos where spread+carry assumptions exceed most per-trade edges).

| family | n | mean bps | t | PF | n_oos | mean_oos | t_oos | PF_oos |
|---|---|---|---|---|---|---|---|---|
| ibs | 15975 | 6.5 | 4.02 | 1.12 | 4930 | 9.6 | 3.44 | 1.20 |
| swing_pullback_ma | 4259 | 13.4 | 2.66 | 1.15 | 1272 | 21.2 | 2.38 | 1.25 |
| ema9_pullback | 4188 | 11.6 | 2.00 | 1.11 | 1274 | 21.8 | 2.19 | 1.22 |
| tom | 4924 | -4.9 | -1.11 | 0.94 | 1431 | 15.2 | 1.86 | 1.21 |
| high52_momentum | 883 | 32.4 | 1.92 | 1.35 | 393 | 43.6 | 1.44 | 1.54 |
| donchian55 | 1382 | 35.1 | 1.11 | 1.14 | 422 | 60.5 | 1.31 | 1.27 |
| donchian20 | 2957 | -9.7 | -0.49 | 0.95 | 874 | 31.2 | 1.21 | 1.17 |
| ema_cross | 1158 | 18.2 | 0.60 | 1.08 | 316 | 56.4 | 1.10 | 1.27 |
| three_down_overnight | 7898 | 8.5 | 4.46 | 1.22 | 2331 | 3.4 | 1.02 | 1.08 |
| connors_r3 | 584 | 24.1 | 1.93 | 1.37 | 207 | 19.4 | 1.01 | 1.30 |
| rsi2_mr_trend | 1843 | 15.2 | 2.54 | 1.23 | 645 | 7.8 | 0.72 | 1.11 |
| rsi5_mr | 562 | 16.8 | 1.35 | 1.23 | 198 | 11.8 | 0.64 | 1.16 |
| vc_squeeze | 2561 | -15.2 | -2.30 | 0.86 | 804 | -1.0 | -0.09 | 0.99 |
| rsi2_short_extreme | 760 | -6.5 | -0.45 | 0.93 | 275 | -5.3 | -0.23 | 0.95 |
| williams_r | 880 | 42.0 | 2.23 | 1.30 | 304 | -15.7 | -0.39 | 0.92 |
| gold_3day | 462 | 7.2 | 0.44 | 1.06 | 122 | -36.8 | -0.76 | 0.84 |
| rsi2_mr_deep | 2904 | 32.3 | 4.86 | 1.42 | 882 | -17.8 | -1.39 | 0.83 |
| three_down_swing | 3486 | 12.4 | 2.38 | 1.16 | 1061 | -27.6 | -3.09 | 0.72 |

Key findings:
1. **Robust (positive IS and OOS):** `ibs` (n=15,975 liquid, t=4.0; OOS t=3.4, PF 1.20, positive on 93/150
   instruments), `swing_pullback_ma` (OOS t=2.38), `ema9_pullback` (OOS t=2.19), and `connors_r3`
   (OOS PF 1.30). These are the only blog families whose OOS edge is broad-based rather than concentrated.
2. **Decayed:** the blog's classic mean-reversion jewels — `rsi2_mr_deep` (IS t=4.9, OOS t=-1.4),
   `williams_r` (IS t=+2.2, OOS t=-0.4 with OOS mean -15.7 bps), `three_down_swing` (OOS t=-3.1) —
   flipped negative OOS across liquid instruments. `rsi2_mr_trend` survives only marginally (OOS PF 1.11).
3. **Regime-flip (IS-negative, OOS-positive) — treat with suspicion:** `donchian20/55`, `ema_cross`,
   `tom`, `high52_momentum` are OOS-positive mostly through precious metals (XPT/XPD/XAU: OOS class
   means +314/+863/+658/+899 bps) and crypto majors; their full-sample stats are near zero. This is a
   2026 metals-trend regime artifact, not a validated constant.
4. **Alt-crypto contamination:** pooled all-class OOS "winners" (donchian20 t=3.6, ema_cross t=2.4) are
   carried by 2-8-trade moonshots in illiquid alts (e.g., ENJUSD 4 trades x 4,924 bps). These are
   multiple-testing artifacts, are excluded from the liquid universe, and must not be confused with edge.
5. **Blog overnight edge does not replicate on these index CFDs:** hour decomposition (1H, 5,000 bars)
   shows day-session drift positive and overnight negative on nearly every index (DJ30.p day +5.6/night -2.5, NAS100.p day +4.2/night -1.6, NETH25.p day +11.2/night -5.7, HK50.p day +2.4/night +1.8, AU200.p day +6.9/night -6.2, GER40.p day +7.9/night -2.9).
   The published US-ETF overnight edge is absent after CFD carry/swap drag on this inventory.

## 3. Portfolio construction (pre-declared rules)

Family gate (liquid-only pooled): t_oos >= 1.0, PF_oos >= 1.05, n_oos >= 200. Sleeves selected on **IS-only**
statistics (n_is >= 20, IS mean > 0, ranked by IS PF; max 1 sleeve/symbol, max 2 crypto sleeves;
Tier-1 families up to 8 sleeves, Tier-2 up to 3). Sizing: vol-targeted notional = 0.25% equity daily-vol per
position (a 2-sigma adverse move ~ 0.5% equity, consistent with the desk 0.5% Grade-B cap analog);
class notional caps fx<=100%, pm/index/energy<=50%, crypto<=20% of equity. Seed $2,501.80.

Selected sleeves (33): Tier 1 = ibs, ema9_pullback, swing_pullback_ma (8 each); Tier 2 = donchian20,
three_down_overnight, tom (3 each).

| family | symbol | class | n_is | PF_is | mean_is bps | daily vol % |
|---|---|---|---|---|---|---|
| ema9_pullback | XAUAUD.pi | precious_metal | 36 | 3.20 | 90 | 1.12 |
| ema9_pullback | JP225.p | index | 28 | 2.85 | 137 | 1.57 |
| swing_pullback_ma | BCHUSD.p | crypto_major | 30 | 2.73 | 325 | 4.04 |
| ema9_pullback | SP500.p | index | 27 | 2.65 | 85 | 0.96 |
| ema9_pullback | HK50.p | index | 24 | 2.60 | 128 | 1.62 |
| ema9_pullback | NAS100.p | index | 27 | 2.59 | 120 | 1.33 |
| swing_pullback_ma | DJ30.p | index | 30 | 2.23 | 52 | 0.89 |
| swing_pullback_ma | FR40.p | index | 25 | 2.19 | 42 | 0.95 |
| ibs | CNHJPY.pi | fx_cross | 133 | 2.11 | 24 | 0.55 |
| swing_pullback_ma | SGDJPY.pi | fx_cross | 27 | 2.10 | 32 | 0.46 |
| ema9_pullback | XAUGBP.pi | precious_metal | 34 | 2.10 | 61 | 1.15 |
| swing_pullback_ma | BTCUSD.pi | crypto_major | 22 | 2.10 | 138 | 2.39 |
| ema9_pullback | XAUCNH.pi | precious_metal | 37 | 2.08 | 63 | 1.21 |
| ema9_pullback | EURHUF.pi | fx_cross | 20 | 2.08 | 26 | 0.42 |
| ibs | CHFJPY.pi | fx_cross | 89 | 1.98 | 14 | 0.47 |
| swing_pullback_ma | EURCNH.pi | fx_cross | 49 | 1.95 | 22 | 0.40 |
| swing_pullback_ma | EURNZD.pi | fx_cross | 27 | 1.91 | 18 | 0.41 |
| swing_pullback_ma | UK100.p | index | 32 | 1.90 | 35 | 0.76 |
| ibs | GAUUSD.pi | fx_cross | 30 | 1.87 | 50 | 1.57 |
| ibs | XAUEUR.pi | precious_metal | 96 | 1.86 | 26 | 1.15 |
| ibs | NOKSEK.pi | fx_cross | 160 | 1.75 | 19 | 0.57 |
| ibs | GAUCNH.pi | fx_cross | 27 | 1.66 | 43 | 1.53 |
| ibs | EURZAR.pi | fx_cross | 199 | 1.63 | 22 | 0.78 |
| ibs | XAUSGD.pi | precious_metal | 90 | 1.59 | 18 | 1.16 |
| three_down_overnight | STOXX50.p | index | 36 | 3.23 | 55 | 1.01 |
| three_down_overnight | CHFSGD.pi | fx_cross | 101 | 2.76 | 14 | 0.39 |
| three_down_overnight | USDNOK.pi | fx_cross | 54 | 2.62 | 26 | 0.63 |
| tom | EURAUD.pi | fx_cross | 28 | 2.38 | 38 | 0.43 |
| donchian20 | USDTHB.pi | fx_cross | 34 | 2.21 | 72 | 0.47 |
| tom | EURNOK.pi | fx_cross | 28 | 2.05 | 33 | 0.48 |
| tom | EURGBP.pi | fx_cross | 28 | 1.89 | 15 | 0.26 |
| donchian20 | GAS.p | energy | 28 | 1.84 | 490 | 3.75 |
| donchian20 | GBPNOK.pi | fx_cross | 25 | 1.17 | 16 | 0.64 |

## 4. Simulation (net of all modeled costs; slot caps enforced)

| variant | window | final equity | total % | CAGR % | Sharpe | maxDD % | trades | hit |
|---|---|---|---|---|---|---|---|---|
| A desk-capped (2 slots, 1 crypto) | 2019-07→2026-11 | $3292 | 31.6 | 3.82 | 0.92 | 3.79 | 518 | 53 |
| B research (5 slots, 2 crypto) | 2019-07→2026-11 | $5276 | 110.9 | 10.73 | 1.38 | 5.38 | 955 | 57 |

OOS sub-window (first OOS trade → end): variant A 5.3% over ~24 months; variant B 20.1% —
no in-sample-to-OOS collapse.

**Monte Carlo (stationary block bootstrap, 2,000 paths, 6-month horizon):**

| variant | term P5 | term P50 | term P95 | DD P50 | DD P95 | P(6m loss) | P(6m gain>5%) |
|---|---|---|---|---|---|---|---|
| A | 98.6% | 101.2% | 105.4% | 1.0% | 2.7% | 0.0% | 6% |
| B | 98.6% | 103.4% | 110.9% | 1.6% | 4.7% | 0.4% | 34% |

**Honest expectation setting:** on a $2,501.80 account this stack projects ~$30-$300 over six months at
median, with a realistic worst decile near -1.5%. This is a low-volatility compounding book, not a
"quick money" vehicle. Anyone promising rapid account multiplication from published blog strategies on
2-3 years of broker data after costs is selling survivorship.

## 5. Failure modes (pre-declared)

- FX sleeves live on 8-30 bps/trade edges vs 2-3 bps assumed costs — a live spread 2x the assumption
  erases a third of the edge. Nightly carry on multi-day FX holds can flip nets negative.
- Precious-metals trend sleeves (Tier 2) are one-regime bets; a 2026-style metal trend stall converts
  donchian55 OOS gains to churn. Treat as satellite, size small.
- The 5,000-bar cap means every "IS" window here is 1.5-4.5 years: selection noise is material; the
  33-sleeve book embeds selection bias that only a sealed forward test can price.
- IBS's 9.6 bps OOS mean trades once per ~5 days per symbol; a 2-slot cap under-deploys it — variant B
  is the economically meaningful test, and it remains within 5.4% modeled maxDD.

## 6. Proposal (for Antigravity disposition — GLM does not execute)

1. Freeze variant A (2 slots) as the desk-compliant shadow candidate; keep variant B (5 slots) as a
   research profile. Require a sealed 60-90-day forward shadow log before any capital.
2. Verify live spread/carry/swap against the class assumptions before sizing anything (the 4H spread
   column being zero makes this mandatory, not optional).
3. Keep all alt-crypto cells out of any book (cost + liquidity + moonshot bias).
4. Do NOT deploy `williams_r`, `rsi2_mr_deep`, `three_down_swing`, `gold_3day`, `rsi2_short_extreme` on
   this inventory — OOS-negative after costs.
5. Execution authority remains Antigravity's under the written interlock and 0.5%/1% risk caps.

**Confidence:** VERIFIED for code, data reads, backtest arithmetic, family tables and simulation outputs
produced in this session from `historical_data/mt5_parquet` @ `b1f81d21`. UNVERIFIED-BY-AGENT for live
spreads/swap/financing, fill quality, min-lot granularity, and forward profitability.

**Signed — GLM, independent challenge & quantitative review.**
