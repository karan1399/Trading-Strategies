import { NextRequest, NextResponse } from "next/server";
import { cgGet } from "@/lib/cg/client";
import { BoundedMap } from "@/lib/cg/bounded";
import { parseInterval, instrumentKeyOK } from "@/lib/cg/params";
import { fapiFetch } from "@/lib/live/fapi";
import { hlCoinOf } from "@/lib/binance/aliases";
import { microMapOf } from "@/lib/cg/micro";
import { fetchHlRecentTrades } from "@/lib/cg/fallbacks";
import type { LargeOrderGroup } from "@/lib/cg/types";

export const dynamic = "force-dynamic";

const lastGood = new BoundedMap<{ at: number; groups: LargeOrderGroup[] }>(48);
const LAST_GOOD_MAX_MS = 30 * 60_000;

function extractSymFromKey(symbolKey: string): string {
  const m = symbolKey.match(/^(?:[A-Za-z]+_)?([A-Za-z0-9]+?)(?:USDT|USD|-USDT-SWAP|-USD)?$/i);
  return (m?.[1] ?? symbolKey).toUpperCase();
}

async function fallbackLargeTrades(symbolKey: string): Promise<{ source: string; groups: LargeOrderGroup[] } | null> {
  const sym = extractSymFromKey(symbolKey);
  const binPair = symbolKey.startsWith("Binance_") ? symbolKey.slice("Binance_".length) : `${sym}USDT`;
  try {
    const agg = (await fapiFetch(`/fapi/v1/aggTrades?symbol=${encodeURIComponent(binPair)}&limit=1000`, 4_500)) as
      | { p: string; q: string; T: number; m: boolean }[]
      | null;
    if (Array.isArray(agg) && agg.length) {
      const blocks = agg
        .map((t) => {
          const price = Number(t.p);
          const qty = Number(t.q);
          const usd = price * qty;
          return {
            price,
            quoteVol: Math.round(usd),
            volUsd: Math.round(usd),
            // Coinglass LargeOrder side: 2 = buy / bid, 1 = sell / ask; Binance m=true means buyer was maker (taker sell)
            side: t.m ? 1 : 2,
            turnoverTime: Number(t.T),
            turnoverNumber: 1,
          };
        })
        .filter((x) => Number.isFinite(x.price) && x.quoteVol >= 25_000)
        .sort((a, b) => b.quoteVol - a.quoteVol)
        .slice(0, 60);
      if (blocks.length) {
        return {
          source: "binance-fapi/aggTrades",
          groups: [
            {
              instrument: { exName: "Binance", instrumentId: binPair, baseAsset: sym, contractValCurrency: "USDT" },
              list: blocks as unknown as LargeOrderGroup["list"],
            },
          ],
        };
      }
    }
  } catch {
    /* try HL below */
  }
  const hlCoin = hlCoinOf(sym) ?? microMapOf(sym)?.hl?.coin;
  if (hlCoin) {
    const trades = await fetchHlRecentTrades(hlCoin).catch(() => []);
    const blocks = trades
      .filter((t) => t.usd >= 10_000)
      .map((t) => ({
        price: t.px,
        quoteVol: Math.round(t.usd),
        volUsd: Math.round(t.usd),
        side: t.side === "buy" ? 2 : 1,
        turnoverTime: t.time,
        turnoverNumber: 1,
      }))
      .sort((a, b) => b.quoteVol - a.quoteVol)
      .slice(0, 40);
    if (blocks.length) {
      return {
        source: "hl-recentTrades",
        groups: [
          {
            instrument: { exName: "Hyperliquid", instrumentId: hlCoin, baseAsset: sym, contractValCurrency: "USD" },
            list: blocks as unknown as LargeOrderGroup["list"],
          },
        ],
      };
    }
  }
  return null;
}

/**
 * GET /api/cg/largeorder?hours=24
 * Returns large resting orders and large trades per venue (futures + spot).
 */
export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const hours = Math.min(Math.max(Number(sp.get("hours") ?? 24) || 24, 1), 24 * 30);
  const interval = parseInterval(sp.get("interval"));
  if (!interval) {
    return NextResponse.json({ ok: false, error: "invalid interval" }, { status: 400 });
  }
  const symbol = sp.get("symbol") ?? "Binance_BTCUSDT";
  // instrument-key shape validation (see kline route)
  if (!instrumentKeyOK(symbol)) {
    return NextResponse.json({ ok: false, error: `invalid symbol ${symbol}` }, { status: 400 });
  }
  const end = Math.floor(Date.now() / 1000);
  const start = end - hours * 3600;
  const cacheKey = `${symbol}|${interval}|${hours}`;
  try {
    const groups = (await cgGet(
      `/api/largeOrder?symbol=${encodeURIComponent(symbol)}&interval=${interval}&startTime=${start}&endTime=${end}&minLimit=false`,
      30_000
    )) as LargeOrderGroup[] | null;
    const valid = groups ?? [];
    if (valid.length > 0) {
      lastGood.set(cacheKey, { at: Date.now(), groups: valid });
      return NextResponse.json({ ok: true, supported: true, source: "coinglass", groups: valid, updatedAt: Date.now() });
    }
    const fb = await fallbackLargeTrades(symbol);
    if (fb) {
      return NextResponse.json({
        ok: true,
        supported: true,
        source: fb.source,
        groups: fb.groups,
        note: "Coinglass /api/largeOrder returned empty; showing live exchange large executed blocks",
        updatedAt: Date.now(),
      });
    }
    return NextResponse.json({ ok: true, supported: true, source: "coinglass", groups: valid, updatedAt: Date.now() });
  } catch (e) {
    const reason = e instanceof Error ? e.message : "upstream largeOrder unavailable";
    const lg = lastGood.get(cacheKey);
    if (lg && Date.now() - lg.at <= LAST_GOOD_MAX_MS) {
      return NextResponse.json({
        ok: true,
        supported: true,
        stale: true,
        source: "coinglass-stale",
        reason,
        groups: lg.groups,
        updatedAt: lg.at,
      });
    }
    const fb = await fallbackLargeTrades(symbol);
    if (fb) {
      return NextResponse.json({
        ok: true,
        supported: true,
        source: fb.source,
        reason,
        groups: fb.groups,
        note: "Coinglass /api/largeOrder rate-limited; showing live exchange large executed blocks",
        updatedAt: Date.now(),
      });
    }
    return NextResponse.json({
      ok: true,
      supported: false,
      groups: [],
      reason,
      updatedAt: Date.now(),
    });
  }
}
