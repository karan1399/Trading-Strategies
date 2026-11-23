"use client";

import { useMemo, useState } from "react";
import { usePolling } from "./use-cg-data";
import CoinglassHeatmapTable, { type DetailLevel } from "./charts/coinglass-heatmap-table";
import CoinglassWalls from "./charts/coinglass-walls";
import { COLORSCHEME_IDS, COLORSCHEMES, fmtUsd } from "./charts/heatmap-colormaps";

/**
 * Liquidation HeatMap — the CURRENT Coinglass /LiquidationHeatMap page,
 * rebuilt panel-for-panel. Two lenses, like the page's Lens selector:
 *
 *   Walls — the classic wall view (operator request 2026-11-22): the top-24
 *     liquidation clusters drawn as horizontal wall bars from when they
 *     formed until swept, thickness/colour = size, price · $size labels at
 *     the right end, faint heat wash behind, mono candles, max-callouts.
 *     EXACT port of the page's buildWalls()/drawWalls() (see the component).
 *   Heat  — the table grid: rows = price levels, columns = time buckets,
 *     cell background = heat colour (the page's Heat lens).
 *
 *   ┌ controls ─────────────────────────────────────────────────────┐
 *   │ Model 1|2|3 · Coin|Pair · Range ▾ · Lens Walls|Heat · Colours  │
 *   │ Intensity ▾ · (Walls: Split ☐ · row height fixed like the page) │
 *   ├ chart ┬─────┬ overview ──────┤
 *   │       │     │ key levels     │
 *   └───────┴─────┴────────────────┘
 *
 *  - Coin mode = ALL exchanges aggregated (default); Pair = Binance
 *  - Key liquidation levels: top-8 clusters/side with distance % (page panel)
 *  - Market overview: open levels, longs/shorts split, liquidated in 24h,
 *    largest level, price step / column period
 *  - Data: /api/cg/stopmap?model=&mode=&range= — needs CG_SESSION in .env
 *    (the anonymous Coinglass API serves BTC pair 24h only)
 */

const RANGES = ["12h", "24h", "48h", "3d", "7d", "14d", "30d", "90d", "180d", "1y", "2y"] as const;
const MODELS = ["1", "2", "3"] as const;
const LENSES = ["walls", "heat"] as const;
const DETAILS: { v: DetailLevel; l: string }[] = [
  { v: "low", l: "Low" },
  { v: "mid", l: "Mid" },
  { v: "high", l: "High" },
  { v: "full", l: "Full" },
];

interface Model1Response {
  ok: boolean;
  supported?: boolean;
  reason?: string;
  error?: string;
  sym: string;
  symbol?: string;
  instrument?: string;
  source?: string;
  stale?: boolean;
  window?: { interval: string; bars: number; from: number; to: number };
  model?: {
    model: string;
    mode: "coin" | "pair";
    range: string;
    symbol?: string;
    updateTime: number;
    precision: number;
    yAxis: number[];
    columns: { ts: number; cells: [number, number][] }[];
    candles: { ts: number; o: number; h: number; l: number; c: number }[];
    lastColumn: number[];
    keyLevels: { above: { price: number; usd: number }[]; below: { price: number; usd: number }[] };
    overview: {
      lastPrice: number;
      longs: number;
      shorts: number;
      total: number;
      largestLevel: number;
      priceStep: number;
      columnMinutes: number;
      chgPct24h: number | null;
      liq24h: { long: number; short: number } | null;
    };
    liqRows: number;
  };
}

/** price decimals from magnitude (page precision is per-instrument) */
function priceDecs(p: number): number {
  if (p >= 1000) return 2;
  if (p >= 10) return 2;
  if (p >= 1) return 3;
  return 6;
}

export default function StopmapSection({ sym = "BTC" }: { sym?: string }) {
  const [model, setModel] = useState<(typeof MODELS)[number]>("1");
  const [mode, setMode] = useState<"coin" | "pair">("coin");
  /**
   * Default to 24h, not 3d (R827 fix).
   *
   * The anonymous Coinglass liqHeatMap endpoint serves **BTC 24h only**; every
   * other (symbol, range) combination returns code 40000 "needs a login".
   * Defaulting to 3d meant the Liquidation Heat Map tab opened on a guaranteed
   * failure for every symbol including BTC — the operator saw an error before
   * ever touching a control. 24h is the one range that renders out of the box,
   * so it is the honest default; widen it once CG_SESSION is configured.
   */
  const [range, setRange] = useState<string>("24h");
  const [lens, setLens] = useState<(typeof LENSES)[number]>("walls");
  const [colours, setColours] = useState<string>("viridis");
  const [intensity, setIntensity] = useState<number>(0);
  const [split, setSplit] = useState(false);
  const [detail, setDetail] = useState<DetailLevel>("mid");
  const [showCandles, setShowCandles] = useState(true);
  const [showValues, setShowValues] = useState(false);

  const q = usePolling<Model1Response>(
    `/api/cg/stopmap?sym=${sym}&model=${model}&mode=${mode}&range=${range}`,
    30_000
  );
  // defense-in-depth: never render a model that belongs to another symbol
  const m = q.data && q.data.sym === sym ? q.data.model : undefined;
  const isModel1Shape = Boolean(m?.keyLevels && m?.overview && m?.yAxis);
  const decs = useMemo(() => priceDecs(m?.overview?.lastPrice ?? 1000), [m]);
  const fmtP = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: decs, maximumFractionDigits: decs });
  const px = m?.overview?.lastPrice;

  // ---- error / setup states ----
  if (!m || !isModel1Shape) {
    const needsSession = /CG_SESSION|login|40000/.test(q.error ?? "");
    return (
      <div className="flex min-h-72 flex-col items-center justify-center gap-3 rounded-lg border border-neutral-800 bg-neutral-900/60 p-6 text-center text-sm text-neutral-400">
        <div className="text-4xl">🌡️</div>
        {q.loading ? (
          <div>Loading Model {model} {range} heatmap…</div>
        ) : needsSession ? (
          <>
            <div className="font-medium text-neutral-200">
              {sym} @ {range} needs a Coinglass login
            </div>
            <div className="max-w-lg text-xs leading-relaxed text-neutral-500">
              Free range: <span className="text-emerald-300">BTC · 24h</span>. Every other symbol and
              range needs your Coinglass web session. One-time local setup:
            </div>
            <code className="rounded bg-neutral-800 px-3 py-1.5 text-[11px] text-emerald-300">
              CG_USER=you@example.com CG_PASS=yourpass node scripts/cg-session.mjs
            </code>
            <div className="text-xs text-neutral-500">then restart the terminal (npm run dev)</div>
            {!(sym === "BTC" && range === "24h") && (
              <button
                type="button"
                onClick={() => setRange("24h")}
                className="rounded border border-emerald-700/60 bg-emerald-900/30 px-3 py-1.5 text-[11px] font-medium text-emerald-300 transition hover:bg-emerald-900/60"
              >
                Switch to BTC · 24h (works without login)
              </button>
            )}
          </>
        ) : (
          <div className="max-w-lg">
            {q.error ? (
              /429|rate limit/i.test(q.error) ? (
                <div className="space-y-2">
                  <div className="font-medium text-amber-400">Coinglass is cooling down (HTTP 429 rate limit)</div>
                  <div className="text-xs text-neutral-400">
                    High-speed telemetry collection is active. The terminal will automatically refresh when the cooldown clears.
                  </div>
                </div>
              ) : `Heatmap error: ${q.error}`
            ) : q.data?.reason || q.data?.error || "No data"}
          </div>
        )}
        {q.data?.source && !q.data?.source.includes("coinglass") && (
          <div className="text-[11px] text-neutral-600">source: {q.data.source}</div>
        )}
      </div>
    );
  }

  const ov = m.overview;

  // key-level row (page: price · size · from-price % · strength bar)
  const levelRow = (price: number, usd: number, side: "above" | "below") => {
    const distPct = px ? ((price - px) / px) * 100 : 0;
    const strength = ov.largestLevel > 0 ? Math.min(100, (usd / ov.largestLevel) * 100) : 0;
    return (
      <tr key={`${side}-${price}`} className="border-t border-neutral-800/40">
        <td className="py-1 font-mono text-neutral-200">{fmtP(price)}</td>
        <td className={`py-1 text-right font-mono ${side === "above" ? "text-red-400/90" : "text-emerald-400/90"}`}>
          {side === "above" ? "+" : ""}{distPct.toFixed(2)}%
        </td>
        <td className="py-1 text-right font-mono text-neutral-200">{fmtUsd(usd)}</td>
        <td className="py-1 pl-2">
          <div className="h-1.5 w-full overflow-hidden rounded bg-neutral-800">
            <div className={`h-full ${side === "above" ? "bg-red-500/80" : "bg-emerald-500/80"}`} style={{ width: `${strength}%` }} />
          </div>
        </td>
      </tr>
    );
  };

  const updated = m.updateTime ? new Date(m.updateTime).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "—";

  return (
    <div className="flex flex-col gap-3">
      {/* ---- controls row (the page's selector strip) ---- */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-neutral-800 bg-neutral-900/60 px-3 py-2">
        <div className="flex overflow-hidden rounded-md border border-neutral-700 text-xs">
          {MODELS.map((v) => (
            <button
              key={v}
              onClick={() => setModel(v)}
              className={`px-2.5 py-1 ${model === v ? "bg-neutral-100 font-medium text-neutral-900" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
            >
              Model {v}
            </button>
          ))}
        </div>
        <div className="flex overflow-hidden rounded-md border border-neutral-700 text-xs">
          {(["coin", "pair"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setMode(v)}
              title={v === "coin" ? "All exchanges aggregated" : "Binance execution venue"}
              className={`px-2.5 py-1 capitalize ${mode === v ? "bg-neutral-100 font-medium text-neutral-900" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
            >
              {v}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-1.5 text-xs text-neutral-400">
          Range
          <select
            value={range}
            onChange={(e) => setRange(e.target.value)}
            className="rounded-md border border-neutral-700 bg-neutral-900 px-1.5 py-1 text-xs text-neutral-200"
          >
            {RANGES.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </label>
        <div className="flex overflow-hidden rounded-md border border-neutral-700 text-xs">
          {(["walls", "heat"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setLens(v)}
              title={v === "walls" ? "Top-24 clusters as wall bars — formed until swept (the classic view)" : "Continuous heat grid — price rows × time cols"}
              className={`px-2.5 py-1 capitalize ${lens === v ? "bg-neutral-100 font-medium text-neutral-900" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
            >
              {v}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-1.5 text-xs text-neutral-400">
          Colours
          <select
            value={colours}
            onChange={(e) => setColours(e.target.value)}
            className="rounded-md border border-neutral-700 bg-neutral-900 px-1.5 py-1 text-xs text-neutral-200"
          >
            {COLORSCHEME_IDS.map((id) => (
              <option key={id} value={id}>{COLORSCHEMES[id].label}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-neutral-400">
          Intensity
          <input
            type="range"
            min={0}
            max={90}
            step={5}
            value={intensity * 100}
            onChange={(e) => setIntensity(Number(e.target.value) / 100)}
            className="h-1 w-24 accent-emerald-400"
          />
          <span className="w-8 font-mono text-[10px] text-neutral-500">≥{intensity * 100}%</span>
        </label>
        {lens === "walls" ? (
          <label className="flex items-center gap-1.5 text-xs text-neutral-400">
            <input type="checkbox" checked={split} onChange={(e) => setSplit(e.target.checked)} className="accent-emerald-400" />
            Split
          </label>
        ) : (
          <>
            <div className="flex overflow-hidden rounded-md border border-neutral-700 text-xs">
              {DETAILS.map((d) => (
                <button
                  key={d.v}
                  onClick={() => setDetail(d.v)}
                  title={`~${d.v === "low" ? "24 price rows × 36 time cols" : d.v === "mid" ? "48 × 72" : d.v === "high" ? "96 × 144" : "every raw bin"} grid`}
                  className={`px-2 py-1 ${detail === d.v ? "bg-neutral-100 font-medium text-neutral-900" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
                >
                  {d.l}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-1.5 text-xs text-neutral-400">
              <input type="checkbox" checked={showValues} onChange={(e) => setShowValues(e.target.checked)} className="accent-emerald-400" />
              Values
            </label>
            <label className="flex items-center gap-1.5 text-xs text-neutral-400">
              <input type="checkbox" checked={showCandles} onChange={(e) => setShowCandles(e.target.checked)} className="accent-emerald-400" />
              Close path
            </label>
          </>
        )}
        <span className="ml-auto flex items-center gap-2 text-[10px] text-neutral-500">
          <span className={`inline-block h-1.5 w-1.5 rounded-full ${q.data?.stale ? "bg-amber-400" : "bg-emerald-400"}`} />
          Updated {updated} · {mode === "coin" ? "all venues" : "Binance"} · {q.data?.instrument ?? m.symbol}
        </span>
      </div>

      {/* ---- chart + right panels ---- */}
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900/60 p-1.5">
          {lens === "walls" ? (
            <CoinglassWalls
              model={{
                yAxis: m.yAxis,
                columns: m.columns,
                candles: m.candles,
                lastPrice: ov.lastPrice,
                priceStep: ov.priceStep,
              }}
              colors={colours}
              intensityMin={intensity}
              split={split}
              showCandles={showCandles}
              height={560}
              priceDecs={Math.min(2, decs)}
            />
          ) : (
            <CoinglassHeatmapTable
              model={{
                yAxis: m.yAxis,
                columns: m.columns,
                candles: m.candles,
                keyLevels: m.keyLevels,
                lastPrice: ov.lastPrice,
                priceStep: ov.priceStep,
                columnMinutes: ov.columnMinutes,
                updateTime: m.updateTime,
              }}
              colors={colours}
              intensityMin={intensity}
              detail={detail}
              showCandles={showCandles}
              showValues={showValues}
              priceDecs={Math.min(2, decs)}
            />
          )}
        </div>

        <div className="flex flex-col gap-3">
          {/* Market overview — the page's right panel */}
          <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 p-3">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">Market overview</h3>
            <dl className="space-y-1.5 text-[11px]">
              <div className="flex justify-between">
                <dt className="text-neutral-500">Price</dt>
                <dd className="font-mono text-neutral-100">
                  {fmtP(ov.lastPrice)}
                  {ov.chgPct24h !== null && (
                    <span className={ov.chgPct24h >= 0 ? " text-emerald-400" : " text-red-400"}> {ov.chgPct24h >= 0 ? "+" : ""}{ov.chgPct24h}%</span>
                  )}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-neutral-500">Open liquidation levels</dt>
                <dd className="font-mono font-semibold text-neutral-100">{fmtUsd(ov.total)}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-neutral-500">Longs (below price)</dt>
                <dd className="font-mono text-emerald-400">
                  {fmtUsd(ov.longs)} <span className="text-neutral-500">· {ov.total > 0 ? Math.round((ov.longs / ov.total) * 100) : 0}%</span>
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-neutral-500">Shorts (above price)</dt>
                <dd className="font-mono text-red-400">
                  {fmtUsd(ov.shorts)} <span className="text-neutral-500">· {ov.total > 0 ? Math.round((ov.shorts / ov.total) * 100) : 0}%</span>
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-neutral-500">Liquidated in 24h</dt>
                <dd className="font-mono text-neutral-200">
                  {ov.liq24h ? fmtUsd(ov.liq24h.long + ov.liq24h.short) : "—"}
                </dd>
              </div>
              {ov.liq24h && (
                <div className="flex justify-between">
                  <dt className="pl-3 text-neutral-600">of which longs / shorts</dt>
                  <dd className="font-mono text-[10px] text-neutral-400">
                    {fmtUsd(ov.liq24h.long).slice(1)} / {fmtUsd(ov.liq24h.short).slice(1)}
                  </dd>
                </div>
              )}
              <div className="flex justify-between">
                <dt className="text-neutral-500">Largest single level</dt>
                <dd className="font-mono text-neutral-100">{fmtUsd(ov.largestLevel)}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-neutral-500">Price step / column</dt>
                <dd className="font-mono text-neutral-300">{ov.priceStep.toFixed(2)} / {ov.columnMinutes}m</dd>
              </div>
            </dl>
            {/* long/short split bar */}
            <div className="mt-2 flex h-1.5 overflow-hidden rounded bg-neutral-800">
              <div className="bg-emerald-500/80" style={{ width: `${ov.total > 0 ? (ov.longs / ov.total) * 100 : 0}%` }} />
              <div className="bg-red-500/80" style={{ width: `${ov.total > 0 ? (ov.shorts / ov.total) * 100 : 0}%` }} />
            </div>
            <div className="mt-1 flex justify-between text-[9px] text-neutral-500">
              <span>longs {ov.total > 0 ? Math.round((ov.longs / ov.total) * 100) : 0}%</span>
              <span>shorts {ov.total > 0 ? Math.round((ov.shorts / ov.total) * 100) : 0}%</span>
            </div>
          </div>

          {/* Key liquidation levels — the page's right panel */}
          <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 p-3">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-400">Key liquidation levels</h3>
              <span className="text-[10px] text-neutral-600">From price</span>
            </div>

            <div className="mb-1 text-[10px] font-medium text-red-400/90">🔴 Above — short liquidations</div>
            <table className="mb-2 w-full text-[11px]">
              <tbody>
                {m.keyLevels.above.map((k) => levelRow(k.price, k.usd, "above"))}
                {!m.keyLevels.above.length && (
                  <tr><td colSpan={4} className="py-3 text-center text-neutral-600">No clusters above.</td></tr>
                )}
              </tbody>
            </table>

            <div className="mb-1 text-[10px] font-medium text-emerald-400/90">🟢 Below — long liquidations</div>
            <table className="w-full text-[11px]">
              <tbody>
                {m.keyLevels.below.map((k) => levelRow(k.price, k.usd, "below"))}
                {!m.keyLevels.below.length && (
                  <tr><td colSpan={4} className="py-3 text-center text-neutral-600">No clusters below.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* ---- reading guide ---- */}
      <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 px-3 py-2 text-[10px] leading-relaxed text-neutral-500">
        Coinglass Model {m.model} estimates ({m.mode} mode · {m.range} window · {m.liqRows.toLocaleString()} raw price bins · row height fixed at
        {" "}{(560 / m.yAxis.length).toFixed(1)}px/bin like the page): leveraged positions that would be force-closed at each price — exchanges do not
        publish individual liquidation prices, so every number is a Coinglass estimate. <strong className="text-neutral-400">Walls</strong> — the page&rsquo;s
        exact buildWalls(): runs of normalized ≥0.3 merged across ±1 row (80% column overlap), re-split on ≥2 empty columns, top 24 by
        size×width, drawn formed→swept with thickness tier 3/5/8/11px and LUT colour at peak intensity (floor .35); walls reaching the right
        edge are <em>active</em> (not yet swept), and the top-14 carry price·size labels. <strong className="text-neutral-400">Heat</strong> — the
        continuous grid (rows aggregate {" "}{m.overview.priceStep.toFixed(2)}-wide bins, cell colour = that row·column&rsquo;s open liquidations, right Σ
        column time-summed). Bright clusters are liquidity magnets that <em>attract</em> sweeps. For pullback entries along the trend: in an
        uptrend the below-price (green) walls are the zones price is drawn toward; above-price (red) walls cap squeezes.
      </div>
    </div>
  );
}
