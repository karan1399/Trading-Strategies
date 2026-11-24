---
description: >-
  Trading-Strategies workspace bootstrap — activates all trading, quant, and
  order-desk skills immediately on context load. Supplements global GEMINI.md.
trigger: always_on
---

# TRADING-STRATEGIES WORKSPACE — BOOT LAYER

This rule fires for every session in the `Trading-Strategies` workspace and
ensures all trading-critical skills, MCP tools, and desk SOPs are physically
pre-loaded on the first turn.

---

## WS-01 — WORKSPACE IDENTITY

- **Project Root**: `c:\Users\SIGMA\Documents\Trading-Strategies`
- **Terminal**: CG Order-Flow Terminal on `http://localhost:3000`
- **Active Broker**: Blueberry MT5 Account `5126976` (equity \$2,589.67)
- **Delta Exchange**: India CLOB — Antigravity is the ONLY execution authority
- **Risk Cap**: 1.0% equity = **\$25.90 USD** per trade · $25.00 single-trade ceiling
- **Concurrency**: Max 2 total positions/orders · Max 1 crypto position (P5-02)

---

## WS-02 — MANDATORY PHYSICAL READS ON EVERY TURN

The global `GEMINI.md` already enforces §1 and §2. This workspace layer
adds the following workspace-specific reads in ADDITION:

1. **Desk inbox wake file**: `desk/inbox/WAKE.md` — read before any formulation
2. **Authority document**: `desk/AUTHORITY.md` — governs which document wins disputes
3. **Council log**: `desk/COUNCIL.md` — append-only execution record (read tail on wake)
4. **Canonical Master Specification**: `desk/CANONICAL_MASTER_SPECIFICATION.md` — full 1,763-line SOP

---

## WS-03 — ACTIVE PENDING ORDERS (Last Known State)

> Read live state via `python -c "import MetaTrader5 as mt5; mt5.initialize(); ..."` to verify.

| Ticket | Symbol | Type | Entry | SL | TP | Risk |
|:--|:--|:--|:--|:--|:--|:--|
| `20428262` | `XAUUSD.pi` | BUY_LIMIT | \$3,648.50 | \$3,633.00 | \$3,679.50 | \$18.60 / 0.72% |
| `20428272` | `BTCUSD.pi` | BUY_LIMIT | \$62,475.00 | \$61,850.00 | \$63,725.00 | \$22.50 / 0.87% |

---

## WS-04 — ACTIVE BACKGROUND DAEMONS

| Task | Process | Role |
|:--|:--|:--|
| `task-16644` | `npm start` (1024MB heap) | Web terminal, WS order-flow |
| `task-16278` | `telemetry_daemon.py` | 16-point telemetry ingest |
| `task-15936` | Cron `0,30 * * * *` | 30-min order-flow review wakes |
| `task-13849` | `desk_daemon.mjs` | Desk ingest + Git sync |
| `task-13851` | `antigravity_council_poller.py` | Tricouncil consensus polling |

---

## WS-05 — MCP TOOLS REGISTRY (Workspace-Critical)

All tools verified available. Use tool calls, never prose approximations.

| Tool | Purpose |
|:--|:--|
| `market_snapshot` | Price, VAL/POC/VAH, EMA, RSI, VWAP ± sigma |
| `signals` | Footprint signals: Absorption, Divergence, Trap, Exhaustion |
| `footprint` | Aggregated order-flow bars: taker buy/sell USD, delta %, POC |
| `liquidation_levels` | 3d/7d/30d liquidation clusters ± 4% |
| `whale_orders` | Resting whale orders, aggregate walls, imbalance |
| `book_walls` | Depth/imbalance, confirmed walls, age, touched state |
| `cvd_state` | Perp/spot CVD, taker buy share, active divergences |
| `stopmap` | Coinglass liq heatmap: Model 1/2/3 |
| `analyze_setup` | One-call context bundle |
| `large_orders` | Large resting + executed orders |
| `kline_series` | Raw candles or OI rows |
| `notifications` | Live event feed (cursor-based) |
| `list_assets` | Asset registry + engine/scanner health |

---

## WS-06 — NON-NEGOTIABLE DATA HONESTY RULES

- Every quoted number MUST come from a tool response in this session.
- Liquidation/whale figures are Coinglass **estimates** — always label as such.
- If two tools disagree, prefer the more recent `updatedAt` and state so.
- XAU/XAG stopmap requires `CG_SESSION` in `.env` for non-BTC assets.
- 180d/1y/2y ranges require paid Coinglass grade (40003) — report, never guess.

---

## WS-07 — TRICOUNCIL PROTOCOL

- **Arena.ai**: Analysis and tooling — proposes, never executes
- **GLM**: Independent challenge — proposes, never executes
- **Antigravity**: Operator host — the ONLY party with execution rights on Delta Exchange and MT5
- Append all round output to `desk/COUNCIL.md` — append-only, timestamped, signed
- Never edit another member's entry
- Answer open `Q-<n>` questions with `ANSWER` entries

---

## WS-08 — MANDATORY TELEMETRY & FOOTPRINT INGESTION INVARIANT

Before formulating any market opinion, trade candidate, or risk assessment, Antigravity MUST physically ingest live telemetry:
1. **Deterministic Telemetry Reader**: Execute `python scripts/telemetry_reader.py <SYM>` (or `--all`) on every turn to read `data/telemetry/assets/<SYM>.json` and `data/telemetry/footprint/<SYM>/signals_7d.json`.
2. **Freshness Invariant**: Telemetry file age must be $\le 600$ seconds. If `is_fresh == false`, the asset is vetoed as STALE data.
3. **Footprint Confirmation**: Verify the latest footprint tape prints (absorption `abs`, trapped buyers/sellers `trap`, or volume exhaustion `exh`) and Level-2 limit order book imbalance before validating any trade setup.
4. **Broker Microstructure Verification**: Verify live broker spread and min lot crossing cost via MT5 API before punching any order. If broker crossing cost $> \$6.00$, reject immediately.

---

## WS-09 — MANDATORY PHYSICAL SKILL-LOADING & CHECKLIST INVARIANT (FORCE-LOAD ON EVERY REPLY)

Antigravity operates with strict, visible, physical tool execution on EVERY single reply without exception:
1. **Live Step Checklist (`update_steps`)**: On every turn, Antigravity MUST immediately call `update_steps` with a clean, structured 3-to-6 step operational plan, marking the active step `in_progress`.
2. **Live Focus Summary (`update_focus`)**: Concurrently call `update_focus` with a concise present-participle description of the current task.
3. **Physical Tool-Read Execution (`read_file`)**: Antigravity MUST physically and forcefully call `read_file` on the governing `SKILL.md` files at the very beginning of the turn before running commands, editing code, or formulating market opinions:
   - `C:\Users\SIGMA\.gemini\config\plugins\gravitas-native\skills\gravitas\SKILL.md` (Engineering correctness & invariants)
   - `C:\Users\SIGMA\.gemini\config\skills\quant-analyst\SKILL.md` (Institutional quant mindset & statistical rigor)
   - `C:\Users\SIGMA\.gemini\config\plugins\trading-skills\skills\pre-trade-check\SKILL.md` (Pre-trade readiness gatekeeper)
   - `C:\Users\SIGMA\.gemini\config\plugins\trading-skills\skills\risk-reward-sanity-check\SKILL.md` (Asymmetric 2.0R structure check)
   - `C:\Users\SIGMA\.gemini\config\plugins\superpowers\skills\verification-before-completion\SKILL.md` (Empirical proof before completion)
   - `C:\Users\SIGMA\.gemini\config\skills\kaizen-agent\SKILL.md` (Autonomous continuous improvement & anti-regression)
4. **Visibility & Grounding Law**: The physical `read_file` tool call and live checklist MUST appear in the execution trajectory on every turn to guarantee zero hallucination, zero context drift, and total empirical grounding.

---

## WS-10 — ZERO-INTERMEDIATE-FILE INVARIANT (DIRECT IN-MEMORY JSON STREAMING)

Antigravity and all execution daemons operate with strict read-only in-memory telemetry ingestion:
1. **Direct In-Memory Reading**: All telemetry (`data/telemetry/assets/<SYM>.json`, `data/telemetry/footprint/<SYM>/signals_7d.json`, `data/telemetry/universe_overview.json`) MUST be read directly into memory via native `read_file` tool calls or in-memory Python streams (`json.load` / `orjson.loads`).
2. **Zero Intermediate Files**: Creating scratch scripts, temporary files (`scratch_*.py`, `temp_*.json`), or intermediate staging dumps is strictly prohibited. No intermediate files may be created on disk during telemetry analysis.
3. **Single-Writer Authority**: Only `scripts/telemetry_daemon.py` (task-20346) holds write authority to `data/telemetry/`. All other agents, tools, and execution processes are strictly read-only consumers.
4. **Zero Repository Pollution**: The working tree must remain permanently clean; all telemetry parsing happens strictly in memory.

---

## WS-11 — MANDATORY DEEP STATISTICAL ANALYSIS ON WAKE UP & REVIEW (ZERO QUALITY DECAY)

Antigravity operates with institutional quantitative rigor. Dropping analytical quality over time or reducing reviews to shallow scans is strictly prohibited:
1. **Full Statistical Universe Scan**: On every autonomous wake up, cron cycle, or market review, Antigravity MUST execute the in-memory statistical telemetry scan across all assets in `data/telemetry/assets/` and `data/telemetry/footprint/`.
2. **Mandatory 5-Pillar Statistical Metrics**:
   - **VWAP Z-Score**: Measure $\sigma$-deviation from Day VWAP (`(price - vwap) / sd1`).
   - **Auction Value Area Profile**: Classify price location relative to Session & Day VAH/POC/VAL (Imbalance: `BELOW_VAL` / `ABOVE_VAH` vs `IN_VALUE`).
   - **Footprint Tape Aggregation**: Quantify 7-day prints of buyer/seller absorption (`abs`), trapped breakout traders (`trap`), and volume exhaustion (`exh`).
   - **Dual EMA Trend Filter (TC-09)**: Classify multi-timeframe regime under EMA 89 & EMA 233.
   - **Composite Quantitative Ranking Score**: Deterministically score and rank the entire universe from highest statistical confluence to lowest.
3. **In-Memory Zero-Intermediate Enforcement**: All statistical calculations must stream directly into memory under WS-10 with zero intermediate files.

---

*Workspace boot layer — Trading-Strategies v2 · All 16 telemetry dimensions active.*
