"use client";

import { useQuery } from "@tanstack/react-query";

const POLL_INTERVAL_MS = 30_000;

export type LiveSnapshot = {
  captured_at: string;
  schema_version: number;
  wallet: {
    total_equity_usd: string;
    accounts: Array<{ accountType: string; totalEquity: string; valuationCurrency: string }>;
  };
  market: {
    btc_price: string;
    btc_24h_change_pct: string;
    btc_funding_rate: string;
    eth_price: string;
    eth_24h_change_pct: string;
    eth_funding_rate: string;
  };
  perp_market: Record<
    string,
    { symbol: string; funding_rate_8h: string; mark_price: string; orderbook_depth_50bps_usd: string }
  >;
  usdc_peg: { price_usd: string; deviation_bps: string; source: string; fetched_at: string };
  earn_positions_count: number;
  product_counts: Record<string, number>;
  errors: string[];
};

export type LiveDecision = {
  thesis: string;
  venues: Array<{
    venue_id: string;
    weight: number;
    picks: Array<{ product_id: string; weight: number; notes: string[] }>;
  }>;
  hedges: unknown[];
  confidence: number;
  risk_flags: string[];
  notes: string[];
};

export type LiveProduct = {
  category: string;
  product_id: string;
  coin: string;
  effective_apr: number;
  apr_source: string;
  base_apr_string: string | null;
  redeem_lockup_minutes: number | null;
  notes: string[];
};

export type LiveBybitEarn = {
  captured_at: string;
  category: string | null;
  coin: string | null;
  limit: number;
  products: Record<string, LiveProduct[]>;
};

export type LivePortfolio = {
  captured_at: string;
  total_equity_usd: number;
  accounts: Array<{
    accountType: string;
    totalEquity: number;
    valuationCurrency: string;
    holdings: Array<{ coin: string; equity: number; category?: string }>;
  }>;
  active_earn_positions: Array<{
    productId: string;
    coin: string;
    category: string;
    amount: number;
    totalPnl: number;
    claimableYield: number;
    availableAmount: number;
  }>;
};

export type EarnProductRow = {
  category: string;
  product_id: string;
  coin: string;
  effective_apr_pct: number;
  apr_source: string;
  apr_history_pct: number[] | null;
  funding_rate: number | null;
  funding_annual_pct: number | null;
  mark_price: number | null;
  // Coin-quality metrics (computed server-side; null in the dev file-fallback).
  is_stable?: boolean;
  avg_apr_7d_pct?: number | null;
  net_apr_pct?: number | null;
  apr_stability?: number | null; // 0..1
  price_volatility_pct?: number | null;
  price_stability?: number | null; // 0..1
  stability_score?: number | null; // 0..100
  funding_7d_annual_pct?: number | null;
  quality_score?: number | null; // 0..100
  // Realized/projected profit on notional by horizon.
  profit_1d?: ProfitHorizon | null;
  profit_7d?: ProfitHorizon | null;
  profit_30d?: ProfitHorizon | null;
};

export type ProfitHorizon = {
  earn_pct: number | null;
  funding_pct: number | null;
  total_pct: number | null;
  basis: "realized" | "projected" | "unavailable";
  note: string | null;
};

export type EarnProducts = {
  captured_at: string | null;
  products: EarnProductRow[];
};

export type FundingHistoryPoint = {
  ts: string;
  funding_rate: number | null;
  funding_annual_pct: number | null;
};

export type FundingHistory = {
  coin: string;
  points: FundingHistoryPoint[];
};

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json() as Promise<T>;
}

export function useSnapshot() {
  return useQuery({
    queryKey: ["snapshot"],
    queryFn: () => getJson<LiveSnapshot>("/api/snapshot"),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  });
}

export function useDecision() {
  return useQuery({
    queryKey: ["decision"],
    queryFn: () => getJson<LiveDecision>("/api/decision"),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  });
}

export function useBybitEarn(params?: { category?: string; coin?: string; limit?: number }) {
  const search = new URLSearchParams();
  if (params?.category) search.set("category", params.category);
  if (params?.coin) search.set("coin", params.coin);
  if (params?.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  const url = qs ? `/api/bybit-earn?${qs}` : "/api/bybit-earn";
  return useQuery({
    queryKey: ["bybit-earn", params ?? {}],
    queryFn: () => getJson<LiveBybitEarn>(url),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  });
}

export function useEarnExplorer(params?: { category?: string; coin?: string; limit?: number }) {
  const search = new URLSearchParams();
  if (params?.category) search.set("category", params.category);
  if (params?.coin) search.set("coin", params.coin);
  if (params?.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  const url = qs ? `/api/earn-products?${qs}` : "/api/earn-products";
  return useQuery({
    queryKey: ["earn-products", params ?? {}],
    queryFn: () => getJson<EarnProducts>(url),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  });
}

export function useFundingHistory(coin: string | null, limit = 60) {
  return useQuery({
    queryKey: ["funding-history", coin, limit],
    queryFn: () =>
      getJson<FundingHistory>(
        `/api/earn-funding-history?coin=${encodeURIComponent(coin ?? "")}&limit=${limit}`,
      ),
    enabled: !!coin,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  });
}

export function usePortfolio() {
  // Keyed under `live-portfolio` (not `portfolio`) to avoid a cache
  // collision with `agent-store-context.usePortfolio`, which hits
  // `/api/store/portfolio` and exposes a different shape (`Portfolio`
  // vs `LivePortfolio`). React Query was deduping the two queries and
  // serving whichever fetched first, which made `live.accounts` and
  // `live.active_earn_positions` undefined inside AllocationSection.
  return useQuery({
    queryKey: ["live-portfolio"],
    queryFn: () => getJson<LivePortfolio>("/api/portfolio"),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  });
}
