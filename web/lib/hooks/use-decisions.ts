"use client";

import { useMemo } from "react";
import { type Decision } from "@/lib/data";
import { formatDate, formatDateTime, formatTime } from "@/lib/datetime";
import { useCycles } from "@/lib/agent-store-context";
import type { CycleSummary } from "@/lib/agent-api";
import {
  useDecisionEvents,
  type OnChainDecisionEvent,
} from "@/lib/hooks/use-decision-events";
import { useIsMounted } from "@/lib/hooks/use-is-mounted";

const MATCH_WINDOW_SEC = 90;

export interface DecisionsResult {
  decisions: Decision[];
  isLoading: boolean;
  isError: boolean;
  isLive: boolean;
}

function shortHash(hex: string, head = 6, tail = 4): string {
  if (hex.length <= head + tail + 2) return hex;
  return `${hex.slice(0, head)}…${hex.slice(-tail)}`;
}

function formatTs(unixSec: number): string {
  return formatDateTime(unixSec * 1000);
}

function formatAgo(unixSec: number, nowMs: number = Date.now()): string {
  const diffSec = Math.max(0, Math.floor(nowMs / 1000 - unixSec));
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  const diffD = Math.floor(diffH / 24);
  const remH = diffH - diffD * 24;
  return remH > 0 ? `${diffD}d ${remH}h ago` : `${diffD}d ago`;
}

function cycleStartedSec(cycle: CycleSummary): number {
  return Math.floor(new Date(cycle.started_at).getTime() / 1000);
}

function findMatchingCycle(
  event: OnChainDecisionEvent,
  cycles: CycleSummary[],
): CycleSummary | null {
  let best: { cycle: CycleSummary; diff: number } | null = null;
  for (const cycle of cycles) {
    const diff = Math.abs(cycleStartedSec(cycle) - event.timestamp);
    if (best === null || diff < best.diff) {
      best = { cycle, diff };
    }
  }
  return best && best.diff <= MATCH_WINDOW_SEC ? best.cycle : null;
}

function deriveSummary(cycle: CycleSummary | null): string {
  if (!cycle) return "Decision recorded on-chain";
  if (cycle.error) return `Cycle errored: ${cycle.error.slice(0, 80)}`;
  const acted = cycle.actions_executed ?? 0;
  if (cycle.result === "no_change" || acted === 0) {
    return `Held — ${cycle.wake_reason}`;
  }
  return `${acted} action${acted === 1 ? "" : "s"} executed (${cycle.wake_reason})`;
}

function deriveRisk(cycle: CycleSummary | null): "LOW" | "MED" | "HIGH" {
  if (!cycle) return "LOW";
  if (cycle.error) return "HIGH";
  if ((cycle.confidence ?? 1) < 0.6) return "MED";
  return "LOW";
}

function deriveExec(cycle: CycleSummary | null): string {
  if (!cycle || !cycle.finished_at) return "—";
  const ms = new Date(cycle.finished_at).getTime() - new Date(cycle.started_at).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

function deriveConviction(cycle: CycleSummary | null): "low" | "med" | "high" {
  const c = cycle?.confidence ?? 0;
  if (c >= 0.75) return "high";
  if (c >= 0.6) return "med";
  return "low";
}

function buildDecision(
  event: OnChainDecisionEvent,
  cycle: CycleSummary | null,
  nowMs: number,
): Decision {
  const conf = cycle?.confidence ?? 0;
  return {
    id: shortHash(event.decisionId),
    full: event.decisionId,
    ts: formatTs(event.timestamp),
    dateLabel: formatDate(event.timestamp * 1000),
    timeLabel: formatTime(event.timestamp * 1000),
    ago: formatAgo(event.timestamp, nowMs),
    summary: deriveSummary(cycle),
    risk: deriveRisk(cycle),
    exec: deriveExec(cycle),
    confidence: conf,
    profitable: cycle ? !cycle.error && (cycle.expected_apr_pct ?? 0) > 0 : false,
    conviction: deriveConviction(cycle),
    thesis: cycle
      ? `Cycle ${cycle.cycle_ts} · wake=${cycle.wake_reason} · result=${cycle.result}. Full rationale loads on expand (.9).`
      : "Full rationale loads from off-chain cycle data once available.",
    risks: cycle?.error ? `Cycle errored: ${cycle.error}` : "None.",
    allora: "—",
    flags: [],
    ipfs: event.ipfsCid,
    tx: event.txHash,
    cycleTs: cycle?.cycle_ts,
  };
}

function decisionFromCycle(cycle: CycleSummary, nowMs: number): Decision {
  const startedSec = cycleStartedSec(cycle);
  return {
    id: cycle.cycle_ts.slice(0, 19),
    full: cycle.cycle_ts,
    ts: formatTs(startedSec),
    dateLabel: formatDate(startedSec * 1000),
    timeLabel: formatTime(startedSec * 1000),
    ago: formatAgo(startedSec, nowMs),
    summary: deriveSummary(cycle),
    risk: deriveRisk(cycle),
    exec: deriveExec(cycle),
    confidence: cycle.confidence ?? 0,
    profitable: !cycle.error && (cycle.expected_apr_pct ?? 0) > 0,
    conviction: deriveConviction(cycle),
    thesis: `Cycle ${cycle.cycle_ts} · wake=${cycle.wake_reason} · result=${cycle.result}. Expand for full snapshot + decision detail.`,
    risks: cycle.error ? `Cycle errored: ${cycle.error}` : "None.",
    allora: "—",
    flags: [],
    ipfs: "",
    tx: "",
    cycleTs: cycle.cycle_ts,
  };
}

/**
 * Joined on-chain decision rows: each row's identity (decisionId,
 * ipfsCid, txHash, timestamp) is the on-chain event; the surrounding
 * cycle metadata (summary, risk, confidence, exec) comes from the
 * off-chain agent API via `useCycles`. Matched by closest timestamp
 * within a ±90s window.
 *
 * Pre-deploy fallback: when the DecisionLog contract isn't configured
 * (Phase A on-chain leg deferred), we derive decisions directly from
 * the agent cycle history — no on-chain join, no mock data.
 */
export function useDecisions(): DecisionsResult {
  const mounted = useIsMounted();
  const eventsQuery = useDecisionEvents();
  const cyclesQuery = useCycles({ limit: 50 });

  return useMemo<DecisionsResult>(() => {
    if (!mounted) {
      return { decisions: [], isLoading: false, isError: false, isLive: false };
    }
    const cycles = cyclesQuery.data ?? [];
    const nowMs = Date.now();
    if (!eventsQuery.isLive) {
      return {
        decisions: cycles.map((c) => decisionFromCycle(c, nowMs)),
        isLoading: cyclesQuery.isLoading,
        isError: cyclesQuery.isError,
        isLive: false,
      };
    }
    const events = eventsQuery.events;
    const decisions = events.map((event) => {
      const cycle = findMatchingCycle(event, cycles);
      return buildDecision(event, cycle, nowMs);
    });
    return {
      decisions,
      isLoading: eventsQuery.isLoading || cyclesQuery.isLoading,
      isError: eventsQuery.isError || cyclesQuery.isError,
      isLive: true,
    };
  }, [mounted, eventsQuery.events, eventsQuery.isLive, eventsQuery.isLoading, eventsQuery.isError, cyclesQuery.data, cyclesQuery.isLoading, cyclesQuery.isError]);
}
