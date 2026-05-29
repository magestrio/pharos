"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { AbiEvent } from "viem";
import { usePublicClient, useReadContracts } from "wagmi";
import { BybitAttestorABI } from "@vault8004/abi";

import {
  BYBIT_ATTESTOR_ADDRESS,
  DECISION_LOG_DEPLOY_BLOCK,
  VUSDC_CHAIN_ID,
  bybitAttestorContract,
} from "@/lib/contracts";

const REFETCH_INTERVAL_MS = 30_000;
const WARN_THRESHOLD_SEC = 30 * 60; // 30 minutes — soft alert
const CRITICAL_THRESHOLD_SEC = 60 * 60; // 60 minutes — vault freezes allocations
const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000" as const;

const BALANCE_UPDATED_EVENT = (BybitAttestorABI as readonly AbiEvent[]).find(
  (item) => item.type === "event" && item.name === "BalanceUpdated",
) as AbiEvent;

export type AttestorStatus = "HEALTHY" | "DEGRADED" | "CRITICAL" | "UNKNOWN";

export interface AttestorHealthState {
  // Unix-sec timestamp of the last attestation. null when not configured.
  lastAttestationSec: number | null;
  // Seconds since the last attestation (ticks every second).
  lagSec: number;
  // Last-known attested USDC balance (raw 1e6 units).
  attestedBalance: bigint | null;
  // The address authorised to push attestations (the 2-of-3 Safe).
  attestorAddress: `0x${string}` | null;
  // Expected push interval from the contract (seconds). Typically 300.
  heartbeatSec: number | null;
  // Lifetime count of `BalanceUpdated` events on the attestor.
  pushCount: number | null;
  status: AttestorStatus;
  warnThresholdSec: number;
  criticalThresholdSec: number;
  isLoading: boolean;
  isLive: boolean;
}

function statusFromLag(lagSec: number): AttestorStatus {
  if (lagSec < 0) return "UNKNOWN";
  if (lagSec > CRITICAL_THRESHOLD_SEC) return "CRITICAL";
  if (lagSec > WARN_THRESHOLD_SEC) return "DEGRADED";
  return "HEALTHY";
}

function pickBig(
  entry: { status?: "success" | "failure"; result?: unknown } | undefined,
): bigint | null {
  if (!entry || entry.status !== "success") return null;
  if (typeof entry.result === "bigint") return entry.result;
  return null;
}

function pickAddress(
  entry: { status?: "success" | "failure"; result?: unknown } | undefined,
): `0x${string}` | null {
  if (!entry || entry.status !== "success") return null;
  if (typeof entry.result === "string" && entry.result.startsWith("0x")) {
    return entry.result as `0x${string}`;
  }
  return null;
}

export function useAttestorHealth(): AttestorHealthState {
  const publicClient = usePublicClient({ chainId: VUSDC_CHAIN_ID });
  const isConfigured = BYBIT_ATTESTOR_ADDRESS !== ZERO_ADDRESS;

  const reads = useReadContracts({
    allowFailure: true,
    contracts: [
      { ...bybitAttestorContract, functionName: "lastAttestationTime" },
      { ...bybitAttestorContract, functionName: "attestor" },
      { ...bybitAttestorContract, functionName: "HEARTBEAT" },
      { ...bybitAttestorContract, functionName: "attestedBalance" },
    ],
    query: {
      enabled: isConfigured,
      refetchInterval: REFETCH_INTERVAL_MS,
    },
  });

  // Lifetime BalanceUpdated count via getLogs — cached on the same
  // cadence; separate from the multicall since it's a different
  // primitive.
  const pushCountQuery = useQuery({
    queryKey: ["attestor-push-count", BYBIT_ATTESTOR_ADDRESS],
    queryFn: async (): Promise<number> => {
      if (!publicClient || !isConfigured) return 0;
      const logs = await publicClient.getLogs({
        address: BYBIT_ATTESTOR_ADDRESS,
        event: BALANCE_UPDATED_EVENT,
        fromBlock: DECISION_LOG_DEPLOY_BLOCK,
        toBlock: "latest",
      });
      return logs.length;
    },
    enabled: isConfigured && !!publicClient,
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: 5_000,
  });

  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(id);
  }, []);

  if (!isConfigured) {
    return {
      lastAttestationSec: null,
      lagSec: 0,
      attestedBalance: null,
      attestorAddress: null,
      heartbeatSec: null,
      pushCount: null,
      status: "UNKNOWN",
      warnThresholdSec: WARN_THRESHOLD_SEC,
      criticalThresholdSec: CRITICAL_THRESHOLD_SEC,
      isLoading: false,
      isLive: false,
    };
  }

  const [lastTsR, attestorR, heartbeatR, balanceR] = reads.data ?? [];
  const lastTsBig = pickBig(lastTsR);
  const lastAttestationSec = lastTsBig !== null ? Number(lastTsBig) : null;
  const lagSec =
    lastAttestationSec !== null ? Math.max(0, nowSec - lastAttestationSec) : 0;
  const heartbeatBig = pickBig(heartbeatR);
  const heartbeatSec = heartbeatBig !== null ? Number(heartbeatBig) : null;

  return {
    lastAttestationSec,
    lagSec,
    attestedBalance: pickBig(balanceR),
    attestorAddress: pickAddress(attestorR),
    heartbeatSec,
    pushCount: pushCountQuery.data ?? null,
    status: lastAttestationSec === null ? "UNKNOWN" : statusFromLag(lagSec),
    warnThresholdSec: WARN_THRESHOLD_SEC,
    criticalThresholdSec: CRITICAL_THRESHOLD_SEC,
    isLoading: reads.isLoading || pushCountQuery.isLoading,
    isLive: true,
  };
}

export function formatLagShort(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const remM = m - h * 60;
  return remM > 0 ? `${h}h ${remM}m` : `${h}h`;
}

export function formatHeartbeatShort(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds % 60 === 0) {
    const m = seconds / 60;
    return `~${m} min cron`;
  }
  return `~${seconds}s cron`;
}
