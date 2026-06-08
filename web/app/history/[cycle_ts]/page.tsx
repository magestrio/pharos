/**
 * Cycle detail panel (`data-store.6`).
 *
 * Server-rendered against the FastAPI `/cycles/{ts}` endpoint. Shows
 * the cycle metadata, decision thesis + venue allocations, positions
 * at snapshot time, executions, and any watcher events that triggered
 * the cycle.
 */
import Link from "next/link";
import { notFound } from "next/navigation";

import {
  type CycleDetail,
  type EventRow,
  type ExecutionRow,
  type PositionRow,
  fetchCycle,
} from "@/lib/agent-api";
import { ThesisView } from "@/components/thesis-view";

export const dynamic = "force-dynamic";

type ValidatorResult = { ok?: boolean; errors?: string[] };

type Decision = {
  thesis?: string;
  reflection?: string;
  venues?: Array<{
    venue_id: string;
    weight: number;
    picks?: Array<{ product_id: string; weight: number; notes?: string[] }>;
  }>;
  hedges?: Array<{ coin: string; notional_usd: number; notes?: string[] }>;
  confidence?: number;
  risk_flags?: string[];
  notes?: string[];
  expected_blended_apr_pct?: number;
  _meta?: { _validator?: ValidatorResult } & Record<string, unknown>;
  // The agent has historically written _validator at the top level too;
  // accept either path so older + newer cycles both render.
  _validator?: ValidatorResult;
};

function readValidator(decision: Decision): ValidatorResult {
  return decision._meta?._validator ?? decision._validator ?? {};
}

export default async function CycleDetailPage({
  params,
}: {
  params: { cycle_ts: string };
}) {
  const cycleTs = decodeURIComponent(params.cycle_ts);
  const cycle = await fetchCycle(cycleTs);
  if (cycle === null) notFound();

  return (
    <main className="min-h-screen bg-ink-950 text-white">
      <header className="border-b border-ink-600/60 bg-ink-950/90 backdrop-blur sticky top-0 z-10">
        <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between gap-4">
          <Link
            href="/history"
            className="font-mono text-[13px] text-white font-semibold tracking-tight hover:text-neon"
          >
            ← History
          </Link>
          <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-dim-500 truncate">
            Cycle · {cycle.cycle_ts}
          </div>
        </div>
      </header>

      <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-8 sm:py-10 space-y-6">
        <CycleMeta cycle={cycle} />
        <DecisionPanel decision={cycle.decision as Decision | null} />
        <PositionsPanel positions={cycle.positions} />
        <ExecutionsPanel executions={cycle.executions} />
        <EventsPanel events={cycle.events} />
        <SnapshotRawPanel snapshot={cycle.snapshot} />
      </section>
    </main>
  );
}

function CycleMeta({ cycle }: { cycle: CycleDetail }) {
  const wakeIsEvent = cycle.wake_reason.startsWith("event:");
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-ink-600/60 border border-ink-600/70 rounded-md overflow-hidden">
      <MetaCell label="Wake reason" value={cycle.wake_reason} highlight={wakeIsEvent} />
      <MetaCell label="Result" value={cycle.result} />
      <MetaCell
        label="Confidence"
        value={cycle.confidence !== null ? cycle.confidence.toFixed(2) : "—"}
      />
      <MetaCell
        label="Expected APR"
        value={
          cycle.expected_apr_pct !== null
            ? `${cycle.expected_apr_pct.toFixed(2)}%`
            : "—"
        }
      />
      <MetaCell label="Started" value={cycle.started_at} />
      <MetaCell label="Finished" value={cycle.finished_at ?? "—"} />
      <MetaCell
        label="Actions"
        value={`${cycle.actions_executed ?? "—"} / ${cycle.actions_planned ?? "—"}`}
      />
      <MetaCell label="Error" value={cycle.error ?? "—"} tone={cycle.error ? "danger" : "dim"} />
    </div>
  );
}

function MetaCell({
  label,
  value,
  highlight = false,
  tone = "dim",
}: {
  label: string;
  value: string;
  highlight?: boolean;
  tone?: "dim" | "danger";
}) {
  const valueClass = highlight
    ? "text-neon"
    : tone === "danger"
    ? "text-danger"
    : "text-white";
  return (
    <div className="bg-ink-900 px-4 py-3">
      <div className="text-[9.5px] font-mono uppercase tracking-[0.14em] text-dim-500 mb-1">
        {label}
      </div>
      <div className={`text-[12px] font-mono ${valueClass} truncate`}>{value}</div>
    </div>
  );
}

function DecisionPanel({ decision }: { decision: Decision | null }) {
  if (!decision) {
    return (
      <Panel title="Decision">
        <div className="text-[12px] font-mono text-dim-500">
          No decision recorded for this cycle (likely an error before LLM call).
        </div>
      </Panel>
    );
  }
  const validator = readValidator(decision);
  const validatorErrors = validator.errors ?? [];
  const validatorOk = validator.ok;
  return (
    <Panel title="Decision">
      {decision.reflection && (
        <div className="mb-5">
          <div className="text-[10px] font-mono uppercase tracking-[0.14em] text-accent mb-1.5">
            Agent&apos;s notes
          </div>
          <p className="font-sans text-[15px] leading-[1.7] text-dim-100 whitespace-pre-wrap">
            {decision.reflection}
          </p>
        </div>
      )}
      {decision.thesis && (
        <div className="mb-4">
          <ThesisView body={decision.thesis} />
        </div>
      )}
      <div className="mb-4">
        <div className="text-[10px] font-mono uppercase tracking-[0.14em] text-dim-500 mb-1">
          Venues
        </div>
        <div className="space-y-1.5">
          {(decision.venues ?? []).map((v) => (
            <div key={v.venue_id} className="text-[12px] font-mono">
              <span className="text-white">{v.venue_id}</span>
              <span className="text-dim-500"> · </span>
              <span className="text-neon tabular">
                {(v.weight * 100).toFixed(2)}%
              </span>
              {v.picks && v.picks.length > 0 && (
                <span className="text-dim-400">
                  {" → "}
                  {v.picks
                    .map((p) => `${p.product_id}@${(p.weight * 100).toFixed(0)}%`)
                    .join(", ")}
                </span>
              )}
            </div>
          ))}
        </div>
      </div>
      {validatorOk === false && validatorErrors.length > 0 && (
        <div className="border border-danger/40 bg-danger/5 rounded-sm p-3 mt-3">
          <div className="text-[10px] font-mono uppercase tracking-[0.14em] text-danger mb-1.5">
            Validator rejected
          </div>
          <ul className="text-[11.5px] font-mono text-dim-200 space-y-0.5 list-disc list-inside">
            {validatorErrors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {(decision.risk_flags ?? []).length > 0 && (
        <div className="mt-3 text-[12px] font-mono">
          <span className="text-dim-500">Risk flags:</span>{" "}
          <span className="text-warn">{(decision.risk_flags ?? []).join(", ")}</span>
        </div>
      )}
    </Panel>
  );
}

function PositionsPanel({ positions }: { positions: PositionRow[] }) {
  if (positions.length === 0) {
    return (
      <Panel title="Positions">
        <div className="text-[12px] font-mono text-dim-500">No positions held at this snapshot.</div>
      </Panel>
    );
  }
  return (
    <Panel title="Positions" count={positions.length}>
      <div className="grid grid-cols-12 gap-2 text-[10.5px] font-mono uppercase tracking-[0.14em] text-dim-500 mb-1.5">
        <div className="col-span-2">Venue</div>
        <div className="col-span-3">Product</div>
        <div className="col-span-2">Coin</div>
        <div className="col-span-3 text-right">Amount</div>
        <div className="col-span-2 text-right">USD</div>
      </div>
      <div className="divide-y divide-ink-600/30">
        {positions.map((p) => (
          <div
            key={`${p.venue}-${p.product_id}`}
            className="grid grid-cols-12 gap-2 py-1.5 text-[12px] font-mono"
          >
            <div className="col-span-2 text-dim-300">{p.venue}</div>
            <div className="col-span-3 text-white truncate">{p.product_id}</div>
            <div className="col-span-2 text-dim-200">{p.coin ?? "—"}</div>
            <div className="col-span-3 text-right text-dim-200 tabular">
              {p.amount ?? "—"}
            </div>
            <div className="col-span-2 text-right text-dim-300 tabular">
              {p.amount_usd ?? "—"}
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function ExecutionsPanel({ executions }: { executions: ExecutionRow[] }) {
  if (executions.length === 0) {
    return (
      <Panel title="Executions">
        <div className="text-[12px] font-mono text-dim-500">No actions executed.</div>
      </Panel>
    );
  }
  return (
    <Panel title="Executions" count={executions.length}>
      <div className="space-y-1.5">
        {executions.map((ex) => (
          <div key={ex.idx} className="text-[12px] font-mono">
            <span className="text-dim-500">#{ex.idx}</span>
            <span className="text-dim-500"> · </span>
            <span
              className={
                ex.status === "ok"
                  ? "text-neon"
                  : ex.status === "error"
                  ? "text-danger"
                  : "text-warn"
              }
            >
              {ex.status}
            </span>
            <span className="text-dim-500"> · </span>
            <span className="text-white">{String(ex.action.kind ?? "?")}</span>
            {Boolean(ex.action.coin) && (
              <>
                <span className="text-dim-500"> · </span>
                <span className="text-dim-200">{String(ex.action.coin)}</span>
              </>
            )}
            {ex.action.amount !== undefined && ex.action.amount !== null && (
              <>
                <span className="text-dim-500"> · </span>
                <span className="text-dim-300 tabular">{String(ex.action.amount)}</span>
              </>
            )}
            {ex.error && (
              <div className="text-[11px] text-danger pl-4 mt-0.5">{ex.error}</div>
            )}
          </div>
        ))}
      </div>
    </Panel>
  );
}

function EventsPanel({ events }: { events: EventRow[] }) {
  if (events.length === 0) {
    return (
      <Panel title="Wake events">
        <div className="text-[12px] font-mono text-dim-500">
          Heartbeat cycle — no watcher events triggered this re-decide.
        </div>
      </Panel>
    );
  }
  return (
    <Panel title="Wake events" count={events.length}>
      <div className="space-y-1.5">
        {events.map((e) => (
          <div key={e.id} className="text-[12px] font-mono">
            <span
              className={
                e.severity === "P0"
                  ? "text-danger"
                  : e.severity === "P1"
                  ? "text-warn"
                  : "text-dim-400"
              }
            >
              [{e.severity}]
            </span>
            <span className="text-dim-500"> </span>
            <span className="text-white">{e.kind}</span>
            <span className="text-dim-500"> · </span>
            <span className="text-dim-200">
              {String(e.payload.message ?? "")}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function SnapshotRawPanel({
  snapshot,
}: {
  snapshot: Record<string, unknown> | null;
}) {
  if (!snapshot) return null;
  return (
    <Panel title="Snapshot (raw)">
      <details>
        <summary className="text-[11px] font-mono text-dim-400 cursor-pointer hover:text-white">
          Show JSON ({JSON.stringify(snapshot).length} chars)
        </summary>
        <pre className="mt-3 text-[10.5px] font-mono text-dim-300 bg-ink-950 border border-ink-600/40 rounded-sm p-3 overflow-x-auto max-h-[60vh] overflow-y-auto">
          {JSON.stringify(snapshot, null, 2)}
        </pre>
      </details>
    </Panel>
  );
}

function Panel({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <section className="border border-ink-600/70 rounded-md bg-ink-900 p-5">
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-[11px] font-mono uppercase tracking-[0.18em] text-dim-300">
          {title}
        </h2>
        {count !== undefined && (
          <span className="text-[10px] font-mono text-dim-500 tabular">[{count}]</span>
        )}
      </div>
      {children}
    </section>
  );
}
