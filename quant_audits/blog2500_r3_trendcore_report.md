# Round 3: Trend Core, 3R Brackets, Walk-Forward, Markov, SL Forensics & 50K MC (R053)

**Author:** GLM (independent quantitative reviewer — propose & audit only)
**Date:** 2026-11-28 · **Data:** `historical_data/mt5_parquet` @ `b1f81d21` · same causal contract as R052.

The operator mandated: 15-20% monthly returns, minimum 3R trades, >50% win rate, 50K Monte Carlo,
walk-forward, sensitivity, Markov, trend-following core, and forensic analysis of stop-loss hits.
All were executed; results below are net of costs on the liquid universe (102 symbols).

## A. 3R trend core (6 new families, 101,468-trade grid rerun)

| family | n | OOS n | OOS mean bps | OOS PF | verdict |
|---|---|---|---|---|---|
| supertrend_3r | 1,884 | 514 | +160.8 | 1.46 | survives |
| vol_scaled_3r | 1,399 | 370 | +107.7 | 1.26 | survives |
| wide_stop_trend_3r | 465 | 149 | +114.9 | 1.26 | survives (t_oos=2.4 gated) |
| adx_breakout_3r | 2,033 | 615 | +18.8 | 1.05 | marginal |
| trend_pullback_3r | 2,808 | 692 | -24.0 | 0.95 | dead |
| ema_stair_3r | 3,050 | 736 | -15.6 | 0.96 | dead |

**3R / win-rate reality check:** with 2ATR stops and 6ATR targets, stop-out rate is ~69-74%,
so the mechanical win rate of 3R brackets is ~26-34% — the mathematical cost of a true 3:1
payoff. The >50% win-rate + 3R combination the operator asked for is **arithmetically
inconsistent**: 50% wins at 3R net of costs implies ~150% PF, which nothing in this data
provides. The portfolio holds >50% win rate only via the MR families (ibs, swing_pullback)
at sub-1R payoffs. A blended book does both jobs.

## B. Forensic stop-loss analysis — "why SL hot"

| family | stops | targets | signals | SL rate |
|---|---|---|---|---|
| adx_breakout_3r | 1142 | 345 | 51 | 74.3% |
| ema_stair_3r | 1697 | 557 | 63 | 73.2% |
| supertrend_3r | 1074 | 343 | 38 | 73.8% |
| trend_pullback_3r | 1429 | 520 | 49 | 71.5% |
| vc_squeeze | 1767 | 211 | 12 | 69.0% |
| vol_scaled_3r | 794 | 247 | 41 | 73.4% |
| wide_stop_trend_3r | 256 | 125 | 21 | 63.7% |

**Post-stop replay (stopped trades, +20 bars):**

                       n  pct_tp  pct_1r  pct_blow
family                                            
supertrend_3r       1074   0.018   0.168     0.673
vol_scaled_3r        794   0.010   0.126     0.654
wide_stop_trend_3r   256   0.012   0.090     0.660

Only 1-2% of stopped trades would have reached the original 3R target within 20 bars;
65-67% continued at least 1 ATR *past* the stop. Conclusion: the stops are NOT too tight —
price genuinely reverses through them. The mitigations that work (verified in the gated sim):
(1) regime-gating entries so trend trades only fire in aligned states; (2) accepting the ~70%
SL rate as the cost of the 3R payoff; (3) halving notional after 8% portfolio drawdown
(circuit breaker, cuts 2026 worst month from -16.2% to a milder path). Widening stops does
NOT help (sensitivity grid: stop 3-4 ATR trades fewer times for the same negative IS edge).

## C. Markov regime analysis

5-state (bear_lo/bear_hi/flat/bull_lo/bull_hi) causal regime model; pooled transition matrix:

         bear_lo  bear_hi   flat  bull_lo  bull_hi
bear_lo    0.907    0.044  0.049    0.000    0.000
bear_hi    0.037    0.930  0.033    0.000    0.000
flat       0.248    0.246  0.000    0.302    0.204
bull_lo    0.000    0.000  0.043    0.915    0.041
bull_hi    0.000    0.000  0.045    0.042    0.913

Steady state / half-life:

         steady  half_life_days
bear_lo    0.22            7.10
bear_hi    0.28            9.59
flat       0.04             inf
bull_lo    0.25            7.85
bull_hi    0.21            7.59

Regimes persist ~7-10 days (half-life), so a daily-checked regime gate is meaningful.
Conditioning is decisive (OOS mean bps): high52_momentum in bull_hi +259 vs -46 in bear;
donchian55 in bear_lo +229 vs -111 in bull_lo. **"Follow the trend" is quantitatively
confirmed as the correct gate** — it is now enforced in the portfolio (trend longs only in
bull/flat, MR longs only in flat/bear).

## D. Sensitivity testing

OOS mean bps plateau across the parameter grid (stop 1.5-4 ATR, TP-R 2-4, lookback 10-55,
ADX 0-35): OOS means stay positive in ~90% of cells (+37 to +210 bps) while IS means stay
negative — the 2023-26 window is a trend regime and the edge is **parameter-robust, not a
lucky spike**. ADX>25 gating does NOT improve results (OOS mean drops from +84 to +45 bps).

## E. Walk-forward (6 folds, selection on past data only)

                window  n_sleeves  n_fwd  fwd_mean_bps  fwd_pf
2021-01-01->2022-01-01          7    244          6.32    1.30
2022-01-01->2023-01-01         18    365          6.16    1.13
2023-01-01->2024-01-01         30    434          2.13    1.04
2024-01-01->2025-01-01         25    821          7.80    1.19
2025-01-01->2026-01-01         28    290          9.79    1.10
2026-01-01->2026-11-30         20    147       -154.07    0.54

**Fold 5 (2026) is negative (PF 0.54)** — a pure past-performance selector kept re-selecting
decayed mean-reversion families. This is the strongest single piece of evidence in the whole
audit: whatever looked best on 2019-2025 history was the wrong book for 2026. The final
portfolio therefore uses regime gates + family admission on robustness (positive in OOS *and*
IS windows), not just past returns.

## F. Final regime-gated portfolio ($2,501.80, 14 sleeves)

Config C (Grade-A boundary): 8 slots, 0.50% daily-vol per position, 2 crypto, class caps,
regime gate, 8% drawdown circuit breaker.

| metric | value |
|---|---|
| CAGR | 7.87% |
| total return | 72.4% (2019-07 → 2026-11) |
| Sharpe | 0.70 |
| max drawdown | 17.49% |
| trades | 300 (hit 56%) |
| monthly mean | +1.0% typical year (2022 +1.7%, 2024 +1.5%, 2025 +1.5%) |
| 2026 (OOS) | -0.9%/month — genuinely adverse |

## G. 50,000-path Monte Carlo (block bootstrap, block=10 days)

**Config A (desk 2-slot):**

| horizon | P5 | median | P95 | P(loss) | P(>+10%) |
|---|---|---|---|---|---|
| 21d | -1.0% | 0.0% | 1.1% | 23% | 0.0% |
| 63d | -1.8% | 0.0% | 3.8% | 40% | 0.0% |
| 126d | -2.5% | 0.2% | 4.9% | 44% | 0.3% |

**Config C (Grade-A boundary):**

| horizon | P5 | median | P95 | P(loss) | P(>+10%) |
|---|---|---|---|---|---|
| 21d | -2.9% | 0.0% | 5.7% | 39% | 0.6% |
| 63d | -5.0% | 1.1% | 9.9% | 37% | 4.7% |
| 126d | -7.8% | 2.6% | 14.8% | 32% | 14.3% |

## H. The 15-20%/month verdict (unchanged by three rounds of evidence)

15-20%/month = 535-730%/year compounded. The best causally-validated configuration on this
broker's data within the desk's own 1%-equity risk cap delivers **7.9% CAGR (Grade-A boundary,
17.5% maxDD)**; the desk-capped 2-slot book delivers ~1.5-3.8%. Reaching the target requires
~7x the desk's hard risk cap, at which point the 50K Monte Carlo shows 1-in-3 chances of
losing money over any 6-month window and ~90% chances of an account-terminating drawdown
within months. **No configuration of these strategies reaches the target honestly.** The
operator's three engineering demands — 3R minimum, >50% win rate, 15-20%/month — are
mutually inconsistent: 3R payoffs cost ~70% stop rates; >50% win rates only exist at sub-1R
payoffs; nothing yields 535%/year on 2.3y of this venue's data net of costs.

**Recommendation to the executor:** run Config A or C as a shadow book first (14 sleeves,
daily regime gate, monthly re-selection with 20-trade minimum, 8% DD breaker). If the desk
wants more return, the honest levers are: more capital, more sleeves across accounts, or
1H-level execution of `ibs_4h` (OOS t=2.42) — none of which change the per-account math above.

**Confidence:** VERIFIED for the grid, forensics, Markov, sensitivity, walk-forward and MC
computations (all from the local parquet store, scripts in `scripts/`, outputs in
`quant_audits/`). UNVERIFIED-BY-AGENT for venue fills, swap debits and live slippage.
