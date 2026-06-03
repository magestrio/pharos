"use client";

import { useMemo } from "react";
import { type ActiveHedge } from "@/lib/data";
import { usePortfolio } from "@/lib/agent-store-context";
import type { PositionRow } from "@/lib/agent-api";

export interface ActiveHedgesResult {
  hedges: ActiveHedge[];
  isLive: boolean;
  isLoading: boolean;
}

const VENUE_LABELS: Record<string, string> = {
  bybit_onchain: "Bybit OnChain",
  bybit_flex: "Bybit Flex",
  bybit_lm: "Bybit LM",
  bybit_dual_asset: "Bybit DualAsset",
  bybit_discount_buy: "Bybit DiscountBuy",
  bybit_hold_to_earn: "Bybit Hold-to-Earn",
  perp: "Bybit USDT-Perp",
};

function venueLabel(v: string): string {
  return VENUE_LABELS[v] ?? v;
}

function n(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function pairPositions(positions: PositionRow[]): ActiveHedge[] {
  // Group by coin; the perp side is whichever rows have venue === 'perp'.
  // Multiple earn legs per coin are summed; the perp leg is typically
  // one symbol per coin but we sum just in case.
  const byCoin = new Map<string, { perps: PositionRow[]; earns: PositionRow[] }>();
  for (const p of positions) {
    if (!p.coin) continue;
    let bucket = byCoin.get(p.coin);
    if (!bucket) {
      bucket = { perps: [], earns: [] };
      byCoin.set(p.coin, bucket);
    }
    if (p.venue === "perp") {
      bucket.perps.push(p);
    } else {
      bucket.earns.push(p);
    }
  }

  const hedges: ActiveHedge[] = [];
  for (const [coin, { perps, earns }] of byCoin.entries()) {
    if (perps.length === 0 || earns.length === 0) continue;
    const perpAmt = perps.reduce((s, p) => s + n(p.amount), 0);
    const perpUsd = perps.reduce((s, p) => s + n(p.amount_usd), 0);
    const earnAmt = earns.reduce((s, p) => s + n(p.amount), 0);
    const earnUsd = earns.reduce((s, p) => s + n(p.amount_usd), 0);
    const earnVenues = Array.from(new Set(earns.map((e) => venueLabel(e.venue)))).join(" + ");
    hedges.push({
      key: coin,
      label: `${coin} basis trade`,
      venueSpot: earnVenues ? `${earnVenues} (${coin})` : coin,
      venuePerp: `Bybit USDT-Perp (${coin}-PERP)`,
      spotQty: `${earnAmt.toFixed(2)} ${coin}`,
      spotUsd: Math.round(earnUsd),
      hedgeQty: `${coin}-PERP short`,
      hedgeUsd: Math.round(Math.abs(perpUsd)),
      // perp leg should be negative (short) — earn leg is positive.
      // Delta = earn + perp; <0.01 of either leg counts as neutral.
      netDelta: earnAmt + perpAmt,
      fundingEarned24h: 0,
      spotApr: 0,
      fundingApr: 0,
      blendedApr: 0,
      openedAgo: "—",
      closeTrigger: "—",
    });
  }
  return hedges.sort((a, b) => b.hedgeUsd - a.hedgeUsd);
}

export function useActiveHedges(): ActiveHedgesResult {
  const portfolioQuery = usePortfolio();
  return useMemo<ActiveHedgesResult>(() => {
    const positions = portfolioQuery.data?.positions ?? [];
    const live = pairPositions(positions);
    // No mock fallback — when there are no live hedge pairs (no perp leg
    // matched to an earn leg) the panel renders an empty/honest state.
    return { hedges: live, isLive: live.length > 0, isLoading: portfolioQuery.isLoading };
  }, [portfolioQuery.data, portfolioQuery.isLoading]);
}
