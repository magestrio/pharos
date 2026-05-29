"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { AbiEvent } from "viem";
import { usePublicClient, useWatchContractEvent } from "wagmi";
import {
  DECISION_LOG_ADDRESS,
  DECISION_LOG_DEPLOY_BLOCK,
  VUSDC_CHAIN_ID,
  decisionLogContract,
  isDecisionLogConfigured,
} from "@/lib/contracts";
import { DecisionLogABI } from "@vault8004/abi";

const DECISION_RECORDED_EVENT = (DecisionLogABI as readonly AbiEvent[]).find(
  (item) => item.type === "event" && item.name === "DecisionRecorded",
) as AbiEvent;

const REFETCH_INTERVAL_MS = 30_000;
const QUERY_KEY = ["decision-log-events", DECISION_LOG_ADDRESS] as const;

export interface OnChainDecisionEvent {
  decisionId: `0x${string}`;
  ipfsCid: string;
  actionHash: `0x${string}`;
  timestamp: number; // Unix seconds — from event payload, NOT block timestamp
  txHash: `0x${string}`;
  blockNumber: bigint;
  logIndex: number;
}

export interface DecisionEventsResult {
  events: OnChainDecisionEvent[];
  isLoading: boolean;
  isError: boolean;
  isLive: boolean;
}

type RawEventLog = {
  args: {
    agentId?: bigint;
    decisionId?: `0x${string}`;
    ipfsCid?: string;
    actionHash?: `0x${string}`;
    timestamp?: bigint;
  };
  transactionHash: `0x${string}`;
  blockNumber: bigint;
  logIndex: number;
};

function normalize(log: RawEventLog): OnChainDecisionEvent | null {
  const { decisionId, ipfsCid, actionHash, timestamp } = log.args;
  if (!decisionId || ipfsCid === undefined || !actionHash || timestamp === undefined) {
    return null;
  }
  return {
    decisionId,
    ipfsCid,
    actionHash,
    timestamp: Number(timestamp),
    txHash: log.transactionHash,
    blockNumber: log.blockNumber,
    logIndex: log.logIndex,
  };
}

function sortNewestFirst(events: OnChainDecisionEvent[]): OnChainDecisionEvent[] {
  return [...events].sort((a, b) => {
    if (b.blockNumber !== a.blockNumber) {
      return b.blockNumber > a.blockNumber ? 1 : -1;
    }
    return b.logIndex - a.logIndex;
  });
}

/**
 * Read the historical + live DecisionRecorded events from DecisionLog.
 *
 * History: `publicClient.getLogs` from a configurable deploy block
 * (`NEXT_PUBLIC_DECISION_LOG_DEPLOY_BLOCK`, default 0n). Wrapped in
 * React Query so the result is cacheable and revalidated every 30s.
 *
 * Live: `useWatchContractEvent` invalidates the query whenever a new
 * event is emitted, triggering a re-fetch that picks up the new log.
 *
 * Pre-deploy (address = 0x0…): returns empty events + isLive=false so
 * the caller can fall back to a mock list.
 */
export function useDecisionEvents(): DecisionEventsResult {
  const publicClient = usePublicClient({ chainId: VUSDC_CHAIN_ID });
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: async (): Promise<OnChainDecisionEvent[]> => {
      if (!publicClient || !isDecisionLogConfigured) return [];
      const logs = await publicClient.getLogs({
        address: DECISION_LOG_ADDRESS,
        event: DECISION_RECORDED_EVENT,
        fromBlock: DECISION_LOG_DEPLOY_BLOCK,
        toBlock: "latest",
      });
      const normalized = (logs as unknown as RawEventLog[])
        .map(normalize)
        .filter((e): e is OnChainDecisionEvent => e !== null);
      return sortNewestFirst(normalized);
    },
    enabled: isDecisionLogConfigured && !!publicClient,
    refetchInterval: REFETCH_INTERVAL_MS,
    refetchOnWindowFocus: true,
    staleTime: 5_000,
  });

  // Live invalidation when a new event arrives between refetch ticks.
  useWatchContractEvent({
    ...decisionLogContract,
    eventName: "DecisionRecorded",
    enabled: isDecisionLogConfigured,
    onLogs: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });

  // If publicClient becomes available after enabled flipped, re-fetch.
  useEffect(() => {
    if (publicClient && isDecisionLogConfigured) {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    }
  }, [publicClient, queryClient]);

  return {
    events: query.data ?? [],
    isLoading: query.isLoading,
    isError: query.isError,
    isLive: isDecisionLogConfigured,
  };
}
