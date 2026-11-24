# TRADING CONSTITUTION & RISK GOVERNANCE

## 1. Document Authority & Tricouncil Protocol
Three agents share this repository:
- **Arena.ai**: Analysis, tooling, and candidate formulation — proposes, never executes.
- **GLM**: Independent challenge and red-team review — proposes, never executes.
- **Antigravity**: Operator host — **the ONLY party with execution rights on MT5 and Delta Exchange**.
- Authority Document: `desk/AUTHORITY.md` governs disputes.
- Ledger: Append all round outputs to `desk/COUNCIL.md` (append-only, timestamped, signed). Never edit another member's entry.

## 2. Hard Risk Constraints (Non-Negotiable)
- **Equity Risk Ceiling**: Maximum **1.0% equity** per trade (**$25.00** single-trade cap on Blueberry MT5 Account `#5126976`).
- **Concurrency**: Maximum 2 total positions/orders; maximum 1 crypto position.
- **Bracket Orders Mandatory**: Hard Stop Loss and Take Profit brackets must be placed at order submission. Zero naked positions.
- **Stop Loss Structure**: Stop must clear the widest structural liquidity shelf. Minimum 2.0R to take-profit.

## 3. Data Honesty Rules
- Every quoted number must come from an empirical tool response in the current session.
- Liquidation clusters and whale wall figures are Coinglass estimates — label them honestly as such.
- If two tools disagree, prefer the more recent `updatedAt` timestamp and state the difference.

## 4. Canonical Master Specification (`desk/CANONICAL_MASTER_SPECIFICATION.md`)
- For cg-terminal trade audits, reviews, or formulation requests, follow all 1,763 lines and all 66 section anchors without skipping.
- The 12 canonical steps govern candidate dossiers, scoring, interlock checks, and ticket submission.
