"use client";

import { useQuery } from "@tanstack/react-query";
import type { AbiEvent } from "viem";
import { usePublicClient } from "wagmi";
import {
  DECISION_LOG_DEPLOY_BLOCK,
  REPUTATION_ORACLE_ADDRESS,
  VUSDC_CHAIN_ID,
  isReputationOracleConfigured,
} from "@/lib/contracts";
import { ReputationOracleABI } from "@vault8004/abi";

const REPUTATION_UPDATED_EVENT = (ReputationOracleABI as readonly AbiEvent[]).find(
  (item) => item.type === "event" && item.name === "ReputationUpdated",
) as AbiEvent;

const REFETCH_INTERVAL_MS = 30_000;

export interface ReputationHistoryPoint {
  updateIndex: number;
  caller: `0x${string}`;
  currentAssets: bigint;
  scoreBps: number;
  elapsedSeconds: number;
  blockNumber: bigint;
  txHash: `0x${string}`;
}

export interface ReputationHistoryResult {
  points: ReputationHistoryPoint[];
  isLoading: boolean;
  isError: boolean;
  isLive: boolean;
}

type RawLog = {
  args: {
    updateIndex?: bigint;
    caller?: `0x${string}`;
    currentAssets?: bigint;
    scoreBps?: bigint;
    elapsedSeconds?: bigint;
  };
  transactionHash: `0x${string}`;
  blockNumber: bigint;
};

function normalize(log: RawLog): ReputationHistoryPoint | null {
  const { updateIndex, caller, currentAssets, scoreBps, elapsedSeconds } = log.args;
  if (
    updateIndex === undefined ||
    !caller ||
    currentAssets === undefined ||
    scoreBps === undefined ||
    elapsedSeconds === undefined
  ) {
    return null;
  }
  return {
    updateIndex: Number(updateIndex),
    caller,
    currentAssets,
    scoreBps: Number(scoreBps),
    elapsedSeconds: Number(elapsedSeconds),
    blockNumber: log.blockNumber,
    txHash: log.transactionHash,
  };
}

export function useReputationHistory(): ReputationHistoryResult {
  const publicClient = usePublicClient({ chainId: VUSDC_CHAIN_ID });

  const query = useQuery({
    queryKey: ["reputation-history", REPUTATION_ORACLE_ADDRESS],
    queryFn: async (): Promise<ReputationHistoryPoint[]> => {
      if (!publicClient || !isReputationOracleConfigured) return [];
      const logs = await publicClient.getLogs({
        address: REPUTATION_ORACLE_ADDRESS,
        event: REPUTATION_UPDATED_EVENT,
        fromBlock: DECISION_LOG_DEPLOY_BLOCK,
        toBlock: "latest",
      });
      return (logs as unknown as RawLog[])
        .map(normalize)
        .filter((p): p is ReputationHistoryPoint => p !== null)
        .sort((a, b) => a.updateIndex - b.updateIndex);
    },
    enabled: isReputationOracleConfigured && !!publicClient,
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: 5_000,
  });

  return {
    points: query.data ?? [],
    isLoading: query.isLoading,
    isError: query.isError,
    isLive: isReputationOracleConfigured,
  };
}
