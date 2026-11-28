---
description: Code-First Math & Anti-Hallucination Grounding Rule — enforces Python-verified calculations for all numerical metrics, statistics, and backtest returns.
trigger: always_on
---

# CODE-FIRST MATH & ANTI-HALLUCINATION RULE (`07-code-first-math.md`)

1. **Zero Inline Mental Math**: Never estimate, interpolate, or calculate numerical metrics inline in prose. Always execute a Python script (`numpy` / `pandas` / `scipy`) to compute and verify metrics, p-values, Z-scores, Sharpe/Calmar ratios, drawdowns, and backtest returns.
2. **Schema-First Data Inspection**: Before analyzing any `.parquet` or `.csv` dataset, inspect only its schema, row count, date range, and `df.head(5)` via Python rather than loading raw files into context.
3. **Source Grounding**: Every quoted metric, trade count, equity figure, or statistical test result must come directly from a Python script or tool output executed in the current turn. Unverified numbers must be marked `[UNVERIFIED]`.
