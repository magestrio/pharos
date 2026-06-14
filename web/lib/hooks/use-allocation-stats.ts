"use client";

import { useMemo } from "react";
import { type Allocation } from "@/lib/data";
import { useCycleDetail, useCycles, usePortfolio } from "@/lib/agent-store-context";
import type { PositionRow } from "@/lib/agent-api";
import { useIsMounted } from "@/lib/hooks/use-is-mounted";

const REFETCH_INTERVAL_MS = 30_000;

export interface AllocationRow extends Allocation {
  valueUsdc: number;
}

export interface AllocationStats {
  rows: AllocationRow[];
  totalUsdc: number;
  isLoading: boolean;
  isError: boolean;
  isLive: boolean;
  // "planned" = derived from latest decision's venues (what the agent
  // WANTS to hold). "actual" = real positions from portfolio snapshot
  // (what the agent IS holding right now). They diverge whenever the
  // executor hasn't filled the diff yet (dry-run, partial-fill, between
  // cycles). The donut shows planned by default; UI surfaces the mode.
  source: "planned" | "actual" | "empty";
  cycleTs?: string;
}

// Venue metadata - one row per real venue. Bybit Attestor used to be a
// single bucket; now every sub-venue (Flex, LM, OnChain, DiscountBuy,
// DualAsset, Hold-to-Earn, perp) renders independently. Cash leg
// included so the donut can hold it when there's signal.
const VENUE_META: Record<
  string,
  { label: string; sub: string; color: string }
> = {
  bybit_flex: {
    label: "Bybit Flexible Earn",
    sub: "off-chain · 200+ stable products",
    color: "#A78BFA",
  },
  bybit_onchain: {
    label: "Bybit OnChain Earn",
    sub: "off-chain · staked yield (TON, ATOM, USDE, ...)",
    color: "#C4B5FD",
  },
  bybit_lm: {
    label: "Bybit Liquidity Mining",
    sub: "off-chain · CPMM LP (BTC/USDC, ETH/USDC, ...)",
    color: "#A6BEFC",
  },
  bybit_discount_buy: {
    label: "Bybit DiscountBuy",
    sub: "off-chain · range payoff (BTC/ETH 1-14d)",
    color: "#9C7BFB",
  },
  bybit_dual_asset: {
    label: "Bybit DualAsset",
    sub: "off-chain · either-side delivery",
    color: "#B488FB",
  },
  bybit_hold_to_earn: {
    label: "Bybit Hold-to-Earn",
    sub: "off-chain · USDE / USDTB / USD1",
    color: "#7BCBFB",
  },
  bybit_alpha: {
    label: "Bybit Alpha Farm",
    sub: "off-chain · directional DEX exposure",
    color: "#FBBF24",
  },
  perp: {
    label: "Bybit USDT-Perp",
    sub: "off-chain · delta-neutral hedge leg",
    color: "#7C5CD6",
  },
  cash_usdc: {
    label: "Cash USDC",
    sub: "idle · liquidity buffer",
    color: "#3F4860",
  },
};

const FALLBACK_COLORS = [
  "#A78BFA",
  "#C4B5FD",
  "#A6BEFC",
  "#9C7BFB",
  "#B488FB",
  "#7BCBFB",
  "#FBBF24",
  "#7C5CD6",
];

function venueMeta(venue: string, idx: number): { label: string; sub: string; color: string } {
  const known = VENUE_META[venue];
  if (known) return known;
  // Unknown venue - derive a label from the snake_case key.
  const pretty = venue
    .split("_")
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
  return {
    label: pretty,
    sub: "off-chain",
    color: FALLBACK_COLORS[idx % FALLBACK_COLORS.length],
  };
}

function n(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function parseTotalEquityUsd(
  wallet: Record<string, unknown> | null | undefined,
): number | null {
  if (!wallet || typeof wallet !== "object") return null;
  const raw =
    (wallet as { total_equity_usd?: unknown }).total_equity_usd ??
    (wallet as { totalEquity?: unknown }).totalEquity;
  if (raw === undefined || raw === null) return null;
  const parsed = parseFloat(String(raw));
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Build allocation rows from the latest decision's `venues[]`. This is
 * the TARGET state the agent picked this cycle - what we want to show
 * on the dashboard so the donut reflects the agent's intent, not the
 * executor's lag. Each venue becomes one row sized at `venue.weight ×
 * totalEquityUsd`; if the venue lists multiple picks, we keep the
 * venue as a single row (sub-picks render in the Decision panel).
 */
function buildFromDecision(
  decision: Record<string, unknown> | null | undefined,
  totalEquityUsd: number | null,
  cycleTs: string,
): AllocationStats | null {
  if (!decision || typeof decision !== "object") return null;
  const venues = (decision as { venues?: unknown }).venues;
  if (!Array.isArray(venues) || venues.length === 0) return null;

  const total = totalEquityUsd ?? 0;
  const rows: AllocationRow[] = [];
  let weightSum = 0;
  venues.forEach((v, idx) => {
    if (!v || typeof v !== "object") return;
    const venueId = String((v as { venue_id?: unknown }).venue_id ?? "");
    const weight = Number((v as { weight?: unknown }).weight ?? 0);
    if (!venueId || !Number.isFinite(weight) || weight <= 0) return;
    weightSum += weight;
    const meta = venueMeta(venueId, idx);
    const valueUsdc = total * weight;
    rows.push({
      key: venueId,
      label: meta.label,
      sub: meta.sub,
      color: meta.color,
      pct: Math.round(weight * 1000) / 10,
      apy: 0,
      notional: Math.round(valueUsdc),
      valueUsdc,
    });
  });

  if (rows.length === 0) return null;

  rows.sort((a, b) => b.valueUsdc - a.valueUsdc);

  return {
    rows,
    totalUsdc: total,
    isLoading: false,
    isError: false,
    isLive: true,
    source: "planned",
    cycleTs,
  };
}

/**
 * Build allocation rows from the latest snapshot's positions. Each
 * unique `venue` becomes one row; cash is whatever total equity
 * isn't explained by positions. Zero/negligible rows are dropped so
 * the donut isn't cluttered with venues the agent isn't actually
 * holding this cycle.
 */
function buildFromPositions(
  positions: PositionRow[],
  totalEquityUsd: number | null,
): AllocationStats {
  // Sum USD by full venue id (do NOT collapse all bybit_* into BYBIT).
  const valueByVenue = new Map<string, number>();
  for (const p of positions) {
    const usd = n(p.amount_usd);
    if (usd <= 0) continue;
    valueByVenue.set(p.venue, (valueByVenue.get(p.venue) ?? 0) + usd);
  }
  const positioned = Array.from(valueByVenue.values()).reduce((s, v) => s + v, 0);
  const total = totalEquityUsd ?? positioned;
  const cash = Math.max(0, total - positioned);
  if (cash > 0) {
    valueByVenue.set("cash_usdc", (valueByVenue.get("cash_usdc") ?? 0) + cash);
  }

  if (total <= 0) {
    return {
      rows: [],
      totalUsdc: 0,
      isLoading: false,
      isError: false,
      isLive: false,
      source: "empty",
    };
  }

  // Sort by USD desc so the biggest position is on top.
  const sorted = Array.from(valueByVenue.entries()).sort((a, b) => b[1] - a[1]);

  const rows: AllocationRow[] = sorted.map(([venue, valueUsdc], idx) => {
    const meta = venueMeta(venue, idx);
    const pct = Math.round((valueUsdc / total) * 1000) / 10;
    return {
      key: venue,
      label: meta.label,
      sub: meta.sub,
      color: meta.color,
      pct,
      apy: 0, // no real-time APY here yet - left as 0; the donut hides it.
      notional: Math.round(valueUsdc),
      valueUsdc,
    };
  });

  // Drop near-zero rows (<0.05% of total) so they don't pollute the
  // donut / table when the agent is fully in another venue.
  const filtered = rows.filter((r) => r.pct >= 0.05);

  return {
    rows: filtered,
    totalUsdc: total,
    isLoading: false,
    isError: false,
    isLive: positions.length > 0,
    source: positions.length > 0 ? "actual" : "empty",
  };
}

export function useAllocationStats(): AllocationStats {
  const mounted = useIsMounted();
  const portfolioQuery = usePortfolio();
  return useMemo<AllocationStats>(() => {
    if (!mounted || (portfolioQuery.isLoading && !portfolioQuery.data)) {
      return {
        rows: [],
        totalUsdc: 0,
        isLoading: !mounted ? false : true,
        isError: false,
        isLive: false,
        source: "empty",
      };
    }
    // Single source of truth = `/portfolio/current` (positions from
    // the latest snapshot of the SAFE-controlled Bybit account). The
    // decision is what Claude wants; this is what's actually held.
    // Planned-vs-actual divergence is surfaced separately via
    // `usePlannedVsActual` so the donut never lies about reality.
    const positions = portfolioQuery.data?.positions ?? [];
    const totalEquityUsd = parseTotalEquityUsd(portfolioQuery.data?.wallet);
    return buildFromPositions(positions, totalEquityUsd);
  }, [mounted, portfolioQuery.data, portfolioQuery.isLoading]);
}

// ─── planned-vs-actual divergence (separate hook, separate panel) ───

export interface PlannedVsActualRow {
  venue: string;
  label: string;
  plannedPct: number;
  actualPct: number;
  // Positive = under-allocated vs plan, negative = over-allocated.
  diffPct: number;
}

export interface PlannedVsActual {
  rows: PlannedVsActualRow[];
  cycleTs: string | null;
  actionsPlanned: number | null;
  actionsExecuted: number | null;
  hasGap: boolean;
}

export function usePlannedVsActual(): PlannedVsActual {
  const mounted = useIsMounted();
  const portfolioQuery = usePortfolio();
  const cyclesQuery = useCycles({ limit: 1 });
  const latestCycle = cyclesQuery.data?.[0] ?? null;
  const detailQuery = useCycleDetail(latestCycle?.cycle_ts ?? null);

  return useMemo<PlannedVsActual>(() => {
    const venues = (detailQuery.data?.decision as
      | { venues?: Array<{ venue_id?: string; weight?: number }> }
      | undefined)?.venues;
    if (!mounted || !latestCycle || !Array.isArray(venues) || venues.length === 0) {
      return {
        rows: [],
        cycleTs: latestCycle?.cycle_ts ?? null,
        actionsPlanned: latestCycle?.actions_planned ?? null,
        actionsExecuted: latestCycle?.actions_executed ?? null,
        hasGap: false,
      };
    }
    const positions = portfolioQuery.data?.positions ?? [];
    const totalEquityUsd = parseTotalEquityUsd(portfolioQuery.data?.wallet);

    // Sum actual USD per venue from positions.
    const actualByVenue = new Map<string, number>();
    let positioned = 0;
    for (const p of positions) {
      const usd = n(p.amount_usd);
      if (usd <= 0) continue;
      actualByVenue.set(p.venue, (actualByVenue.get(p.venue) ?? 0) + usd);
      positioned += usd;
    }
    const total = totalEquityUsd ?? positioned;
    const cashActual = Math.max(0, total - positioned);
    if (cashActual > 0) {
      actualByVenue.set(
        "cash_usdc",
        (actualByVenue.get("cash_usdc") ?? 0) + cashActual,
      );
    }
    const actualTotal = Array.from(actualByVenue.values()).reduce(
      (s, v) => s + v,
      0,
    );

    // Build rows: every venue that appears in either side gets one.
    const keys = new Set<string>();
    venues.forEach((v) => v.venue_id && keys.add(v.venue_id));
    actualByVenue.forEach((_, k) => keys.add(k));

    const rows: PlannedVsActualRow[] = [];
    keys.forEach((venue) => {
      const planned = venues.find((v) => v.venue_id === venue);
      const plannedPct = planned ? (planned.weight ?? 0) * 100 : 0;
      const actualUsd = actualByVenue.get(venue) ?? 0;
      const actualPct = actualTotal > 0 ? (actualUsd / actualTotal) * 100 : 0;
      // Round to 1 decimal and drop rows with no meaningful presence.
      const plannedR = Math.round(plannedPct * 10) / 10;
      const actualR = Math.round(actualPct * 10) / 10;
      if (plannedR === 0 && actualR === 0) return;
      const meta = venueMeta(venue, 0);
      rows.push({
        venue,
        label: meta.label,
        plannedPct: plannedR,
        actualPct: actualR,
        diffPct: Math.round((plannedR - actualR) * 10) / 10,
      });
    });
    rows.sort((a, b) => Math.abs(b.diffPct) - Math.abs(a.diffPct));
    const hasGap = rows.some((r) => Math.abs(r.diffPct) >= 1.0);

    return {
      rows,
      cycleTs: latestCycle.cycle_ts,
      actionsPlanned: latestCycle.actions_planned,
      actionsExecuted: latestCycle.actions_executed,
      hasGap,
    };
  }, [
    mounted,
    latestCycle,
    detailQuery.data,
    portfolioQuery.data,
  ]);
}
