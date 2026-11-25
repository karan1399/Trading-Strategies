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
- **Active Broker**: Blueberry MT5 Account `5126976` (always verify live balance/equity via MT5 API — never cite stale figures)
- **Delta Exchange**: India CLOB — Antigravity is the ONLY execution authority
- **Risk Cap**: **\$5.00–\$6.00 USD** standard risk per trade ($R \ge 2.0$); aggregate portfolio risk strictly capped $\le \mathbf{1.0\% / \$25.00\text{ USD}}$
- **Concurrency**: Trend-aligned multi-asset exposure permitted under `TC-09` strictly within the \$25.00 aggregate risk ceiling (`engine/unified_alpha_pilot.py` sleeves retain `engine/config.json` `max_global_positions: 2`)

---

## WS-02 — MANDATORY PHYSICAL READS ON EVERY TURN

The global `GEMINI.md` already enforces §1 and §2. This workspace layer
adds the following workspace-specific reads in ADDITION:

1. **Desk inbox wake file**: `desk/inbox/WAKE.md` — read before any formulation
2. **Authority document**: `desk/AUTHORITY.md` — governs which document wins disputes
3. **Council log**: `desk/COUNCIL.md` — append-only execution record (read tail on wake)
4. **Canonical Master Specification**: `desk/CANONICAL_MASTER_SPECIFICATION.md` — full 1,763-line SOP

---

## WS-03 — LIVE PENDING ORDERS & POSITIONS VERIFICATION

> Static snapshots are banned under `WS-15`. Query live state directly via `MetaTrader5` API (`positions_get()`, `orders_get()`, `account_info()`) on every review and audit all orders against `TC-09` and `WS-14`.

---

## WS-04 — ACTIVE BACKGROUND DAEMONS REGISTRY

> Verify active daemon uptime dynamically via `manage_task(Action='list')` on every wake-up rather than assuming prior task IDs:
> - **Web Terminal**: `npm start` (port `3000`, 2048MB heap)
> - **Telemetry Poller**: `python scripts/telemetry_daemon.py`
> - **Unified Multi-Sleeve Engine**: `python engine/unified_alpha_pilot.py --live --i-understand-live-risk`
> - **Scheduled Wake Trigger**: Active Antigravity cron (`0,30 * * * *`) and/or desktop `scripts/antigravity_auto_wake.ps1`

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
- [ ] **2. Plugin Grounding & Confirmation**: Run `list_plugin_accounts` (when plugin state hasn't been checked) and verify local rules/skills vs MCP auth status (`gravitas-native`, `superpowers`, `coderabbit`, `trading-skills`), disclosing any unsigned-in MCP servers honestly.
- [ ] **3. In-Memory Telemetry JSON Ingestion (`WS-10`)**: Direct in-memory parsing of `data/telemetry/assets/*.json`, `data/telemetry/footprint/*/signals_7d.json` + `summary_7d.json` + `summary_1h.json`, and `universe_overview.json` with `WS-08` freshness gate (`<= 600s`) and zero intermediate scratch files.
- [ ] **4. 16-Parameter Telemetry Audit (`WS-12`)**: Execute `python scripts/telemetry_16p_engine.py --verify-gate` covering all 16 canonical parameters across Crypto + TradFi (disclosing `N/A` bounds like DVOL BTC/ETH-only or COT CFTC-only).
- [ ] **5. MT5 Live Blotter & Portfolio Risk Reconciliation**: Real-time readback of Blueberry account `#5126976` via `MetaTrader5` API (balance, equity, open positions, pending orders, and total book risk `<= $25.00`).
- [ ] **6. Trend Protocol Adherence (`TC-09`)**: Verify `Price < EMA 233` (Short-Only) or `Price > EMA 233` (Long-Only) across M15/H1 + Finished Auction Zero-Prints (`0 | X` / `X | 0`); purge any counter-trend orders immediately.
- [ ] **7. Asymmetric Bounded Risk & `WS-14` Re-Validation**: Verify `$5.00–$6.00` setup risk, $R \ge 2.0$, stop clearing widest `3d/7d/30d` shelf + M15 ATR noise, and audit pending limits for developing POC migration.
- [ ] **8. Background Daemons Health**: Inspect live processes dynamically via `manage_task(Action='list')` (`telemetry_daemon.py`, `unified_alpha_pilot.py`, `npm start`, plus wake scheduler / `antigravity_auto_wake.ps1`).
- [ ] **9. Kaizen 4-Pillar Self-Optimization**: Complete response with verified empirical Benchmark, 5-Whys, Surgical Fix, and Anti-Regression Ledger (`antigravity_selfcheck.py` on desk rounds).

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

## WS-16 — MANDATORY EXECUTION REGISTER SYNC & TELEMETRY FALLBACK INTEGRITY

1. **Three-Way Register Sync (`PRE_TRADE_INTERLOCK_RULE §5`)**: After every live MT5 or Delta `retcode=10009` execution batch (open, modify, cancel, SL/TP ratchet), Antigravity MUST update [COUNCIL.md](file:///c:/Users/SIGMA/Documents/Trading-Strategies/desk/COUNCIL.md), append the concise `EXECUTE` / `MODIFY` block to [log.md](file:///c:/Users/SIGMA/Documents/Trading-Strategies/desk/log.md), and flag or reconcile open-order drift in [state.json](file:///c:/Users/SIGMA/Documents/Trading-Strategies/desk/state.json) without touching `config`.
2. **Corrupted-Cache Detection & Guard**: If `telemetry_16p_engine.py` outputs `LAST=0.0000`, `UNKNOWN`, or `ticker=null` (or `EURUSD` shows `feed_type=coinglass_perp`), treat disk summaries as degraded, ground prices/EMAs/ATRs directly from `MetaTrader5` + serial `:3000` `/api/cg/market`, and immediately surface the daemon defect for fix.

---

*Workspace boot layer — Trading-Strategies v2.1 · All 16 telemetry dimensions, wake-up checklist, WS-14, WS-15 & WS-16 active.*
