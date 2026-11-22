# Coinglass API — Reverse-Engineering Record

**Status:** knowledge captured. The minified vendor bundles that produced this record were
**removed from the repo on 2026-11-22** (121 MB, see `desk/archive/removed_20261122/MANIFEST.json`).
This file is the durable replacement — it is what those 18 MB of JavaScript taught us.

Source bundle hashes remain retrievable from the manifest if re-verification is ever needed.

---

## Endpoints in use

| Endpoint | Purpose | Notes |
| :--- | :--- | :--- |
| `/api/index/v2/liqHeatMap` | per-symbol liquidation heat map | requires session cookie (`CG_SESSION`) |
| `/api/index/v3|v4/aggregate/liqHeatMap` | multi-venue aggregated heat map | v3 = named venues, v4 = range param |
| `/api/index/v5|v6/liqHeatMap` | newer generations | v6 is what the current site calls |
| `/api/index/aggregate/liqHeatMap` | unversioned aggregate | returns 40000 without a login |
| `/api/v2/kline` | OHLC | needs `exchange` + `symbol` + `interval` |
| `/api/fundingRate/v2/home` | funding across venues | no per-symbol param; filter client-side |
| `/fapi/v1/ticker/24hr` | Binance public | **HTTP 451 from this sandbox** — use the Coinglass proxy |

## The liquidation-levels model (ported to `src/lib/cg/liq-model.ts`)

Verbatim semantics recovered from `pages/liquidation-levels/[symbol]-*.js`:

- Candles arrive **inside** the `liquidationLevels/v2` response as `prices`, not from a separate
  kline call.
- Each level row: `{x|beginDate, endX|endDate, price, size, level2, side}`
  → `[startBar, endBar|null, price, size, tier, side]`
  where tier `h1=0/h2=1/h3=2` and side `1=long (below price) / 2=short (above price)`.
- A level with a valid `endBar` has been **hit** (price reached it) and stops counting.
- Price grid: **520 rows** spanning `0.88 × min(low) … 1.12 × max(high)`.
- `keyLevels(lvR)`: top-5 unhit rows per side within ±`lvR`% of last price,
  strength normalised so the strongest row = **100**.
- Leverage tier of a row = tier with the largest summed size
  (`h1 ≈ 100× High`, `h2 ≈ 50× Mid`, `h3 ≈ 25× Low`).

## Payload shape hazards (live-verified 2026-11-12)

The decrypted payload shape **varies between calls**:

1. bare `{ prices, data }`
2. wrapped `{ data: { prices, data } }`
3. double-wrapped `{ data: { data: [...] } }`
4. `{ code, msg, success }` with no data — a rate-limit shadow, not an error

The legacy archiver did `for (const lv of r.data ?? [])`; on the wrapped shape `r.data` is an
**object**, `for..of` throws "not iterable", the catch swallowed it, and **nothing was ever
archived** — the rolling heat map stayed empty on every fresh boot.

`parseLiqLevelsPayload()` / `isUsableLiqPayload()` / `unwrapLiqPayload()` in
`src/lib/cg/liq-model.ts` are the shape-proof replacements. Always use them.

## Per-combo gaps

The upstream can return `40001` ("price/OI row unavailable") for one `(symbol, range)` pair while
serving the neighbouring range fine — live-verified 2026-11-21:
`Hyperliquid_BTC-USD 3d → 40001`, `7d → 1813 rows`; ETH/SOL 3d fine.

Consequence: a single bad combo must **not** drop the whole venue from the aggregate. The range
ladder retries every wider range; only genuine failures (429, network, auth) propagate.

## Rate limiting

`src/lib/cg/client.ts` implements the gate the reverse-engineering revealed is necessary:

- `MAX_CG_CONCURRENCY = 6`
- host-level 429 cooldown, default 30 s (honours `Retry-After`)
- stale-cache serving during cooldown (stale is better than failing)
- background-priority queue so engine pollers cannot starve interactive requests

**Practical ceiling:** ~3.5 requests/second sustained. Pulling 3 liquidation ranges × 57 symbols
(171 calls) at higher concurrency collapses to <25 % success. Pull ranges **sequentially**.

## Terms of use

These endpoints are the ones the public coinglass.com site calls from the browser. No API key is
used; the `/api/index/*` family requires a logged-in session (`CG_SESSION`) for some variants.
Scraping them is a terms-of-service question for the operator, not a technical one — the terminal
has always spoken to them directly and that is an explicit, standing product decision recorded in
`README.md`.
