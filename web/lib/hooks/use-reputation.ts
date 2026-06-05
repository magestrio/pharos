"use client";

import { useEffect, useState } from "react";
import { useReadContracts } from "wagmi";
import {
  isReputationOracleConfigured,
  reputationOracleContract,
} from "@/lib/contracts";
import { useIsMounted } from "@/lib/hooks/use-is-mounted";

const REFETCH_INTERVAL_MS = 30_000;

export interface ReputationState {
  // Last on-chain score in bps (signed — negative if underwater).
  lastScoreBps: number | null;
  // Preview of what the next call to updateReputation() would set.
  previewScoreBps: number | null;
  // Seconds elapsed since last update (from the contract's perspective).
  previewElapsedSec: number | null;
  // Timestamp (Unix sec) of last updateReputation() call.
  lastUpdateTimestamp: number | null;
  // Hard-coded throttle constant from the contract (usually 3600s).
  minIntervalSec: number;
  // Live countdown to the next allowed update. 0 when ready.
  secondsUntilNext: number;
  // True when the contract permits another call right now.
  canUpdate: boolean;
  // Lifetime count of successful updateReputation() calls.
  updateCount: number | null;
  isLoading: boolean;
  isLive: boolean;
}

// Returned during SSR + the very first client render, so the
// hydration tree matches whatever the server emitted (server has no
// wagmi data and no wall clock — we MUST NOT diverge here).
const SSR_PLACEHOLDER: ReputationState = {
  lastScoreBps: null,
  previewScoreBps: null,
  previewElapsedSec: null,
  lastUpdateTimestamp: null,
  minIntervalSec: 3600,
  secondsUntilNext: 0,
  canUpdate: false,
  updateCount: null,
  isLoading: false,
  isLive: false,
};

export function useReputation(): ReputationState {
  // Gate every dynamic value (wagmi result, Date.now-derived state)
  // behind a post-mount flag so SSR and the first client render emit
  // an identical tree.
  const mounted = useIsMounted();

  const query = useReadContracts({
    allowFailure: true,
    contracts: [
      { ...reputationOracleContract, functionName: "lastScore" },
      { ...reputationOracleContract, functionName: "lastUpdateTimestamp" },
      { ...reputationOracleContract, functionName: "MIN_INTERVAL" },
      { ...reputationOracleContract, functionName: "canUpdate" },
      { ...reputationOracleContract, functionName: "previewScore" },
      { ...reputationOracleContract, functionName: "updateCount" },
    ],
    query: {
      enabled: mounted && isReputationOracleConfigured,
      refetchInterval: REFETCH_INTERVAL_MS,
    },
  });

  // Tick once per second to drive the countdown UI. Initialize to 0
  // (deterministic on the server) and set the real wall clock after
  // mount; otherwise `Date.now()` in the initializer diverges
  // server-vs-client.
  const [nowSec, setNowSec] = useState(0);
  useEffect(() => {
    setNowSec(Math.floor(Date.now() / 1000));
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(id);
  }, []);

  if (!mounted || !isReputationOracleConfigured) {
    return SSR_PLACEHOLDER;
  }

  const [
    lastScoreR,
    lastUpdateTsR,
    minIntervalR,
    canUpdateR,
    previewR,
    updateCountR,
  ] = query.data ?? [];

  const lastScoreBps = pickBigSigned(lastScoreR);
  const lastUpdateTsBig = pickBigUnsigned(lastUpdateTsR);
  const minIntervalBig = pickBigUnsigned(minIntervalR);
  const updateCountBig = pickBigUnsigned(updateCountR);

  const previewTuple =
    previewR?.status === "success" && Array.isArray(previewR.result)
      ? (previewR.result as readonly [bigint, bigint, bigint])
      : null;
  const previewScoreBps = previewTuple ? Number(previewTuple[0]) : null;
  const previewElapsedSec = previewTuple ? Number(previewTuple[2]) : null;

  const lastUpdateTimestamp = lastUpdateTsBig !== null ? Number(lastUpdateTsBig) : null;
  const minIntervalSec = minIntervalBig !== null ? Number(minIntervalBig) : 3600;

  const secondsUntilNext =
    lastUpdateTimestamp !== null && minIntervalSec > 0
      ? Math.max(0, lastUpdateTimestamp + minIntervalSec - nowSec)
      : 0;

  // Prefer the contract's own canUpdate() if it succeeded; otherwise
  // fall back to our local countdown derivation.
  const canUpdate =
    canUpdateR?.status === "success" ? Boolean(canUpdateR.result) : secondsUntilNext === 0;

  return {
    lastScoreBps,
    previewScoreBps,
    previewElapsedSec,
    lastUpdateTimestamp,
    minIntervalSec,
    secondsUntilNext,
    canUpdate,
    updateCount: updateCountBig !== null ? Number(updateCountBig) : null,
    isLoading: query.isLoading,
    isLive: true,
  };
}

function pickBigUnsigned(
  entry: { status?: "success" | "failure"; result?: unknown } | undefined,
): bigint | null {
  if (!entry || entry.status !== "success") return null;
  if (typeof entry.result === "bigint") return entry.result;
  return null;
}

function pickBigSigned(
  entry: { status?: "success" | "failure"; result?: unknown } | undefined,
): number | null {
  if (!entry || entry.status !== "success") return null;
  if (typeof entry.result === "bigint") return Number(entry.result);
  return null;
}

export function formatBpsAsPct(bps: number | null): string {
  if (bps === null) return "—";
  return `${(bps / 100).toFixed(2)}%`;
}

export function formatCountdown(seconds: number): string {
  if (seconds <= 0) return "ready";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}
