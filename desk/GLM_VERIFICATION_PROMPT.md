# KAIZEN DESK AUDIT & CONTINUOUS SELF-IMPROVEMENT PROMPT (CANONICAL)

> **Directive**: Use this operational prompt at the start of any round, after execution cycles, or when troubleshooting anomalies to verify desk integrity, reconcile broker state against order-flow telemetry, and apply the Kaizen Continuous Improvement Loop using empirical data.

---

## 1. PRE-FLIGHT SYSTEM & DAEMON VERIFICATION

Execute and verify each of the following before making any execution claims:

1. **Physical Grounding Check (Invariants)**:
   - Verify `desk/CANONICAL_MASTER_SPECIFICATION.md` exists and is current.
   - Verify core governance plugins are grounded: `harness-antigravity`, `trading-skills`, `coderabbit`, `gravitas-native`.
   - Verify skills active: `kaizen-agent`, `reverse-engineer-anything`, `karpathy-guidelines`, `ponytail`, `verification-before-completion`.

2. **Server & Background Daemon Health**:
   - Next.js Production Terminal: Verify HTTP 200 on `http://127.0.0.1:3000/api/health`.
   - Desk Daemon: Verify `scripts/desk_daemon.mjs` is actively logging 45-second heartbeats.
   - Git State: Ensure local working tree is 100% synchronized with `origin/main` (`git pull --rebase` before modifying state).

---

## 2. DUAL-VENUE RECONCILIATION & EXECUTION AUDIT

Audit both active venues using direct, live tool calls (zero mock numbers):

### A. Blueberry Markets MT5 (`Account: 5126976`)
Run:
```python
import MetaTrader5 as mt5
mt5.initialize()
ti = mt5.terminal_info()
ai = mt5.account_info()
print("Connected:", ti.connected, "| Trade Allowed (GUI):", ti.trade_allowed, "| Equity:", ai.equity)
for p in mt5.positions_get():
    print(f"Position: {p.symbol} {p.type} {p.volume} lots @ {p.price_open}, SL={p.sl}, TP={p.tp}, PnL=${p.profit:.2f}")
mt5.shutdown()
```
- **Audit Gate**: If `ti.trade_allowed == False`, verify that the operator is notified to click the "Algo Trading" button in the MT5 desktop GUI window (red -> green).
- **Spread Ratio Gate (PIT-18)**: For any pending order, verify $\text{Stop Distance} \ge 5 \times \text{Live Spread}$ ($\text{Spread Ratio} \le 20\%$).
- **Algo Isolation Gate**: Verify only tickets with `magic=42001` or `comment="cg_terminal"` are managed; never touch `ALPHA*` tickets.

### B. Delta Exchange India (`Account: 26902927`)
Run:
- `delta-exchange-mcp:get_wallet_balances`: Verify net equity, available margin, and cash buffer.
- `delta-exchange-mcp:get_margined_positions`: Verify open positions, mark price, and unrealized PnL.
- `delta-exchange-mcp:get_open_orders`: Verify open maker limit orders, post-only state, and bracket SL/TP attachments.
- **Margin Ceiling Gate**: Verify order margin does not exceed available balance (micro-account risk cap $\le \$0.50$ per trade).

---

## 3. TELEMETRY & PLAYBOOK DATA AUDIT (`LLM_TRADE_FORMULATION_PLAYBOOK.md`)

For every nominated asset (`BTC`, `ETH`, `SOL`, `XRP`, etc.):
1. Dump live 8-tab bundle: `python scripts/trading/dump_for_llm.py --sym <SYM>`.
2. Inspect `data/llm-dump/<SYM>_latest.json`:
   - **Trend Gate (§2)**: Check `tracker.trend.gate` (5m EMA-233). If `short_only`, veto all longs. If `long_only`, veto all shorts.
   - **Finished Auction (§3)**: Check `tracker.strikes`. If dominance ratio $\ge 5.0\times$, check bar freshness ($\le 3$ bars old). If stale, treat as support shelf for pullback limits, NOT an immediate market chase.
   - **Confluence Score (§4)**: Calculate 0–10 score (Zone + Auction + CVD + Book Wall + Signals).
   - **Verdict Form**: Emit exact JSON schema with zero null values for structural setups: populate conditional Limit Entry, PIT-18 compliant Stop, and T1/T2 Targets.

---

## 4. THE 4 KAIZEN OPERATIONAL PILLARS (DATA-DRIVEN LEARNING)

Evaluate every round using empirical telemetry:

### Pillar 1: Behavioral Benchmark Evaluation
- State explicit success criteria (e.g. "Order #X placed as post-only maker inside ask wall").
- Measure actual broker execution: Was it accepted or rejected? What was the exact fill price or retcode?

### Pillar 2: 5-Whys Root-Cause Diagnosis
When any operation encounters friction:
1. *Why did it fail?* (e.g., retcode 10027 or insufficient margin).
2. *Why was that state present?* (e.g., GUI button toggled off or margin blocked by concurrent position).
3. *Why did the automation not detect it prior?* (e.g., pre-check inspected account instead of terminal info).
4. *Why did the client boundary reject it?* (e.g., Windows UIPI blocks synthetic keystrokes).
5. *What is the permanent structural fix?* (e.g., operator GUI toggle + automated pre-flight assertion).

### Pillar 3: Surgical Self-Refinement
- Implement minimal, high-leverage fixes (Karpathy simplicity).
- Purge intermediate scratch files and cache memory (`scripts/clean_pycache.py`).
- Route instruments to their optimal venue (micro-contracts to Delta, fractional coins to MT5).

### Pillar 4: Anti-Regression Learning Ledger
- Record every pitfall and update `GEMINI.md` / `desk/CANONICAL_MASTER_SPECIFICATION.md` lessons (PIT-01 through PIT-27).
- Verify that historical mistakes never recur in future rounds.

---

## 5. STANDARD PROCEED COMMANDS

To execute a clean, verified cycle:
```bash
# 1. Sync repository
git pull --rebase origin main

# 2. Check MT5 status
python -c "import MetaTrader5 as mt5; mt5.initialize(); print('MT5 GUI Trade Allowed:', mt5.terminal_info().trade_allowed); mt5.shutdown()"

# 3. Dump fresh telemetry
python scripts/trading/dump_for_llm.py --sym "BTC,ETH,SOL,XRP"

# 4. Run automated formulation audit
python scripts/fast_crypto_formulation.py

# 5. Clean cache and synchronize git
python scripts/clean_pycache.py
git add -A && git commit -m "desk: kaizen verified cycle" && git push origin main
```
