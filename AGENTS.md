# CG Order-Flow Terminal — Antigravity Agent Protocol

This project ships a **local order-flow terminal** (Next.js, port 3000) plus an
**MCP server** (`mcp/server.mjs`) that plugs all terminal data directly into
your Antigravity agent. Everything is live Coinglass API data — zero mock
values.


## ⚠️ READ FIRST — tricouncil & document authority

Three agents share this repo: **Arena.ai** (analysis/tooling), **GLM** (independent challenge),
**Antigravity** (operator host — **the only party with Delta Exchange and MT5 execution rights**).

1. Read `desk/AUTHORITY.md` — it names which document wins on every topic, and the hard rules no
   agent may override (1% equity cap, interlock-first, stop must clear the widest shelf, VERIFIED vs
   UNVERIFIED-BY-AGENT labelling).
2. Append your round output to `desk/COUNCIL.md` — append-only, timestamped, signed. Never edit
   another member's entry. Answer open `Q-<n>` questions with an `ANSWER` entry.
3. Arena and GLM **propose**; they never punch. An execution claim from either is invalid.
4. Never cite anything under `desk/archive/` as live policy.

## Assets covered

CURATED 231 (expanded from the original 61; user directive 2026-11-12, "no gaps"): 200 crypto USDT-M perps
(BTC ETH BNB SOL XRP ADA DOGE TRX LINK LTC AVAX DOT NEAR UNI ATOM XLM ETC
FIL ICP HBAR APT ARB OP SUI SEI INJ TIA VET 1000PEPE 1000SHIB) + 28 TradFi
USDT-M TRADIFI perps under the plain names (XAU NVDA SNDK SOXL SPCX GOOGL MU
SKHY XAG CL INTC CRCL MSTR QQQ ANTHROPIC AAPL TSLA DRAM KORU META NBIS MSFT
AMZN AMD COIN NFLX AVGO TSM) + 3 classic (SPX500 UKO NDX100 — Yahoo quotes,
order-flow tabs intentionally hidden). Canonical list:
`src/lib/universe/curated.ts`. Overview + Footprint are single-exchange
(Binance, the execution venue); Large Orders · Liq Levels · Book Walls · CVD
are multi-exchange aggregates with Binance as the lead.

## MCP tools (after you register `mcp/server.mjs`)

| Tool | What it gives you |
| --- | --- |
| `list_assets` | legacy Tier-1 registry + engine/scanner/feed health (the full curated 61 rides in `curated`/`curatedClassic`; the scanner iterates all 58 curated perps) |
| `market_snapshot` | price, auction VAL/POC/VAH (session 8h / UTC day / UTC month), EMA 13/89/233 + RSI(14) on m15/h1/h4/d1, day VWAP ±1σ/2σ/3σ |
| `signals` | Coinglass-exact footprint signals: Absorption / Divergence / Strong Divergence / Trap / Exhaustion on 15m time bars, auto volume bars and auto range bars |
| `notifications` | the live event feed (cursor-based). Sources: `footprint`, `indicators`, `whale-orders`, `liqlevels`, `cvd`, `engine` |
| `liquidation_levels` | aggregated liquidation clusters 3d/7d/30d ±4% — the sweep magnets |
| `whale_orders` | resting whale orders per exchange, aggregate bid/ask walls + imbalance |
| `book_walls` | order-book walls: depth/imbalance (now vs 5m ago), confirmed wall list with age + touched state, add/fill/pull events. Params: `market`, `interval`, `limit`, `band`, `endTime` (30-day history) |
| `cvd_state` | perp/spot CVD, taker buy share, active divergences. Params: `mode`, `itv` (1m–1d), `limit`, `anchor` (all/day/week) |
| `stopmap` | Liquidation heatmap — the CURRENT Coinglass page: Model 1/2/3, `mode=coin` (all venues) / `pair` (Binance), `range` 12h..2y. Returns the page's key liquidation levels above/below + market overview (open longs/shorts, largest level). Legacy `sym`-only form = BTC 24h grid; `endTime` pages ~30d history. Coin mode for non-BTC needs `CG_SESSION` in `.env` (renew: `node scripts/cg-session.mjs`) |
| `footprint` | aggregated order-flow bars: per-bar taker buy/sell USD, delta %, POC. Params: `interval`, `limit`, `last` |
| `large_orders` | large resting orders AND executed large trades per instrument: resting bid/ask walls, executed notional split. Params: `symbol`, `hours`, `interval` |
| `kline_series` | raw candles or open-interest rows for any instrument key (type `kline`\|`oi`, any interval, ≤1000 rows) |
| `analyze_setup` | one-call context bundle: market + signals + liquidation clusters + whale walls + book walls + CVD + recent events + formulation guide |

The `notifications` tool persists a cursor in `data/mcp-cursor.json` — calling
it with no arguments returns only events newer than the cursor and advances it.

## Trade desk and Antigravity

For a trading wake, load the [single Antigravity launch prompt](desk/CANONICAL_PROMPT.txt).
The [Canonical Master Specification](desk/CANONICAL_MASTER_SPECIFICATION.md)
governs the round order, evidence, scoring, risk, venue actions and scheduling.
Use its wake command first, then read `desk/inbox/WAKE.md` during pre-flight.
Market alerts and `analyze_setup` can nominate a candidate. They do not establish
complete coverage or authorize an order.

- `zai-desk` provides desk analyses and suggested orders. Its replies and digests
  also arrive in `desk/inbox/WAKE.md` and can reference `desk/deepscan/`. Handle
  them under canonical §12, including the `DIALOG` acknowledgment where applicable.
- `scripts/desk_daemon.mjs` maintains snapshots, Git sync, and the inbox. Its
  data/inbox cadence is separate from the approved execution scheduler.
- `atg-exec` validates each suggested order under the Kernel. The exact venue
  response and subsequent readback govern ticket and lifecycle state.
- The operator owns `state.json` `config`. Always `git pull --rebase` before
  editing. Never force-push.

## Data honesty rules (non-negotiable)

- Every number you quote must come from a tool response in this session.
- Liquidation/whale figures are Coinglass *estimates* — label them as such.
- XAU/XAG have no Hyperliquid venue. Their Model 1 heatmaps (and every
  non-BTC symbol) need the `CG_SESSION` web session in `.env`; without it the
  anonymous upstream serves BTC pair 24h only (code 40000) and the stopmap
  falls back to the badged rolling liq-snapshot construction. 180d/1y/2y
  ranges need a paid Coinglass grade (40003) — report, do not guess.
- If two tools disagree, prefer the more recent `updatedAt` and say so.

## REAL-TIME architecture (the live ladder)

Every displayed number is driven by the fastest tier that can serve it —
the UI badge always names the tier currently active:

| Tier | Latency | What | Source |
| --- | --- | --- | --- |
| 0 · WS | sub-100 ms | price (last trade + best bid/ask), footprint forming bar, CVD live delta | browser → `wss://fstream.binance.com` (`<sym>usdt@bookTicker` + `<sym>usdt@trade`), single connection, live SUBSCRIBE on symbol switch, 5 s watchdog, 10 Hz render throttle |
| 1 · REST | 250 ms / 1.2 s | price fallback (`/api/cg/live`), live order book (`/api/cg/livebook`: Binance top-1000 depth, self-driven 2.5 s poller per active symbol, same wall scanner as the aggregate) | server → fapi.binance.com |
| 2 · CG | 3–30 s | multi-venue aggregates (footprint venues, book-walls depth/imbalance, CVD, market, signals, liq levels, stopmap, whale orders) | Coinglass API via the existing routes |

Key invariants:
- **One price, everywhere.** The header, Overview, Footprint and Book-Walls
  "last price" all read the same WS ticker — the heatmap's own snapshot mid
  (a different, 5-venue aggregate) is never shown as "the price".
- **No double counting.** The footprint forming bar = the multi-venue
  aggregate (8 s resync, `?live=1` lane) + Binance tape strictly NEWER than
  the aggregate's `updatedAt` (+1.5 s guard). It can undercount for a
  moment; it can never show phantom volume.
- **Weight-bounded.** All fapi traffic flows through a shared circuit
  breaker (`src/lib/live/fapi.ts`): on HTTP 429/418 everything backs off
  (60 s probe-capped) and serves stale-marked data — the terminal never
  extends a ban by hammering.
- **Honest coverage.** The live book covers Binance top-1000 levels
  (≈ ±0.2% around mid on BTC — narrower on alts); every live KPI prints
  its covered range, and the full-depth numbers stay Coinglass aggregates.

## Operating the terminal

```bash
npm run dev        # terminal on http://localhost:3000 (dev)
npm run build && npm start   # production
npm run mcp        # test-run the MCP server standalone
```

The engine boots with backfill: every series (klines per asset, flows,
stopmap, bookwalls, liq levels) resumes from the last stored timestamp and
fills the offline gap from upstream history — so the terminal is always
continuous, even after days off. Point-in-time series (whale open orders)
have no upstream history; gaps during downtime are unavoidable there.

<!-- BEGIN:nextjs-agent-rules -->

## This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
