import { NextRequest, NextResponse } from "next/server";
import { cgGet } from "@/lib/cg/client";
import { makeObeToken } from "@/lib/cg/crypto";
import { buildStopModel, buildRollingStopModel, lastRollingNullReason, type LiqHeatmapPayload, type StopModel } from "@/lib/cg/liq-heatmap";
import { isAsset } from "@/lib/cg/assets";
import { isCuratedPerp } from "@/lib/cg/assets";
import { anyTerminalSym } from "@/lib/cg/sym-gate";
import { BoundedMap } from "@/lib/cg/bounded";
import {
  fetchModel1Heatmap,
  HEATMAP_RANGES,
  HeatmapUpstreamError,
  type HeatmapMode,
  type HeatmapModelId,
} from "@/lib/cg/model1-heatmap";

export const dynamic = "force-dynamic";

/**
 * GET /api/cg/stopmap?sym=BTC[&model=1|2|3][&mode=coin|pair][&range=12h..2y][&endTime=<unix-sec>]
 *
 * Liquidation heatmap ("stops map") — the CURRENT Coinglass
 * /LiquidationHeatMap page (the "new UI": Model 1/2/3 tabs, Pair/Coin
 * toggle, 12h..2y ranges), plus the legacy BTC 24h window.
 *
 * ── model / mode / range (the live page's semantics, 2026-11-22) ──
 *   model 1 = the classic 5m/15m/… leverage grid (DEFAULT)
 *   model 2 / 3 = the page's alternative leverage models
 *   mode  coin = ALL venues aggregated under the plain symbol (DEFAULT —
 *                the most useful view for pullback trading)
 *   mode  pair = the Binance execution venue only
 *   range = 12h|24h|48h|3d|7d|14d|30d|90d|180d|1y|2y
 * The response carries the page's exact panel math: keyLevels (the
 * "Key liquidation levels" panel: top-4 local maxima per side of the last
 * column), overview (the "Market overview" panel: open longs/shorts,
 * total, largest level, price step, column period), the sparse heat grid
 * and the cumulative swept-from-price curves. Panel math verified 1:1
 * against the operator's screenshots (XAU coin 3d: 3614→13.6M, 3510→
 * 18.3M, total 458.5M, step 3.38/15m).
 *
 * ── auth tiers (live-verified 2026-11-22, see
 *    scripts/research/probe_session_matrix.mjs) ──
 *   CG_SESSION set   → every symbol, models 1/2/3, coin+pair, 12h..90d
 *                      (180d/1y/2y need a paid grade → upstream 40003)
 *   CG_SESSION unset → BTC pair 24h (+endTime history) natively;
 *                      every other perp falls back to the honest
 *                      LIQ-SNAPSHOT ROLLING heatmap (badged, never a 400).
 *
 * Pure API data: every number comes from the fapi liqHeatMap family.
 */

/** module-level cache keyed by sym|endTime — the `data` token changes every
 * call, so cgGet's URL-keyed cache would never hit; cache the parsed model.
 * BOUNDED (the engine pages history here — varying endTime must not grow it). */
const cache = new BoundedMap<{ at: number; body: unknown }>(16);

async function loadStopmap(sym: string, endTime: number | undefined, ttlMs = 25_000) {
  const ck = `${sym}|${endTime ?? ""}`;
  const hit = cache.get(ck);
  if (hit && Date.now() - hit.at < ttlMs) return hit.body;
  const refSymbol = `Binance_${sym}USDT`;
  // URLSearchParams encodes the token once — do NOT pre-encode it
  const q = new URLSearchParams({
    merge: "true",
    symbol: refSymbol,
    interval: "5",
    limit: "288",
    data: makeObeToken(),
  });
  if (endTime) q.set("endTime", String(endTime));
  try {
    const payload = await cgGet(`/api/index/v2/liqHeatMap?${q.toString()}`, 45_000);
    cache.set(ck, { at: Date.now(), body: payload });
    return payload;
  } catch (err) {
    if (hit) return hit.body;
    throw err;
  }
}

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const sym = (sp.get("sym") ?? "BTC").toUpperCase();
  if (!(await anyTerminalSym(sym))) {
    return NextResponse.json({ ok: false, error: `unknown asset ${sym}` }, { status: 400 });
  }

  const hasSession = Boolean(process.env.CG_SESSION?.trim());

  // ── NEW SURFACE: /api/cg/stopmap?sym=XAU&model=1&mode=coin&range=3d ──
  // requested whenever any of model/mode/range is present; the plain
  // ?sym=BTC legacy form keeps its exact old behavior.
  const modelParam = sp.get("model");
  const modeParam = sp.get("mode");
  const rangeParam = sp.get("range");
  if (modelParam || modeParam || rangeParam) {
    const model = (modelParam ?? "1") as HeatmapModelId;
    const mode = (modeParam ?? "coin") as HeatmapMode;
    const range = rangeParam ?? "24h";
    if (!["1", "2", "3"].includes(model)) {
      return NextResponse.json({ ok: false, error: `model must be 1, 2 or 3` }, { status: 400 });
    }
    if (mode !== "coin" && mode !== "pair") {
      return NextResponse.json({ ok: false, error: `mode must be coin or pair` }, { status: 400 });
    }
    if (!HEATMAP_RANGES.includes(range)) {
      return NextResponse.json({ ok: false, error: `range must be one of ${HEATMAP_RANGES.join(", ")}` }, { status: 400 });
    }
    if (!isCuratedPerp(sym)) {
      return NextResponse.json({
        ok: true,
        supported: false,
        sym,
        reason: "Liquidation heatmaps require perp leverage tiers (unavailable for classic cash/CFD instruments)",
        dataAt: Date.now(),
        ageMs: 0,
        stale: false,
        source: "stopmap-gate",
      });
    }
    try {
      const m = await fetchModel1Heatmap({ sym, model, mode, range });
      return NextResponse.json({
        ok: true,
        sym,
        symbol: m.symbol,
        reference: mode === "coin" ? `aggregate:${sym}` : m.symbol,
        model: m,
        window: {
          interval: `${m.overview.columnMinutes}m`,
          bars: m.candles.length,
          from: m.candles[0]?.ts,
          to: m.candles[m.candles.length - 1]?.ts,
        },
        instrument: m.instrument,
        dataAt: m.updateTime,
        ageMs: Date.now() - m.updateTime,
        stale: Date.now() - m.updateTime > 10 * 60_000,
        source: `coinglass:model${model}-${mode}:${hasSession ? "session" : "anonymous"}`,
        updatedAt: m.updateTime,
      });
    } catch (e) {
      // 429 / upstream error graceful degradation: fall back to rolling liquidation heatmap
      try {
        const rolling = await buildRollingStopModel(sym);
        if (rolling) {
          return NextResponse.json({
            ok: true,
            sym,
            symbol: sym,
            reference: `multi-venue:${sym}`,
            window: { interval: "15m", bars: rolling.candles.length, from: rolling.candles[0]?.ts, to: rolling.lastTs },
            instrument: `rolling aggregate (${rolling.source})`,
            model: rolling,
            dataAt: rolling.updateTime,
            ageMs: Date.now() - rolling.updateTime,
            stale: true,
            source: `liq-snapshot-rolling:${rolling.source}:cooldown-fallback`,
            updatedAt: rolling.updateTime,
          });
        }
      } catch {
        /* proceed to standard error handling */
      }
      // cgGet's decryptCgResponse throws plain Errors ("upstream code X: …")
      // before this layer sees the payload — map those codes the same way.
      const upstreamCode = e instanceof Error ? /^upstream code (\d+)/.exec(e.message)?.[1] : undefined;
      if (e instanceof HeatmapUpstreamError || upstreamCode) {
        const code = e instanceof HeatmapUpstreamError ? e.code : (upstreamCode as string);
        const status = code === "40003" ? 402 : 502;
        const hint =
          code === "40000"
            ? hasSession
              ? "session token rejected — update CG_SESSION (node scripts/cg-session.mjs)"
              : "this symbol/range needs a Coinglass login — set CG_SESSION in .env (see scripts/cg-session.mjs)"
            : code === "40003"
              ? "this range (180d/1y/2y) needs a paid Coinglass grade"
              : code === "50001"
                ? "upstream rate-limited, retry shortly"
                : "upstream returned no heatmap data";
        return NextResponse.json(
          { ok: false, error: `liqHeatMap ${code}: ${hint}`, code },
          { status }
        );
      }
      return NextResponse.json(
        { ok: false, error: e instanceof Error ? e.message : "fetch failed" },
        { status: 502 }
      );
    }
  }

  // ── LEGACY: plain ?sym=… form (BTC native, others rolling) ──
  // The anonymous /api/index/v2/liqHeatMap endpoint serves BTC only — every
  // other symbol returns upstream code 40000 (live-verified for ETH/SOL/XRP/
  // XAU/XAG across the v1/v2/v3/v5/v6 and aggregate variants). The other
  // curated perps get the honest LIQ-SNAPSHOT ROLLING HEATMAP (archived +
  // live multi-venue liquidation levels — see liq-heatmap.ts), clearly
  // badged, never a dead 400. With CG_SESSION set the caller can instead
  // use the full model/mode/range surface above for ANY symbol.
  if (sym !== "BTC") {
    if (!isCuratedPerp(sym)) {
      return NextResponse.json({
        ok: true,
        supported: false,
        sym,
        reason: "Liquidation heatmaps require perp leverage tiers (unavailable for classic cash/CFD instruments)",
        dataAt: Date.now(),
        ageMs: 0,
        stale: false,
        source: "stopmap-gate",
      });
    }
    try {
      const rolling = await buildRollingStopModel(sym);
      if (!rolling) {
        return NextResponse.json({
          ok: true,
          supported: false,
          sym,
          reason:
            lastRollingNullReason ||
            "no liquidation-level snapshots archived yet for this perp — the engine rotates the full universe every ~45 min; retry after the next pass",
          dataAt: Date.now(),
          ageMs: 0,
          stale: false,
          source: "liq-archive",
        });
      }
      return NextResponse.json({
        ok: true,
        sym,
        symbol: sym,
        reference: `multi-venue:${sym}`,
        window: { interval: "15m", bars: rolling.candles.length, from: rolling.candles[0]?.ts, to: rolling.lastTs },
        instrument: `rolling aggregate (${rolling.source})`,
        model: rolling,
        dataAt: rolling.updateTime,
        ageMs: Date.now() - rolling.updateTime,
        stale: Date.now() - rolling.updateTime > 20 * 60_000,
        source: `liq-snapshot-rolling:${rolling.source}`,
        updatedAt: rolling.updateTime,
      });
    } catch (e) {
      return NextResponse.json(
        { ok: false, error: e instanceof Error ? e.message : "rolling heatmap failed" },
        { status: 502 }
      );
    }
  }
  const REF_SYMBOL = `Binance_${sym}USDT`;
  const endTimeRaw = Number(sp.get("endTime") ?? 0);
  if (Number.isFinite(endTimeRaw) && endTimeRaw !== 0 && !(endTimeRaw > 0)) {
    return NextResponse.json({ ok: false, error: "endTime must be a positive unix-seconds value" }, { status: 400 });
  }
  // anonymous heatmap history reaches back ~30 days — reject anything else
  // (also keeps the cache key from being used as a memory-growth primitive)
  const MAX_LOOKBACK_S = 30 * 86_400;
  let endTime: number | undefined;
  if (Number.isFinite(endTimeRaw) && endTimeRaw > 0) {
    const nowS = Math.floor(Date.now() / 1000);
    if (endTimeRaw > nowS + 300 || endTimeRaw < nowS - MAX_LOOKBACK_S) {
      return NextResponse.json({ ok: false, error: `endTime out of range (max ${MAX_LOOKBACK_S / 86_400} days back)` }, { status: 400 });
    }
    endTime = Math.floor(endTimeRaw);
  }

  try {
    const payload = (await loadStopmap(sym, endTime)) as LiqHeatmapPayload & { code?: string; msg?: string };
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.liq)) {
      const code = (payload as { code?: string })?.code;
      const hint =
        code === "40000"
          ? "this window needs a Coinglass login (anonymous = 24h view only)"
          : code === "50001"
            ? "upstream rate-limited, retry shortly"
            : "upstream returned no heatmap data";
      return NextResponse.json({ ok: false, error: `liqHeatMap ${code ?? "empty"}: ${hint}` }, { status: 502 });
    }

    const model: StopModel | null = buildStopModel(payload);
    if (!model) {
      return NextResponse.json({ ok: false, error: "no usable heatmap window" }, { status: 502 });
    }

    return NextResponse.json({
      ok: true,
      sym,
      symbol: sym,
      reference: REF_SYMBOL,
      window: { interval: "5m", bars: model.candles.length, from: model.candles[0]?.ts, to: model.lastTs },
      instrument: payload.instrument?.exName
        ? `${payload.instrument.exName} ${payload.instrument.instrumentId}`
        : REF_SYMBOL,
      model,
      dataAt: model.updateTime,
      ageMs: Date.now() - model.updateTime,
      stale: Date.now() - model.updateTime > 60_000,
      source: "coinglass:native-heatmap",
      updatedAt: Date.now(),
    });
  } catch (e) {
    try {
      const rolling = await buildRollingStopModel(sym);
      if (rolling) {
        return NextResponse.json({
          ok: true,
          sym,
          symbol: sym,
          reference: REF_SYMBOL,
          window: { interval: "15m", bars: rolling.candles.length, from: rolling.candles[0]?.ts, to: rolling.lastTs },
          instrument: `rolling aggregate (${rolling.source})`,
          model: rolling,
          dataAt: rolling.updateTime,
          ageMs: Date.now() - rolling.updateTime,
          stale: true,
          source: `liq-snapshot-rolling:${rolling.source}:cooldown-fallback`,
          updatedAt: rolling.updateTime,
        });
      }
    } catch {
      /* proceed to error response */
    }
    return NextResponse.json(
      { ok: false, error: e instanceof Error ? e.message : "fetch failed" },
      { status: 502 }
    );
  }
}
