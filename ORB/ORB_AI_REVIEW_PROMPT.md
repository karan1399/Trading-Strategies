# MISSION BRIEF: QUANTITATIVE AUDIT & NEXT-GEN EVOLUTION OF OPENING RANGE BREAKOUT (ORB)

You are operating as a Senior Quantitative Portfolio Manager and Algorithmic Trading Engineer. Your objective is to audit an existing multi-asset, multi-timeframe Opening Range Breakout (ORB) engine, stress-test its empirical findings, and architect an evolved, superior version tested directly on historical parquet market data.

---

## 1. REPOSITORY & DATA CONTEXT

- **Project Root**: `C:\Users\SIGMA\Documents\Trading-Strategies`
- **Baseline ORB Codebase**: `C:\Users\SIGMA\Documents\Trading-Strategies\ORB`
  * Core Single-TF Engine: `ORB/orb_engine.py`
  * Multi-Timeframe Engine: `ORB/orb_mtf_engine.py`
  * Baseline Sweeps: `ORB/orb_sweep.py`, `ORB/orb_mtf_sweep.py`
  * Portfolio Combiner: `ORB/orb_champion_portfolio.py`
  * Comprehensive Report: `ORB/ORB_STRATEGY_RESEARCH_REPORT.md`
  * Data CSVs: `ORB/results/orb_qualified.csv`, `ORB/results/orb_mtf_qualified.csv`
- **Historical Data Directory**: `C:\Users\SIGMA\Documents\Trading-Strategies\historical_parquet`
  * Available Timeframes: `5M/`, `15M/`, `1H/`, `4H/`
  * Parquet Schema: `datetime` (UTC ISO string/timestamp), `open`, `high`, `low`, `close`, `volume`, `spread` (points), `digits`.
  * Date Coverage:
    - `1H`: ~10 months (Jan 2026 – Nov 2026, 5,000 bars, ~220 trading days)
    - `15M`: ~2.5 months (Sep 2026 – Nov 2026, 5,000 bars, ~55 trading days)
    - `5M`: ~1 month (late Oct 2026 – Nov 2026, 5,000 bars, ~20 trading days)
    - `4H`: 3+ years (2023 – Nov 2026, 5,000 bars)
  * Target Assets:
    - **Equity Indices**: `SP500.p`, `NAS100.p`, `DJ30.p`, `GER40.p`, `UK100.p`, `JP225.p`
    - **Crypto**: `BTCUSD.pi`, `ETHUSD.pi`, `SOLUSD.p`
    - **Metals & Energy**: `XAUUSD.pi` (Gold), `XAGUSD.pi` (Silver), `USWTI.p` (Crude Oil)
    - **Forex**: `EURUSD.pi`, `GBPUSD.pi`, `USDJPY.pi`, `GBPJPY.pi`

---

## 2. BASELINE QUANT BENCHMARK (WHAT ALREADY WORKS)

The baseline engine evaluated **2,560 parameter configurations** across single-timeframe and 4 multi-timeframe architectures under real broker spreads and a 70/30 chronological split:
1. **Equity Indices Are King**: Single-TF 15M sweep alone produced 126 positive Out-of-Sample setups out of 200 tested (avg OOS Profit Factor 3.45, +876.94 Net R).
2. **Multi-Timeframe Decoupling**: Defining the Opening Range on the **1H bar** (e.g., 14:00–15:00 UTC) while triggering entries on **15M closed bars** with a **4H 200 EMA Macro Trend filter** transformed Bitcoin (`BTCUSD.pi`) into an OOS PF 3.43 (+12.73R OOS) and 4.00 (+10.43R OOS) winner.
3. **Forex Majors Structural Failure**: Pure breakout models on Forex Majors (`EURUSD`, `GBPUSD`, `USDJPY`) lose heavily (-11,460R) due to institutional dealer liquidity harvesting. Only high-beta crosses (`GBPJPY`) survived (+6.32R OOS, PF 3.79).
4. **Diversified Champion Ensemble**: Combining 6 uncorrelated champions (`SP500`, `NAS100`, `DJ30`, `GER40`, `JP225`, `BTCUSD`) produced:
   - **Total Net R**: **+168.40 R** across 221 trades
   - **Portfolio Profit Factor**: **2.31**
   - **Win Rate**: **39.8%** (Payoff ratio: 3.51 : 1)
   - **Max Portfolio Drawdown**: **-24.39 R** (Return-to-Drawdown: 6.90x)
   - **Student's $t$-stat**: **2.91** ($p < 0.0001$)

---

## 3. YOUR DUAL MANDATE

### PHASE 1: INDEPENDENT QUANT AUDIT & RED-TEAMING
Examine `ORB/orb_engine.py`, `ORB/orb_mtf_engine.py`, and `ORB/ORB_STRATEGY_RESEARCH_REPORT.md` and address these specific vulnerabilities:
1. **Regime & Selection Bias**: Does the September–November 2026 data window over-represent a persistent bull market in US equities? How vulnerable are these setups to high-volatility chop or bear markets?
2. **Execution Friction & Open Spreads**: In live trading on Blueberry MT5 or Delta Exchange, spreads and slippage widen significantly during the first 5–15 minutes of the NYSE open (14:30–14:45 UTC). Are the spread models in the engine conservative enough?
3. **Lookahead Bias Verification**: Inspect the causal backward `merge_asof` logic in `orb_mtf_engine.py`. Confirm that the 1-bar shift prevents any future 4H bar leakage into 15M decisions.
4. **Midpoint vs Swing Stop Robustness**: Why does the 50% Opening Range Midpoint stop loss outperform the opposite extreme? Under what market conditions does it get prematurely hunted?

### PHASE 2: ARCHITECT & TEST YOUR EVOLVED VERSION (`ORB/orb_evolved.py`)
Build your own enhanced ORB engine that directly imports and processes the parquet files in `historical_parquet/`. Incorporate one or more of the following quant innovations:

1. **Developing Session VWAP Integration**:
   - Calculate intraday Volume Weighted Average Price (VWAP) and rolling standard deviation bands ($\pm 1\sigma, \pm 2\sigma$).
   - Rule A: Only take Long breakouts if price is above Day VWAP and VWAP is sloping upward.
   - Rule B: Use the upper $+2\sigma$ or $+3\sigma$ VWAP band as an asymmetric dynamic target or scale-out trigger.
2. **Initial Balance (IB) Extension & Rejection Models**:
   - Model the first 30 or 60 minutes as the Initial Balance (IB).
   - Classify open types (Open-Drive, Open-Test-Drive, Open-Rejection-Reverse).
   - If an open is classified as Open-Rejection-Reverse, fade the false breakout back toward the IB Midpoint.
3. **Volatility Compression & Range Expansion Filters (VCR / NR4 / Inside Day)**:
   - Pre-Session Volatility Ratio: $\text{PreSession Range} / \text{ATR}_{14} \le \text{Threshold}$.
   - Narrow Range 4 (NR4): Day $T-1$ range was the smallest of the last 4 days $\to$ prime volatility expansion day.
4. **Volume & Momentum Breakout Confirmation**:
   - Require breakout bar volume to exceed $1.5\times$ the 20-period volume SMA.
   - Or require candle body to represent $\ge 65\%$ of the total bar range (closing near high/low, eliminating weak wicks).
5. **Adaptive Trailing Stops (Chandelier / ATR Trail)**:
   - Replace rigid End-of-Day (EOD) exits with an adaptive Chandelier Exit: $\text{Highest High}(k) - 2.5 \times \text{ATR}_{14}$.

---

## 4. STRICT BACKTESTING PROTOCOL

To ensure your results can be benchmarked against the baseline:
1. **No Lookahead Bias**: Entry must evaluate on **closed bar $i$** and execute on **market open of bar $i+1$**.
2. **Realistic Frictions**: Charge `spread * 10^(-digits)` on both entry and exit.
3. **Chronological 70% In-Sample / 30% Out-of-Sample Split**: Train parameters on the first 70% of days; evaluate out-of-sample performance strictly on the final 30%.
4. **Performance Metrics Required**:
   - In-Sample & Out-of-Sample: Trade Count ($N$), Net $R$, Profit Factor (PF), Win Rate (%), Average $R$, Max Drawdown ($R$), and Student's $t$-statistic.
5. **Multi-Asset Validation**: Test your evolved strategy across at least 4 asset classes:
   - 2 Indices (`NAS100.p`, `SP500.p` or `GER40.p`)
   - 1 Crypto (`BTCUSD.pi`)
   - 1 Metal (`XAUUSD.pi`)

---

## 5. DELIVERABLES REQUIRED

1. **Detailed Audit Report**: Your objective evaluation of the baseline ORB engine, identifying key strengths, edge cases, and failure modes.
2. **Complete, Self-Contained Python Code**: Save your complete evolved engine as `ORB/orb_evolved.py` (fully executable, robust error handling, no missing imports).
3. **Comparative Performance Table**:
   | Asset | Timeframe / Setup | Baseline OOS PF | Evolved OOS PF | Baseline OOS R | Evolved OOS R | Evolved Max DD | Statistical $t$-stat |
4. **Institutional Trade Recommendations**: Clear, actionable rulebook for live execution on MT5 and Delta Exchange.

Begin by inspecting `ORB/orb_mtf_engine.py` and reading parquet data from `historical_parquet/`.
