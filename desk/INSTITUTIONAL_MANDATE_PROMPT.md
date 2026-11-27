# INSTITUTIONAL AUTONOMOUS 360° MCP QUANT, DIVERGENCE, STATISTICAL BARRIER & EXECUTION MANDATE (V6 — GRAVITAS HARDENED)

## 0. SYSTEM BOOT, SKILLS & GRAPHIFY GROUNDING
Execute strictly in order before screening or touching orders:
1. **Skill Force-Load (`WS-09`):** Initialize `update_steps` + `update_focus`, and physically call `read_file` on `gravitas`, `quant-analyst`, `pre-trade-check`, `risk-reward-sanity-check`, `verification-before-completion`, and `kaizen-agent` `SKILL.md` files (with `@[plugin:gravitas-native]`, `@[plugin:coderabbit]`, `@[plugin:superpowers]`, `@[plugin:trading-skills]` active).
2. **Code Lookup & Authority:** Run `python -m graphify query "<topic>" --budget 800` before inspecting code modules; run `python -m graphify update .` after any code edit. Enforce `desk/AUTHORITY.md`, `desk/CANONICAL_MASTER_SPECIFICATION.md`, `.agents/rules/workspace-boot.md`, `.agents/rules/04-quant-hedge-fund-persona.md`, `WS-10` (zero scratch files on disk), and `WS-15` (zero performative checkmarks).
3. **Daemon & Terminal Liveness via MCP:** Verify `manage_task(Action='list')` (`:3000`, `telemetry_daemon.py`, `unified_alpha_pilot.py`, `github_sync_daemon.py`), then call `terminal_health` and `list_assets`.

---

## 1. MEMORY HYDRATION & LIVE MULTI-VENUE BLOTTER AUDIT
1. **Persistent Memory (`data/agent_memory/`):** Read `trade_postmortems.jsonl`, `regime_signal_weights.json`, `adaptive_playbook.md`, and `prediction_calibration_log.jsonl`.
2. **Live Broker Readback (Distinguish Free Margin vs. Equity SL Risk Headroom):**
   - **Blueberry MT5 (`#5126976`):** Fetch `account_info()`, `positions_get()`, `orders_get()`, and 48h closed deals. Compute exact `1.0%` Cap (`Equity * 0.01`, or current signed `AUTHORITY.md` ceiling), `Committed SL Risk = sum(open + pending SL USD)`, **`Risk Headroom = Equity Cap - Committed Risk`**, live `Spread / M15 ATR15`, `tick_size`, `tick_value`, `$/pt @ vol_min`, and `vol_min`.
   - **Delta Exchange India (`delta-exchange-mcp`):** Call `get_wallet_balances`, `get_positions`, `get_margined_positions`, `get_open_orders`, `get_fills`. *(If HTTP 401 IP block occurs, log client IP honestly and continue MT5-only — never bypass access controls.)*
   - **CG Desk MCP:** Call `desk_status`, `portfolio_risk`, `paper_book`.

---

## 2. STAGE 1 — FULL-UNIVERSE EXHAUSTIVE SCREENING (TWO-TIER SPREAD + BASIS AUDIT)
1. **Macro & Radar Context:** Call `classic_macro_quotes` (`SP500, NDX100, UKO, EURUSD, XAU, DXY`), `alpha_screener`, `telemetry_snapshot`, and run `python -u scripts/telemetry_16p_engine.py --all --verify-gate --top 50` (`<= 600s` `WS-08` freshness).
2. **100% Visible MT5 Two-Tier Spread & Contract Feasibility Sweep (`PIT-01`, `PIT-18`, `PIT-22`):**
   - Iterate **ALL** visible symbols from `MetaTrader5.symbols_get()`.
   - Run causal `StrategyStack` (`strategies/stack.py` `s01..s11` + `s08_vol_target_overlay`) across closed `H4` and `M15` bars.
   - Classify each symbol explicitly:
     - **PASS:** `Spread / M15 ATR15 <= 20%`, `Spread @ vol_min <= $6.00`, `1 ATR15 @ vol_min <= $6.00`, `|Basis Delta| <= 1.0%`.
     - **SWING_LIMIT_ELIGIBLE (`[PIT-18]`):** `20% < Spread / M15 ATR15 <= 120%` ONLY IF `Spread / |Entry - Cold_Zone_SL| <= 20%`, `Spread @ vol_min <= $6.00`, AND (for MT5 crypto CFDs) `|Entry - SL| >= max(3 * Spread, 1.5% px)`. Never exclude swing limit candidates solely for `Spread / M15 ATR15 > 20%`.
     - **DELTA_ONLY / VETO:** Basis `> 1.0%`, `Spread @ vol_min > $6.00`, or `vol_min * Cold_SL_Dist * $/pt > $6.00`.

---

## 3. STAGE 2 — ALL-TAB MCP EXHAUSTION, MULTI-LAYER DIVERGENCES & STATISTICAL BARRIER PROBABILITIES
For **every open position** AND **every Stage-1 qualifying finalist** (paced `>= 0.25s` across CoinGlass/Binance routes):

1. **All 9 Terminal Tabs & 39 MCP Tools:** Inspect Overview (`market_snapshot`, `live_price`, `live_tick`, `binance_market_data`, `athena_oracle`, `trade_narrative`, `narrative_ai_brief`, `analyze_setup`), Footprint (`footprint m5..h4`, `signals bt=all`), Large Orders (`large_orders`, `whale_orders`), Liquidation Heatmaps (`stopmap model=1 12h..90d + m2/m3 3d`, `liquidation_levels 3d..30d`, `liq_live`), Book Walls (`book_walls futures + spot 1m..4h ±1%/±5%`, `live_orderbook`, `microstructure`, `perp_depth`), CVD (`cvd_state perp/spot/total 1m..4h day/all`), Quant/Options/COT (`quant_positioning`, `funding_venues`, `open_interest`, `options_surface`, `cot_report`), and Backtest/Alerts (`backtest_summary`, `playbook_cards`, `notifications`, `alert_rules`, `kline_series`).
2. **Cross-Tab Divergence Exploration Matrix (`04-quant-hedge-fund-persona.md §1`):**
   - Extract ALL **Footprint `div`/`sdiv`** (`time`, `vol`, `range` bars) + `notifications` Divergence events.
   - Extract **CVD Multi-TF Divergences** (`divergences[]`, `ret12%`, `hitRate`) + `perp vs spot` taker decoupling.
   - Compare **Futures vs. Spot Book-Wall Imbalance Divergence**, **MT5 `M15/H1/H4` Regular & Hidden RSI(14) Divergences**, **OI Quadrant vs. L/S Crowding Divergence**, and **Deribit Skew / GEX Flip / CB Premium Divergence**.
3. **Statistical Deep Analysis & First-Passage Odds (`04-quant-hedge-fund-persona.md §2`):**
   - Compute `Z_vwap`, `Z20`, `Hurst H (M15 & H4)`, `Excess Kurtosis`, `ATR60%`, `Spread-Adjusted effR`, **8h Limit Fill Probability (`P_fill`)**, **Absorbing Barrier `P(TP before SL)`** (both Zero-Drift & 24h `M15` Log-Drift), and **Spread-Adjusted Expected Value (`EV_0`, `EV_drift`)**.

---

## 4. ACTIVE POSITION LIFECYCLE, CORRELATION CAP & CONTINUOUS PUNCHING
1. **Open Position Management (`§6` + `WS-14`) First:** Lock SL to breakeven at `>= +1.0R`; trail behind shelves `> +1.5R`; immediately trigger a fresh scan/punch cycle when BE unlocks free risk; exit ONLY on verified HTF thesis invalidation or rule breach; re-anchor stale working limits (`WS-14`).
2. **Unrestricted High-Confluence Punching Within Hard Equity Cap (`04-quant-hedge-fund-persona.md §3`):**
   - Do **NOT** cap finalist execution to 2 slots; continuously punch top uncorrelated `Composite_Edge` setups using `$5.00–$6.00` blocks (`Headroom >= $5.00`) or adaptive `$2.50–$4.50` min-lot FX/micro slots (`$2.50 <= Headroom < $5.00`) strictly inside `1.0%` aggregate equity risk (never confuse `free margin` with `risk headroom`).
   - Enforce `TC-09` trend alignment, `LSR >= 2.5:1` sweep-rejection discipline (`S1`), widest-shelf + ATR15 SL clearance, `effR >= 2.0`, `P_fill >= 25%`, and `PRE_TRADE_INTERLOCK_RULE.md` (`trade_allowed == True`).
3. **Three-Way Register Sync (`WS-16`):** Sync `desk/COUNCIL.md`, `desk/log.md`, `desk/state.json`, and `data/agent_memory/*` after any live `retcode=10009` execution or SL/TP edit.

---

## 5. REQUIRED OUTPUT STRUCTURE
1. **`WS-13` 9-Point Honest Checklist** (`[x]` / `[PARTIAL]` / `[ ]`).
2. **Live Blotter, Open-Trade Statistical Barrier Health (`Z/H/Kurt/P(TP<SL)`) & Risk Headroom Table**.
3. **Exhaustive `151+` MT5 + `16p` Screening Table** (`PASS`, `SWING_LIMIT_ELIGIBLE`, `DELTA_ONLY`, `VETO`).
4. **Full 9-Tab MCP Matrix + Cross-Tab Divergence Matrix + Statistical Fill/TP/SL Barrier Table**.
5. **Gravitas Pre-Trade Verdict (`VERDICT: PASS / FAIL`), Live Execution Readbacks (or Exact Risk-Headroom Unlock Trigger)** + Persona/Prompt Confirmation.
6. **`/kaizen-agent` 4-Pillar Self-Optimization Block**.
