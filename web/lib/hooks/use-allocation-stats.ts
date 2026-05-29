"use client";

import { useReadContracts } from "wagmi";
import { ALLOCATIONS, type Allocation } from "@/lib/data";
import {
  CAPITAL_MANAGER_ADDRESS,
  aaveUsdcAdapterContract,
  aaveWethAdapterContract,
  bybitAttestorContract,
  isAllocationConfigured,
  usdcContract,
} from "@/lib/contracts";

const USDC_SCALE = 1_000_000n; // 6 decimals — USDC + adapter valueInUsdc units
const REFETCH_INTERVAL_MS = 30_000;

// Order matches the existing `ALLOCATIONS` constant so the donut/table
// rendering doesn't have to reconcile keys.
const KEYS = ["AAVE_USDC", "AAVE_WETH", "BYBIT", "CASH"] as const;
type AllocationKey = (typeof KEYS)[number];

export interface AllocationRow extends Allocation {
  valueUsdc: number;
}

export interface AllocationStats {
  rows: AllocationRow[];
  totalUsdc: number;
  isLoading: boolean;
  isError: boolean;
  isLive: boolean;
}

function buildFromMock(): AllocationStats {
  const rows = ALLOCATIONS.map((a) => ({ ...a, valueUsdc: a.notional }));
  const totalUsdc = rows.reduce((s, r) => s + r.valueUsdc, 0);
  return { rows, totalUsdc, isLoading: false, isError: false, isLive: false };
}

export function useAllocationStats(): AllocationStats {
  const query = useReadContracts({
    allowFailure: true,
    contracts: [
      { ...aaveUsdcAdapterContract, functionName: "valueInUsdc" },
      { ...aaveWethAdapterContract, functionName: "valueInUsdc" },
      { ...bybitAttestorContract, functionName: "valueInUsdc" },
      { ...usdcContract, functionName: "balanceOf", args: [CAPITAL_MANAGER_ADDRESS] },
    ],
    query: {
      enabled: isAllocationConfigured,
      refetchInterval: REFETCH_INTERVAL_MS,
      refetchOnWindowFocus: true,
    },
  });

  if (!isAllocationConfigured) {
    return buildFromMock();
  }

  if (query.isLoading || !query.data) {
    return { ...buildFromMock(), isLoading: true };
  }

  const raw: Partial<Record<AllocationKey, bigint>> = {};
  const [aaveUsdc, aaveWeth, bybit, cash] = query.data;
  if (aaveUsdc?.status === "success") raw.AAVE_USDC = aaveUsdc.result as bigint;
  if (aaveWeth?.status === "success") raw.AAVE_WETH = aaveWeth.result as bigint;
  if (bybit?.status === "success") raw.BYBIT = bybit.result as bigint;
  if (cash?.status === "success") raw.CASH = cash.result as bigint;

  const haveAll = KEYS.every((k) => raw[k] !== undefined);
  if (!haveAll) {
    // Partial multicall failure — fall back to mock so the demo stays
    // readable rather than rendering a half-zero allocation.
    return { ...buildFromMock(), isError: query.isError };
  }

  const totalRaw = KEYS.reduce((s, k) => s + (raw[k] ?? 0n), 0n);
  if (totalRaw === 0n) {
    return { ...buildFromMock(), isError: query.isError };
  }

  const rows: AllocationRow[] = ALLOCATIONS.map((a) => {
    const key = a.key as AllocationKey;
    const valueRaw = raw[key] ?? 0n;
    const valueUsdc = Number(valueRaw) / Number(USDC_SCALE);
    // Round to 1dp to keep donut tidy; pct sum drift handled by donut.
    const pct = Math.round((Number(valueRaw) * 1000) / Number(totalRaw)) / 10;
    return { ...a, valueUsdc, notional: Math.round(valueUsdc), pct };
  });

  return {
    rows,
    totalUsdc: Number(totalRaw) / Number(USDC_SCALE),
    isLoading: false,
    isError: query.isError,
    isLive: true,
  };
}
