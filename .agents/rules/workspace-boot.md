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

## WS-12 — MANDATORY 16-PARAMETER TELEMETRY ANALYSIS BY DEFAULT

Antigravity operates with institutional-grade 360° order-flow telemetry. On every autonomous wake-up, market review, and pre-trade formulation, Antigravity MUST analyze all 16 canonical telemetry parameters by default:
1. **Ticker**: Last trade price, tick direction, venue source (Binance execution lead).
2. **Market Structure**: Multi-timeframe auction Value Areas (Session/Day/Month VAL, POC, VAH), Day VWAP ±1/2/3σ, EMAs (13/89/233), RSI(14) across M15/H1/H4/D1.
3. **Narrative Engine**: Regime classification (`TREND_DOWN`/`RANGE_BALANCE`), active algorithmic model, direction, quantitative confidence %, entry ladder, stop, and R-multiples.
4. **Athena Oracle**: 6-factor composite consensus score (0-100) combining Apollo (trend), Poseidon (CVD orderflow), Hades (liq gravity), Zeus (whales), Atlas (book walls), and Nemesis (funding).
5. **Microstructure L2/L3**: National Best Bid & Offer (BBO) spread in bps, multi-exchange depth (Binance, OKX, Bybit, Coinbase).
6. **Quant Positioning**: Top accounts & top positions Long/Short ratios across 15m/1h/4h, Open Interest quadrant (`LONG_LIQUIDATION`/`NEW_SHORTS`), taker buy/sell USD volume.
7. **Options Deribit**: Deribit Volatility Index (DVOL), volatility term structure, 25-delta skew, options Max Pain strike, Put/Call open interest & volume ratios.
8. **Liquidations**: 3d, 7d, and 30d liquidation clusters ±4%, Coinglass Model 1/2/3 stopmaps, real-time liquidation tape prints.
9. **Book Walls**: Confirmed resting limit order bid/ask walls across M15/H1/H4 bands, wall age, touched state, and spot book depth.
10. **CVD**: Perp Cumulative Volume Delta, Spot Cumulative Volume Delta, Taker Buy Share %, and active multi-timeframe volume divergences.
11. **Footprint**: Aggregated 7-day footprint bar history (300 bars), per-bar delta USD, POC migration, and footprint signal tape (Absorption `abs`, Trapped traders `trap`, Exhaustion `exh`, Divergence `div`).
12. **Large Orders**: Resting and executed institutional whale orders, large taker blocks ($>\$100\text{k}$).
13. **Playbook Cards**: Systematic trade formulation cards and structural invalidation checklists.
14. **Backtest Metrics**: Empirical statistical validation ($5,000+$ historical trades evaluated across `M1_TREND_PULLBACK` and `M2_MEAN_REVERSION`).
15. **COT Report**: Institutional Commitment of Traders positioning (Asset Manager vs Leveraged Funds net bias).
16. **Funding & OI**: Cross-exchange funding rates across 21 venues, OI-weighted 8h funding rate, annualized carry APR, open interest distribution, and Coinbase spot premium.

---

## WS-13 — MANDATORY WAKE-UP PHYSICAL VERIFICATION CHECKLIST

On EVERY autonomous wake-up, scheduled cron review, and conversational turn, Antigravity MUST explicitly emit and verify this structured Wake-Up Verification Checklist:

- [ ] **1. Physical SKILL.md Loading**: Physical `read_file` calls executed on the 6 governing skill bibles (`gravitas`, `quant-analyst`, `pre-trade-check`, `risk-reward-sanity-check`, `verification-before-completion`, `kaizen-agent`).
- [ ] **2. Plugin Grounding & Confirmation**: Operational status confirmed for the 4 governing bibles (`gravitas-native`, `superpowers`, `coderabbit`, `trading-skills`).
- [ ] **3. In-Memory Telemetry JSON Ingestion**: Direct in-memory parsing of `data/telemetry/assets/<SYM>.json`, `data/telemetry/footprint/<SYM>/signals_7d.json`, and `universe_overview.json` with zero intermediate scratch files (`WS-10`).
- [ ] **4. 16-Parameter Telemetry Audit**: Institutional analysis across all 16 parameters under `WS-12` (Ticker, Market Structure, Narrative Engine, Athena Oracle, Microstructure L2/L3, Quant Positioning, Options Deribit, Liquidations, Book Walls, CVD, Footprint, Large Orders, Playbook Cards, Backtest Metrics, COT Report, Funding & OI).
- [ ] **5. MT5 Live Blotter & Concurrency Reconciliation**: Real-time readback of Blueberry account `#5126976`, open positions, pending orders, balance, and floating PnL.
- [ ] **6. Trend Protocol Adherence (TC-09)**: Strict $\text{Price} < \text{EMA 233}$ short-only regime; immediate purge of any counter-trend orders.
- [ ] **7. Asymmetric Bounded Risk**: Dollar risk strictly bounded to $\$5.00–\$6.00$ USD per trade, reward $\ge 2.0R$, total book risk $< 1.0\%$ (\$25.00 cap).
- [ ] **8. Background Daemons Health**: Verify continuous uptime of `task-20346` (telemetry), `task-20953` (unified engine), `task-21217` (terminal), and `task-21661` (cron).
- [ ] **9. Kaizen 4-Pillar Self-Optimization**: Complete response with Benchmark, 5-Whys, Surgical Fix, and Anti-Regression Ledger.

---

## WS-14 — MANDATORY LIMIT ORDER RE-VALIDATION & EXHAUSTIVE OPPORTUNITY SCAN PROTOCOL

Antigravity operates with continuous dynamic order book and auction adaptation:
1. **Dynamic Value Area / POC Re-Validation**: On EVERY review and wake-up, Antigravity MUST re-evaluate all active pending limit orders against developing Value Areas (Session POC, Day VAL, Day POC). If developing POC or volume profile has migrated, evaluate whether the order risks being front-run or left behind. Re-anchor the limit order to the active confluence node (e.g. Session POC) to maximize fill probability while maintaining the exact same bounded dollar risk ($5.00–$6.00 USD) and $R \ge 2.0$.
2. **Exhaustive Multi-Asset Opportunity Scan**: Antigravity is NOT limited to 2 candidates. On every review cycle, Antigravity MUST systematically screen the broader universe (TradFi perps: QQQ, MSTR, GOOGL, NVDA, XAG; Crypto perps: UNI, XRP, DOGE, ADA, INJ, AVAX, DOT) for high-confluence setups aligning with Protocol TC-09 (Price < EMA 89/233) and Value Area retests.
3. **Decisive Order Staging**: If any asset displays high statistical confluence, structural asymmetry ($R \ge 2.0$), tight broker spread, and contract sizing conforming to the $5.00–$6.00 USD risk cap, Antigravity MUST stage the limit order decisively on MT5, expanding portfolio exposure while strictly capping total aggregate risk below 1.0% ($25.00 USD).
4. **Anti-Shallow-Loop Guarantee**: Antigravity is strictly prohibited from executing superficial or rushed review loops. Re-evaluating existing orders and scanning for fresh setups is mandatory on every operational cycle.

---

## WS-15 — PROHIBITION OF PERFORMATIVE CHECKLISTS & MANDATORY EMPIRICAL ASSERTION GATES

Antigravity operates with strict empirical grounding under Bible 1 Invariant 6 ("Say what you can't verify") and Bible 2 Iron Law of Verification:
1. **Zero Fake Checkmarks**: A checkbox `[x]` is strictly prohibited unless the thinking chain and tool calls in the active turn contain fresh, direct empirical evidence verifying every sub-clause of that item.
2. **Mandatory Disclosed Partiality**: If any check is partially executed (e.g. only 5 of 16 parameters scanned, or plugin status not tested with tools), Antigravity MUST NOT check `[x]`. It must explicitly mark `[PARTIAL]` or `[ ]` and disclose the exact missing components. Fabricating completeness is a critical safety violation.
3. **Deterministic 16-Parameter Execution**: The 16-parameter telemetry audit under WS-12 MUST be executed via `scripts/telemetry_16p_engine.py` (or identical in-memory parser) evaluating all 16 canonical fields (with explicit disclosures for asset-specific boundaries such as DVOL being BTC/ETH only and COT being CFTC-only). Ad-hoc truncated scripts that cherry-pick 4 fields are permanently banned.
4. **Footprint Schema Compliance**: Footprint signal counts MUST parse the verified `'t'` schema code (`abs`, `trap`, `exh`, `div`). Silently reporting 0 signals due to schema mismatches without investigation is strictly prohibited.

---

*Workspace boot layer — Trading-Strategies v2 · All 16 telemetry dimensions, wake-up checklist, WS-14 & WS-15 active.*
