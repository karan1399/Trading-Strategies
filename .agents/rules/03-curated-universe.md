# CURATED 61 SOVEREIGN UNIVERSE & REAL-TIME ARCHITECTURE

## 1. Two-Tier Universe Structure
1. **Tier-1: Curated Sovereign Universe (The Curated 61)**:
   - Canonical Definition: `src/lib/universe/curated.ts`.
   - Constituents: 30 crypto USDT-M perps + 28 TradFi USDT-M perps + 3 classic indices.
   - Microstructure Telemetry: 100% active live monitoring with Binance Tier-0 WS tape, top-1000 orderbook walls (`/api/cg/livebook`), cumulative 3d/7d/30d liquidation heatmaps, and taker CVD deltas.
   - Execution Authority: All high-confluence trade formulations, dossiers, and order intents are exclusively executed on this Curated 61 universe.
2. **Tier-3: Broker Background Inventory (354 Uncurated Symbols)**:
   - Evaluated for broker account context only. Unmonitored symbols do not qualify for algorithmic breakout or footprint signals.

## 2. Real-Time 3-Tier Ladder
- **Tier 0 · WS (<100 ms)**: Last trade price, best bid/ask, footprint forming bar (`wss://fstream.binance.com`).
- **Tier 1 · REST (250 ms – 1.2 s)**: Live orderbook (`/api/cg/livebook`), Binance top-1000 depth, wall scanner.
- **Tier 2 · CG (3 s – 30 s)**: Multi-venue aggregates (footprint venues, book-wall imbalance, CVD, stopmap, whale orders).

## 3. Terminal Operating Invariants
- **One Price Everywhere**: Header, Overview, Footprint, and Book Walls all read the same WS ticker.
- **No Double Counting**: Footprint forming bar = multi-venue aggregate + Binance tape strictly newer than aggregate's `updatedAt`.
- **Weight-Bounded Circuit Breaker**: All `fapi` traffic passes through the shared circuit breaker (`src/lib/live/fapi.ts`); back off on HTTP 429/418.
