/**
 * Client-side data plumbing for the agent cycle store
 * (`frontend-complete.5`).
 *
 * Receives the initial server-fetched cycles + portfolio from
 * `app/page.tsx` (Server Component) and hydrates a React Query cache
 * so child tabs can consume `useCycles()` / `usePortfolio()` without
 * an initial spinner. Revalidates client-side every 30s + on window
 * focus so the demo feels live.
 *
 * All client-side fetches go through `/api/store/*` proxy routes
 * (same-origin Next.js), NOT direct to the FastAPI on Hetzner — keeps
 * Postgres firewalled to localhost and avoids CORS.
 */
"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";

import type {
  CycleDetail,
  CycleSummary,
  EventRow,
  Portfolio,
} from "@/lib/agent-api";

// Revalidate cadence — matches the FastAPI's expected refresh rate
// (agent heartbeat is ~4h plus reactive cycles, so 30s is plenty fast
// for the demo without hammering the proxy).
const REFETCH_INTERVAL_MS = 30_000;

type StoreContextValue = {
  initialCycles: CycleSummary[];
  initialPortfolio: Portfolio | null;
};

const StoreCtx = createContext<StoreContextValue | null>(null);

export function StoreProvider({
  initialCycles,
  initialPortfolio,
  children,
}: StoreContextValue & { children: ReactNode }) {
  return (
    <StoreCtx.Provider value={{ initialCycles, initialPortfolio }}>
      {children}
    </StoreCtx.Provider>
  );
}

function useStoreContext(): StoreContextValue {
  const ctx = useContext(StoreCtx);
  if (ctx === null) {
    throw new Error(
      "agent-store hooks must be used inside <StoreProvider> " +
        "(provided by app/page.tsx via Shell).",
    );
  }
  return ctx;
}

// ─────────────────────────── hooks ───────────────────────────────────


/**
 * Cycles list. Hydrates from server-fetched initial data; revalidates
 * via the proxy every 30s + on window focus.
 *
 * The query key includes the limit so different consumers
 * (`limit=50` for DecisionLog, `limit=5` for RecentDecisions preview)
 * each get their own cache slot. Both still hydrate from the same
 * server-fetched `initialCycles` (the preview slices client-side).
 */
export function useCycles(opts: { limit?: number } = {}): UseQueryResult<
  CycleSummary[]
> {
  const { initialCycles } = useStoreContext();
  const limit = opts.limit ?? 50;
  return useQuery({
    queryKey: ["cycles", { limit }],
    queryFn: async () => {
      const res = await fetch(`/api/store/cycles?limit=${limit}`, {
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`/api/store/cycles ${res.status}`);
      return (await res.json()) as CycleSummary[];
    },
    initialData: initialCycles.slice(0, limit),
    refetchOnWindowFocus: true,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

/**
 * Per-cycle detail. NOT hydrated from initial data — the detail
 * blob is heavy (full snapshot JSONB), only fetch when a row is
 * actually expanded. `enabled` lets callers gate this on
 * `expanded === true`.
 */
export function useCycleDetail(
  cycleTs: string | null,
): UseQueryResult<CycleDetail> {
  return useQuery({
    queryKey: ["cycle", cycleTs],
    queryFn: async () => {
      if (!cycleTs) throw new Error("cycleTs required");
      const res = await fetch(
        `/api/store/cycles/${encodeURIComponent(cycleTs)}`,
        { cache: "no-store" },
      );
      if (!res.ok) {
        throw new Error(`/api/store/cycles/${cycleTs} ${res.status}`);
      }
      return (await res.json()) as CycleDetail;
    },
    enabled: !!cycleTs,
    staleTime: REFETCH_INTERVAL_MS, // detail rarely changes after recording
  });
}

/**
 * Portfolio (latest cycle's positions + wallet block). Hydrates
 * from server fetch; revalidates on the same 30s cadence.
 */
export function usePortfolio(): UseQueryResult<Portfolio | null> {
  const { initialPortfolio } = useStoreContext();
  return useQuery({
    queryKey: ["portfolio"],
    queryFn: async () => {
      const res = await fetch(`/api/store/portfolio`, { cache: "no-store" });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`/api/store/portfolio ${res.status}`);
      return (await res.json()) as Portfolio;
    },
    initialData: initialPortfolio,
    refetchOnWindowFocus: true,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

/**
 * Recent watcher events. Lazy — used by the wake-events widget on
 * Vault Card (`.11`); not part of the server-rendered initial data
 * because it's a secondary signal.
 */
export function useRecentEvents(limit = 10): UseQueryResult<EventRow[]> {
  return useQuery({
    queryKey: ["events", { limit }],
    queryFn: async () => {
      const res = await fetch(`/api/store/events?limit=${limit}`, {
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`/api/store/events ${res.status}`);
      return (await res.json()) as EventRow[];
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
