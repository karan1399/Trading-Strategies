#!/usr/bin/env node
/**
 * CG Terminal — MCP server for Antigravity (or any MCP client).
 * ---------------------------------------------------------------------------
 * Zero-dependency Model Context Protocol server (JSON-RPC 2.0 over stdio)
 * that plugs the local order-flow terminal directly into the agent:
 *
 *   - market_snapshot   auction VAL/POC/VAH · EMA 13/89/233 · RSI · VWAP bands
 *   - signals           Coinglass-exact footprint signals (3 bar types)
 *   - notifications     the live event feed (cursor-based polling)
 *   - liquidation_levels, whale_orders, book_walls, cvd_state
 *   - stopmap           BTC liquidation-heatmap window (24h, endTime history)
 *   - footprint         aggregated order-flow bars (per-bar delta + POC)
 *   - large_orders       large resting orders AND executed large trades
 *   - kline_series       raw candles / open-interest series per instrument
 *   - analyze_setup     one-call context bundle for trade formulation
 *
 * Every tool exposes the full dimension set of its HTTP route (asset,
 * timeframe, market, band, history endTime) so the agent has access to each
 * data point at each dimension — see the access matrix in the Trade
 * Formulation Playbook.
 *
 * Configure in Antigravity (Settings → MCP → add server):
 *   command: node  /ABSOLUTE/PATH/TO/mcp/server.mjs
 *   env:     TERMINAL_URL=http://localhost:3000
 *
 * The agent wake-up loop (see AGENTS.md):
 *   1. call `notifications` with the stored cursor id
 *   2. for every new HIGH-severity event call `analyze_setup`
 *   3. formulate the entry, stop and targets from the returned data
 */

import readline from "node:readline";
import { readFileSync as fsRead, writeFileSync as fsWrite, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const TERMINAL_URL = (process.env.TERMINAL_URL || "http://127.0.0.1:3000").replace(/\/+$/, "");
const CURSOR_FILE = new URL("../data/mcp-cursor.json", import.meta.url);

function resolveSymArg(args, fallback = "BTC") {
  const raw = String(args?.sym ?? args?.symbol ?? fallback).trim();
  if (!raw) return fallback;
  const m = raw.match(/^(?:[A-Za-z]+_)?([A-Za-z0-9]+?)(?:USDT|USD|-USDT-SWAP|-USD)?$/i);
  return (m?.[1] ?? raw).toUpperCase();
}

async function api(path) {
  const res = await fetch(`${TERMINAL_URL}${path}`, { signal: AbortSignal.timeout(120_000) });
  const json = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
  return json;
}

function readCursor() {
  try {
    return JSON.parse(fsRead(fileURLToPath(CURSOR_FILE))).lastId ?? 0;
  } catch {
    return 0;
  }
}
function writeCursor(id) {
  try {
    fsWrite(fileURLToPath(CURSOR_FILE), JSON.stringify({ lastId: id, updatedAt: Date.now() }, null, 2));
  } catch {
    /* non-fatal */
  }
}
function ensureDataDir() {
  try {
    mkdirSync(path.dirname(fileURLToPath(CURSOR_FILE)), { recursive: true });
  } catch {}
}

const fmtP = (v, d = 2) => (v == null || !Number.isFinite(v) ? null : +Number(v).toFixed(d));
const fmtUsd = (v) =>
  v == null || !Number.isFinite(v)
    ? "—"
    : Math.abs(v) >= 1e9
      ? `$${(v / 1e9).toFixed(2)}B`
      : Math.abs(v) >= 1e6
        ? `$${(v / 1e6).toFixed(1)}M`
        : `$${Math.round(v / 1e3)}K`;
const fmtM = (v) => (v == null || !Number.isFinite(v) ? "—" : `$${(v / 1e6).toFixed(1)}M`);
const tsOf = (t) => new Date(t > 1e12 ? t : t * 1000).toISOString().slice(5, 16).replace("T", " ");

// ---- interval vocabularies (keep in lockstep with the routes) ----
// The routes do NOT share one interval vocabulary — verified per route:
//   · orderflow-agg / largeorder / kline validate via src/lib/cg/params.ts
//     parseInterval → m-tokens ONLY: m1 m3 m5 m15 m30 h1 h2 h4 h6 h12 d1 w1
//   · bookwalls accepts page tokens 1m 5m 15m 1h 4h 1d (+ m-token aliases
//     via its NORMALIZE map)
//   · cvd accepts page tokens 1m 5m 15m 30m 1h 4h 1d (+ m-token aliases)
// Historical bug: every MCP tool defaulted to "15m", so the parseInterval
// routes 400'd on each default call ("invalid interval"). Each tool now
// validates against its route's NATIVE vocabulary and accepts the other
// style as an alias — an agent passing "15m" or "m15" always gets the
// duration it asked for, never a silent fallback to the default.
const M_INTERVALS = ["m1", "m3", "m5", "m15", "m30", "h1", "h2", "h4", "h6", "h12", "d1", "w1"];
const PAGE_TO_M = { "1m": "m1", "3m": "m3", "5m": "m5", "15m": "m15", "30m": "m30", "1h": "h1", "2h": "h2", "4h": "h4", "6h": "h6", "12h": "h12", "1d": "d1", "1w": "w1" };
const M_TO_PAGE = { m1: "1m", m5: "5m", m15: "15m", m30: "30m", h1: "1h", h4: "4h", d1: "1d" };
const BOOKWALLS_ITV = ["1m", "5m", "15m", "1h", "4h", "1d"];
const CVD_ITV = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
function resolveInterval(v, native, alias, fallback) {
  const s = typeof v === "string" ? v.trim() : "";
  if (native.includes(s)) return s;
  const a = alias[s];
  return a != null && native.includes(a) ? a : fallback;
}

// ---------------------------------------------------------------------------
// Tool implementations — each returns MCP content (text + structured)
// ---------------------------------------------------------------------------

async function toolListAssets() {
  const d = await api("/api/cg/assets");
  // honest failure (adversarial review finding 2): a non-ok route body must
  // surface as an MCP error — `d.assets ?? []` on a 500 would report
  // "0 tradable assets" as SUCCESS, a fabricated answer to the agent.
  if (!d || d.ok === false || !Array.isArray(d.assets)) {
    throw new Error(`assets route unhealthy: ${JSON.stringify(d).slice(0, 200)}`);
  }
  // FULL CURATED-61 (2026-11-12 directive): 58 curated perps (30 crypto +
  // 28 TradFi) + 3 classic (SPX500/UKO/NDX100) — membership live from the
  // route (source of truth: src/lib/universe/curated.ts), never hardcoded.
  const curated = Array.isArray(d.curated) ? d.curated : [];
  const classic = Array.isArray(d.curatedClassic) ? d.curatedClassic : [];
  if (!curated.length) {
    throw new Error("assets route returned no curated list — universe unavailable");
  }
  const crypto = curated.filter((a) => a.kind === "crypto-perp");
  const tradfi = curated.filter((a) => a.kind === "tradfi-perp");
  const lines = [
    `${curated.length + classic.length} assets: ${crypto.length} crypto perps + ${tradfi.length} TradFi perps + ${classic.length} classic — live from the terminal:`,
    "",
    "-- CRYPTO PERPS --",
    ...crypto.map((a) => `${a.sym.padEnd(10)} ${a.name} [${a.sub}]${a.punchable ? "" : " (live-only, no testnet)"}`),
    "-- TRADFI PERPS (Binance plain-name USDT-M) --",
    ...tradfi.map((a) => `${a.sym.padEnd(10)} ${a.name} [${a.sub}]${a.spotPair ? ` · spot ${a.spotPair}` : ""}${a.punchable ? "" : " (live-only)"}`),
    "-- CLASSIC (no perp engine — quotes + bars) --",
    ...classic.map((a) => `${a.sym.padEnd(10)} ${a.name} [${a.sub}]`),
  ];
  return { text: lines.join("\n"), data: { curatedTotal: d.curatedTotal ?? curated.length, classicTotal: classic.length, total: curated.length + classic.length, curated, curatedClassic: classic } };
}

async function toolMarketSnapshot(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/market?sym=${sym}`);
  if (!d.ok) throw new Error(d.error || "market snapshot failed");
  const a = d.auction;
  const lines = [
    `${d.sym} (${d.name}) — last ${d.price.last} · day ${fmtP(d.price.chgDayPct)}% · H ${d.price.dayHigh} / L ${d.price.dayLow} (UTC day)`,
    `Auction session(8h): VAL ${fmtP(a.session.val)} · POC ${fmtP(a.session.poc)} · VAH ${fmtP(a.session.vah)}`,
    `Auction day (UTC):   VAL ${fmtP(a.day.val)} · POC ${fmtP(a.day.poc)} · VAH ${fmtP(a.day.vah)}`,
    `Auction month (UTC): VAL ${fmtP(a.month.val)} · POC ${fmtP(a.month.poc)} · VAH ${fmtP(a.month.vah)}`,
    `VWAP (day): ${fmtP(d.vwap.vwap)} · +1σ ${fmtP(d.vwap.vwap + d.vwap.bands.sd1)} · +2σ ${fmtP(d.vwap.vwap + d.vwap.bands.sd2)} · +3σ ${fmtP(d.vwap.vwap + d.vwap.bands.sd3)} · −1σ ${fmtP(d.vwap.vwap - d.vwap.bands.sd1)} · −2σ ${fmtP(d.vwap.vwap - d.vwap.bands.sd2)} · −3σ ${fmtP(d.vwap.vwap - d.vwap.bands.sd3)}`,
  ];
  for (const tf of ["m15", "h1", "h4", "d1"]) {
    lines.push(
      `${tf}: EMA13 ${fmtP(d.ema[tf].e13)} · EMA89 ${fmtP(d.ema[tf].e89)} · EMA233 ${fmtP(d.ema[tf].e233)} · 13>89 ${d.ema[tf].e13 > d.ema[tf].e89 ? "BULL" : "BEAR"} · RSI ${fmtP(d.rsi[tf], 1)}`
    );
  }
  return { text: lines.join("\n"), data: d };
}

async function toolSignals(args) {
  const sym = resolveSymArg(args, "BTC");
  const bt = args?.bt ?? "all";
  const d = await api(`/api/cg/signals?sym=${sym}&bt=${bt}`);
  if (!d.ok) throw new Error(d.error || "signals failed");
  const lines = [`${sym} footprint signals — ${d.counts.all} total (${d.counts.bullish} bullish / ${d.counts.bearish} bearish) · thresholds: volume ${d.thresholds.vol} ${sym}, range ${d.thresholds.range}`];
  for (const set of d.sets) {
    lines.push(`\n[${set.label}] ${set.sigs.length} signals`);
    for (const s of set.sigs.slice(-12).reverse()) {
      lines.push(
        `  ${new Date(s.ts * 1000).toISOString().slice(5, 16).replace("T", " ")} ${s.side === "buy" ? "BULL" : "BEAR"} ${s.name ?? s.t} @ ${s.p}${s.main ? " — " + s.main : ""}`
      );
    }
  }
  return { text: lines.join("\n"), data: { sym: d.sym, counts: d.counts, sets: d.sets.map((x) => ({ bt: x.bt, label: x.label, sigs: x.sigs.slice(-30) })) } };
}

async function toolNotifications(args) {
  ensureDataDir();
  const since = args?.since != null ? Number(args.since) : readCursor();
  const limit = Math.min(Math.max(Number(args?.limit ?? 50), 1), 200);
  const p = new URLSearchParams({ since: String(since), limit: String(limit) });
  if (args?.asset) p.set("asset", String(args.asset).toUpperCase());
  if (args?.source) p.set("source", args.source);
  const d = await api(`/api/cg/notifications?${p.toString()}`);
  if (!d.ok) throw new Error(d.error || "notifications failed");
  const lines = d.items.map(
    (n) =>
      `#${n.id} ${new Date(n.ts).toISOString().slice(5, 16).replace("T", " ")} [${n.severity === "high" ? "HIGH" : "info"}] ${n.asset} ${n.dir.toUpperCase()} ${n.source}: ${n.title} — ${n.detail}`
  );
  if (d.items.length) {
    const lastId = Math.max(...d.items.map((n) => n.id));
    if (args?.advance_cursor !== false) writeCursor(lastId);
  }
  return {
    text: (lines.length ? lines.join("\n") : "no new notifications since cursor") + `\n(cursor now ${readCursor()}, ${d.stats.last24h} events/24h)`,
    data: { items: d.items, stats: d.stats, cursor: readCursor() },
  };
}

async function toolLivePrice(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/ticker?sym=${sym}`);
  if (!d.ok || d.price == null) throw new Error(d.error || "ticker failed");
  const dir = d.dir === "up" ? "▲" : d.dir === "down" ? "▼" : "=";
  const age = d.ageMs != null ? ` · upstream age ${Math.round(d.ageMs)}ms` : "";
  return {
    text: `${sym} ${d.price} ${dir}${age} — 1s-grade feed (forming m1 close via Coinglass)`,
    data: d,
  };
}

async function toolLiquidationLevels(args) {
  const sym = resolveSymArg(args, "BTC");
  const range = ["3d", "7d", "14d", "30d"].includes(args?.range) ? args.range : "3d";
  const d = await api(`/api/cg/liqlevels?sym=${sym}&range=${range}&lvR=4`);
  if (!d.ok) throw new Error(d.error || "liquidation levels failed");
  if (!d.stats) return { text: d.reason ?? `${sym}: liquidation data unavailable`, data: d };
  const s = d.stats;
  const lines = [
    `${sym} liquidation clusters (${range}, ±4%, ${d.venues.join("+")}) — reference price ${s.last}`,
    ...(d.stale ? [`STALE snapshot from ${new Date(d.updatedAt).toISOString()}`] : []),
    ...(d.failedVenues?.length ? [`degraded coverage — ${d.failedVenues.join(", ")} not responding`] : []),
    ...(Object.keys(d.rangeFallback ?? {}).length
      ? [`${Object.entries(d.rangeFallback).map(([v, r]) => `${v}: ${r.toUpperCase()} window (requested range unavailable upstream)`).join(" · ")}`]
      : []),
    `est. open liq: shorts(above) ${fmtP(s.totals.shorts, 0)} · longs(below) ${fmtP(s.totals.longs, 0)}`,
    `strongest above: ${s.keyLevels.above.map((l) => `${fmtP(l.price)} (+${fmtP(l.distPct)}%, str ${fmtP(l.strength, 0)}, ${l.lev} lev)`).join(" | ")}`,
    `strongest below: ${s.keyLevels.below.map((l) => `${fmtP(l.price)} (−${fmtP(l.distPct)}%, str ${fmtP(l.strength, 0)}, ${l.lev} lev)`).join(" | ")}`,
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolLiqLive(args) {
  const sym = resolveSymArg(args, "");
  const limit = Math.max(1, Math.min(60, Number(args?.limit ?? 15) || 15));
  const url = sym
    ? `/api/cg/liq-live?sym=${encodeURIComponent(sym)}&limit=${limit}`
    : `/api/cg/liq-live?globalLimit=${limit}`;
  const d = await api(url);
  if (!d.ok) throw new Error(d.error || "live liquidations failed");
  const venues = (d.venues ?? []).map((v) => `${v.venue}:${v.status}(${v.events}ev)`).join(" ");
  const lines = [
    `LIVE liquidations — collector ${d.stale ? "STALE" : d.quiet ? "quiet (healthy, no events)" : "live"} · venues ${venues}`,
    `source: ${d.source}`,
  ];
  if (d.asset) {
    const a = d.asset;
    if (!a.supported) {
      lines.push(`${a.sym}: ${a.reason ?? "no perp venue carries this instrument"}`);
    } else {
      lines.push(
        `${a.sym} 1h: ${a.stats1h.count} prints $${(a.stats1h.usd / 1000).toFixed(1)}K (${a.stats1h.longs}L/${a.stats1h.shorts}S) · 24h: ${a.stats24h.count} prints $${(a.stats24h.usd / 1000).toFixed(1)}K (${a.stats24h.longs}L/${a.stats24h.shorts}S)`
      );
      for (const e of a.events.slice(0, limit)) {
        lines.push(
          `  ${new Date(e.t).toISOString().slice(11, 19)} ${e.venue.toUpperCase()} ${e.side === "long" ? "LONG-LIQ" : "SHORT-LIQ"} ${e.qtyBase ?? "?"} @ ${e.price} ($${e.usd != null ? e.usd.toFixed(0) : "?"})`
        );
      }
      if (!a.events.length) lines.push(`  no prints in the stored 24h window${a.stats24h.lastAt ? ` (last ${Math.round((Date.now() - a.stats24h.lastAt) / 60000)}m ago)` : ""}`);
    }
  }
  if (d.global) {
    const g = d.global;
    lines.push(
      `universe 1h: ${g.universe1h.count} prints $${(g.universe1h.usd / 1000).toFixed(1)}K · 24h: ${g.universe24h.count} prints $${(g.universe24h.usd / 1000).toFixed(1)}K`
    );
    const hot = g.assets.filter((x) => x.count24h > 0).slice(0, 10);
    if (hot.length) lines.push(`hot (24h): ${hot.map((x) => `${x.sym} ${x.count24h}/$${(x.usd24h / 1000).toFixed(0)}K`).join(" · ")}`);
    for (const e of g.tape.slice(0, limit)) {
      lines.push(
        `  ${new Date(e.t).toISOString().slice(11, 19)} ${e.asset} ${e.venue.toUpperCase()} ${e.side === "long" ? "LONG-LIQ" : "SHORT-LIQ"} ${e.qtyBase ?? "?"} @ ${e.price} ($${e.usd != null ? e.usd.toFixed(0) : "?"})`
      );
    }
  }
  return { text: lines.join("\n"), data: d };
}

async function toolWhaleOrders(args) {
  const sym = resolveSymArg(args, "BTC");
  const minUsd = Math.max(0, Number(args?.minUsd ?? 1_000_000));
  const d = await api(`/api/cg/lto?symbol=${sym}&minVolUsd=${minUsd}`);
  if (!d.ok) throw new Error(d.error || "whale orders failed");
  const fmtUsd = (v) => (Math.abs(v) >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${(v / 1e3).toFixed(0)}K`);
  let bid = 0;
  let ask = 0;
  const rows = [];
  for (const g of d.groups ?? []) {
    for (const o of g.list ?? []) {
      // Coinglass largeTakerOrder side convention (src/lib/cg/types.ts
      // LtoOrder.side): 1 = ask (sell wall), 2 = bid (buy wall) — side is
      // NEVER 0. Verified empirically against live /api/cg/lto BTC data:
      // every side=1 order sits above the live price, every side=2 below.
      // The old `side === 0` test classified ALL whale size as asks.
      if (o.side === 2) bid += o.currentUsd;
      else ask += o.currentUsd;
      rows.push({ ex: g.instrument?.exName, inst: g.instrument?.instrumentId, side: o.side === 2 ? "bid" : "ask", price: o.price, usd: o.currentUsd, filled: o.volUsd });
    }
  }
  rows.sort((a, b) => b.usd - a.usd);
  const imb = bid + ask > 0 ? ((bid - ask) / (bid + ask)) * 100 : 0;
  const lines = [
    `${sym} whale open orders ≥ ${fmtUsd(minUsd)} across ${Object.keys(d.perExchange ?? {}).length} exchanges — ${rows.length} orders${d.stale ? " [STALE CACHED]" : ""}${d.reason ? ` (${d.reason})` : ""}`,
    `aggregate: bids ${fmtUsd(bid)} vs asks ${fmtUsd(ask)} · imbalance ${fmtP(imb, 1)}% ${imb > 0 ? "(bid-heavy)" : "(ask-heavy)"}`,
    ...(rows.length
      ? [`top walls:`, ...rows.slice(0, 10).map((r) => `  ${r.side.toUpperCase()} ${fmtUsd(r.usd)} @ ${r.price} (${r.ex} ${r.inst})${r.filled ? ` · filled ${fmtUsd(r.filled)}` : ""}`)]
      : ["no resting Coinglass whale walls ≥ threshold"]),
  ];
  if (d.hlFallback) {
    const h = d.hlFallback;
    const hBids = h.bids ?? [];
    const hAsks = h.asks ?? [];
    lines.push(`Hyperliquid ${h.coin} L2 fallback (${hBids.length} bid walls / ${hAsks.length} ask walls / ${(h.recentTrades ?? []).length} recent trades):`);
    for (const b of hBids.slice(0, 4)) lines.push(`  HL BID ${fmtUsd(b.usd)} @ ${b.price}`);
    for (const a of hAsks.slice(0, 4)) lines.push(`  HL ASK ${fmtUsd(a.usd)} @ ${a.price}`);
  }
  return { text: lines.join("\n"), data: { sym, stale: Boolean(d.stale), reason: d.reason, bidUsd: bid, askUsd: ask, imbalancePct: fmtP(imb, 1), top: rows.slice(0, 25), perExchange: d.perExchange, hlFallback: d.hlFallback } };
}

async function toolBookWalls(args) {
  const sym = resolveSymArg(args, "BTC");
  const interval = resolveInterval(args?.interval, BOOKWALLS_ITV, M_TO_PAGE, "15m");
  // limit allowlist mirrors the route exactly (bookwalls route clamps 30-300,
  // verified live — the old 10-500 clamp let MCP advertise ranges the route
  // silently coerced: limit=500 ran as 300, limit=10 as 30) — review finding 5
  const limit = Math.min(Math.max(Number(args?.limit ?? 150) || 150, 30), 300);
  // band allowlist mirrors the route exactly ([0.5, 1, 2, 5], else it silently
  // falls back to 5 server-side) — the old 0.5–20 clamp let MCP advertise
  // bands the route never honored (band=10 silently ran as 5).
  const band = [0.5, 1, 2, 5].includes(Number(args?.band)) ? Number(args?.band) : 5;
  const market = args?.market === "spot" ? "spot" : "futures";
  let endQ = "";
  const endN = Number(args?.endTime);
  if (Number.isFinite(endN) && endN > 0) endQ = `&endTime=${Math.floor(endN)}`;
  const d = await api(`/api/cg/bookwalls?sym=${sym}&market=${market}&interval=${interval}&limit=${limit}&band=${band}${endQ}`);
  if (!d.ok) throw new Error(d.error || "book walls failed");
  const m = d.model;
  const pct = (v) => (v == null || !Number.isFinite(v) ? "—" : `${(v * 100).toFixed(1)}%`);
  const walls = (m.walls ?? []).slice(0, 10);
  const events = (m.events ?? []).slice(0, 8);
  const lines = [
    `${sym} order-book depth ±${band}% (${market} aggregate ${d.venues?.join("+") ?? ""}, ${interval} snapshots, ${m.snapshots} frames) — price ${m.last?.price ?? "—"}`,
    `bid depth ${fmtUsd(m.depth?.bidUsd)} · ask depth ${fmtUsd(m.depth?.askUsd)} · imbalance ${pct(m.imbalance)} (5m ago ${pct(m.imbalance5mAgo)}) · min wall ${fmtUsd(d.minWallUsd)}`,
    walls.length ? `live walls (±3% of mid):` : "no confirmed walls within ±3%",
    ...walls.map(
      (w) =>
        `  ${w.side === "b" ? "BID" : "ASK"} ${fmtUsd(w.usd)} @ ${w.price} (${w.distPct > 0 ? "+" : ""}${w.distPct}%, age ${Math.round((w.ageMs ?? 0) / 60000)}min${w.touched ? ", touched" : ""})`
    ),
  ];
  if (events.length) {
    lines.push("recent wall events (newest first):");
    for (const e of events) {
      lines.push(`  ${tsOf(e.t)} ${String(e.type).toUpperCase()} ${e.side === "b" ? "BID" : "ASK"} ${fmtUsd(e.usd)} @ ${e.p}`);
    }
  }
  return {
    text: lines.join("\n"),
    data: { sym, market, interval, band, depth: m.depth, imbalance: m.imbalance, imbalance5mAgo: m.imbalance5mAgo, walls, events: m.events?.slice(0, 30) },
  };
}

async function toolCvdState(args) {
  const sym = resolveSymArg(args, "BTC");
  const mode = ["perp", "spot", "total"].includes(args?.mode) ? args.mode : "total";
  const itv = resolveInterval(args?.itv, CVD_ITV, M_TO_PAGE, "15m");
  // cvd route clamps limit 300-1500 (verified live) — the old 50-1500 clamp
  // advertised a 50-299 range the route silently ran as 300 — review finding 5
  const limit = Math.min(Math.max(Number(args?.limit ?? 600) || 600, 300), 1500);
  const anchor = ["all", "day", "week"].includes(args?.anchor) ? args.anchor : "all";
  const d = await api(`/api/cg/cvd?sym=${sym}&itv=${itv}&limit=${limit}&mode=${mode}&anchor=${anchor}`);
  if (!d.ok) throw new Error(d.error || "cvd failed");
  const lines = [
    `${sym} CVD (${mode}, ${itv}, ${d.n} bars, anchor ${anchor}) — perp ${fmtM(d.kpis.perpCvd)} · spot ${fmtM(d.kpis.spotCvd)} · buy share ${d.kpis.buyRatio}%`,
    `24h perp taker: buy ${fmtM(d.kpis.perpBuy)} vs sell ${fmtM(d.kpis.perpSell)}`,
    d.divergences.length
      ? `divergences:\n${d.divergences.slice(-6).map((x) => `  ${x.type} @ ${new Date(x.ts).toISOString().slice(5, 16).replace("T", " ")} prices ${x.priceA}→${x.priceB}→${x.priceC} ret12 ${x.ret12}% hit:${x.hit}`).join("\n")}`
      : "no active divergences",
  ];
  return { text: lines.join("\n"), data: { kpis: d.kpis, divergences: d.divergences.slice(-10), n: d.n, itv, anchor } };
}

/** Exact JS port of `src/components/cg/charts/coinglass-walls.tsx` `buildWalls()` */
function computeStopmapWalls(model, thr = 0) {
  const yAxis = model?.yAxis;
  const colsArr = model?.columns;
  const candles = model?.candles;
  if (!Array.isArray(yAxis) || yAxis.length < 2 || !Array.isArray(colsArr) || !colsArr.length || !Array.isArray(candles) || !candles.length) {
    return null;
  }
  const rows = yAxis.length;
  const cols = colsArr.length;
  const lo = Number(yAxis[0]);
  const binSz = Math.abs(Number(yAxis[1]) - Number(yAxis[0]));
  if (!Number.isFinite(lo) || !Number.isFinite(binSz) || binSz <= 0) return null;

  const grid = Array.from({ length: cols }, () => new Float64Array(rows));
  let mxGlobal = 0;
  for (let c = 0; c < cols; c++) {
    const col = colsArr[c] ?? [];
    for (let k = 0; k < col.length; k++) {
      const tuple = col[k];
      if (!Array.isArray(tuple) || tuple.length < 3) continue;
      const cIdx = Number(tuple[0]);
      const rIdx = Number(tuple[1]);
      const v = Number(tuple[2]);
      if (cIdx >= 0 && cIdx < cols && rIdx >= 0 && rIdx < rows && Number.isFinite(v) && v > 0) {
        grid[cIdx][rIdx] += v;
        if (grid[cIdx][rIdx] > mxGlobal) mxGlobal = grid[cIdx][rIdx];
      }
    }
  }
  if (mxGlobal <= 0) return { total_walls_found: 0, walls_top24: [], active_short_prices: [], active_long_prices: [], max_short: null, max_long: null };

  const active = Array.from({ length: cols }, () => new Uint8Array(rows));
  const vals = [];
  for (let c = 0; c < cols; c++) {
    for (let r = 0; r < rows; r++) {
      const n = grid[c][r] / mxGlobal;
      if (n > 0) vals.push(n);
    }
  }
  vals.sort((a, b) => a - b);
  const p70 = vals.length ? vals[Math.min(vals.length - 1, Math.floor(vals.length * 0.7))] : 0;
  const cut = Math.max(thr, p70, 0.25);
  for (let c = 0; c < cols; c++) {
    for (let r = 0; r < rows; r++) {
      if (grid[c][r] / mxGlobal >= cut) active[c][r] = 1;
    }
  }

  const lastClose = Number(candles[candles.length - 1]?.close ?? model?.lastPrice ?? 0);
  const closeRow = Number.isFinite(lastClose) && lastClose > 0 ? Math.floor((lastClose - lo) / binSz) : -1;
  const seen = Array.from({ length: cols }, () => new Uint8Array(rows));
  const walls = [];
  for (let r0 = 0; r0 < rows; r0++) {
    for (let c0 = 0; c0 < cols; c0++) {
      if (!active[c0][r0] || seen[c0][r0]) continue;
      const q = [[c0, r0]];
      seen[c0][r0] = 1;
      let rmin = r0, rmax = r0, cmin = c0, cmax = c0;
      for (let i = 0; i < q.length; i++) {
        const [cc, rr] = q[i];
        if (rr < rmin) rmin = rr;
        if (rr > rmax) rmax = rr;
        if (cc < cmin) cmin = cc;
        if (cc > cmax) cmax = cc;
        for (let dc = -1; dc <= 1; dc++) {
          for (let dr = -1; dr <= 1; dr++) {
            const nc = cc + dc, nr = rr + dr;
            if (nc >= 0 && nc < cols && nr >= 0 && nr < rows && active[nc][nr] && !seen[nc][nr]) {
              seen[nc][nr] = 1;
              q.push([nc, nr]);
            }
          }
        }
      }
      let a = -1, empties = 0;
      for (let n = cmin; n <= cmax + 1; n++) {
        let has = false;
        if (n <= cmax) {
          for (let r = rmin; r <= rmax; r++) if (active[n][r]) { has = true; break; }
        }
        if (has) { if (a < 0) a = n; empties = 0; }
        else if (a >= 0) {
          empties++;
          if (empties >= 2 || n > cmax) {
            const cEnd = n - empties;
            if (cEnd - a + 1 >= 2) {
              let mx = 0, rpk = rmin;
              for (let s = a; s <= cEnd; s++) {
                for (let r = rmin; r <= rmax; r++) {
                  if (grid[s][r] > mx) { mx = grid[s][r]; rpk = r; }
                }
              }
              if (mx > 0) {
                walls.push({
                  price: +(lo + (rpk + 0.5) * binSz).toFixed(6),
                  mx_usd: +mx.toFixed(2),
                  score: +(mx * (cEnd - a + 1)).toFixed(2),
                  side: rpk > closeRow ? "short" : "long",
                  active: cEnd >= cols - 1,
                  bars: cEnd - a + 1,
                });
              }
            }
            a = -1; empties = 0;
          }
        }
      }
    }
  }
  walls.sort((a, b) => b.score - a.score);
  const top24 = walls.slice(0, 24);
  const maxShort = top24.filter((w) => w.side === "short").sort((a, b) => b.mx_usd - a.mx_usd)[0] ?? null;
  const maxLong = top24.filter((w) => w.side === "long").sort((a, b) => b.mx_usd - a.mx_usd)[0] ?? null;
  const activeShortPrices = top24.filter((w) => w.active && w.side === "short").map((w) => w.price).sort((a, b) => a - b);
  const activeLongPrices = top24.filter((w) => w.active && w.side === "long").map((w) => w.price).sort((a, b) => b - a);
  return { total_walls_found: walls.length, walls_top24: top24, active_short_prices: activeShortPrices, active_long_prices: activeLongPrices, max_short: maxShort, max_long: maxLong };
}

async function toolStopmap(args) {
  const sym = resolveSymArg(args, "BTC");
  // Multi-horizon sweep mode (`range="all"`): sequentially gather 12h..90d with polite delay
  if (String(args?.range).toLowerCase() === "all") {
    const model = ["1", "2", "3"].includes(String(args?.model)) ? String(args.model) : "1";
    const mode = args?.mode === "pair" ? "pair" : "coin";
    const horizons = {};
    const lines = [`${sym} MULTI-HORIZON LIQUIDATION HEATMAP (Model ${model}, ${mode} mode):`];
    for (const rng of ["12h", "24h", "48h", "3d", "7d", "14d", "30d", "90d"]) {
      const res = await toolStopmap({ sym, model, mode, range: rng }).catch((e) => ({ text: `${rng}: err ${e.message}`, data: null }));
      if (res?.data) {
        horizons[rng] = res.data;
        const w = res.data.walls;
        const ov = res.data.overview;
        lines.push(
          `  [${rng.padEnd(3)}] lg=${ov ? fmtUsd(ov.largestLevel) : "–"} · activeShort=[${(w?.active_short_prices ?? []).slice(0, 4).join(", ")}] · activeLong=[${(w?.active_long_prices ?? []).slice(0, 4).join(", ")}]` +
            (w?.max_short ? ` · maxS=${w.max_short.price}(${fmtUsd(w.max_short.mx_usd)})` : "") +
            (w?.max_long ? ` · maxL=${w.max_long.price}(${fmtUsd(w.max_long.mx_usd)})` : "")
        );
      } else {
        lines.push(`  [${rng.padEnd(3)}] unavailable`);
      }
      await new Promise((r) => setTimeout(r, 300));
    }
    return { text: lines.join("\n"), data: { sym, model, mode, horizons } };
  }

  // Single-window Model-1/2/3 surface
  const model = ["1", "2", "3"].includes(String(args?.model)) ? String(args.model) : null;
  const mode = args?.mode === "pair" ? "pair" : args?.mode === "coin" ? "coin" : null;
  const range = typeof args?.range === "string" && args.range ? args.range : null;
  const wantsNewSurface = Boolean(model || mode || range);
  let endQ = "";
  const endN = Number(args?.endTime);
  if (Number.isFinite(endN) && endN > 0) endQ = `&endTime=${Math.floor(endN)}`;
  const newQ = `&model=${model ?? "1"}&mode=${mode ?? "coin"}&range=${encodeURIComponent(range ?? "24h")}`;

  const d = await api(`/api/cg/stopmap?sym=${sym}${wantsNewSurface ? newQ : endQ}`);
  if (!d.ok) throw new Error(d.error || "stopmap failed");
  if (d.supported === false) {
    return { text: `${sym} liquidation heatmap: ${d.reason ?? "rolling liq-snapshot archive not yet available for this perp"}`, data: d };
  }
  const m = d.model;
  if (!m) throw new Error("stopmap returned no model");

  if (m.keyLevels && m.overview) {
    const ov = m.overview;
    const pct = (x) => (x * 100) / (ov.total || 1);
    const walls = computeStopmapWalls(m);
    const lines = [
      `${sym} liquidation heatmap — Model ${m.model} ${m.mode} mode, ${m.range} window, ${ov.columnMinutes}m columns (Coinglass estimates, source=${d.source ?? "session"})`,
      `price ${ov.lastPrice}${ov.chgPct24h == null ? "" : ` (${ov.chgPct24h >= 0 ? "+" : ""}${ov.chgPct24h}% 24h)`} · open liq levels ${fmtUsd(ov.total)}: longs below ${fmtUsd(ov.longs)} (${Math.round(pct(ov.longs))}%) · shorts above ${fmtUsd(ov.shorts)} (${Math.round(pct(ov.shorts))}%)`,
      `largest single level ${fmtUsd(ov.largestLevel)} · price step ${Number(ov.priceStep).toFixed(2)} · instrument ${d.instrument ?? m.symbol}`,
      ...(walls ? [
        `UI active short walls (right-edge untouched): [${walls.active_short_prices.slice(0, 6).join(", ")}]${walls.max_short ? ` · maxShort ${walls.max_short.price} (${fmtUsd(walls.max_short.mx_usd)})` : ""}`,
        `UI active long walls  (right-edge untouched): [${walls.active_long_prices.slice(0, 6).join(", ")}]${walls.max_long ? ` · maxLong ${walls.max_long.price} (${fmtUsd(walls.max_long.mx_usd)})` : ""}`,
      ] : []),
      `key levels ABOVE (short liq — pullback resistance / squeeze fuel):`,
      ...m.keyLevels.above.map((k) => `  ${fmtUsd(k.usd)} @ ${k.price} (+${(((k.price - ov.lastPrice) / ov.lastPrice) * 100).toFixed(2)}%)`),
      `key levels BELOW (long liq — pullback magnets along the trend):`,
      ...m.keyLevels.below.map((k) => `  ${fmtUsd(k.usd)} @ ${k.price} (${(((k.price - ov.lastPrice) / ov.lastPrice) * 100).toFixed(2)}%)`),
    ];
    return {
      text: lines.join("\n"),
      data: { sym, model: m.model, mode: m.mode, range: m.range, source: d.source, stale: d.stale, lastPrice: ov.lastPrice, overview: ov, keyLevels: m.keyLevels, walls, window: d.window },
    };
  }

  const lines = [
    `${sym} liquidation heatmap (24h window @ 5m bars, ${m.totals?.rows ?? 0} priced rows${endQ ? ", historical" : ""}) — price ${m.lastPrice}`,
    `est. resting liq exposure: ${fmtUsd(m.totals?.below)} below (longs) · ${fmtUsd(m.totals?.above)} above (shorts)`,
    `strongest below (long-liq magnets):`,
    ...(m.clusters?.below ?? []).slice(0, 5).map((c) => `  ${fmtUsd(c.usd)} @ ${c.price} (−${Math.abs(c.distPct)}%, str ${c.strength})`),
    `strongest above (short-liq magnets):`,
    ...(m.clusters?.above ?? []).slice(0, 5).map((c) => `  ${fmtUsd(c.usd)} @ ${c.price} (+${c.distPct}%, str ${c.strength})`),
    `distance bands: ` + (m.bands ?? []).map((b) => `±${b.a}-${b.b}% ↓${fmtUsd(b.below)} ↑${fmtUsd(b.above)}`).join(" · "),
  ];
  return { text: lines.join("\n"), data: { sym, source: d.source, lastPrice: m.lastPrice, totals: m.totals, clusters: m.clusters, bands: m.bands, window: d.window } };
}

function analyzeFpBarMicro(bar) {
  const fp = bar?.fp;
  if (fp && Array.isArray(fp.p) && Array.isArray(fp.b) && Array.isArray(fp.s) && fp.p.length >= 2) {
    const n = fp.p.length;
    let buy = 0, sell = 0, maxTot = -1, pocP = null, diagBuy3 = 0, diagSell3 = 0;
    for (let i = 0; i < n; i++) {
      const bv = Number(fp.b[i]) || 0;
      const sv = Number(fp.s[i]) || 0;
      buy += bv; sell += sv;
      if (bv + sv > maxTot) { maxTot = bv + sv; pocP = +Number(fp.p[i]).toPrecision(6); }
      if (i > 0 && bv > 0 && bv >= 3 * (Number(fp.s[i - 1]) || 0)) diagBuy3++;
      if (i + 1 < n && sv > 0 && sv >= 3 * (Number(fp.b[i + 1]) || 0)) diagSell3++;
    }
    const topB = Number(fp.b[n - 1]) || 0, topS = Number(fp.s[n - 1]) || 0;
    const botB = Number(fp.b[0]) || 0, botS = Number(fp.s[0]) || 0;
    const finHigh = topB === 0 && topS > 0 ? `0x${topS.toFixed(1)}` : null;
    const finLow = botS === 0 && botB > 0 ? `${botB.toFixed(1)}x0` : null;
    const delta = buy - sell;
    return { ts: bar.ts, buyUsd: Math.round(buy), sellUsd: Math.round(sell), deltaUsd: Math.round(delta), deltaPct: +(buy + sell > 0 ? (100 * delta) / (buy + sell) : 0).toFixed(1), poc: pocP, diagBuy3, diagSell3, finHigh, finLow };
  }
  let buy = 0, sell = 0, pocUsd = -1, pocP = null;
  for (const bk of bar?.buckets ?? []) {
    const b = Number(bk[4]) || 0, s = Number(bk[5]) || 0;
    buy += b; sell += s;
    if (b + s > pocUsd) { pocUsd = b + s; pocP = +((Number(bk[0]) + Number(bk[1])) / 2).toPrecision(6); }
  }
  const delta = buy - sell;
  return { ts: bar.ts, buyUsd: Math.round(buy), sellUsd: Math.round(sell), deltaUsd: Math.round(delta), deltaPct: +(buy + sell > 0 ? (100 * delta) / (buy + sell) : 0).toFixed(1), poc: pocP, diagBuy3: null, diagSell3: null, finHigh: null, finLow: null };
}

async function toolFootprint(args) {
  const sym = resolveSymArg(args, "BTC");
  const interval = resolveInterval(args?.interval, M_INTERVALS, PAGE_TO_M, "m15");
  const limit = Math.min(Math.max(Number(args?.limit ?? 96) || 96, 10), 500);
  const lastN = Math.min(Math.max(Number(args?.last ?? 12) || 12, 1), 48);
  const sourcePref = args?.source ?? "auto";
  let d = sourcePref !== "agg" ? await api(`/api/cg/fp?sym=${sym}&itv=${interval}&limit=${limit}`).catch(() => null) : null;
  let routeUsed = "cg/fp";
  if (!d?.ok || !Array.isArray(d.bars) || !d.bars.length) {
    d = await api(`/api/cg/orderflow-agg?sym=${sym}&interval=${interval}&limit=${limit}`).catch(() => null);
    routeUsed = "cg/orderflow-agg";
  }
  if ((!d?.ok || !Array.isArray(d.bars) || !d.bars.length) && args?.symbol) {
    d = await api(`/api/cg/orderflow?symbol=${encodeURIComponent(String(args.symbol))}&interval=${interval}&limit=${limit}`);
    routeUsed = "cg/orderflow";
  }
  if (!d?.ok) throw new Error(d?.error || "footprint failed");
  const rows = (d.bars ?? []).slice(-lastN).map(analyzeFpBarMicro);
  const lines = [
    `${sym} footprint (${routeUsed}, ${interval}, buckets=${d.bucketsAvailable ?? "?"}, source=${d.source ?? "?"}) — last ${rows.length} bars`,
    ...rows.slice(-12).reverse().map((r) => {
      const diag = r.diagBuy3 != null ? ` 3xB:${r.diagBuy3}/S:${r.diagSell3}` : "";
      const fin = [r.finHigh ? `finH:${r.finHigh}` : "", r.finLow ? `finL:${r.finLow}` : ""].filter(Boolean).join(" ");
      return `  ${tsOf(r.ts)} Δ ${r.deltaPct > 0 ? "+" : ""}${r.deltaPct}% POC ${r.poc ?? "—"}${diag}${fin ? " " + fin : ""}`;
    }),
  ];
  return { text: lines.join("\n"), data: { sym, routeUsed, interval, bucketsAvailable: d.bucketsAvailable, bars: rows } };
}

async function toolLargeOrders(args) {
  const sym = resolveSymArg(args, "BTC");
  const hours = Math.min(Math.max(Number(args?.hours ?? 24) || 24, 1), 720);
  const interval = resolveInterval(args?.interval, M_INTERVALS, PAGE_TO_M, "m15");
  let symbol = String(args?.symbol ?? "");
  if (!/^[A-Za-z0-9_-]{3,48}$/.test(symbol) || !symbol.includes("_")) symbol = `Binance_${sym}USDT`;
  const d = await api(`/api/cg/largeorder?symbol=${encodeURIComponent(symbol)}&interval=${interval}&hours=${hours}`);
  if (!d.ok) throw new Error(d.error || "large orders failed");
  if (d.supported === false) {
    return { text: `${symbol} large orders: unavailable (${d.reason ?? "upstream unavailable"})`, data: d };
  }
  let restingBid = 0;
  let restingAsk = 0;
  let tradedBuy = 0;
  let tradedSell = 0;
  const resting = [];
  const trades = [];
  for (const g of d.groups ?? []) {
    const ex = g.instrument?.exName ?? "?";
    for (const o of g.list ?? []) {
      const usd = Number(o.quoteVol ?? 0) || 0;
      if (o.turnoverTime) {
        if (o.side === 2) tradedBuy += usd;
        else tradedSell += usd;
        trades.push({ ex, side: o.side === 2 ? "buy" : "sell", price: o.price, usd, fills: o.turnoverNumber ?? 0, at: o.turnoverTime });
      } else {
        if (o.side === 2) restingBid += usd;
        else restingAsk += usd;
        resting.push({ ex, side: o.side === 2 ? "bid" : "ask", price: o.price, usd, remainingUsd: o.volUsd ?? usd, at: o.createTime });
      }
    }
  }
  resting.sort((a, b) => b.usd - a.usd);
  trades.sort((a, b) => b.usd - a.usd);
  const imb = restingBid + restingAsk > 0 ? ((restingBid - restingAsk) / (restingBid + restingAsk)) * 100 : 0;
  const lines = [
    `${symbol} large orders (last ${hours}h, ${interval}) — ${resting.length} resting · ${trades.length} executed`,
    `resting: bids ${fmtUsd(restingBid)} vs asks ${fmtUsd(restingAsk)} · imbalance ${fmtP(imb, 1)}% ${imb > 0 ? "(bid-heavy)" : "(ask-heavy)"}`,
    `executed notional: buys ${fmtUsd(tradedBuy)} vs sells ${fmtUsd(tradedSell)}`,
    resting.length ? `largest resting:` : "no resting walls",
    ...resting.slice(0, 8).map((r) => `  ${r.side.toUpperCase()} ${fmtUsd(r.usd)} @ ${r.price} (${r.ex}) · remaining ${fmtUsd(r.remainingUsd)}`),
  ];
  if (trades.length) {
    lines.push("largest executed:");
    lines.push(...trades.slice(0, 8).map((t) => `  ${t.side.toUpperCase()} ${fmtUsd(t.usd)} @ ${t.price} (${t.ex}, ${t.fills} fills, ${tsOf(t.at)})`));
  }
  return { text: lines.join("\n"), data: { symbol, hours, restingBid, restingAsk, tradedBuy, tradedSell, resting: resting.slice(0, 25), trades: trades.slice(0, 25) } };
}

async function toolKline(args) {
  const sym = resolveSymArg(args, "BTC");
  const type = args?.type === "oi" ? "oi" : "kline";
  const interval = resolveInterval(args?.interval, M_INTERVALS, PAGE_TO_M, "m15");
  const limit = Math.min(Math.max(Number(args?.limit ?? 300) || 300, 10), 1000);
  let symbol = String(args?.symbol ?? "");
  if (!/^[A-Za-z0-9_-]{3,48}$/.test(symbol) || !symbol.includes("_")) symbol = `Binance_${sym}USDT`;
  const d = await api(`/api/cg/kline?symbol=${encodeURIComponent(symbol)}&type=${type}&interval=${interval}&limit=${limit}`);
  if (!d.ok) throw new Error(d.error || "kline failed");
  const rows = d.rows ?? [];
  const last = rows[rows.length - 1] ?? [];
  const lines = [
    `${symbol} ${type === "oi" ? "open-interest" : "kline"} ${interval} — ${rows.length} rows`,
    `last: ${tsOf(Number(last[0]) || 0)} O ${last[1]} H ${last[2]} L ${last[3]} C ${last[4]}${type === "kline" ? ` V ${last[5]}` : ""}`,
  ];
  return { text: lines.join("\n"), data: { symbol, type, interval, rows: rows.slice(-Math.min(rows.length, 500)) } };
}

async function toolAthenaOracle(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/oracle?sym=${sym}`);
  if (!d?.ok) throw new Error(d?.error || "oracle failed");
  const lines = [
    `${sym} Athena Oracle: score ${d.score}/100 · ${d.verdict} (regime ${d.regime ?? "–"}${d.partial?.length ? `, partial missing: ${d.partial.join(",")}` : ""})`,
    ...(d.factors ?? []).map((f) => `  ${f.id.padEnd(9)} (${Math.round((f.weight ?? 0) * 100)}%) ${String(f.score).padStart(3)}/100 — ${f.detail ?? ""}`),
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolMicrostructure(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/micro?sym=${sym}`);
  if (!d?.ok) throw new Error(d?.error || "microstructure failed");
  const hl = d.hl ?? {};
  const bbo = d.bbo ?? {};
  const lines = [
    `${sym} L2/L3 Microstructure (age ${Math.round((d.ageMs ?? 0) / 1000)}s${d.stale ? " STALE" : ""}):`,
    hl.ok ? `  HL L2: mark ${hl.mark} · spread ${fmtP(hl.spreadPct, 4)}% · imb20 ${fmtP(hl.imbalance20, 2)} · OI $${fmtUsd(hl.oiNotionalUsd)}` : `  HL L2: ${hl.error ?? "n/a"}`,
    bbo.best ? `  Best BBO: ${bbo.best.venue} bid ${bbo.best.bid} / ask ${bbo.best.ask} (spread ${fmtP(bbo.best.spreadBps, 2)} bps)` : `  BBO: unavailable`,
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolLiveOrderbook(args) {
  const sym = resolveSymArg(args, "BTC");
  const band = [0.5, 1, 2, 5].includes(Number(args?.band)) ? Number(args.band) : 5;
  const d = await api(`/api/cg/livebook?sym=${sym}&band=${band}`);
  if (!d?.ok) throw new Error(d?.error || "livebook failed");
  const m = d.model ?? {};
  const lines = [
    `${sym} Binance Live Top-1000 Book (band ±${band}%, covered ${d.covered?.lo ?? "?"}..${d.covered?.hi ?? "?"} [${d.covered?.loPct ?? "?"}%..+${d.covered?.hiPct ?? "?"}%]):`,
    `  imbalance ${fmtP((m.imbalance ?? 0) * 100, 1)}% · 5m ago ${fmtP((m.imbalance5mAgo ?? 0) * 100, 1)}% · walls ${(m.walls ?? []).length} · minWall ${fmtUsd(d.minWallUsd)}`,
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolLiveTick(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/live?sym=${sym}`);
  if (!d?.ok) throw new Error(d?.error || "live tick failed");
  return { text: `${sym} LIVE BBO (${d.source}): last ${d.price} · bid ${d.bid} · ask ${d.ask} · age ${d.ageMs}ms`, data: d };
}

async function toolFundingVenues(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/funding?sym=${sym}`);
  if (!d?.ok) throw new Error(d?.error || "funding failed");
  const agg = d.aggregate ?? {};
  const lines = [
    `${sym} Funding (${d.venues?.length ?? 0} venues, source=${d.source}): 8h=${agg.fundingPct8h ?? "–"}% · APR=${agg.fundingPct8h != null ? fmtP(agg.fundingPct8h * 3 * 365, 2) : "–"}%`,
    ...(d.venues ?? []).slice(0, 8).map((v) => `  ${String(v.exchange ?? v.name ?? "?").padEnd(12)} ${v.rate8h ?? v.fundingRate ?? "–"}%`),
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolOpenInterest(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/oi?sym=${sym}`);
  if (!d?.ok) throw new Error(d?.error || "oi failed");
  const agg = d.aggregate ?? {};
  const lines = [
    `${sym} Open Interest: total ${fmtUsd(agg.totalUsd ?? agg.oiUsd ?? agg.total)} across ${(d.venues ?? []).length} venues`,
    ...(d.venues ?? []).slice(0, 8).map((v) => `  ${String(v.exchange ?? v.name ?? "?").padEnd(12)} ${fmtUsd(v.oiUsd ?? v.openInterestUsd ?? v.usd)}`),
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolPerpDepth(args) {
  const sym = resolveSymArg(args, "SP500");
  const d = await api(`/api/cg/perpdepth?sym=${sym}`);
  if (!d?.ok) throw new Error(d?.error || "perpdepth failed");
  return { text: `${sym} Tier-3 Perp Depth: OI=${d.oi?.last ?? "–"} (1h ${d.oi?.d1h ?? "–"}% / 4h ${d.oi?.d4h ?? "–"}% / 24h ${d.oi?.d24h ?? "–"}%) · flow15m=${fmtUsd(d.netFlow15mUsd)}`, data: d };
}

async function toolCotReport(args) {
  const sym = resolveSymArg(args, "XAU");
  const d = await api(`/api/cg/cot?sym=${sym}&quiet=1`);
  if (!d?.ok) throw new Error(d?.error || "cot failed");
  const lines = [
    `${sym} CFTC COT (${d.contract ?? "?"} · code ${d.code ?? "?"} · asOf ${d.asOf ?? "?"}): OI ${d.openInterest ?? "–"}`,
    d.noncomm ? `  Non-Commercial (Leveraged/Specs): L ${d.noncomm.long} vs S ${d.noncomm.short} (net ${d.noncomm.net})` : "",
  ].filter(Boolean);
  return { text: lines.join("\n"), data: d };
}

async function toolClassicMacroQuotes(args) {
  const sym = resolveSymArg(args, "SPX500");
  const syms = args?.syms ?? "SP500,NDX100,EURUSD";
  const [c, m] = await Promise.all([
    api(`/api/cg/classic?sym=${encodeURIComponent(sym)}`).catch(() => null),
    api(`/api/cg/cnbc?syms=${encodeURIComponent(syms)}`).catch(() => null),
  ]);
  const lines = [];
  if (c?.ok) lines.push(`${sym} Classic (${c.exchange ?? "?"}): ${c.price} (${fmtP(c.chgPct, 2)}% vs ${c.prevClose})`);
  if (m?.ok && m.quotes) {
    lines.push("CNBC Macro Quotes:");
    for (const [k, q] of Object.entries(m.quotes)) lines.push(`  ${k}: ${q?.price ?? q?.last ?? "?"} (${q?.chgPct ?? "?"}%)`);
  }
  return { text: lines.join("\n") || "classic/macro unavailable", data: { classic: c, cnbc: m } };
}

async function toolPlaybookCards(args) {
  const sym = args?.sym ? String(args.sym).toUpperCase() : "BTC,ETH,SOL";
  const d = await api(`/api/strategy/cards?sym=${encodeURIComponent(sym)}`);
  if (!d?.ok) throw new Error(d?.error || "playbook cards failed");
  const cards = [];
  for (const row of d.assets ?? []) {
    for (const card of row.cards ?? []) cards.push(`${row.sym} ${card.engine} ${card.direction} entry=${card.entry} SL=${card.stop} TP=${card.tp} R=${card.rMultiple}`);
  }
  const lines = [
    `Playbook Cards (${sym}): ${cards.length} active setups` + (d.suspended ? ` [SUSPENDED: ${d.suspended}]` : ""),
    ...cards,
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolAlertRules() {
  const d = await api(`/api/cg/alert-rules`);
  if (!d?.ok) throw new Error(d?.error || "alert-rules failed");
  const rules = d.rules ?? [];
  return {
    text: `Alert Rules (${rules.length}, source=${d.source}):\n` + rules.slice(0, 15).map((r) => `  [${r.enabled ? "ON" : "off"}] ${r.name ?? r.id}: ${r.lastTriggered ?? "never"}`).join("\n"),
    data: d,
  };
}

async function toolTerminalHealth() {
  const [h, eng, mem] = await Promise.all([
    api(`/api/health`).catch(() => null),
    api(`/api/cg/engine/status`).catch(() => null),
    api(`/api/cg/debug/mem`).catch(() => null),
  ]);
  const lines = [
    `Terminal Health: uptime=${h?.uptimeSec ?? "?"}s · RSS=${h?.rssMb ?? "?"}MB · Heap=${h?.heapUsedMb ?? "?"}/${h?.heapTotalMb ?? "?"}MB`,
    eng?.ok ? `Engine: started=${eng.started} · backfill=${eng.backfillDays}d · instrumentKeys=${eng.instrumentKeys} · series=${eng.series} · assetSeries=${eng.assetSeries}` : "Engine: unavailable",
  ];
  return { text: lines.join("\n"), data: { health: h, engine: eng, mem } };
}

async function toolBinanceMarketData(args) {
  const action = ["quotes", "market", "klines", "universe", "account"].includes(args?.action) ? args.action : "market";
  const sym = resolveSymArg(args, "BTC");
  const interval = args?.interval ?? "15m";
  const limit = Math.min(Math.max(Number(args?.limit ?? 100) || 100, 5), 1000);
  const pathMap = {
    quotes: `/api/binance/quotes${args?.syms ? `?syms=${encodeURIComponent(args.syms)}` : ""}`,
    market: `/api/binance/market?sym=${encodeURIComponent(sym)}`,
    klines: `/api/binance/klines?sym=${encodeURIComponent(sym)}&interval=${encodeURIComponent(interval)}&limit=${limit}`,
    universe: `/api/binance/universe`,
    account: `/api/binance/account`,
  };
  const d = await api(pathMap[action]);
  if (!d) throw new Error("binance market data failed");
  return { text: `Binance ${action} (${sym}): ok=${d.ok}\n${JSON.stringify(d).slice(0, 1800)}`, data: d };
}

async function toolTelemetrySnapshot(args) {
  const sym = resolveSymArg(args, "BTC");
  const assetPath = fileURLToPath(new URL(`../data/telemetry/assets/${sym}.json`, import.meta.url));
  const fpPath = fileURLToPath(new URL(`../data/telemetry/footprint/${sym}/summary_7d.json`, import.meta.url));
  let asset = null, fp = null;
  try { asset = JSON.parse(fsRead(assetPath, "utf-8")); } catch {}
  try { fp = JSON.parse(fsRead(fpPath, "utf-8")); } catch {}
  if (!asset) throw new Error(`No telemetry JSON found at data/telemetry/assets/${sym}.json`);
  const ageSec = asset.updated_epoch ? Math.round(Date.now() / 1000 - asset.updated_epoch) : null;
  const smap = asset.liquidations?.stopmap ?? {};
  const lines = [
    `${sym} TELEMETRY SNAPSHOT (disk age=${ageSec ?? "?"}s, feed=${asset.feed_type}, MT5=${asset.mt5?.symbol ?? "none"} spread=${asset.mt5?.spread_bps ?? "–"}bps):`,
    `Price: ${asset.ticker?.price ?? asset.mt5?.mid ?? "–"} · Oracle: ${asset.athena_oracle?.score ?? "–"}/100 · Narrative: ${asset.narrative_engine?.regime ?? "–"} ${asset.narrative_engine?.direction ?? ""} (${asset.narrative_engine?.grade ?? "–"})`,
    `Stopmap Horizons (${Object.keys(smap).filter((k) => k !== "rolling_archive_shared_across").length}):`,
    ...["m1_coin_12h", "m1_coin_24h", "m1_coin_48h", "m1_coin_3d", "m1_coin_7d", "m1_coin_14d", "m1_coin_30d", "m1_coin_90d"]
      .filter((k) => smap[k])
      .map((k) => {
        const w = smap[k].walls ?? {};
        return `  ${k.padEnd(11)} kind=${smap[k].kind ?? "?"} maxS=${w.max_short?.price ?? "–"}(${fmtUsd(w.max_short?.mx_usd)}) maxL=${w.max_long?.price ?? "–"}(${fmtUsd(w.max_long?.mx_usd)})`;
      }),
    fp ? `Footprint 7d: ${fp.rows?.length ?? 0} bars · cumDelta=${fmtUsd(fp.cum_delta_7d ?? fp.cum_delta)}` : "Footprint 7d: n/a",
  ];
  return { text: lines.join("\n"), data: { sym, ageSec, asset, fpSummaryBars: fp?.rows?.slice(-8) ?? [] } };
}

async function toolOpenLiveVisual(args) {
  const sym = resolveSymArg(args, "BTC");
  const [snap, setup, oracle, sm3, sm30, desk] = await Promise.all([
    api(`/api/cg/market?sym=${sym}`).catch(() => null),
    api(`/api/cg/narrative?sym=${sym}`).catch(() => null),
    api(`/api/cg/oracle?sym=${sym}`).catch(() => null),
    toolStopmap({ sym, model: "1", mode: "coin", range: "3d" }).catch(() => null),
    toolStopmap({ sym, model: "1", mode: "coin", range: "30d" }).catch(() => null),
    api(`/api/desk/live`).catch(() => null),
  ]);
  const structuredContent = {
    sym,
    fetchedAt: new Date().toISOString(),
    market: snap,
    narrative: setup,
    oracle,
    stopmap3d: sm3?.data ?? null,
    stopmap30d: sm30?.data ?? null,
    desk,
  };
  return {
    text: `Live CG Order-Flow Visual updated for ${sym} (Price ${snap?.price?.last ?? "?"}, Oracle ${oracle?.score ?? "?"}/100, 3d+30d Stopmap & Desk live).`,
    structuredContent,
    data: structuredContent,
  };
}

async function toolAnalyzeSetup(args) {
  const sym = resolveSymArg(args, "BTC");
  const [market, signals, liq, whale, book, cvd, notif, narrative, quant, options, oracle, micro, livebook, sm3] = await Promise.all([
    toolMarketSnapshot({ sym }).catch(() => null),
    toolSignals({ sym }).catch(() => null),
    toolLiquidationLevels({ sym, range: "14d" }).catch(() => toolLiquidationLevels({ sym, range: "3d" }).catch(() => null)),
    toolWhaleOrders({ sym }).catch(() => null),
    toolBookWalls({ sym }).catch(() => null),
    toolCvdState({ sym }).catch(() => null),
    api(`/api/cg/notifications?asset=${sym}&limit=15`).catch(() => null),
    api(`/api/cg/narrative?sym=${sym}`).catch(() => null),
    api(`/api/cg/quant?sym=${sym}`).catch(() => null),
    api(`/api/cg/options?sym=${sym}`).catch(() => null),
    toolAthenaOracle({ sym }).catch(() => null),
    toolMicrostructure({ sym }).catch(() => null),
    toolLiveOrderbook({ sym }).catch(() => null),
    toolStopmap({ sym, model: "1", mode: "coin", range: "3d" }).catch(() => null),
  ]);
  const parts = [];
  parts.push(`=== ${sym} TRADE CONTEXT (all values live) ===`);
  if (narrative?.ok && narrative.supported !== false) {
    parts.push(
      `\n--- NARRATIVE VERDICT (M1/M2 engine) ---\n` +
        `${narrative.regime} · ${narrative.activeModel} · ${narrative.direction} · grade ${narrative.grade} (conf ${narrative.confidence}/100)\n` +
        `${narrative.thesis}\n` +
        (narrative.plan?.entryZone
          ? `Plan: entry ${narrative.plan.entryZone[0]?.toFixed(2)}-${narrative.plan.entryZone[1]?.toFixed(2)} · stop ${narrative.plan.stop?.toFixed(2)} · bestR ${narrative.plan.bestR?.toFixed(2)}\n` +
            `Targets: ${(narrative.plan.targets || []).filter((t) => t.level != null).map((t) => `${t.label} @ ${t.level.toFixed(2)} (${t.rMultiple?.toFixed(1) ?? "?"}R)`).join(" | ")}\n`
          : "") +
        (narrative.plan?.vetoes?.length ? `Vetoes: ${narrative.plan.vetoes.join("; ")}\n` : "") +
        `Freshness: worstAgeMs ${narrative.freshness?.worstAgeMs ?? "?"}${narrative.freshness?.staleFields?.length ? ` · stale: ${narrative.freshness.staleFields.join(",")}` : ""}`
    );
  }
  if (market) parts.push(`\n--- MARKET ---\n${market.text}`);
  if (signals) parts.push(`\n--- FOOTPRINT SIGNALS ---\n${signals.text}`);
  if (liq) parts.push(`\n--- LIQUIDATION CLUSTERS (sweep magnets) ---\n${liq.text}`);
  if (whale) parts.push(`\n--- WHALE OPEN ORDERS ---\n${whale.text}`);
  if (book) parts.push(`\n--- ORDER-BOOK WALLS ---\n${book.text}`);
  if (livebook) parts.push(`\n--- BINANCE LIVE TOP-1000 BOOK ---\n${livebook.text}`);
  if (sm3) parts.push(`\n--- STOPMAP (3D WALLS) ---\n${sm3.text}`);
  if (oracle) parts.push(`\n--- ATHENA ORACLE ---\n${oracle.text}`);
  if (micro) parts.push(`\n--- L2/L3 MICROSTRUCTURE ---\n${micro.text}`);
  if (cvd) parts.push(`\n--- CVD STATE ---\n${cvd.text}`);
  if (notif?.items?.length) {
    parts.push(
      `\n--- RECENT EVENTS ---\n` +
        notif.items.map((n) => `${new Date(n.ts).toISOString().slice(5, 16).replace("T", " ")} [${n.severity}] ${n.dir.toUpperCase()} ${n.source}: ${n.title}`).join("\n")
    );
  }
  if (quant?.ok && quant.supported !== false) {
    const ls1h = quant.lsRatios?.["1h"] ?? {};
    parts.push(
      `\n--- QUANT POSITIONING ---\n` +
        `OI-weighted funding APR ${quant.funding?.annualizedAprPct?.toFixed(2) ?? "–"}% · spread ${quant.funding?.fundingSpreadBps?.toFixed(2) ?? "–"}bps\n` +
        `L/S 1h: global ${ls1h.global?.ratio?.toFixed(2) ?? "–"} · top-trader positions ${ls1h.topPositions?.ratio?.toFixed(2) ?? "–"} · taker ${ls1h.taker?.buySellRatio?.toFixed(2) ?? "–"}\n` +
        `OI quadrants: 1h ${quant.oiQuadrant?.["1h"]?.quadrant ?? "–"} · 4h ${quant.oiQuadrant?.["4h"]?.quadrant ?? "–"} · 24h ${quant.oiQuadrant?.["24h"]?.quadrant ?? "–"}\n` +
        `Basis ${quant.basis?.basisPct?.toFixed(3) ?? "–"}% · Coinbase premium ${quant.basis?.coinbasePremiumBps?.toFixed(1) ?? "–"}bps`
    );
  }
  if (options?.ok && options.supported !== false) {
    parts.push(
      `\n--- OPTIONS SURFACE (Deribit) ---\n` +
        `DVOL ${options.dvol?.value?.toFixed(1) ?? "–"} (${options.dvol?.changePct24h?.toFixed(1) ?? "–"}% 24h) · ATM IV 7/30/90d ` +
        `${options.termStructure?.["7d"]?.toFixed(1) ?? "–"}/${options.termStructure?.["30d"]?.toFixed(1) ?? "–"}/${options.termStructure?.["90d"]?.toFixed(1) ?? "–"}\n` +
        `RR25d ${options.riskReversal25d?.value?.toFixed(1) ?? "–"} · GEX ${options.gex?.regime ?? "–"} · P/C OI ${options.putCallRatio?.oi?.toFixed(2) ?? "–"}`
    );
  }
  parts.push(
    `\n--- FORMULATION GUIDE ---\n` +
      `Bias: align footprint signal direction (absorption/trap = strongest) with the 4H EMA13×EMA89 trend and position vs day VWAP.\n` +
      `Entry: absorption zone or retest of day/month POC, or the level a fresh HIGH signal attached to.\n` +
      `Stop: beyond the invalidation — other side of the absorption zone / beyond the ±3σ VWAP band / beyond the sweep wick that just took the cluster.\n` +
      `Targets: nearest same-direction liquidation cluster (sweep magnet), day VAH/VAL, whale wall prices. Take profit into clusters, never against them.\n` +
      `Caution: notifications during low-liquidity hours (weekend/Asia) produce weaker sweeps; wait for the 15m close confirmation.`
  );
  return {
    text: parts.join("\n"),
    data: { sym, narrative, market: market?.data, signals: signals?.data, liq: liq?.data, whale: whale?.data, quant, options, notifications: notif?.items ?? [] },
  };
}

async function toolAlphaScreener(args) {
  const kindFilter = args?.kind ? String(args.kind).toLowerCase() : null; // crypto-perp | tradfi-perp | classic
  const gradeMin = args?.gradeMin ? String(args.gradeMin).toUpperCase() : null; // A | A+
  const d = await api(`/api/cg/screener`);
  if (!d?.ok) throw new Error(d?.error || "screener failed");
  let rows = Array.isArray(d.rows) ? d.rows : [];
  if (kindFilter) rows = rows.filter((r) => r.kind === kindFilter);
  if (gradeMin) {
    const rank = { "A+": 4, A: 3, B: 2, SKIP: 1 };
    const min = rank[gradeMin] ?? 3;
    rows = rows.filter((r) => (rank[r.m1m2?.grade] ?? 0) >= min);
  }
  rows = [...rows].sort((a, b) => (b.m1m2?.evR ?? -Infinity) - (a.m1m2?.evR ?? -Infinity));
  const radar = d.radar ?? {};
  const lines = [
    `Alpha Screener — ${rows.length}/${d.totalCount} Curated-61 rows (${d.warmCount} warm) · radar ${radar.statesCached ?? 0}/${radar.universeSize ?? 61} swept (${radar.fresh5m ?? 0} fresh <5m, ${radar.errors ?? 0} err) · source: ${d.source}`,
    ...rows.slice(0, 20).map((r) => {
      const m = r.m1m2;
      const model = m ? `${m.activeModel} ${m.grade} ${m.direction}${m.evR != null ? ` EV${m.evR >= 0 ? "+" : ""}${m.evR}R` : ""}` : "warming";
      const chg = r.chg24hPct != null ? `${r.chg24hPct >= 0 ? "+" : ""}${r.chg24hPct}%` : "–";
      const vw = r.vwapZ != null ? `z=${r.vwapZ}` : "";
      const mag = r.liqMagnet ? `mag:${r.liqMagnet.side}@${r.liqMagnet.distPct}%` : "";
      const liq5 = r.liq5m ? `liq5m=$${Math.round((r.liq5m.usd ?? 0) / 1000)}k(${r.liq5m.side[0]})` : "";
      return `${r.sym.padEnd(9)} px=${r.price ?? "–"} (${chg}) · ${model} · ${vw} ${mag} ${liq5}`.trim();
    }),
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolTradeNarrative(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/narrative?sym=${sym}`);
  if (d?.ok === false) throw new Error(d?.error || "narrative failed");
  if (d?.supported === false) {
    return { text: `${sym}: ${d.reason ?? "no narrative available for this instrument"}`, data: d };
  }
  const plan = d.plan ?? {};
  const lines = [
    `${sym} — ${d.regime} · ${d.activeModel} · ${d.direction} · grade ${d.grade} (confidence ${d.confidence}/100)`,
    d.thesis ?? "",
    "",
    `Entry zone: ${plan.entryZone ? `${plan.entryZone[0]} – ${plan.entryZone[1]}` : "n/a"}`,
    `Trigger: ${plan.triggerRule ?? "n/a"}`,
    `Stop: ${plan.stop ?? "n/a"} (${plan.invalidation ?? ""})`,
    ...(plan.targets ?? []).map((t) => `${t.label}: ${t.level ?? "n/a"}${t.rMultiple != null ? ` (${t.rMultiple.toFixed(2)}R)` : ""}`),
    `bestR: ${plan.bestR != null ? plan.bestR.toFixed(2) : "n/a"} (min 1.5)`,
    ...(plan.vetoes?.length ? ["", `Vetoes:`, ...plan.vetoes.map((v) => `- ${v}`)] : []),
    "",
    "Checklist (exact measured values):",
    ...(d.checklist ?? []).map((c) => `- [${c.pass === true ? "PASS" : c.pass === false ? "FAIL" : "N/A"}] ${c.label}: ${c.value}`),
    "",
    `Freshness: worstAgeMs ${d.freshness?.worstAgeMs ?? "?"}${d.freshness?.staleFields?.length ? ` · stale fields: ${d.freshness.staleFields.join(", ")}` : " (all fresh)"}`,
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolQuantPositioning(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/quant?sym=${sym}`);
  if (d?.ok === false) throw new Error(d?.error || "quant positioning failed");
  if (d?.supported === false) return { text: `${sym}: ${d.reason ?? "no positioning data"}`, data: d };
  const per = (p) => {
    const x = d.lsRatios?.[p] ?? {};
    return `${p}: global ${x.global?.ratio?.toFixed(2) ?? "–"} (${x.global?.longPct?.toFixed(1) ?? "–"}% long) · top accounts ${x.topAccounts?.ratio?.toFixed(2) ?? "–"} · top positions ${x.topPositions?.ratio?.toFixed(2) ?? "–"} · taker ${x.taker?.buySellRatio?.toFixed(3) ?? "–"}`;
  };
  const lines = [
    `${sym} quant positioning (${d.pair}):`,
    per("15m"),
    per("1h"),
    per("4h"),
    "",
    `OI-weighted funding: ${d.funding?.oiWeightedRate8h ?? "–"}%/8h → ${d.funding?.annualizedAprPct?.toFixed(2) ?? "–"}% APR · cross-venue spread ${d.funding?.fundingSpreadBps?.toFixed(2) ?? "–"}bps (${(d.funding?.spreadVenues ?? []).join(" vs ")})`,
    `Venues: ${(d.funding?.venues ?? []).map((v) => `${v.name} ${v.rate8h}%/${v.fundingIntervalHours}h`).join(" · ")}`,
    "",
    `OI×price quadrants: 1h ${d.oiQuadrant?.["1h"]?.quadrant ?? "–"} (Δoi ${d.oiQuadrant?.["1h"]?.oiDeltaPct?.toFixed(2) ?? "–"}%, Δpx ${d.oiQuadrant?.["1h"]?.priceDeltaPct?.toFixed(2) ?? "–"}%) · 4h ${d.oiQuadrant?.["4h"]?.quadrant ?? "–"} · 24h ${d.oiQuadrant?.["24h"]?.quadrant ?? "–"}`,
    `OI/24h-vol ratio: ${d.oiQuadrant?.oiToVolRatio?.toFixed(2) ?? "–"}`,
    `Spot-perp basis: ${d.basis?.basisPct?.toFixed(4) ?? "–"}% (spot ${d.basis?.spotPair ?? "n/a"} @ ${d.basis?.spotPrice ?? "–"}) · Coinbase premium ${d.basis?.coinbasePremiumBps?.toFixed(1) ?? "–"}bps`,
    "",
    `Freshness: dataAt ${d.dataAt} · ageMs ${d.ageMs} · stale ${d.stale} · source ${d.source}`,
    ...(d.notes?.length ? ["", "Notes:", ...d.notes.map((n) => `- ${n}`)] : []),
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolOptionsSurface(args) {
  const sym = resolveSymArg(args, "BTC");
  const d = await api(`/api/cg/options?sym=${sym}`);
  if (d?.ok === false) throw new Error(d?.error || "options surface failed");
  if (d?.supported === false) return { text: `${sym}: ${d.reason ?? "no options data"}`, data: d };
  const lines = [
    `${sym} options surface (Deribit ${d.currency}):`,
    `DVOL ${d.dvol?.value?.toFixed(1) ?? "–"} (${d.dvol?.changePct24h?.toFixed(1) ?? "–"}% 24h)`,
    `ATM IV term structure: 7d ${d.termStructure?.["7d"]?.toFixed(1) ?? "–"}% · 30d ${d.termStructure?.["30d"]?.toFixed(1) ?? "–"}% · 90d ${d.termStructure?.["90d"]?.toFixed(1) ?? "–"}%`,
    `25Δ risk reversal: ${d.riskReversal25d?.value?.toFixed(1) ?? "–"} vol pts (${d.riskReversal25d?.method ?? "–"}, ${d.riskReversal25d?.expiry ?? "–"})`,
    `Max pain: ${(d.maxPain ?? []).map((m) => `${m.expiry} @ ${m.strike}`).join(" · ")}`,
    d.maxPainStrike != null ? `Max pain strike (nearest monthly, the pin magnet): ${d.maxPainStrike}` : "Max pain strike: n/a",
    d.gammaFlipPrice != null
      ? `GEX flip point (zero-gamma): ${d.gammaFlipPrice} — above it dealer hedging stabilizes price, below it accelerates the move`
      : "GEX flip point: no zero-crossing in the 0.70x-1.30x band",
    `Top call OI walls: ${(d.oiWalls?.calls ?? []).slice(0, 3).map((w) => `${w.strike} ($${(w.oiUsd / 1e6).toFixed(1)}M)`).join(" · ")}`,
    `Top put OI walls: ${(d.oiWalls?.puts ?? []).slice(0, 3).map((w) => `${w.strike} ($${(w.oiUsd / 1e6).toFixed(1)}M)`).join(" · ")}`,
    `GEX: ${d.gex?.netGexUsd != null ? `$${(d.gex.netGexUsd / 1e6).toFixed(1)}M` : "–"} → ${d.gex?.regime ?? "–"} (spot ${d.gex?.spot ?? "–"})`,
    `Put/Call: OI ${d.putCallRatio?.oi?.toFixed(2) ?? "–"} · vol24h ${d.putCallRatio?.vol24h?.toFixed(2) ?? "–"}`,
    `Freshness: dataAt ${d.dataAt} · ageMs ${d.ageMs} · stale ${d.stale}`,
    ...(d.notes?.length ? ["", "Notes:", ...d.notes.map((n) => `- ${n}`)] : []),
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolDeskStatus() {
  const d = await api(`/api/desk/live`);
  if (!d?.ok) throw new Error(d?.error || "desk status failed");
  const acc = d.mt5?.account;
  const lines = [
    `Desk ${d.analysis?.lastRound ?? "–"} · MT5 slots ${d.mt5?.slotsUsed ?? 0}/${d.mt5?.maxSlots ?? 4}` +
      (acc ? ` · eq=$${acc.equity} bal=$${acc.balance} pnl=$${acc.profit}` : ""),
    `MT5 positions: ${(d.mt5?.positions || []).length} · pending orders: ${(d.mt5?.orders || []).length}`,
    ...(d.mt5?.orders || []).map((o) => `  #${o.ticket} ${o.sym} ${o.side.toUpperCase()} @ ${o.entry} (live ${o.livePrice ?? "–"}, dist ${o.distToEntryPct ?? "–"}%) SL ${o.sl} TP ${o.tp} R=${o.riskR ?? "–"}`),
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolPaperBook(args) {
  const sym = resolveSymArg(args, "");
  const status = args?.status;
  const limit = args?.limit ?? 30;
  const params = new URLSearchParams();
  if (sym) params.set("sym", sym);
  if (status) params.set("status", String(status).toUpperCase());
  params.set("limit", String(limit));
  const d = await api(`/api/cg/paperbook?${params.toString()}`);
  if (d?.ok === false) throw new Error(d?.error || "paper book failed");
  const statsAll = (d.stats ?? []).find((s) => s.bucket === "ALL");
  const tel = d.telemetry ?? {};
  const lines = [
    `Virtual Book — ${tel.armed ?? 0} armed · ${tel.open ?? 0} open · ${tel.closed ?? 0} closed · sweep ${tel.sweepPos ?? 0}/${tel.universe ?? "?"} of the 61-asset universe`,
    `Scoreboard (ALL): ${statsAll?.filled ?? 0} filled · ${statsAll?.wins ?? 0}W/${statsAll?.losses ?? 0}L` +
      ` · win ${statsAll?.winRatePct != null ? `${statsAll.winRatePct}%` : "–"}` +
      ` · expectancy ${statsAll?.expectancyR != null ? `${statsAll.expectancyR}R` : "–"}` +
      ` · total ${statsAll?.totalR != null ? `${statsAll.totalR}R` : "–"}` +
      ` · avg hold ${statsAll?.avgHoldMin != null ? `${statsAll.avgHoldMin}m` : "–"}`,
    `Freshness: dataAt ${d.freshness?.dataAt} · ageMs ${d.freshness?.ageMs} · stale ${d.freshness?.stale}`,
  ];
  const tickets = d.tickets ?? [];
  if (sym && d.ticket) lines.push(``, `Most relevant ticket for ${sym}: ${d.ticket.status}${d.ticket.outcome ? ` · ${d.ticket.outcome}` : ""} · ${d.ticket.totalR != null ? `${d.ticket.totalR.toFixed(2)}R` : "no R yet"}`);
  if (!tickets.length) {
    lines.push("", "No tickets match the filter — the sweep arms setups as narratives qualify (M1 pullback · M1 wait-zone · M2 stretch).");
  } else {
    lines.push("", "Tickets:");
    for (const t of tickets) {
      const rungStr = (t.targets ?? []).filter((x) => x.level != null).map((x, i) => `${x.hitAt ? "✓" : ""}T${i + 1} ${x.level}`).join(" / ");
      lines.push(
        `  ${t.sym} ${t.model} ${t.direction} ${t.status}${t.outcome ? `·${t.outcome}` : ""}` +
          ` · entry ${t.filledPrice ?? `zone ${t.entryZone?.[0]}–${t.entryZone?.[1]}`}` +
          ` · mark ${t.lastMark ?? "–"} · stop ${t.stop}${t.stopAtBe ? " (BE)" : ""}` +
          ` · R ${t.totalR != null ? t.totalR.toFixed(2) : "–"}` +
          ` · MFE ${t.mfeR.toFixed(2)}/MAE ${t.maeR.toFixed(2)}` +
          (rungStr ? ` · ${rungStr}` : "")
      );
    }
  }
  return { text: lines.join("\n"), data: d };
}

// ---------------------------------------------------------------------------
// MCP protocol plumbing (JSON-RPC 2.0 over stdio, line-delimited)
// ---------------------------------------------------------------------------

async function toolBacktestSummary(args) {
  const sym = args?.sym ? String(args.sym).toUpperCase() : null;
  const d = await api(`/api/cg/backtest${sym ? `?trades=1` : `?trades=0`}`);
  if (!d?.ok) throw new Error(d?.error || "backtest feed failed");
  if (!d.metricsAll) {
    return {
      text: `Backtest: no completed run on disk yet${d.status?.running ? ` — a run is in progress (${d.status.progress.done}/${d.status.progress.total} assets)` : " — trigger one from the Backtest tab or /api/cg/backtest?run=1"}. ${d.status?.lastError ? `last error: ${d.status.lastError}` : ""}`.trim(),
      data: d,
    };
  }
  const fmtM = (m) =>
    m
      ? `trades ${m.trades} · fill ${m.fillRatePct ?? "–"}% · win ${m.winRatePct ?? "–"}% · PF ${m.profitFactor ?? "–"} · expNet ${m.expectancyNetR ?? "–"}R · DD ${m.maxDrawdownR}R · sharpe ${m.sharpePerTrade ?? "–"} · sortino ${m.sortinoPerTrade ?? "–"}${m.maxConsecutiveLosses != null ? ` · max loss streak ${m.maxConsecutiveLosses}` : ""}`
      : "n/a";
  let assetLine = "";
  if (sym) {
    const trades = (d.trades ?? []).filter((t) => t.sym === sym);
    const wins = trades.filter((t) => (t.netR ?? 0) > 0).length;
    const totalR = trades.reduce((s, t) => s + (t.netR ?? 0), 0);
    assetLine = `\n${sym} historical trades: ${trades.length} · wins ${trades.length ? Math.round((wins / trades.length) * 100) : "–"}% · total ${totalR.toFixed(2)}R`;
  }
  const lines = [
    `Walk-Forward Backtest — ${d.params.days}d · ${d.universe.length} Curated-58 perps · generated ${new Date(d.generatedAt).toISOString().slice(0, 16)}Z · ${d.metricsAll?.trades ?? 0} closed trades`,
    `ALL: ${fmtM(d.metricsAll)}`,
    `M1 TREND_PULLBACK: ${fmtM(d.byModel?.M1_TREND_PULLBACK)}`,
    `M2 MEAN_REVERSION: ${fmtM(d.byModel?.M2_MEAN_REVERSION)}`,
    d.monteCarlo
      ? `Monte Carlo ruin (${d.monteCarlo.paths}-path bootstrap): P(≥5R DD) ${(d.monteCarlo.pDd5R * 100).toFixed(1)}% · P(≥10R DD) ${(d.monteCarlo.pDd10R * 100).toFixed(1)}% · median maxDD ${d.monteCarlo.medianMaxDdR}R · p95 ${d.monteCarlo.p95MaxDdR}R`
      : "Monte Carlo ruin: not enough closed trades",
    `Tiers — majors: ${fmtM(d.byTier?.["crypto-major"])}`,
    `        alts: ${fmtM(d.byTier?.["crypto-alt"])}`,
    `        tradfi: ${fmtM(d.byTier?.tradfi)}`,
    "Sensitivity:",
    ...(d.sensitivity ?? []).map(
      (c) => `  ${c.axis}=${c.value}: trades ${c.metrics.trades} · win ${c.metrics.winRatePct ?? "–"}% · expNet ${c.metrics.expectancyNetR ?? "–"}R · PF ${c.metrics.profitFactor ?? "–"}`
    ),
    "Calibration curves (probability.ts blends with w_emp = n/(n+30)):",
    ...(d.calibration ?? []).slice(0, 6).map(
      (c) => `  ${c.model}|${c.regime}|${c.tier}: n=${c.n} · win ${c.winRate} · avgNetR ${c.avgNetR}${c.logitFit ? ` · fit σ(${c.logitFit.intercept} + ${c.logitFit.slope}·logit(prior))` : ""}`
    ),
    `Accounting: ${d.source}`,
    `Freshness: dataAt ${d.dataAt} · age ${Math.round((d.ageMs ?? 0) / 1000)}s · stale ${d.stale}`,
    assetLine,
  ];
  return { text: lines.filter(Boolean).join("\n"), data: d };
}

async function toolPortfolioRisk() {
  const d = await api(`/api/cg/risk`);
  if (!d?.ok) throw new Error(d?.error || "risk engine failed");
  const beta = (d.beta ?? []).map((b) => `${b.vs} β7d ${b.beta7d ?? "–"} (24h ${b.beta24h ?? "–"})`).join(" · ");
  const clusters = (d.clusters ?? [])
    .slice()
    .sort((a, b) => b.pctOfGross - a.pctOfGross)
    .map((c) => `${c.cluster}: ${c.pctOfGross}% gross${c.heatPct != null ? ` · heat ${c.heatPct}%` : ""} (${c.positions.slice(0, 6).join(",")}${c.positions.length > 6 ? "…" : ""})`);
  const lines = [
    `Portfolio Risk — ${d.positions.length} positions across surfaces · equity $${d.heat.equityUsd ?? "n/a"} · N_eff ${d.nEff ?? "n/a"}`,
    `Heat: ${d.heat.heatPct ?? "n/a"}% of equity at stop risk (cluster cap ${d.heat.clusterCapPct}%) · worst cluster ${d.heat.worstCluster ? `${d.heat.worstCluster.cluster} ${d.heat.worstCluster.heatPct}%` : "none"}`,
    `Beta: ${beta || "n/a"}`,
    "Clusters:",
    ...clusters.map((c) => `  ${c}`),
    ...(d.warnings?.length ? ["Warnings:", ...d.warnings.map((w) => `  [${w.level.toUpperCase()}] ${w.message}`)] : ["Warnings: none"]),
    `Attribution — Virtual Book: ${(d.attribution?.virtualBook ?? [])
      .filter((a) => a.model !== "ALL")
      .map((a) => `${a.model === "M1_TREND_PULLBACK" ? "M1" : a.model === "WAIT_PULLBACK" ? "M1w" : "M2"} ${a.closed} closed · win ${a.winRatePct ?? "–"}% · ${a.totalR >= 0 ? "+" : ""}${a.totalR}R`)
      .join(" · ")} | MT5 live: ${d.attribution?.mt5Live?.positions ?? 0} positions / ${d.attribution?.mt5Live?.orders ?? 0} orders`,
    `Sources: ${d.source} · dataAt ${d.dataAt} · age ${Math.round((d.ageMs ?? 0) / 1000)}s`,
  ];
  return { text: lines.join("\n"), data: d };
}

async function toolNarrativeAiBrief(args) {
  const sym = resolveSymArg(args, "BTC");
  // DUAL-AGENT ARCHITECTURE — the terminal runtime makes ZERO external
  // AI/LLM calls. This tool assembles the deterministic BRIEF KIT: the
  // verified NarrativeVerdict + quant + options telemetry plus the
  // institutional briefing protocol, all from the local terminal. The
  // Antigravity trading-committee personas formulate the actual desk brief
  // NATIVELY from this kit — grounded only in the supplied numbers.
  const t0 = Date.now();
  const getJson = async (path) => {
    try {
      const res = await fetch(`${TERMINAL_URL}${path}`, { signal: AbortSignal.timeout(20_000) });
      if (!res.ok) return { __http: res.status };
      return await res.json();
    } catch (e) {
      return { __err: e instanceof Error ? e.message : String(e) };
    }
  };
  const [verdict, quant, options] = await Promise.all([
    getJson(`/api/cg/narrative?sym=${encodeURIComponent(sym)}`),
    getJson(`/api/cg/quant?sym=${encodeURIComponent(sym)}`),
    getJson(`/api/cg/options?sym=${encodeURIComponent(sym)}`),
  ]);
  const ms = Date.now() - t0;
  if (verdict?.ok !== true) {
    return {
      text: `${sym} brief kit unavailable: ${verdict?.__err ?? verdict?.error ?? `no verified narrative (route answered ${verdict?.__http ?? "ok:false"})`} — the terminal serves the deterministic verdict at /api/cg/narrative; the kit needs it as ground truth.`,
      data: { ok: false, sym, verdict },
    };
  }
  const fmt = (x, d = 2) => (typeof x === "number" && Number.isFinite(x) ? x.toFixed(d) : "n/a");
  const planLines = (verdict.plan?.targets ?? [])
    .map((t) => `- ${t.label}: ${t.level ?? "n/a"}${t.rMultiple != null ? ` (${fmt(t.rMultiple)}R)` : ""}`)
    .join("\n");
  const ladderLines = (verdict.plan?.ladder ?? [])
    .map((r) => `- ${r.weight != null ? `${Math.round(r.weight * 100)}%` : "?"} @ ${r.level ?? "n/a"} (${r.label ?? "rung"})`)
    .join("\n");
  const checklistLines = (verdict.checklist ?? [])
    .map((c) => `- ${c.label}: ${c.pass === true ? "PASS" : c.pass === false ? "FAIL" : "n/a"} (${c.value})`)
    .join("\n");
  const p = verdict.probability;
  const probLine = p
    ? `pFill ${fmt(p.pFill != null ? p.pFill * 100 : null, 1)}% · pT1 ${fmt(p.pT1 != null ? p.pT1 * 100 : null, 1)}% · pMeanRevert ${fmt(p.pMeanRevert != null ? p.pMeanRevert * 100 : null, 1)}% · EV ${fmt(p.evR)}R · σ_bar ${fmt(p.sigmaBar, 4)}`
    : "n/a";
  const q = quant && !quant.__err && !quant.__http ? quant : null;
  const o = options && !options.__err && !options.__http ? options : null;
  const text = [
    `${sym} deterministic AI brief kit (dual-agent — Antigravity formulates the brief natively; terminal runtime makes zero external LLM calls) · assembled in ${ms} ms`,
    ``,
    `BRIEFING PROTOCOL (institutional desk brief, max ~220 words):`,
    `- Cite ONLY the numbers in this kit — never invent prices, probabilities or data; n/a stays n/a.`,
    `- Structure: (1) one-line state of the setup, (2) strongest confirming evidence, (3) strongest RISK / blindspot that could invalidate it (stale fields, missing feeds, correlation/crowding, funding cost), (4) one concrete invalidation level to watch.`,
    `- Personas: Senior Quant Analyst (EV / Bayesian updates / GEX flip / Monte Carlo ruin), Order-Flow Microstructure Specialist (CVD divergence, absorption at VWAP/POC, liq sweeps), Chief Risk Officer (heat caps, spread drag, stop-first sizing).`,
    ``,
    `VERDICT (verified, deterministic): ${verdict.name} (${verdict.kind}) · regime ${verdict.regime} — ${verdict.regimeNote}`,
    `MODEL: ${verdict.activeModel} · direction ${verdict.direction} · grade ${verdict.grade} · confidence ${verdict.confidence}/100`,
    `THESIS: ${verdict.thesis}`,
    `PLAN: entry ${verdict.plan?.entryZone ? `${verdict.plan.entryZone[0]}–${verdict.plan.entryZone[1]}` : "n/a"} · stop ${verdict.plan?.stop ?? "n/a"} · bestR ${verdict.plan?.bestR ?? "n/a"}`,
    planLines,
    ladderLines ? `LADDER (composite-entry risk):\n${ladderLines}` : ``,
    `PROBABILITY/EV: ${probLine}`,
    `CHECKLIST:\n${checklistLines}`,
    `FRESHNESS: worst age ${Math.round((verdict.freshness?.worstAgeMs ?? 0) / 1000)}s · stale fields: ${verdict.freshness?.staleFields?.join(", ") || "none"}`,
    `QUANT: ${q ? `OI-weighted funding APR ${q.funding?.annualizedAprPct ?? "n/a"}% · OI 4h quadrant ${q.oiQuadrant?.["4h"]?.quadrant ?? "n/a"} · top-trader L/S ${q.lsRatios?.["1h"]?.topPositions?.ratio ?? "n/a"}${q.basis?.coinbasePremiumBps != null ? ` · Coinbase premium ${q.basis.coinbasePremiumBps}bps` : ""}` : "n/a"}`,
    `OPTIONS: ${o ? `GEX ${o.gex?.regime ?? "n/a"} · gamma flip ${o.gex?.gammaFlipPrice ?? o.gammaFlipPrice ?? "n/a"} · max pain ${o.maxPainStrike ?? "n/a"} · RR25d ${o.riskReversal25d?.value ?? "n/a"} · DVOL ${o.dvol?.value ?? "n/a"}` : "n/a (BTC/ETH only)"}`,
    `M1 STATUS: ${verdict.m1?.status ?? "n/a"} — ${verdict.m1?.note ?? ""}`,
    `M2 STATUS: ${verdict.m2?.status ?? "n/a"} — ${verdict.m2?.note ?? ""}`,
  ].join("\n");
  return {
    text,
    data: { ok: true, sym, architecture: "dual-agent", ms, verdict, quant: q, options: o },
  };
}

const TOOLS = [
  {
    name: "list_assets",
    description: "List the tradable assets with engine + scanner status. Count and membership come live from the terminal (source of truth: src/lib/cg/assets.ts ASSETS) — no hardcoded count.",
    inputSchema: { type: "object", properties: {} },
    handler: toolListAssets,
  },
  {
    name: "market_snapshot",
    description: "Live market snapshot: price, auction VAL/POC/VAH (session 8h / UTC day / UTC month), EMA 13/89/233 and RSI(14) on m15/h1/h4/d1, day VWAP with ±1σ/2σ/3σ bands.",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string", description: "Asset symbol, e.g. BTC, ETH, XAU" } },
      required: ["sym"],
    },
    handler: toolMarketSnapshot,
  },
  {
    name: "live_price",
    description: "Real-time price tick (1-second-grade feed): forming m1 candle close from Coinglass — the same live number the site charts show. Safe to poll at 1s; the terminal shares one upstream fetch across all callers.",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string", description: "Asset symbol, e.g. BTC, ETH, XAU" } },
      required: ["sym"],
    },
    handler: toolLivePrice,
  },
  {
    name: "signals",
    description: "Footprint signals (Coinglass-exact engine): Absorption / Divergence / Strong Divergence / Trap / Exhaustion on 15m time bars, auto volume bars and auto range bars.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string" },
        bt: { type: "string", enum: ["all", "time", "vol", "range"], description: "Bar type filter (default all)" },
      },
      required: ["sym"],
    },
    handler: toolSignals,
  },
  {
    name: "notifications",
    description: "The live notification feed (footprint signals, indicator events, whale walls, liq sweep setups, CVD divergences). Cursor-based: pass `since` (or rely on the stored cursor) to get only new events. Cursor auto-advances.",
    inputSchema: {
      type: "object",
      properties: {
        since: { type: "number", description: "Return events with id > since (default: stored cursor)" },
        asset: { type: "string" },
        source: { type: "string", enum: ["footprint", "indicators", "whale-orders", "liqlevels", "cvd", "paperbook", "engine"] },
        limit: { type: "number" },
        advance_cursor: { type: "boolean", description: "Advance the persistent cursor to the newest id (default true)" },
      },
    },
    handler: toolNotifications,
  },
  {
    name: "liquidation_levels",
    description: "Aggregated liquidation clusters (3d/7d/14d/30d, ±4%, Binance+OKX+Bybit+Hyperliquid) with strongest levels above/below — the sweep magnets.",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string" }, range: { type: "string", enum: ["3d", "7d", "14d", "30d"] } },
      required: ["sym"],
    },
    handler: toolLiquidationLevels,
  },
  {
    name: "liq_live",
    description: "LIVE liquidation prints, server-collected from Binance (!forceOrder@arr) + Bybit (allLiquidation) + OKX (liquidation-orders) and persisted per asset (24h journal). Per-asset: recent prints + 1h/24h aggregates (count, USD, long/short split). No sym = universe-wide tape + hottest assets. Event-stream freshness: quiet (healthy, no events) is reported separately from stale (collector down).",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string", description: "Terminal asset (BTC, 1000PEPE, NVDA...) — omit for the universe-wide view" },
        limit: { type: "number", description: "Max prints returned (default 15, max 60)" },
      },
    },
    handler: toolLiqLive,
  },
  {
    name: "whale_orders",
    description: "Whale resting open orders across all supported exchanges: aggregate bid/ask USD, imbalance and the largest walls (ask/bid wall info like the Liquidity Heatmap page).",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string" }, minUsd: { type: "number", description: "Minimum order size in USD (default 1,000,000)" } },
      required: ["sym"],
    },
    handler: toolWhaleOrders,
  },
  {
    name: "book_walls",
    description: "Order-book depth walls from the aggregate liquidity-heatmap stream: bid/ask depth, imbalance (now vs 5m ago), confirmed wall list with age/touched state and the add/fill/pull event feed. Full dimension control: market (futures/spot), interval, limit, band %, endTime history.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string" },
        market: { type: "string", enum: ["futures", "spot"] },
        interval: { type: "string", enum: ["1m", "5m", "15m", "1h", "4h", "1d"], description: "Snapshot interval (m-token aliases m1/m5/m15/h1/h4/d1 also accepted)" },
        limit: { type: "number", description: "Snapshot frames (30-300, default 150 — route clamp)" },
        band: { type: "number", enum: [0.5, 1, 2, 5], description: "Depth band % around mid — route allowlist 0.5/1/2/5, default 5" },
        endTime: { type: "number", description: "Unix seconds — historical window (up to 30 days back)" },
      },
      required: ["sym"],
    },
    handler: toolBookWalls,
  },
  {
    name: "cvd_state",
    description: "Cumulative Volume Delta state: perp/spot CVD, taker buy share, active price/CVD divergences. Timeframe (itv), bar count and KPI anchor (all/day/week) configurable.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string" },
        mode: { type: "string", enum: ["perp", "spot", "total"] },
        itv: { type: "string", enum: ["1m", "5m", "15m", "30m", "1h", "4h", "1d"] },
        limit: { type: "number", description: "Bars (300-1500, default 600 — route clamp)" },
        anchor: { type: "string", enum: ["all", "day", "week"] },
      },
      required: ["sym"],
    },
    handler: toolCvdState,
  },
  {
    name: "stopmap",
    description:
      "Liquidation heatmap (stops map) — the CURRENT Coinglass /LiquidationHeatMap page: Model 1/2/3 grids with Pair (Binance venue) or Coin (all venues aggregated) mode over 12h..2y ranges. Returns the page's key liquidation levels above/below (sweep magnets for pullback entries along the trend) and the market overview (open longs/shorts, total, largest level). Default: model 1, coin mode, 24h. Legacy form (sym only) serves the BTC 24h grid; endTime pages ~30d of history (BTC only).",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string", description: "any curated perp (XAU, BTC, ETH…) in model/mode/range form; legacy form is BTC-only" },
        model: { type: "string", enum: ["1", "2", "3"], description: "Coinglass model tab (default 1)" },
        mode: { type: "string", enum: ["coin", "pair"], description: "coin = all venues aggregated (default), pair = Binance venue" },
        range: { type: "string", enum: ["12h", "24h", "48h", "3d", "7d", "14d", "30d", "90d", "180d", "1y", "2y", "all"], description: "window (default 24h; 'all' sweeps 12h..90d; 180d+ need a paid Coinglass grade)" },
        endTime: { type: "number", description: "Unix seconds — legacy BTC 24h historical window (up to 30 days back)" },
      },
      required: ["sym"],
    },
    handler: toolStopmap,
  },
  {
    name: "footprint",
    description: "Aggregated order-flow bars across the deepest perp venues: per-bar taker buy/sell USD, delta %, POC price — the footprint series behind the signal engine. Interval and bar count configurable.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string" },
        interval: { type: "string", enum: ["m1", "m3", "m5", "m15", "m30", "h1", "h2", "h4", "h6", "h12", "d1", "w1"], description: "Bar interval (route vocabulary; 15m-style page aliases accepted), default m15" },
        limit: { type: "number", description: "Bars to fetch (10-500, default 96)" },
        last: { type: "number", description: "Bars to return in detail (1-48, default 12)" },
      },
      required: ["sym"],
    },
    handler: toolFootprint,
  },
  {
    name: "large_orders",
    description: "Large resting orders AND executed large trades for one instrument (default Binance perp): resting bid/ask walls + executed notional split — the aggressive-whale tape. hours + interval configurable.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string", description: "Asset symbol — derives the default Binance instrument key" },
        symbol: { type: "string", description: "Explicit instrument key, e.g. OKX_BTC-USDT-SWAP (overrides sym)" },
        hours: { type: "number", description: "Lookback hours (1-720, default 24)" },
        interval: { type: "string", enum: ["m1", "m3", "m5", "m15", "m30", "h1", "h2", "h4", "h6", "h12", "d1", "w1"], description: "Bar interval (route vocabulary; 15m-style page aliases accepted), default m15" },
      },
      required: ["sym"],
    },
    handler: toolLargeOrders,
  },
  {
    name: "kline_series",
    description: "Raw candle or open-interest series for any instrument key (default Binance perp): type kline|oi, any interval, up to 1000 rows — the building block for custom studies.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string", description: "Asset symbol — derives the default Binance instrument key" },
        symbol: { type: "string", description: "Explicit instrument key, e.g. Hyperliquid_BTC-USD (overrides sym)" },
        type: { type: "string", enum: ["kline", "oi"] },
        interval: { type: "string", enum: ["m1", "m3", "m5", "m15", "m30", "h1", "h2", "h4", "h6", "h12", "d1", "w1"], description: "Bar interval m1…w1 (15m-style page aliases accepted), default m15" },
        limit: { type: "number", description: "Rows (10-1000, default 300)" },
      },
      required: ["sym"],
    },
    handler: toolKline,
  },
  {
    name: "analyze_setup",
    description: "One-call trade-formulation context for an asset: market snapshot + footprint signals + liquidation clusters + whale walls + book walls + CVD state + recent events + the formulation guide (bias / entry / stop / targets).",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolAnalyzeSetup,
  },
  {
    name: "trade_narrative",
    description: "The deterministic M1/M2 trade narrative: regime classification (TREND_UP/DOWN · RANGE_BALANCE · COMPRESSION_SQUEEZE), dual-model evaluation (M1 trend-pullback, M2 VWAP ±2σ-3σ mean reversion), hard vetoes, full plan (entry zone · trigger · stop · T1-T3 R-ladder) and the exact-value checklist with freshness (worstAgeMs, staleFields). Works for crypto, TradFi perps and classic (reduced checklist).",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string", description: "Asset symbol, e.g. BTC, NVDA, SPX500" } },
      required: ["sym"],
    },
    handler: toolTradeNarrative,
  },
  {
    name: "quant_positioning",
    description: "Derivatives positioning: Binance long/short ratios (global accounts, top-trader accounts, top-trader positions, taker buy/sell) at 15m/1h/4h, OI-weighted funding + annualized APR + cross-venue arb spread, OI×price 4-quadrant states (1h/4h/24h), spot-perp basis and Coinbase premium (majors).",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string", description: "Asset symbol, e.g. BTC, NVDA, 1000PEPE" } },
      required: ["sym"],
    },
    handler: toolQuantPositioning,
  },
  {
    name: "options_surface",
    description: "Deribit options surface (BTC/ETH): DVOL index, ATM IV term structure (7/30/90d), 25-delta risk-reversal skew, max pain + maxPainStrike (nearest monthly pin magnet), GEX flip point (zero-gamma regime boundary), top call/put OI walls, dealer net gamma (GEX) regime and put/call ratios.",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string", enum: ["BTC", "ETH"], description: "Deribit lists options only for BTC/ETH" } },
      required: ["sym"],
    },
    handler: toolOptionsSurface,
  },
  {
    name: "alpha_screener",
    description: "Ranked cross-asset screener over the canonical Curated-61 (30 crypto perps + 28 TradFi perps + 3 classics): live price, 24h%, VWAP σ, RVOL, narrative-radar M1/M2 state (model · grade · direction · EV in R), OI 4h quadrant, OI-weighted funding APR, trailing-5m liquidation cascade USD and multi-factor confluence — with radar sweep liveness telemetry.",
    inputSchema: {
      type: "object",
      properties: {
        kind: { type: "string", enum: ["crypto-perp", "tradfi-perp", "classic"], description: "Optional kind filter" },
        gradeMin: { type: "string", enum: ["A", "A+"], description: "Optional minimum narrative grade" },
      },
    },
    handler: toolAlphaScreener,
  },
  {
    name: "desk_status",
    description: "Read-only live snapshot of MT5 account, open positions, pending limit orders, live distance-to-fill, and recent desk rounds.",
    inputSchema: { type: "object", properties: {} },
    handler: toolDeskStatus,
  },
  {
    name: "paper_book",
    description: "The Virtual Book: live paper-trade outcome tracker for every agent setup (M1 trend-pullback · M1 wait-zone · M2 VWAP ±2σ mean-reversion) across the full 61-asset universe. Tickets carry frozen entry zone/stop/rung ladder, real fills on m1 wicks, live unrealized R, MFE/MAE excursions, and closed outcomes (TP1/TP2/SL/BE/TIME_STOP/FLIP_CLOSE/EXPIRED). Per-model scoreboard: win rate, expectancy in R, total R, fill rate, avg hold.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string", description: "Optional asset filter, e.g. BTC, NVDA, SPX500 — also returns the most relevant ticket for the symbol" },
        status: { type: "string", enum: ["ARMED", "OPEN", "CLOSED"], description: "Optional status filter (default all)" },
        limit: { type: "number", description: "Max tickets returned (default 30)" },
      },
    },
    handler: toolPaperBook,
  },
  {
    name: "backtest_summary",
    description: "Walk-forward M1/M2 backtest summary: fill/win rates, profit factor, expectancy in R, drawdown, Sharpe/Sortino per trade, max consecutive-loss streak, and the 1000-path Monte Carlo ruin profile (P of >=5R / >=10R drawdowns) — plus per-model/tier/regime buckets, parameter sensitivity and probability calibration curves.",
    inputSchema: {
      type: "object",
      properties: {
        sym: { type: "string", description: "Optional asset filter — summarizes that asset's historical trades only" },
      },
    },
    handler: toolBacktestSummary,
  },
  {
    name: "portfolio_risk",
    description: "Live portfolio risk engine across every exposure surface (MT5 positions/orders + desk directives + Binance testnet + Virtual Book open tickets): rolling 24h/7d return-correlation matrix, net beta vs BTC/QQQ, cluster exposure (Crypto Majors/Alts · AI-Semis · Metals · Energy · Indices), effective independent bets N_eff, portfolio heat (% equity at stop risk) and correlation-guard warnings.",
    inputSchema: { type: "object", properties: {} },
    handler: toolPortfolioRisk,
  },
  {
    name: "narrative_ai_brief",
    description:
      "Deterministic AI brief kit for one asset (dual-agent architecture): the verified NarrativeVerdict + quant + options telemetry + the institutional briefing protocol, assembled from the local terminal with ZERO external LLM calls in the runtime. Antigravity's trading-committee personas (Senior Quant Analyst / Order-Flow Microstructure Specialist / Chief Risk Officer) formulate the desk brief natively from this kit — cite only the supplied numbers, n/a stays n/a.",
    inputSchema: {
      type: "object",
      properties: { sym: { type: "string", description: "Asset symbol, e.g. BTC, NVDA" } },
      required: ["sym"],
    },
    handler: toolNarrativeAiBrief,
  },
  {
    name: "athena_oracle",
    description: "Athena Oracle 6-factor confluence score (Apollo trend, Poseidon CVD, Hades liq gravity, Zeus whales, Atlas book walls, Nemesis funding).",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolAthenaOracle,
  },
  {
    name: "microstructure",
    description: "Multi-venue L2/L3 microstructure: Hyperliquid L2, Lighter L3, Binance/OKX/Bybit/Coinbase BBO best spread bps.",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolMicrostructure,
  },
  {
    name: "live_orderbook",
    description: "Binance live top-1000 depth orderbook (/api/cg/livebook): immediate bid/ask imbalance, resting walls, covered range.",
    inputSchema: { type: "object", properties: { sym: { type: "string" }, band: { type: "number", enum: [0.5, 1, 2, 5] } }, required: ["sym"] },
    handler: toolLiveOrderbook,
  },
  {
    name: "live_tick",
    description: "Fast 250ms REST live BBO price tick (/api/cg/live): last, bid, ask, source.",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolLiveTick,
  },
  {
    name: "funding_venues",
    description: "Full 21-venue funding breakdown + OI-weighted 8h rate and annualized APR (/api/cg/funding).",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolFundingVenues,
  },
  {
    name: "open_interest",
    description: "Per-venue Open Interest breakdown and aggregate total (/api/cg/oi).",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolOpenInterest,
  },
  {
    name: "perp_depth",
    description: "Verified derivatives coverage for Tier-3 classic/TradFi instruments (/api/cg/perpdepth): SP500, JP225, CL, NATGAS, etc.",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolPerpDepth,
  },
  {
    name: "cot_report",
    description: "CFTC Legacy Commitment of Traders institutional positioning (/api/cg/cot): XAU, XAG, SP500, EURUSD, etc.",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolCotReport,
  },
  {
    name: "classic_macro_quotes",
    description: "Live classic indices/commodities/FX quotes from Yahoo + CNBC (/api/cg/classic & /api/cg/cnbc).",
    inputSchema: { type: "object", properties: { sym: { type: "string" }, syms: { type: "string", description: "Comma list for CNBC, e.g. SP500,NDX100,EURUSD" } } },
    handler: toolClassicMacroQuotes,
  },
  {
    name: "playbook_cards",
    description: "Validated EA-2 / EA-5 / EA-6 live playbook candidate cards (/api/strategy/cards).",
    inputSchema: { type: "object", properties: { sym: { type: "string", description: "Optional symbol or comma list (default BTC,ETH,SOL)" } } },
    handler: toolPlaybookCards,
  },
  {
    name: "alert_rules",
    description: "Active order-flow alert rules and telemetry status (/api/cg/alert-rules).",
    inputSchema: { type: "object", properties: {} },
    handler: toolAlertRules,
  },
  {
    name: "terminal_health",
    description: "Live CG Terminal server health, Node heap/RSS, engine backfill & active series counts (/api/health, /api/cg/engine/status, /api/cg/debug/mem).",
    inputSchema: { type: "object", properties: {} },
    handler: toolTerminalHealth,
  },
  {
    name: "binance_market_data",
    description: "Direct Binance Futures market telemetry (/api/binance/quotes | market | klines | universe | account).",
    inputSchema: {
      type: "object",
      properties: {
        action: { type: "string", enum: ["quotes", "market", "klines", "universe", "account"] },
        sym: { type: "string" },
        syms: { type: "string" },
        interval: { type: "string" },
        limit: { type: "number" },
      },
    },
    handler: toolBinanceMarketData,
  },
  {
    name: "telemetry_snapshot",
    description: "Read hydrated local telemetry JSON for an asset (MT5 quote/spread, all 12h..90d stopmap walls, 16-param cache, 7d footprint summary).",
    inputSchema: { type: "object", properties: { sym: { type: "string" } }, required: ["sym"] },
    handler: toolTelemetrySnapshot,
  },
  {
    name: "open_live_visual",
    description: "Open or update the live interactive CG Order-Flow Visual panel (Price, Narrative, Oracle, Multi-Horizon Stopmap Walls, MT5 Blotter) directly inside Antigravity.",
    inputSchema: { type: "object", properties: { sym: { type: "string", description: "Asset symbol (default BTC)" } } },
    _meta: {
      ui: { resourceUri: "ui://cg-terminal/dashboard.html" },
      "com.google.antigravity/ui": {
        entrypoints: [
          { type: "aux_pane", title: "CG Order-Flow" },
          { type: "global", title: "CG Terminal" },
        ],
      },
    },
    handler: toolOpenLiveVisual,
  },
];

const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>CG Order-Flow Live Visual</title>
<style>
  :root { color-scheme: dark; --bg:#0b0f17; --card:#121826; --bd:#222c40; --tx:#e5edf7; --mut:#8ea0b8; --up:#22c55e; --dn:#ef4444; --ac:#38bdf8; }
  *{box-sizing:border-box;font-family:Inter,ui-sans-serif,system-ui,sans-serif}
  body{margin:0;padding:12px;background:var(--bg);color:var(--tx);font-size:12px}
  .top{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:10px}
  .pills{display:flex;gap:4px;flex-wrap:wrap}
  button{background:#192235;color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:4px 8px;cursor:pointer;font-size:11px}
  button.on{border-color:var(--ac);background:#102a45;color:#7dd3fc}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:10px}
  .h{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-bottom:6px;display:flex;justify-content:space-between}
  .big{font-size:18px;font-weight:700}
  .up{color:var(--up)} .dn{color:var(--dn)} .ac{color:var(--ac)}
  .row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid rgba(255,255,255,.04)}
  .barbg{height:6px;background:#1e293b;border-radius:3px;overflow:hidden;margin:3px 0 6px}
  .barfg{height:100%;background:linear-gradient(90deg,#38bdf8,#22c55e)}
  code{font-family:ui-monospace,monospace;font-size:11px}
</style>
</head>
<body>
<div class="top">
  <div><strong id="title">CG TERMINAL LIVE</strong> <span id="stamp" style="color:var(--mut)">connecting…</span></div>
  <div class="pills" id="syms"></div>
  <div><button id="refresh">Refresh</button> <button id="auto" class="on">Auto 10s</button></div>
</div>
<div class="grid" id="cards"></div>
<script>
const PRESETS = ["BTC","ETH","SOL","XAU","XAG","CL","NVDA","MSTR","QQQ","ZEC","1000SHIB"];
let activeSym = "BTC", timer = null, rpcId = 1, hasServerBridge = false;
const pending = new Map();
window.addEventListener("message",(ev)=>{
  const m = ev.data; if (!m || typeof m !== "object") return;
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(m.error) : p.res(m.result); }
  if (m.method === "ui/notifications/tool-result" && m.params?.structuredContent) renderData(m.params.structuredContent);
});
function rpc(method, params){
  const id = rpcId++;
  return new Promise((res, rej)=>{
    pending.set(id,{res,rej});
    window.parent.postMessage({jsonrpc:"2.0",id,method,params},"*");
    setTimeout(()=>{if(pending.has(id)){pending.delete(id);rej(new Error("timeout"));}}, 6000);
  });
}
async function fetchLive(sym){
  activeSym = sym; renderPills();
  if (hasServerBridge) {
    try {
      const r = await rpc("tools/call",{name:"open_live_visual",arguments:{sym}});
      if (r?.structuredContent) { renderData(r.structuredContent); return; }
    } catch {}
  }
  try {
    const [m,n,o,s3,s30,d] = await Promise.all([
      fetch("http://127.0.0.1:3000/api/cg/market?sym="+sym).then(r=>r.json()).catch(()=>null),
      fetch("http://127.0.0.1:3000/api/cg/narrative?sym="+sym).then(r=>r.json()).catch(()=>null),
      fetch("http://127.0.0.1:3000/api/cg/oracle?sym="+sym).then(r=>r.json()).catch(()=>null),
      fetch("http://127.0.0.1:3000/api/cg/stopmap?sym="+sym+"&model=1&mode=coin&range=3d").then(r=>r.json()).catch(()=>null),
      fetch("http://127.0.0.1:3000/api/cg/stopmap?sym="+sym+"&model=1&mode=coin&range=30d").then(r=>r.json()).catch(()=>null),
      fetch("http://127.0.0.1:3000/api/desk/live").then(r=>r.json()).catch(()=>null),
    ]);
    renderData({sym,fetchedAt:new Date().toISOString(),market:m,narrative:n,oracle:o,stopmap3d:s3?.model??s3,stopmap30d:s30?.model??s30,desk:d});
  } catch (e) { document.getElementById("stamp").textContent = "err: "+e.message; }
}
const f=(v,d=2)=>(v==null||isNaN(v)?"—":Number(v).toFixed(d));
const usd=(v)=>(v==null||isNaN(v)?"—":Math.abs(v)>=1e6?"$"+(v/1e6).toFixed(1)+"M":"$"+Math.round(v/1e3)+"K");
function renderData(d){
  if (!d) return;
  if (d.sym) activeSym = d.sym;
  renderPills();
  document.getElementById("stamp").textContent = d.sym + " · " + (d.fetchedAt||"").slice(11,19) + "Z";
  const m = d.market||{}, nar = d.narrative||{}, o = d.oracle||{}, s3 = d.stopmap3d||{}, s30 = d.stopmap30d||{}, desk = d.desk||{};
  const px = m.price?.last ?? "—", vw = m.vwap?.vwap, sd1 = m.vwap?.bands?.sd1;
  const vz = (px!=="—"&&vw&&sd1)?((px-vw)/sd1):null;
  const aucD = m.auction?.day||{}, aucS = m.auction?.session||{};
  const w3 = s3.walls||{}, w30 = s30.walls||{};
  const kl3 = s3.keyLevels||{}, kl30 = s30.keyLevels||{};
  const acc = desk.mt5?.account||{};
  document.getElementById("cards").innerHTML = [
    '<div class="card"><div class="h"><span>PRICE &amp; VWAP</span><span>'+(m.source||"live")+'</span></div>' +
      '<div class="big">'+px+' <span class="'+((m.price?.chgDayPct||0)>=0?"up":"dn")+'" style="font-size:12px">'+f(m.price?.chgDayPct)+'%</span></div>' +
      '<div class="row"><span>VWAP / Z</span><code>'+f(vw)+' ('+f(vz)+'σ)</code></div>' +
      '<div class="row"><span>Day VA</span><code>'+f(aucD.val)+' / '+f(aucD.poc)+' / '+f(aucD.vah)+'</code></div>' +
      '<div class="row"><span>Sess VA</span><code>'+f(aucS.val)+' / '+f(aucS.poc)+' / '+f(aucS.vah)+'</code></div>' +
      '<div class="row"><span>M15 EMA</span><code>89:'+f(m.ema?.m15?.e89)+' 233:'+f(m.ema?.m15?.e233)+'</code></div></div>',
    '<div class="card"><div class="h"><span>ATHENA ORACLE</span><span>'+(o.regime||"")+'</span></div>' +
      '<div class="big ac">'+(o.score??"—")+'/100</div>' +
      '<div class="barbg"><div class="barfg" style="width:'+(o.score||0)+'%"></div></div>' +
      (o.factors||[]).map(x=>'<div class="row"><span>'+x.id+'</span><code>'+x.score+'/100</code></div>').join("")+'</div>',
    '<div class="card"><div class="h"><span>NARRATIVE MODEL</span><span>'+(nar.grade||"—")+'</span></div>' +
      '<div class="big '+(nar.direction==="LONG"?"up":nar.direction==="SHORT"?"dn":"")+'">'+(nar.activeModel||"WAIT")+' · '+(nar.direction||"—")+'</div>' +
      '<div class="row"><span>Regime</span><code>'+(nar.regime||"—")+'</code></div>' +
      '<div class="row"><span>Entry</span><code>'+(nar.plan?.entryZone?nar.plan.entryZone.join("–"):"—")+'</code></div>' +
      '<div class="row"><span>Stop / BestR</span><code>'+(nar.plan?.stop??"—")+' / '+f(nar.plan?.bestR)+'R</code></div></div>',
    '<div class="card"><div class="h"><span>STOPMAP 3D &amp; 30D WALLS</span><span>M1 COIN</span></div>' +
      '<div class="row"><span>3D MaxS / MaxL</span><code>'+(w3.max_short?w3.max_short.price+"("+usd(w3.max_short.mx_usd)+")":"—")+' / '+(w3.max_long?w3.max_long.price+"("+usd(w3.max_long.mx_usd)+")":"—")+'</code></div>' +
      '<div class="row"><span>30D MaxS / MaxL</span><code>'+(w30.max_short?w30.max_short.price+"("+usd(w30.max_short.mx_usd)+")":"—")+' / '+(w30.max_long?w30.max_long.price+"("+usd(w30.max_long.mx_usd)+")":"—")+'</code></div>' +
      '<div class="row"><span>3D Above</span><code>'+(kl3.above||[]).slice(0,3).map(k=>k.price).join(", ")+'</code></div>' +
      '<div class="row"><span>3D Below</span><code>'+(kl3.below||[]).slice(0,3).map(k=>k.price).join(", ")+'</code></div>' +
      '<div class="row"><span>30D Above</span><code>'+(kl30.above||[]).slice(0,3).map(k=>k.price).join(", ")+'</code></div></div>',
    '<div class="card"><div class="h"><span>MT5 #5126976 BLOTTER</span><span>pos:'+(desk.mt5?.positions?.length??0)+'</span></div>' +
      '<div class="row"><span>Balance / Eq</span><code>$'+f(acc.balance)+' / $'+f(acc.equity)+'</code></div>' +
      (desk.mt5?.positions||[]).map(p=>'<div class="row"><span>'+p.symbol+' '+p.side+'</span><code>'+p.profit+'</code></div>').join("")+'</div>'
  ].join("");
}
function renderPills(){
  const host = document.getElementById("syms");
  host.innerHTML = PRESETS.map(s=>'<button data-sym="'+s+'" class="'+(s===activeSym?"on":"")+'">'+s+'</button>').join("");
  host.querySelectorAll("button[data-sym]").forEach(b=>{b.onclick=()=>fetchLive(b.dataset.sym);});
}
document.getElementById("refresh").onclick=()=>fetchLive(activeSym);
document.getElementById("auto").onclick=(e)=>{
  if(timer){clearInterval(timer);timer=null;e.target.classList.remove("on");}
  else{timer=setInterval(()=>fetchLive(activeSym),10000);e.target.classList.add("on");}
};
(async()=>{
  try{
    const init = await rpc("ui/initialize",{appCapabilities:{},clientInfo:{name:"cg-visual",version:"1.0.0"},protocolVersion:"2025-03-26"});
    hasServerBridge = Boolean(init?.hostCapabilities?.serverTools);
    window.parent.postMessage({jsonrpc:"2.0",method:"ui/notifications/initialized",params:{}},"*");
  }catch{}
  renderPills();
  fetchLive(activeSym);
  timer = setInterval(()=>fetchLive(activeSym),10000);
})();
</script>
</body>
</html>`;

const rl = readline.createInterface({ input: process.stdin });

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

rl.on("line", (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params } = msg;
  if (method === "initialize") {
    send({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: params?.protocolVersion ?? "2024-11-05",
        capabilities: { tools: {}, resources: {} },
        serverInfo: { name: "cg-terminal", version: "2.0.0" },
      },
    });
    return;
  }
  if (method === "notifications/initialized" || method?.startsWith("notifications/")) return; // no response
  if (method === "ping") {
    send({ jsonrpc: "2.0", id, result: {} });
    return;
  }
  if (method === "resources/list") {
    send({
      jsonrpc: "2.0",
      id,
      result: {
        resources: [
          {
            uri: "ui://cg-terminal/dashboard.html",
            name: "CG Order-Flow Live Dashboard",
            description: "Interactive real-time terminal visual inside Antigravity.",
            mimeType: "text/html;profile=mcp-app",
          },
        ],
      },
    });
    return;
  }
  if (method === "resources/read") {
    if (params?.uri === "ui://cg-terminal/dashboard.html") {
      send({
        jsonrpc: "2.0",
        id,
        result: {
          contents: [
            {
              uri: "ui://cg-terminal/dashboard.html",
              mimeType: "text/html;profile=mcp-app",
              text: DASHBOARD_HTML,
              _meta: {
                ui: {
                  prefersBorder: false,
                  csp: { connectDomains: ["http://localhost:3000", "http://127.0.0.1:3000"] },
                },
              },
            },
          ],
        },
      });
    } else {
      send({ jsonrpc: "2.0", id, error: { code: -32602, message: `unknown resource: ${params?.uri}` } });
    }
    return;
  }
  if (method === "tools/list") {
    send({
      jsonrpc: "2.0",
      id,
      result: {
        tools: TOOLS.map(({ name, description, inputSchema, _meta }) => ({
          name,
          description,
          inputSchema,
          ...(_meta ? { _meta } : {}),
        })),
      },
    });
    return;
  }
  if (method === "tools/call") {
    const name = params?.name;
    const tool = TOOLS.find((t) => t.name === name);
    if (!tool) {
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: `unknown tool ${name}` }], isError: true } });
      return;
    }
    Promise.resolve(tool.handler(params?.arguments ?? {}))
      .then((out) => {
        send({
          jsonrpc: "2.0",
          id,
          result: {
            content: [
              { type: "text", text: out.text },
              ...(out.data ? [{ type: "text", text: "```json\n" + JSON.stringify(out.data).slice(0, 60_000) + "\n```" }] : []),
            ],
            ...(out.structuredContent ? { structuredContent: out.structuredContent } : {}),
          },
        });
      })
      .catch((e) => {
        send({
          jsonrpc: "2.0",
          id,
          result: { content: [{ type: "text", text: `tool ${name} failed: ${e?.message ?? e}` }], isError: true },
        });
      });
    return;
  }
  if (id != null) send({ jsonrpc: "2.0", id, error: { code: -32601, message: `method not found: ${method}` } });
});

process.stderr.write(`[cg-terminal-mcp] v2.0 ready (${TOOLS.length} tools + live UI resource) — terminal at ${TERMINAL_URL}\n`);
