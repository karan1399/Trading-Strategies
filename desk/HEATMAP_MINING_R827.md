# HEATMAP_MINING_R827 — the Liquidation Heat Map as a formulation source

**2026-11-22 18:58 UTC · Arena.ai · analysis only — no order authority**

You said the graphs carry more information than they display. That was correct,
and specifically the payload carries **six dimensions the tab never draws**. This
note is what I found when I mined them, and the one hard problem it exposed.

---

## 1. What the payload actually contains

`GET /api/cg/stopmap?sym=BTC&model=1&mode=coin&range=24h` → 3.4 MB, 20 top-level
fields. The tab draws roughly 24 wall bars from it. The rest:

| field | size (BTC 24h) | what it is | drawn by the tab? |
|---|---|---|---|
| `keyLevels` | 16 rows | top clusters each side with **real USD** notional | yes (the bars) |
| `liqRows` | **14,515** | individual (price, time, size) liquidation cells | **no** |
| `columns` | 286 × ~72 cells | 5-minute slices, each its own cell ladder | **no** |
| `profile` | 140 | time-integrated heat per price bucket | **no** |
| `cumulative` | 139 | purported running fuel per price | **no — and degenerate** |
| `candles` | 288 | 5-min OHLC on **the same grid** as the heat | no |
| `yAxis` | 140 | price buckets, ascending, $51.79 step | implicitly |
| `overview` | 9 | longs/shorts/realised-liq-24h/largest/step | partly |

Two things fall out immediately:

- **The candles share the grid.** Heat and price are aligned in one payload, so
  "was this wall formed before or after price touched it" is answerable without
  a second data source. That is the sold-vs-resting distinction the tab cannot make.
- **`overview` splits open interest into longs vs shorts and gives realised 24h
  liquidation on each side.** The tab shows neither. It is a free read on which
  side is crowded.

## 2. Unit traps — read before trusting any number here

**The raw cells are not dollars.** Summing every cell in BTC's 24h grid gives
**$240.8 B**, against `overview.total` of **$1.295 B** — a ratio of **185.9×**.
The cells are leverage-weighted heat units. Any analysis that multiplies them by
a contract size and calls the result "USD of liquidations" is wrong by two orders
of magnitude. Only `keyLevels[].usd` and `overview.{longs,shorts,total}` are notional.

**`cumulative` is degenerate.** Its `below` field is identically zero across all
139 rows, and its maximum `above` exactly equals `overview.longs` while its
maximum `below` equals `overview.shorts`. It is not a price-ordered running sum;
it is two totals stapled to a price axis. My first build computed a skew from it
and returned **+0.00 for BTC, ETH and SOL** — a silently dead metric. It is now
excluded and the skew is computed from `keyLevels` instead.

**ATR from this payload is 5-minute.** The heat map ships only 5-minute candles,
so raw ATR is 0.17 % on BTC and a $16 M wall 1.4 % away reads as "8 ATR". I
rescale to an hourly equivalent (×√12) and quote that.

## 3. The cross-symbol read — 10 names, 24h window

```
SYM          LAST   ATR%   FUEL ABOVE   FUEL BELOW    SKEW     NEAR^     NEARv   ASYM  BIAS
BTC     60,702.80   0.56      $164.1M      $369.0M   -0.38     +1.4%     -0.6%     42  v FLUSH
ETH      1,715.08   0.54       $81.2M      $166.8M   -0.35     +1.1%     -0.9%     42  v FLUSH
LINK         8.82   0.70        $663k        $1.0M   -0.21     +1.3%     -1.6%     53  v FLUSH
BNB        558.69   0.55        $2.0M        $2.7M   -0.14     +0.5%     -2.2%     62  v FLUSH
LTC         39.92   0.72        $1.0M        $785k   +0.14     +0.7%     -1.6%     57  ^ SQUEEZE
AVAX         6.68   0.72        $552k        $715k   -0.13     +1.7%     -1.6%     48  v FLUSH
DOGE         0.06   0.64        $3.9M        $5.0M   -0.12     +1.7%     -1.2%     49  v FLUSH
SOL         70.38   0.61       $15.3M       $18.9M   -0.11     +0.7%     -1.0%     55  v FLUSH
ADA          0.14   0.81        $1.7M        $2.1M   -0.10     +2.2%     -1.0%     42  v FLUSH
XRP          0.93   0.64       $13.8M       $11.2M   +0.10     +0.3%     -1.0%     62  ^ SQUEEZE
```

**Eight of ten are long-heavy below.** The fuel for a downward move is 1.7–2.3×
the fuel for an upward move on BTC, ETH, LINK. That is a market positioned for a
long flush, not a short squeeze — and it is the single most useful thing in the
whole surface, because it says the *asymmetric* trade is short, not long.

**Corroboration that matters:** on BTC the unitless heat skew (**−0.51**, from
all 14,515 cells) agrees in sign and roughly in magnitude with the wall skew
(**−0.38**, from the top-24). Two independent constructions of the same idea
pointing the same way. That is not proof, but it is not one number agreeing with
itself either.

**XRP is the outlier and the only ^ SQUEEZE on the board** — fuel above
($13.8 M) exceeds fuel below ($11.2 M), the nearest wall is only **+0.3 %** away,
and it carries the highest asym score (62). That is also the one venue where we
already have a resting order.

**Cascade found on BTC**: 4 chained levels 61,541 → 61,955 = **$88.6 M in one
run** above spot, and 2 chained 59,210 → 59,107 = **$84.6 M** below. A cascade
means once price enters the first level it accelerates through all of them; the
gap between the levels is under 0.6 ATR1h, so nothing stops it.

**Vacuum on BTC**: 62,473 → 63,405, +1.5 %, **9.1 ATR wide** with negligible
mass. Price travels fast and without support through it. A TP placed inside a
vacuum will be hit or overshot; a stop placed inside one will be gapped.

## 4. The finding that changes a live position

The ETH short now on record at MT5 is **entry 1,707.01, SL 1,738.50, TP 1,548.74**
— R:R 5.03, stop 3.38 ATR1h away. The note claims the stop "clears the $56.6 M
shelf".

Measured against the live 24h grid:

```
FUEL ABOVE (where a short gets squeezed)
   1,734.25  +1.12%   2.92ATR from entry  $21.4M   below SL · ENTRY->SL zone · ~LIQ
   1,740.13  +1.47%   3.55ATR from entry  $16.8M   ABOVE SL
   1,743.07  +1.64%   3.87ATR             $ 4.7M   ABOVE SL
   1,747.47  +1.89%   4.34ATR             $ 7.3M   ABOVE SL
   1,751.88  +2.15%   4.81ATR             $ 9.7M   ABOVE SL
```

**The stop sits between the two heaviest clusters, not beyond them.** The $21.4 M
wall at 1,734.25 — 3.4 % above mark, and within ~$3 of the liquidation price —
is *inside* the position's risk zone. The stop at 1,738.50 clears that one by
$4.25 and then sits **below $16.8 M + $4.7 M + $7.3 M + $9.7 M = $38.5 M** of
resting short-side fuel that price must wade through on any real squeeze.

So the stop is not "protected" in the sense the note implies. It survives a
normal stop-run and fails exactly when the scenario it exists for begins. The
honest statement is: **this stop will be taken out by the squeeze it is meant to
protect against**, because the squeeze's fuel begins $4.25 below it.

Two defensible alternatives, both for **Antigravity** to weigh — not for me:

- **Push the stop above the cluster chain** (≈1,755, beyond the $9.7 M at
  1,751.88) → risk becomes ~4.9 ATR1h, R:R falls to ~3.4, but the stop is then
  genuinely beyond the fuel.
- **Keep the stop, cut the size.** 0.5 lots on an ETH short with liq 1,731.45
  and the first wall at 1,734.25 leaves $3.45 of clearance between liquidation
  and fuel. Size is the variable, not the stop.

I am not going to pretend this is a clean answer. A 3.38 ATR stop with R:R 5.03
is attractive on the numbers and badly placed on the map. Those two facts
conflict, and the map is the more specific evidence.

## 5. A second, unresolved problem: the position list disagrees with itself

`desk/state.json` currently holds **two** positions, both MT5:

```
20386324  ETHUSD.pi  short 0.5  entry 1,707.01  SL 1,738.50  TP 1,548.74  "protected"
20297836  EURUSD.pi  short 0.02 entry 1,100.26  SL 1,097.50  TP 1,095.50  "in_profit" +$6.10
```

Earlier in this session the record held **four** positions including a **Delta
ETHUSD short −1 @ 1665 with no SL and liq 1731.45**. Those are gone from the file.
Either Antigravity closed them, or the file was overwritten. **I cannot tell
which from here, and I will not guess.** The pending action
`20261122T183510Z_ADD_STOP_ETHUSD.json` still fires on "NO STOP LOSS ON RECORD"
and is stale — `.pi` is the MT5 CFD, not the Delta perp, and it now has a stop.

Per-venue reconciliation required, as always: a position count is not evidence.

## 6. What I built

`scripts/trading/heatmap_analysis.py` — reads the raw payload and emits fuel
above/below in USD, wall ladders with ATR-normalised distance, chained cascades,
vacuum gaps, a proximity-weighted magnet, and a 0–100 asym score. Enforces ≥10 s
pacing because Coinglass returns `50001` under burst.

```
python3 scripts/trading/heatmap_analysis.py --sym BTC ETH XRP --range 24h
python3 scripts/trading/heatmap_analysis.py --json > fuel.json
```

Deliberately excluded: the `cumulative` field (§2), and any multiplication of
raw cells into "dollars" (§2). Both would have looked rigorous and been wrong.

**Model 2 and 3 are untested.** This entire note is Model 1. Models 2/3 are
different liquidation assumptions (different leverage tiers); comparing them
would show how much of a wall survives a change in assumption. That is the
obvious next step and I have not done it.

**Exposure caveat:** the fuel map is a *liquidation* map. It says nothing about
spot flow, and a large wall can be manufactured or spoofed. It is a magnet
field, not a forecast.
