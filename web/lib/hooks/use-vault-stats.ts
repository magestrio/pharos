"use client";

import { useReadContracts } from "wagmi";
import { useCycles, usePortfolio } from "@/lib/agent-store-context";
import { VUSDC_ABI, VUSDC_ADDRESS, VUSDC_CHAIN_ID, isVUsdcConfigured } from "@/lib/contracts";
import { useIsMounted } from "@/lib/hooks/use-is-mounted";

const SSR_PLACEHOLDER_STATS: VaultStats = {
  tvlUsdc: undefined,
  exchangeRate: undefined,
  cumReturnPct: undefined,
  daysLive: 0,
  isLoading: false,
  isError: false,
  isLive: false,
};

const EXCHANGE_RATE_SCALE = 1_000_000_000_000_000_000n; // 1e18
const USDC_SCALE = 1_000_000n; // 1e6 - vUSDC + USDC raw units
const MS_PER_DAY = 86_400_000;
const REFETCH_INTERVAL_MS = 30_000;

export interface VaultStats {
  tvlUsdc: number | undefined;
  exchangeRate: number | undefined;
  cumReturnPct: number | undefined;
  daysLive: number;
  isLoading: boolean;
  isError: boolean;
  isLive: boolean;
}

function deriveDaysLiveFromCycles(firstCycleStartedAt: string | undefined): number | null {
  if (!firstCycleStartedAt) return null;
  const t = Date.parse(firstCycleStartedAt);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / MS_PER_DAY));
}

function parseTotalEquityUsd(wallet: Record<string, unknown> | null | undefined): number | null {
  if (!wallet || typeof wallet !== "object") return null;
  const raw =
    (wallet as { total_equity_usd?: unknown }).total_equity_usd ??
    (wallet as { totalEquity?: unknown }).totalEquity;
  if (raw === undefined || raw === null) return null;
  const n = parseFloat(String(raw));
  return Number.isFinite(n) ? n : null;
}

export function useVaultStats(): VaultStats {
  // SSR/first-render gate. `Date.now()` and wagmi data both diverge
  // server-vs-client; freeze to a placeholder for the first paint.
  const mounted = useIsMounted();

  const portfolioQuery = usePortfolio();
  const cyclesQuery = useCycles({ limit: 50 });

  // Off-chain real values from agent API. Used when the on-chain vUSDC
  // contract isn't deployed (Phase B deferred) so the dashboard still
  // shows the actual ~$50 sandbox vault equity instead of a mock.
  const offchainTvlUsd = parseTotalEquityUsd(portfolioQuery.data?.wallet);
  const cycles = cyclesQuery.data ?? [];
  const firstCycleStartedAt =
    cycles.length > 0 ? cycles[cycles.length - 1].started_at : undefined;
  // No cycles yet → 0 days live. We do not invent an inception date.
  const daysLive = deriveDaysLiveFromCycles(firstCycleStartedAt) ?? 0;

  const query = useReadContracts({
    allowFailure: true,
    contracts: [
      {
        address: VUSDC_ADDRESS,
        abi: VUSDC_ABI,
        chainId: VUSDC_CHAIN_ID,
        functionName: "totalSupply",
      },
      {
        address: VUSDC_ADDRESS,
        abi: VUSDC_ABI,
        chainId: VUSDC_CHAIN_ID,
        functionName: "exchangeRate",
      },
    ],
    query: {
      enabled: mounted && isVUsdcConfigured,
      refetchInterval: REFETCH_INTERVAL_MS,
      refetchOnWindowFocus: true,
    },
  });

  if (!mounted) {
    return SSR_PLACEHOLDER_STATS;
  }

  // No on-chain contract yet: surface the live off-chain numbers we
  // actually have, leave exchangeRate/cumReturnPct undefined (their
  // consumers render placeholders).
  if (!isVUsdcConfigured) {
    return {
      tvlUsdc: offchainTvlUsd ?? undefined,
      exchangeRate: undefined,
      cumReturnPct: undefined,
      daysLive,
      isLoading: portfolioQuery.isLoading,
      isError: portfolioQuery.isError,
      // "Live" means we have real numbers to show - off-chain equity counts.
      isLive: offchainTvlUsd !== null,
    };
  }

  const [supplyResult, rateResult] = query.data ?? [];
  const totalSupplyRaw = supplyResult?.status === "success" ? (supplyResult.result as bigint) : undefined;
  const exchangeRateRaw = rateResult?.status === "success" ? (rateResult.result as bigint) : undefined;

  let tvlUsdc: number | undefined;
  let exchangeRate: number | undefined;
  let cumReturnPct: number | undefined;

  if (totalSupplyRaw !== undefined && exchangeRateRaw !== undefined) {
    const tvlRawUsdc = (totalSupplyRaw * exchangeRateRaw) / EXCHANGE_RATE_SCALE;
    tvlUsdc = Number(tvlRawUsdc) / Number(USDC_SCALE);
    exchangeRate = Number(exchangeRateRaw) / Number(EXCHANGE_RATE_SCALE);
    cumReturnPct = (exchangeRate - 1) * 100;
  }

  return {
    tvlUsdc: tvlUsdc ?? offchainTvlUsd ?? undefined,
    exchangeRate,
    cumReturnPct,
    daysLive,
    isLoading: query.isLoading,
    isError: query.isError,
    isLive: tvlUsdc !== undefined || offchainTvlUsd !== null,
  };
}
