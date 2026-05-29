"use client";

import { useReadContracts } from "wagmi";
import { VAULT } from "@/lib/data";
import { VUSDC_ABI, VUSDC_ADDRESS, VUSDC_CHAIN_ID, isVUsdcConfigured } from "@/lib/contracts";

const EXCHANGE_RATE_SCALE = 1_000_000_000_000_000_000n; // 1e18
const USDC_SCALE = 1_000_000n; // 1e6 — vUSDC + USDC raw units
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

function deriveDaysLive(): number {
  const inception = Date.parse(VAULT.inception);
  if (Number.isNaN(inception)) return VAULT.daysLive;
  return Math.max(0, Math.floor((Date.now() - inception) / MS_PER_DAY));
}

export function useVaultStats(): VaultStats {
  const daysLive = deriveDaysLive();

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
      enabled: isVUsdcConfigured,
      refetchInterval: REFETCH_INTERVAL_MS,
      refetchOnWindowFocus: true,
    },
  });

  if (!isVUsdcConfigured) {
    return {
      tvlUsdc: undefined,
      exchangeRate: undefined,
      cumReturnPct: undefined,
      daysLive,
      isLoading: false,
      isError: false,
      isLive: false,
    };
  }

  const [supplyResult, rateResult] = query.data ?? [];
  const totalSupplyRaw = supplyResult?.status === "success" ? (supplyResult.result as bigint) : undefined;
  const exchangeRateRaw = rateResult?.status === "success" ? (rateResult.result as bigint) : undefined;

  let tvlUsdc: number | undefined;
  let exchangeRate: number | undefined;
  let cumReturnPct: number | undefined;

  if (totalSupplyRaw !== undefined && exchangeRateRaw !== undefined) {
    // totalSupply: 1e6 units (vUSDC has 6 decimals)
    // exchangeRate: 1e18-scaled USDC-per-vUSDC
    // TVL in raw USDC = totalSupply * exchangeRate / 1e18 (still 1e6 units) → divide by 1e6 for dollars
    const tvlRawUsdc = (totalSupplyRaw * exchangeRateRaw) / EXCHANGE_RATE_SCALE;
    tvlUsdc = Number(tvlRawUsdc) / Number(USDC_SCALE);
    exchangeRate = Number(exchangeRateRaw) / Number(EXCHANGE_RATE_SCALE);
    cumReturnPct = (exchangeRate - 1) * 100;
  }

  return {
    tvlUsdc,
    exchangeRate,
    cumReturnPct,
    daysLive,
    isLoading: query.isLoading,
    isError: query.isError,
    isLive: tvlUsdc !== undefined,
  };
}
