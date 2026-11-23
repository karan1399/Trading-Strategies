/**
 * Model 1 liquidation heatmap — the CURRENT Coinglass
 * /LiquidationHeatMap page (the "new UI" with Model 1/2/3 tabs and a
 * Pair/Coin toggle), reverse-engineered live 2026-11-22.
 *
 * UPSTREAM CONTRACT (mined from the page chunk %5Bsymbol%5D-e61edde946656ec4.js
 * and verified with a logged-in session — scripts/research/
 * probe_session_matrix.mjs):
 *
 *   endpoint map G:
 *     model 1 → fapi /api/index/v2/liqHeatMap            (pair)
 *               fapi /api/index/aggregate/liqHeatMap     (coin — ALL venues)
 *     model 2 → v5 / v3-agg      model 3 → v6 / v4-agg (range + cp:false)
 *   mode "coin" → aggregate endpoint, symbol = plain coin (XAU, BTC…)
 *   mode "pair" → primary endpoint,     symbol = Binance_XAUUSDT form
 *   range → interval/limit (models 1/2, map g below) — the anonymous
 *           default view is 24h = interval 5 (minutes) × limit 288 bars.
 *   auth: `data` param = fresh encrypted token (makeObeToken) AND the
 *         `obe` header = web-session token (env CG_SESSION). Anonymous
 *         serves BTC pair 24h ONLY; every other symbol/range needs the
 *         session (upstream 40000) and 180d/1y/2y need a paid grade
 *         (upstream 40003).
 *   response: encrypted capi envelope (v-header key derivation) — the
 *             existing decryptCgResponse handles it; payload keys:
 *             { instrument, liq: [barIdx, yIdx, usd][], precision,
 *               prices: [ts,o,h,l,c,volUsd][], rangeHigh, rangeLow,
 *               updateTime, y: number[] }
 *
 * PAGE PANEL MATH (exact port of the chunk's ec/ep/ed functions — every
 * number the live page shows in "Key liquidation levels" and "Market
 * overview" is computed exactly this way, verified 1:1 against the
 * operator's screenshots of the XAU coin 3d view):
 *   - grid: total/longs/shorts[bar * rows + row]; a cell's USD counts
 *     "shorts" when its price is ABOVE that bar's close, else "longs".
 *   - key levels (ep, t=4): local maxima of the LAST column that are
 *     ≥ 12% of the column max, deduped within 3 bins, 4 per side.
 *   - market overview (ed): longs/shorts sums over the LAST column;
 *     largest single level = column max.
 */

import { cgGet, FAPI } from "./client";
import { makeObeToken } from "./crypto";
import type { LiqHeatmapPayload } from "./liq-heatmap";
import { BoundedMap } from "./bounded";

export type HeatmapModelId = "1" | "2" | "3";
export type HeatmapMode = "coin" | "pair";

/** page map G — model → [pair endpoint, coin endpoint] */
export const MODEL_ENDPOINTS: Record<HeatmapModelId, { pair: string; coin: string }> = {
  "1": { pair: "/api/index/v2/liqHeatMap", coin: "/api/index/aggregate/liqHeatMap" },
  "2": { pair: "/api/index/v5/liqHeatMap", coin: "/api/index/v3/aggregate/liqHeatMap" },
  "3": { pair: "/api/index/v6/liqHeatMap", coin: "/api/index/v4/aggregate/liqHeatMap" },
};

/** page map g — range → {interval, limit} (models 1/2) */
export const RANGE_G: Record<string, { interval: number | string; limit: number }> = {
  "12h": { interval: 5, limit: 144 },
  "24h": { interval: 5, limit: 288 },
  "48h": { interval: 15, limit: 192 },
  "3d": { interval: 15, limit: 288 },
  "7d": { interval: 30, limit: 336 },
  "14d": { interval: 30, limit: 672 },
  "30d": { interval: "h2", limit: 372 },
  "90d": { interval: "h6", limit: 360 },
  "180d": { interval: "h12", limit: 360 },
  "1y": { interval: "h24", limit: 360 },
  "2y": { interval: "d1", limit: 720 },
};

/** page map x — model-3 range aliases */
export const RANGE_X: Record<string, string> = {
  "12h": "12h", "24h": "24h", "48h": "48h", "3d": "72h", "7d": "7d", "14d": "14d",
  "30d": "30d", "90d": "90d", "180d": "180d", "1y": "365d", "2y": "720d",
};

export const HEATMAP_RANGES = Object.keys(RANGE_G);

/** ranges an anonymous (no CG_SESSION) deployment can serve: BTC pair only */
export const ANON_LIMITS = { sym: "BTC", mode: "pair" as HeatmapMode, maxRange: "30d" };

export interface HeatmapKeyLevel {
  price: number;
  usd: number;
}

export interface HeatmapOverview {
  lastPrice: number;
  /** currently-open long exposure below price (last column) */
  longs: number;
  /** currently-open short exposure above price (last column) */
  shorts: number;
  /** longs + shorts — the page's "Open liquidation levels (total)" */
  total: number;
  largestLevel: number;
  /** page's "Price step" — the uniform y-grid bin width */
  priceStep: number;
  /** page's "column period" in minutes (5/15/30/120…) */
  columnMinutes: number;
  chgPct24h: number | null;
  /** "Liquidated in 24h" — realized (swept) liqs, coin-mode aggregate from
   * /api/index/liquidation24h (pair mode: that endpoint serves coin
   * aggregates only, so it reads 0/0 → null) */
  liq24h: { long: number; short: number } | null;
}

export interface Model1Heatmap {
  model: "1" | "2" | "3";
  mode: HeatmapMode;
  range: string;
  sym: string;
  /** display symbol actually queried (Binance_XAUUSDT or plain coin) */
  symbol: string;
  /** raw payload provenance */
  updateTime: number;
  precision: number;
  rangeLow: number;
  rangeHigh: number;
  instrument: string;
  /** the page's grid, per column: sparse cells [yIndex, usd] (y ASC) */
  columns: { ts: number; cells: [number, number][] }[];
  /** per-row LAST-column open exposure — the "Key liquidation levels" source */
  yAxis: number[];
  lastColumn: number[];
  /** window-total per row (their renderChart heat column sums) */
  profile: number[];
  candles: { ts: number; o: number; h: number; l: number; c: number }[];
  /** page ep(): key levels above (shorts) / below (longs), 4 each */
  keyLevels: { above: HeatmapKeyLevel[]; below: HeatmapKeyLevel[] };
  /** page ed(): market overview */
  overview: HeatmapOverview;
  /** cumulative swept-from-price curves (page eu()) */
  cumulative: { price: number; below: number; above: number }[];
  liqRows: number;
}

/** upstream error taxonomy we translate for the route/MCP layer */
export class HeatmapUpstreamError extends Error {
  constructor(
    public code: string,
    message: string
  ) {
    super(message);
    this.name = "HeatmapUpstreamError";
  }
}

// ---------------------------------------------------------------------------
// payload fetch
// ---------------------------------------------------------------------------

const g1 = globalThis as unknown as { __m1Cache?: BoundedMap<{ at: number; model: Model1Heatmap }> };
const cache = (g1.__m1Cache ??= new BoundedMap<{ at: number; model: Model1Heatmap }>(512));

function pairSymbolFor(sym: string): string {
  // classic instruments and 1000X-style tickers use their venue pair as-is;
  // curated perps are Binance_<SYM>USDT (assets.ts leadInstr keeps exact keys)
  return `Binance_${sym}USDT`;
}

/**
 * Fetch one Model-1/2/3 heatmap window and run the page's panel math.
 * Cached (sym|model|mode|range) for ttlMs — the `data` token changes per
 * call so cgGet's URL cache would never hit; this layer makes the model
 * idempotent for polling.
 */
export async function fetchModel1Heatmap(opts: {
  sym: string;
  model?: HeatmapModelId;
  mode?: HeatmapMode;
  range?: string;
  ttlMs?: number;
}): Promise<Model1Heatmap> {
  const sym = opts.sym.trim().toUpperCase();
  const model = opts.model ?? "1";
  const mode = opts.mode ?? "coin";
  const range = opts.range ?? "24h";
  if (!RANGE_G[range]) throw new Error(`unknown range ${range} (use one of ${HEATMAP_RANGES.join(", ")})`);
  const ck = `${sym}|${model}|${mode}|${range}`;
  const hit = cache.get(ck);
  const ttl = opts.ttlMs ?? 20_000;
  if (hit && Date.now() - hit.at < ttl) return hit.model;

  const endpoints = MODEL_ENDPOINTS[model];
  const path = mode === "coin" ? endpoints.coin : endpoints.pair;
  const symbol = mode === "coin" ? sym : pairSymbolFor(sym);
  const q = new URLSearchParams({ merge: "true", symbol });
  if (model === "3") {
    q.set("range", RANGE_X[range]);
    q.set("cp", "false");
  } else {
    const g = RANGE_G[range];
    q.set("interval", String(g.interval));
    q.set("limit", String(g.limit));
  }
  q.set("data", makeObeToken());

  // NOTE: the `obe` header (session) is attached by cgHeaders from
  // CG_SESSION; the `data` param above must stay a FRESH anonymous token
  // even when the session is set (live-verified: session-as-data → 40001).
  let payload: LiqHeatmapPayload & { code?: string; msg?: string };
  try {
    payload = (await cgGet(`${path}?${q.toString()}`, 45_000, FAPI)) as LiqHeatmapPayload & {
      code?: string;
      msg?: string;
    };
  } catch (err) {
    if (hit) {
      return hit.model;
    }
    const anyHit = cache.get(ck);
    if (anyHit) {
      return anyHit.model;
    }
    throw err;
  }
  // "Liquidated in 24h" (coin mode only): /api/index/liquidation24h serves
  // coin aggregates (pair-prefixed symbols read long=short=0 — live-probed
  // 2026-11-22). Short-TTL cached; failures are non-fatal (panel shows —).
  let liq24h: { long: number; short: number } | undefined;
  if (mode === "coin") {
    try {
      const r = (await cgGet(
        `/api/index/liquidation24h?${new URLSearchParams({ symbol: sym, data: makeObeToken() }).toString()}`,
        60_000,
        FAPI
      )) as { long?: number; short?: number };
      if (Number.isFinite(Number(r?.long)) && Number.isFinite(Number(r?.short))) {
        liq24h = { long: Number(r.long), short: Number(r.short) };
      }
    } catch {
      /* panel stays null — the heatmap itself is unaffected */
    }
  }

  const model1 = buildModel1(payload, { model, mode, range, sym, symbol, liq24h });
  cache.set(ck, { at: Date.now(), model: model1 });
  return model1;
}

// ---------------------------------------------------------------------------
// page math — EXACT port of the chunk's ec/ep/ed/eu functions
// ---------------------------------------------------------------------------

export function buildModel1(
  p: LiqHeatmapPayload,
  meta: { model: HeatmapModelId; mode: HeatmapMode; range: string; sym: string; symbol: string; liq24h?: { long: number; short: number } }
): Model1Heatmap {
  if (!p || !Array.isArray(p.prices) || !Array.isArray(p.y) || !Array.isArray(p.liq)) {
    throw new HeatmapUpstreamError("empty", "upstream returned no heatmap grid");
  }
  // ec(): usable bars + ascending y with rank remap (upstream y is NOT
  // guaranteed sorted; the page remaps every liq row through the rank)
  const prices = p.prices.filter(
    (r) => Array.isArray(r) && r.length >= 5 && Number.isFinite(Number(r[0]))
  );
  const cols = prices.length;
  const nY = p.y.length;
  if (!cols || nY < 2) throw new HeatmapUpstreamError("empty", "degenerate heatmap window");

  const yAsc = p.y.map((price, idx) => [Number(price), idx] as const).sort((a, b) => a[0] - b[0]);
  const rank = new Int32Array(nY);
  yAsc.forEach(([, orig], r) => {
    rank[orig] = r;
  });
  const yAxis = yAsc.map(([price]) => price);
  const bin = (yAxis[nY - 1] - yAxis[0]) / (nY - 1) || 1;

  const times = prices.map((r) => (Number(r[0]) < 1e12 ? 1e3 * Number(r[0]) : Number(r[0])));
  const open = prices.map((r) => Number(r[1]));
  const high = prices.map((r) => Number(r[2]));
  const low = prices.map((r) => Number(r[3]));
  const close = prices.map((r) => Number(r[4]));

  // column period: median of consecutive ts deltas (page: round(median/6e4)||5)
  const deltas: number[] = [];
  for (let i = 1; i < cols; i++) deltas.push(times[i] - times[i - 1]);
  deltas.sort((a, b) => a - b);
  const columnMinutes = Math.max(1, Math.round((deltas[Math.floor(deltas.length / 2)] ?? 300_000) / 60_000) || 5);

  // grid — total/longs/shorts[col * nY + row]
  const total = new Float64Array(cols * nY);
  const longs = new Float64Array(cols * nY);
  const shorts = new Float64Array(cols * nY);
  for (const row of p.liq) {
    if (!Array.isArray(row)) continue;
    const c = Number(row[0]) | 0;
    const r = rank[Number(row[1]) | 0];
    const usd = Number(row[2]);
    if (c < 0 || c >= cols || r < 0 || r >= nY || !(usd > 0)) continue;
    const k = c * nY + r;
    total[k] += usd;
    if (yAxis[r] > close[c]) shorts[k] += usd;
    else longs[k] += usd;
  }

  // sparse per-column cells for the UI canvas
  const columns: { ts: number; cells: [number, number][] }[] = [];
  for (let c = 0; c < cols; c++) {
    const cells: [number, number][] = [];
    for (let r = 0; r < nY; r++) {
      const v = total[c * nY + r];
      if (v > 0) cells.push([r, v]);
    }
    if (cells.length) columns.push({ ts: times[c], cells });
  }

  // window-total profile per row
  const profile = new Float64Array(nY);
  for (let c = 0; c < cols; c++) for (let r = 0; r < nY; r++) profile[r] += total[c * nY + r];

  // ---- ep(): key levels from the LAST column ----
  const off = (cols - 1) * nY;
  const lastColumn = new Float64Array(nY);
  for (let r = 0; r < nY; r++) lastColumn[r] = total[off + r];
  let colMax = 0;
  for (let r = 0; r < nY; r++) colMax = Math.max(colMax, lastColumn[r]);
  const peaks: { price: number; value: number }[] = [];
  for (let r = 1; r < nY - 1; r++) {
    const v = lastColumn[r];
    if (v >= lastColumn[r - 1] && v > lastColumn[r + 1] && v > 0.12 * colMax) {
      peaks.push({ price: yAxis[r], value: v });
    }
  }
  peaks.sort((a, b) => b.value - a.value);
  const lastPrice = close[cols - 1];
  // Coinglass panel params (verified against the live page's "Key
  // liquidation levels" panel, 2026-11-22): 8 rows per side, dedup within
  // 2 grid bins (the panel's clustered look — consecutive maxima of one
  // wall collapse into its strongest row).
  const above: HeatmapKeyLevel[] = [];
  const below: HeatmapKeyLevel[] = [];
  for (const pk of peaks) {
    const side = pk.price > lastPrice ? above : below;
    if (side.length >= 8 || side.some((q) => Math.abs(q.price - pk.price) < 2 * bin)) continue;
    side.push({ price: pk.price, usd: pk.value });
  }
  above.sort((a, b) => a.price - b.price);
  below.sort((a, b) => b.price - a.price);

  // ---- ed(): market overview (last column) ----
  let longsSum = 0;
  let shortsSum = 0;
  for (let r = 0; r < nY; r++) {
    longsSum += longs[off + r];
    shortsSum += shorts[off + r];
  }
  // 24h change reference: i = round(1440/dt) bars back, clamped
  const back = Math.min(cols - 1, Math.round(1440 / columnMinutes));
  const chgPct24h = back < cols ? (lastPrice / close[cols - 1 - back] - 1) * 100 : null;
  // "Liquidated in 24h" — attached by the route (coin mode only: the
  // endpoint serves coin aggregates; pair requests read long=short=0)
  const liq24h: { long: number; short: number } | null =
    meta.liq24h && (meta.liq24h.long > 0 || meta.liq24h.short > 0) ? meta.liq24h : null;

  // ---- eu(): cumulative swept-from-price curves ----
  const refRow = Math.min(nY - 1, Math.max(0, Math.floor((lastPrice - yAxis[0]) / bin)));
  const cumulative: { price: number; below: number; above: number }[] = [];
  let runBelow = 0;
  for (let r = refRow + 1; r < nY; r++) {
    runBelow += lastColumn[r];
    cumulative.push({ price: yAxis[r], below: runBelow, above: 0 });
  }
  let runAbove = 0;
  for (let r = refRow - 1; r >= 0; r--) {
    runAbove += lastColumn[r];
    cumulative.push({ price: yAxis[r], below: 0, above: runAbove });
  }

  return {
    model: meta.model,
    mode: meta.mode,
    range: meta.range,
    sym: meta.sym,
    symbol: meta.symbol,
    updateTime: p.updateTime ?? 0,
    precision: p.precision ?? 2,
    rangeLow: p.rangeLow ?? yAxis[0],
    rangeHigh: p.rangeHigh ?? yAxis[nY - 1],
    instrument: p.instrument?.exName
      ? `${String(p.instrument.exName)} ${String(p.instrument.instrumentId ?? "")}`.trim()
      : meta.symbol,
    columns,
    yAxis,
    lastColumn: [...lastColumn],
    profile: [...profile],
    candles: times.map((ts, i) => ({ ts, o: open[i], h: high[i], l: low[i], c: close[i] })),
    keyLevels: { above, below },
    overview: {
      lastPrice,
      longs: longsSum,
      shorts: shortsSum,
      total: longsSum + shortsSum,
      largestLevel: colMax,
      priceStep: bin,
      columnMinutes,
      chgPct24h: chgPct24h === null ? null : Number(chgPct24h.toFixed(2)),
      liq24h,
    },
    cumulative,
    liqRows: p.liq.length,
  };
}
