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

export function usePortfolio() {
  return useQuery({
    queryKey: ["portfolio"],
    queryFn: () => getJson<LivePortfolio>("/api/portfolio"),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS / 2,
  });
}
