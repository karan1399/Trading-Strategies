import { NextRequest, NextResponse } from "next/server";
import { cgGet, FAPI } from "@/lib/cg/client";
import { anyTerminalSym, knownSym } from "@/lib/cg/sym-gate";
import { hlCoinOf, resolveCgSym } from "@/lib/binance/aliases";
import { microMapOf } from "@/lib/cg/micro";
import { fetchHlCtxAndBook, fetchHlRecentTrades } from "@/lib/cg/fallbacks";
import { BoundedMap } from "@/lib/cg/bounded";
import type { LtoGroup, LtoSupport } from "@/lib/cg/types";

export const dynamic = "force-dynamic";

const cache = new BoundedMap<{ at: number; body: unknown }>(32);
const lastGood = new BoundedMap<{ at: number; support: LtoSupport | null; perExchange: Record<string, { market: string; instruments: number; openOrders: number; hasData: boolean }>; groups: LtoGroup[] }>(32);
const LAST_GOOD_MAX_MS = 30 * 60_000;

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const minVolUsd = Math.max(0, Number(sp.get("minVolUsd") ?? 100_000) || 0);
  const symbol = (sp.get("symbol") ?? "BTC").trim().toUpperCase();
  if (!(await anyTerminalSym(symbol))) {
    return NextResponse.json({ ok: false, error: `unknown asset ${symbol}` }, { status: 400 });
  }

  const cacheKey = `${symbol}|${minVolUsd}`;
  const hit = cache.get(cacheKey);
  if (hit && Date.now() - hit.at < 60_000) {
    return NextResponse.json(hit.body);
  }

  const cgSym = resolveCgSym(symbol);
  const hasPerp = await knownSym(symbol);

  try {
    let support: LtoSupport | null = null;
    let groups: LtoGroup[] = [];
    const seen = new Set<string>();
    let perExchange: Record<
      string,
      { market: string; instruments: number; openOrders: number; hasData: boolean }
    > = {};
    let cgError: string | null = null;
    let stale = false;
    let updatedAt = Date.now();

    if (hasPerp) {
      try {
        support = (await cgGet(
          `/api/largeTakerOrder/support/v3?symbol=${encodeURIComponent(cgSym)}`,
          10 * 60_000,
          FAPI
        )) as LtoSupport | null;

        const sections = [
          ...(support?.futures ?? []).map((s) => ({ ...s, market: "futures" as const })),
          ...(support?.spot ?? []).map((s) => ({ ...s, market: "spot" as const })),
        ];

        const byExchange = new Map<string, { market: string; instruments: number }>();
        for (const s of sections) {
          const prev = byExchange.get(s.exchange);
          byExchange.set(s.exchange, {
            market: prev ? "all" : s.market,
            instruments: Math.max(prev?.instruments ?? 0, s.instruments?.length ?? 0),
          });
        }
        const exchanges = [...byExchange.entries()];

        const settled = await Promise.allSettled(
          exchanges.map(([ex]) =>
            cgGet(
              `/api/largeTakerOrder/v3?exchange=${encodeURIComponent(ex)}&symbol=${encodeURIComponent(
                cgSym
              )}&market=all&limit=3000&minVolUsd=${minVolUsd}`,
              30_000,
              FAPI
            )
          )
        );

        settled.forEach((r, i) => {
          const [ex, meta] = exchanges[i];
          const entry = (perExchange[ex] = {
            market: meta.market,
            instruments: meta.instruments,
            openOrders: 0,
            hasData: false as boolean,
          });
          if (r.status !== "fulfilled" || !Array.isArray(r.value)) return;
          for (const g of r.value as LtoGroup[]) {
            const inst = g.instrument ?? {};
            const key = `${inst.exName}|${inst.instrumentId}|${inst.type}`;
            if (seen.has(key)) continue;
            const list = (g.list ?? []).filter(
              (it) => it && typeof it.price === "number" && it.currentUsd >= minVolUsd
            );
            if (!list.length) continue;
            seen.add(key);
            entry.openOrders += list.length;
            entry.hasData = true;
            groups.push({ instrument: inst, list });
          }
        });
      } catch (e) {
        cgError = e instanceof Error ? e.message : "Coinglass LTO unavailable";
      }

      if (groups.length > 0) {
        lastGood.set(cacheKey, { at: Date.now(), support, perExchange, groups });
      } else {
        const lg = lastGood.get(cacheKey);
        if (lg && Date.now() - lg.at <= LAST_GOOD_MAX_MS) {
          support = lg.support;
          perExchange = lg.perExchange;
          groups = lg.groups;
          stale = true;
          updatedAt = lg.at;
        }
      }
    }

    // Hyperliquid L2 resting levels + recent trades (main-dex + xyz)
    let hlFallback: {
      coin: string;
      bids: { price: number; usd: number; sz: number }[];
      asks: { price: number; usd: number; sz: number }[];
      recentTrades: { time: number; px: number; sz: number; usd: number; side: "buy" | "sell" }[];
    } | null = null;
    const hlCoin = hlCoinOf(symbol) ?? microMapOf(symbol)?.hl?.coin;
    if (hlCoin) {
      const [ctxBook, trades] = await Promise.all([
        fetchHlCtxAndBook(symbol).catch(() => null),
        fetchHlRecentTrades(hlCoin).catch(() => []),
      ]);
      if (ctxBook?.book || trades.length) {
        hlFallback = {
          coin: hlCoin,
          bids: (ctxBook?.book?.bids ?? [])
            .map((b) => ({ price: b.px, sz: b.sz, usd: Math.round(b.px * b.sz) }))
            .filter((b) => b.usd >= Math.min(minVolUsd, 10_000))
            .slice(0, 15),
          asks: (ctxBook?.book?.asks ?? [])
            .map((a) => ({ price: a.px, sz: a.sz, usd: Math.round(a.px * a.sz) }))
            .filter((a) => a.usd >= Math.min(minVolUsd, 10_000))
            .slice(0, 15),
          recentTrades: trades.slice(0, 40),
        };
      }
    }

    const body = {
      ok: true,
      supported: hasPerp || !!hlFallback,
      symbol,
      minVolUsd,
      support: { spot: support?.spot ?? [], futures: support?.futures ?? [] },
      perExchange,
      groups,
      hlFallback,
      ...(stale ? { stale: true } : {}),
      ...(cgError ? { reason: cgError } : {}),
      updatedAt,
    };
    if (groups.length > 0 || !cgError) {
      cache.set(cacheKey, { at: Date.now(), body });
    }
    return NextResponse.json(body);
  } catch (e) {
    return NextResponse.json(
      { ok: false, error: e instanceof Error ? e.message : "fetch failed" },
      { status: 502 }
    );
  }
}
