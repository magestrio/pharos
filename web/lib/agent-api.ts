/**
 * Typed client for the Vault8004 read-only API (`data-store.5`).
 *
 * Server-side fetch helpers — call from Server Components or Route
 * Handlers. Base URL comes from `AGENT_API_URL` env (default
 * http://localhost:8000 for local dev against `uvicorn agent.api.server:app`).
 *
 * Mirrors the pydantic response models in
 * `agent/api/server.py`. Keep these in sync when the API changes.
 */

const BASE = (process.env.AGENT_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export type CycleSummary = {
  cycle_ts: string;
  started_at: string;
  finished_at: string | null;
  result: string;
  wake_reason: string;
  confidence: number | null;
  expected_apr_pct: number | null;
  actions_planned: number | null;
  actions_executed: number | null;
  error: string | null;
};

export type PositionRow = {
  venue: string;
  product_id: string;
  coin: string | null;
  amount: string | null;
  amount_usd: string | null;
};

export type ExecutionRow = {
  idx: number;
  action: Record<string, unknown>;
  status: string;
  error: string | null;
};

export type EventRow = {
  id: number;
  event_ts: string;
  kind: string;
  severity: string;
  position_id: string | null;
  coin: string | null;
  payload: Record<string, unknown>;
  triggered_cycle_ts: string | null;
};

export type CycleDetail = CycleSummary & {
  snapshot: Record<string, unknown> | null;
  decision: Record<string, unknown> | null;
  positions: PositionRow[];
  executions: ExecutionRow[];
  events: EventRow[];
};

export type Portfolio = {
  cycle_ts: string;
  started_at: string;
  result: string;
  wake_reason: string;
  positions: PositionRow[];
  wallet: Record<string, unknown> | null;
};

/**
 * All fetchers run with `cache: "no-store"` — history is live data.
 * Server Components built against these helpers re-fetch on every
 * request (or follow whatever the page-level revalidate config sets).
 */
async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText}`, path);
  }
  return (await res.json()) as T;
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public path: string) {
    super(message);
    this.name = "ApiError";
  }
}

export async function fetchCycles(opts: {
  limit?: number;
  since?: string;
  wakeReasonPrefix?: string;
} = {}): Promise<CycleSummary[]> {
  const params = new URLSearchParams();
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.since) params.set("since", opts.since);
  if (opts.wakeReasonPrefix) params.set("wake_reason_prefix", opts.wakeReasonPrefix);
  const qs = params.toString();
  return fetchJson<CycleSummary[]>(`/cycles${qs ? `?${qs}` : ""}`);
}

export async function fetchCycle(cycleTs: string): Promise<CycleDetail | null> {
  try {
    return await fetchJson<CycleDetail>(`/cycles/${encodeURIComponent(cycleTs)}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export async function fetchEvents(opts: {
  limit?: number;
  since?: string;
  kind?: string;
  severity?: string;
} = {}): Promise<EventRow[]> {
  const params = new URLSearchParams();
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.since) params.set("since", opts.since);
  if (opts.kind) params.set("kind", opts.kind);
  if (opts.severity) params.set("severity", opts.severity);
  const qs = params.toString();
  return fetchJson<EventRow[]>(`/events${qs ? `?${qs}` : ""}`);
}

export async function fetchPortfolio(): Promise<Portfolio | null> {
  try {
    return await fetchJson<Portfolio>(`/portfolio/current`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}
