# Quantitative Volatility Compression & Squeeze Breakout Engine

**Workspace**: `Volatility_compression/`  
**Dataset**: MT5 Historical Parquet Store (`historical_parquet/`, 5M, 15M, 1H, 4H)  
**Execution Realism**: Real broker spreads charged on entry & exit, closed-bar causal triggers, 70/30 In-Sample/Out-of-Sample split.

---

## 1. Directory Structure

```
Volatility_compression/
├── quant_volatility_compression.py       # Core backtest engine & sweep runner
├── VOLATILITY_COMPRESSION_STRATEGY_REPORT.md # Comprehensive quantitative research report
├── README.md                             # This documentation file
└── results/
    ├── volatility_compression_full_sweep.csv # Complete 208-configuration evaluation table
    └── volatility_compression_qualified.csv  # Filtered setups (IS PF >= 1.25, OOS PF >= 1.20)
```

---

## 2. Core Quantitative Model

Volatility compression represents a state of metastable market equilibrium: Bollinger Bands contract inside Keltner Channels ($S_t = \text{BB\_width} / \text{KC\_width} < 1.0$), coiling potential energy over $k \ge 3$ consecutive bars.

### Mechanics:
1. **Squeeze Detection**: Bollinger Bands (20, 2.0) contract inside Keltner Channels (20, 1.5 ATR).
2. **Release Trigger**: First closed bar expanding beyond Keltner Channels or Bollinger expansion.
3. **Trend Alignment**: Price must be above 200 EMA for Longs, below 200 EMA for Shorts.
4. **Volume Confirmation**: Bar range $> 1.2 \times \text{ATR}_{14}$ or volume $> 1.2 \times \text{SMA}_{20}$.
5. **Structural Risk**: Stop loss placed at opposite Keltner Channel or $1.5 \times \text{ATR}$.
6. **Multi-Timeframe Models**:
   - Model A: 4H Squeeze Coherence $\to$ 1H Momentum Breakout (e.g. `NAS100.p`, OOS PF 2.68).
   - Model B: Cross-Asset Lead-Lag Squeeze Spillover (`SP500` leads $\to$ `NAS100` lags; `BTCUSD` leads $\to$ `SOLUSD` lags).

---

## 3. Key Findings

- **Gold & Metals (`XAUAUD`, `XAUCNH`, `XAUUSD`, `XAUGBP`) on 4H**: The highest-conviction volatility compression edge in the market. **16 out of 20 setups positive out-of-sample** with **+155.13R net profit and avg OOS PF of 2.55**.
- **Top Strategy: "Gold Squeeze 4.0R" on `XAUAUD.pi` (4H)**:
  - In-Sample: +6.00R (PF 1.29)
  - **Out-of-Sample: +18.97R (PF 4.79, t-stat 2.60)**
  - All-Sample: +24.97R (PF 1.96, Max DD: 6.00R)
- **Forex Majors Collapse**: Squeeze breakouts on FX Majors (`EURUSD`, `GBPUSD`, `USDCAD`) suffer severe dealer mean-reversion chop and spread bleed (-1,901.98R OOS). **Never trade unconstrained squeeze breakouts on Forex.**

---

## 4. How to Run

To run the backtest sweep across all 26 assets:
```bash
python Volatility_compression/quant_volatility_compression.py
```
Results will be automatically updated in `Volatility_compression/results/`.
