/**
 * Cycle history — server-rendered list (`data-store.6`).
 *
 * Hits the FastAPI `/cycles` endpoint on every request (no caching;
 * this is live data). Each row links to the detail panel at
 * `/history/[cycle_ts]`. Out of scope: charting / NAV plots (separate
 * follow-up).
 */
import Link from "next/link";

import { ApiError, fetchCycles, type CycleSummary } from "@/lib/agent-api";
import { formatDateTime } from "@/lib/datetime";
import { formatResult, formatWakeReason } from "@/lib/labels";
import { Card, Eyebrow, SectionHead, Tag } from "@/components/ui";

export const dynamic = "force-dynamic";
export const metadata = { title: "Pharos — Cycle History" };

export default async function HistoryPage() {
  let cycles: CycleSummary[] = [];
  let errorMessage: string | null = null;
  try {
    cycles = await fetchCycles({ limit: 100 });
  } catch (e) {
    errorMessage = renderApiError(e);
  }

  return (
    <main className="min-h-screen bg-ink-950 text-white">
      <header className="border-b border-ink-600/60 bg-ink-950/90 backdrop-blur sticky top-0 z-10">
        <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between">
          <Link
            href="/"
            className="font-mono text-[13px] text-white font-semibold tracking-tight hover:text-neon"
          >
            ← PHAROS
          </Link>
          <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-dim-500">
            Cycle History · {cycles.length} cycles
          </div>
        </div>
      </header>

      <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-8 sm:py-10">
        <SectionHead
          eyebrow="Decision cycles"
          title="Cycle History"
          subtitle="Every re-decide the agent has run — heartbeat and event-driven — with its wake trigger, outcome, confidence and expected APR. Click a row for the full thesis, positions and on-chain proofs."
          right={
            <Eyebrow tone="dim" className="tabular">
              {cycles.length} cycles
            </Eyebrow>
          }
        />
        {errorMessage ? (
          <ErrorBanner message={errorMessage} />
        ) : cycles.length === 0 ? (
          <EmptyState />
        ) : (
          <CycleTable rows={cycles} />
        )}
      </section>
    </main>
  );
}

function CycleTable({ rows }: { rows: CycleSummary[] }) {
  return (
    <Card className="overflow-hidden">
      <div className="grid grid-cols-12 gap-2 px-4 py-3 text-[10.5px] font-mono uppercase tracking-[0.14em] text-dim-500 border-b border-ink-600/70 bg-ink-850/60">
        <div className="col-span-3">Cycle</div>
        <div className="col-span-2">Wake reason</div>
        <div className="col-span-2">Result</div>
        <div className="col-span-1 text-right">Conf</div>
        <div className="col-span-1 text-right">APR%</div>
        <div className="col-span-1 text-right">Actions</div>
        <div className="col-span-2 text-right">Detail</div>
      </div>
      <div className="divide-y divide-ink-600/40">
        {rows.map((row) => (
          <CycleRow key={row.cycle_ts} row={row} />
        ))}
      </div>
    </Card>
  );
}

type Tone = "neutral" | "pos" | "warn" | "red" | "accent";

function resultTagTone(result: string): Tone {
  if (result === "executed") return "pos";
  if (result === "error") return "red";
  if (result.startsWith("skipped") || result === "halted") return "warn";
  return "neutral";
}

function CycleRow({ row }: { row: CycleSummary }) {
  const wakeIsEvent = row.wake_reason.startsWith("event:");
  return (
    <Link
      href={`/history/${encodeURIComponent(row.cycle_ts)}`}
      className="group grid grid-cols-12 gap-2 px-4 py-3 items-center text-[12px] font-mono hover:bg-ink-800/40 transition-colors"
    >
      <div className="col-span-3 text-dim-200 tabular truncate">
        {fmtTs(row.cycle_ts)}
      </div>
      <div className="col-span-2 truncate">
        <Tag tone={wakeIsEvent ? "accent" : "neutral"}>{formatWakeReason(row.wake_reason)}</Tag>
      </div>
      <div className="col-span-2">
        <Tag tone={resultTagTone(row.result)}>{formatResult(row.result)}</Tag>
      </div>
      <div className="col-span-1 text-right text-dim-300 tabular">
        {row.confidence !== null ? row.confidence.toFixed(2) : "—"}
      </div>
      <div className="col-span-1 text-right text-dim-300 tabular">
        {row.expected_apr_pct !== null ? row.expected_apr_pct.toFixed(2) : "—"}
      </div>
      <div className="col-span-1 text-right text-dim-300 tabular">
        {row.actions_executed ?? "—"}
        <span className="text-dim-600">/{row.actions_planned ?? "—"}</span>
      </div>
      <div className="col-span-2 text-right text-dim-500 group-hover:text-accent transition-colors">
        view →
      </div>
    </Link>
  );
}

function EmptyState() {
  return (
    <div className="border border-ink-600/70 rounded-md bg-ink-900 p-8 text-center">
      <div className="font-mono text-[12px] text-dim-300">No cycles recorded yet.</div>
      <div className="font-mono text-[11px] text-dim-500 mt-2">
        The agent will populate this list as it runs.
      </div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="border border-warn/40 rounded-md bg-warn/5 p-4">
      <div className="font-mono text-[12px] text-warn mb-1">
        Could not reach the agent API.
      </div>
      <div className="font-mono text-[11px] text-dim-400">{message}</div>
    </div>
  );
}

function renderApiError(e: unknown): string {
  if (e instanceof ApiError) return `${e.status} ${e.message} (${e.path})`;
  if (e instanceof Error) return e.message;
  return "Unknown error.";
}

function fmtTs(iso: string): string {
  // 2026-05-29T16:02:11+00:00 → Jun 8, 2026, 14:30 UTC
  return formatDateTime(iso);
}
