# TRADING CONSTITUTION & RISK GOVERNANCE

## 1. Document Authority & Tricouncil Protocol
Three agents share this repository:
- **Arena.ai**: Analysis, tooling, and candidate formulation — proposes, never executes.
- **GLM**: Independent challenge and red-team review — proposes, never executes.
- **Antigravity**: Operator host — **the ONLY party with execution rights on MT5 and Delta Exchange**.
- Authority Document: `desk/AUTHORITY.md` governs disputes.
- Ledger: Append all round outputs to `desk/COUNCIL.md` (append-only, timestamped, signed). Never edit another member's entry.

## 2. Hard Risk Constraints (Non-Negotiable)
- **Equity Risk Ceiling**: Maximum **1.0% equity** total portfolio risk ($\approx \mathbf{\$25.00\text{ USD}}$ across open risk + resting risk on Blueberry MT5 Account `#5126976`), with standard setup sizing bounded to **\$5.00–\$6.00 USD** per trade.
- **Concurrency**: Multi-asset trend-aligned execution permitted along `TC-09` (Price vs EMA 89/233) strictly within the **1.0% / \$25.00 total book risk ceiling**; automated `engine/unified_alpha_pilot.py` sleeves retain `engine/config.json` mechanical caps (`max_global_positions: 2`).
- **Bracket Orders Mandatory**: Hard Stop Loss and Take Profit brackets must be placed at order submission. Zero naked positions.
- **Stop Loss Structure**: Stop must clear the widest structural liquidity shelf (`3d`/`7d`/`30d`). Minimum 2.0R to take-profit.

## 3. Data Honesty Rules
- Every quoted number must come from an empirical tool response in the current session.
- Liquidation clusters and whale wall figures are Coinglass estimates — label them honestly as such.
- If two tools disagree, prefer the more recent `updatedAt` timestamp and state the difference.

## 4. Canonical Master Specification (`desk/CANONICAL_MASTER_SPECIFICATION.md`)
- For cg-terminal trade audits, reviews, or formulation requests, follow all 1,763 lines and all 66 section anchors without skipping.
- The 12 canonical steps govern candidate dossiers, scoring, interlock checks, and ticket submission.

---

## 5. TC-09 — FINISHED AUCTION ZERO-PRINT & DUAL EMA (89/233) ORDER-FLOW PROTOCOL
This quantitative strategy combines macro trend filtering, dynamic EMA value-pocket retests, and footprint auction completion mechanics:

1. **Macro Trend Gate (EMA 233)**:
   - **Price < EMA 233**: Pure Bearish Regime. Only plan SHORTS. Zero longing against the macro trend.
   - **Price > EMA 233**: Pure Bullish Regime. Only plan LONGS. Zero shorting against the macro trend.

2. **Dynamic Retest Pocket (EMA 89)**:
   - In a downtrend (Price < EMA 233), pullbacks toward the falling **EMA 89** act as high-probability dynamic resistance.
   - In an uptrend (Price > EMA 233), pullbacks toward the rising **EMA 89** act as high-probability dynamic support.

3. **Footprint Execution Trigger (Finished Auction Zero-Print)**:
   - **For Shorts**: At the peak of a candle testing resistance/EMA 89, verify a **Finished Auction High** where the bid volume at the extreme high tick is **0** (e.g. `0 | X` print on 15M/1H/4H footprint). This proves buyer exhaustion—nobody is willing to bid higher. The auction is finished.
   - **For Longs**: At the trough of a candle testing support/EMA 89, verify a **Finished Auction Low** where the ask volume at the extreme low tick is **0** (e.g. `X | 0` print). This proves seller exhaustion.
   - **Stop Loss Placement**: Hard stop placed exactly 1 tick beyond the Finished Auction extreme (ultra-tight structural invalidation).
   - **Multi-Timeframe Confluence**: Align 15M micro-trigger with 1H/4H finished auctions for maximum statistical edge ($R \ge 2.5\text{R}$).
