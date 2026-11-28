---
name: statistical-analysis
description: Enforces strict statistical validation, hypothesis testing, non-parametric/HAC checks, multiple-testing correction, and reproducible Python verification.
---

# Statistical Analysis & Validation Protocol

Prevent narrative-driven or overfitted quantitative claims by enforcing formal statistical inference in Python (`scipy.stats`, `statsmodels`, `numpy`).

## Mandatory Checks
1. **Sample Adequacy & Distribution**: Report exact `N`, mean, median, std, skewness, excess kurtosis, and check normality (Shapiro-Wilk / Jarque-Bera) before choosing parametric vs non-parametric tests.
2. **Autocorrelation & Overlap**: Financial time series and overlapping horizons are serially correlated. Use Newey-West HAC standard errors or stationary block bootstrap rather than naive IID `sqrt(N)` t-stats.
3. **Multiple-Testing Correction**: Whenever evaluating multiple strategies, assets, or parameter sets, apply Benjamini-Hochberg (FDR) or Deflated Sharpe Ratio (DSR) / Bonferroni correction and disclose the number of trials (`M`).
4. **Effect Size & Uncertainty**: Always report 95% confidence intervals (bootstrap or analytical) alongside p-values; statistical significance without economic effect size after costs is rejected.
5. **Zero Unverified Numbers**: All test statistics, p-values, correlations, and percentiles must be emitted by an executed Python script in the active turn.
