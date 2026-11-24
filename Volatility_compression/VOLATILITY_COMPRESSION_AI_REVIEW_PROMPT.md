# MISSION BRIEF: QUANTITATIVE AUDIT & NEXT-GEN EVOLUTION OF VOLATILITY COMPRESSION STRATEGIES

You are operating as a Senior Quantitative Researcher and Algorithmic Trading Systems Architect. Your objective is to audit an existing multi-asset, multi-timeframe Volatility Compression & Squeeze Breakout engine, stress-test its empirical findings, and develop an evolved, superior strategy tested directly on historical parquet market data.

---

## 1. REPOSITORY & DATA CONTEXT

- **GitHub Repository**: https://github.com/karan1399/Trading-Strategies
- **Clone Command**: `git clone https://github.com/karan1399/Trading-Strategies.git`
- **Volatility Compression Suite**: https://github.com/karan1399/Trading-Strategies/tree/main/Volatility_compression
  * Core Engine & Sweep Runner: [`Volatility_compression/quant_volatility_compression.py`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/quant_volatility_compression.py)
  * Comprehensive Research Report: [`Volatility_compression/VOLATILITY_COMPRESSION_STRATEGY_REPORT.md`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/VOLATILITY_COMPRESSION_STRATEGY_REPORT.md)
  * Module Overview & Documentation: [`Volatility_compression/README.md`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/README.md)
  * Full Sweep Dataset (208 Setups): [`Volatility_compression/results/volatility_compression_full_sweep.csv`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/results/volatility_compression_full_sweep.csv)
  * Validated Out-of-Sample Setups: [`Volatility_compression/results/volatility_compression_qualified.csv`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/results/volatility_compression_qualified.csv)
- **Historical Parquet Data**: https://github.com/karan1399/Trading-Strategies/tree/main/historical_parquet
  * Timeframes Available: `5M/`, `15M/`, `1H/`, `4H/`
  * Parquet Schema: `datetime` (UTC ISO string), `open`, `high`, `low`, `close`, `volume`, `spread` (points), `digits`.
  * Asset Universe:
    - **Metals & Commodities**: `XAUUSD.pi`, `XAUAUD.pi`, `XAUCNH.pi`, `XAUGBP.pi`, `XAGUSD.pi`, `USWTI.p`
    - **Equity Indices**: `SP500.p`, `NAS100.p`, `DJ30.p`, `GER40.p`, `UK100.p`, `JP225.p`
    - **Crypto**: `BTCUSD.pi`, `ETHUSD.pi`, `SOLUSD.p`
    - **Forex Majors & Crosses**: `EURUSD.pi`, `GBPUSD.pi`, `USDJPY.pi`, `GBPJPY.pi`, `AUDUSD.pi`, `USDCAD.pi`

---

## 2. BASELINE QUANT BENCHMARK (WHAT ALREADY WORKS)

The baseline engine evaluated **208 parameter configurations across 26 assets, 4 asset classes, and multiple timeframes (15M, 1H, 4H)** under bidirectional real broker spreads and a 70% In-Sample / 30% Out-of-Sample chronological split:

1. **Asset-Class Polarization (Metals vs. Forex)**:
   - **Gold & Metals (`XAUAUD`, `XAUCNH`, `XAUUSD`, `XAUGBP`) on 4H**: The strongest volatility compression edge in the market. **16 out of 20 setups were positive out-of-sample**, delivering **+155.13R Net Profit with an average OOS Profit Factor of 2.55**!
     * `XAUAUD.pi` 4H (Fixed 3.0R): In-Sample +6.00R (PF 1.29) $\to$ **Out-of-Sample +18.97R (PF 4.79, t-stat 2.60)**.
     * `XAUCNH.pi` 4H (Fixed 4.0R): In-Sample +16.96R (PF 2.13) $\to$ **Out-of-Sample +18.19R (PF 4.63, t-stat 2.15)**.
     * `XAUUSD.pi` 4H (Fixed 4.0R): In-Sample +16.99R (PF 2.13) $\to$ **Out-of-Sample +14.99R (PF 4.00, t-stat 1.80)**.
   - **Forex Majors on 1H**: Completely collapsed (**−1,901.98R OOS**, 0% positive setups). Currencies are mean-reverting; squeeze breakouts on lower timeframes are overwhelmingly false traps.
2. **Multi-Timeframe Squeeze Coherence (4H Squeeze + 1H Breakout)**:
   - Coiling on 4H paired with a 1H momentum breakout created a high-conviction setup on **Nasdaq 100 (`NAS100.p`)**: **OOS PF 2.68 (+6.00R, Total +17.87R, PF 2.54)**.
   - **S&P 500 (`SP500.p`)**: **OOS PF 2.95 (+11.07R)**.
3. **Cross-Asset Lead-Lag Compression (Beta Catch-up)**:
   - **`SP500.p` (Lead) $\to$ `NAS100.p` (Lag) [4H]**: In-Sample +10.00R (PF 1.50) $\to$ **Out-of-Sample +7.78R (PF 1.97)**.
   - **`BTCUSD.pi` (Lead) $\to$ `SOLUSD.p` (Lag) [4H]**: In-Sample +4.16R (PF 1.18) $\to$ **Out-of-Sample +7.83R (PF 2.08)**.

---

## 3. YOUR DUAL MANDATE

### PHASE 1: INDEPENDENT QUANT AUDIT & RED-TEAMING
Examine [`Volatility_compression/quant_volatility_compression.py`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/quant_volatility_compression.py) and [`Volatility_compression/VOLATILITY_COMPRESSION_STRATEGY_REPORT.md`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/VOLATILITY_COMPRESSION_STRATEGY_REPORT.md):
1. **Squeeze Mechanics & Parameter Rigidity**:
   - The baseline uses Bollinger Bands (20, 2.0) vs Keltner Channels (20, 1.5 ATR). Is this standard TTM Squeeze setup prone to curve-fitting, or does an adaptive bandwidth percentile (e.g., Bollinger Bandwidth in the bottom 10th percentile over 100 bars) produce higher stability?
2. **Head-Fake / False Breakout Trap**:
   - Squeezes frequently break out in one direction to trap liquidity before reversing violently in the true direction. How can the entry trigger filter out the initial false release?
3. **Lead-Lag Spillover Latency**:
   - Analyze the cross-asset spillover model (`SP500` $\to$ `NAS100`, `BTC` $\to$ `SOL`). Is there execution latency risk when the lead asset breaks out? What is the optimal lookback window for the lagging asset?
4. **Why Metals Dominate**:
   - Why do Gold crosses show an 80% out-of-sample success rate while FX majors show 0%? What structural macroeconomic forces cause persistent post-squeeze expansion in Gold?

### PHASE 2: ARCHITECT & TEST YOUR EVOLVED VERSION (`Volatility_compression/volatility_compression_evolved.py`)
Build your own enhanced Volatility Compression engine that directly imports the parquet files in [`historical_parquet/`](https://github.com/karan1399/Trading-Strategies/tree/main/historical_parquet). Implement one or more of the following quantitative improvements:

1. **Multi-Factor Squeeze Metric**:
   - Combine John Carter Squeeze ($BB < KC$) with **Chaikin Historical Volatility Ratio** or **Parkinson Volatility percentile** to identify ultra-compressed equilibrium states.
2. **Momentum Directionality (Linear Regression Slope of MACD / Delta)**:
   - When the squeeze fires, calculate the linear regression slope of the momentum histogram. Only enter when momentum is actively accelerating in the breakout direction.
3. **Volume Confirmation & Institutional Absorption**:
   - Require breakout bar volume $> 1.5\times$ 20-period volume SMA, or filter by positive cumulative volume delta (CVD).
4. **Adaptive Chandelier / Parabolic Trailing Exits**:
   - Fixed R targets (e.g. 4.0R) cap potential runners during historic trend days. Implement an adaptive Chandelier trailing stop ($\text{Peak} - 2.5 \times \text{ATR}_{14}$) that locks in gains while letting outsized trends run.
5. **Cross-Asset Basket Expansion**:
   - Test additional macro lead-lag relationships: e.g. Gold leading Silver (`XAUUSD` $\to$ `XAGUSD`), or US Crude Oil leading Canadian Dollar / Energy.

---

## 4. STRICT BACKTESTING PROTOCOL

To ensure direct comparability with baseline benchmarks:
1. **Zero Lookahead Bias**: Signal evaluation on **closed bar $i$**, entry execution on **market open of bar $i+1$**.
2. **Transaction Friction**: Deduct full bid-ask spread `spread * 10^(-digits)` on both entry and exit.
3. **Chronological 70% In-Sample / 30% Out-of-Sample Split**: Calibrate parameters on the first 70% of days; evaluate out-of-sample edge strictly on the final 30%.
4. **Statistical Rigor**: Report Trade Count ($N$), Net $R$, Profit Factor (PF), Win Rate (%), Average $R$, Max Drawdown ($R$), and Student's $t$-statistic.
5. **Core Validation Assets**: Test across at least:
   - 2 Metals: `XAUUSD.pi`, `XAUAUD.pi` (4H)
   - 2 Indices: `NAS100.p`, `SP500.p` (1H or 4H)
   - 1 Crypto: `BTCUSD.pi` (1H or 4H)

---

## 5. DELIVERABLES REQUIRED

1. **Detailed Audit Report**: Critical critique of the baseline engine, identifying mathematical and structural weaknesses.
2. **Executable Python Engine**: Save as [`Volatility_compression/volatility_compression_evolved.py`](https://github.com/karan1399/Trading-Strategies/tree/main/Volatility_compression) with complete logic and zero missing imports.
3. **Comparative Performance Table**:
   | Asset | Timeframe | Baseline OOS PF | Evolved OOS PF | Baseline OOS R | Evolved OOS R | Evolved Max DD | Student's $t$-stat |
4. **Production Order Desk Rules**: Specific execution criteria for live deployment on MT5.

Begin by cloning https://github.com/karan1399/Trading-Strategies, reviewing [`Volatility_compression/quant_volatility_compression.py`](https://github.com/karan1399/Trading-Strategies/blob/main/Volatility_compression/quant_volatility_compression.py), and reading parquet data from [`historical_parquet/`](https://github.com/karan1399/Trading-Strategies/tree/main/historical_parquet).
