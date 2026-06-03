"use client";

import { useMemo, useState } from "react";

import { RISK_FLAGS, type Decision } from "@/lib/data";
import { useDecisions } from "@/lib/hooks/use-decisions";
import { useCycleDetail } from "@/lib/agent-store-context";
import type { CycleDetail, EventRow, ExecutionRow } from "@/lib/agent-api";
import { ipfsGateway, mantleExplorerTx } from "@/lib/explorer";
import { HashChip, Icon, SectionHead, Tag } from "@/components/ui";
import { ThesisView } from "@/components/thesis-view";

export function DecisionLog() {
  const { decisions } = useDecisions();
  const [filter, setFilter] = useState<"all" | "week" | "conf" | "profit">("all");
  const [openIds, setOpenIds] = useState<Set<string>>(() => {
    return decisions.length > 0 ? new Set([decisions[0].id]) : new Set();
  });

  const toggle = (id: string) => {
    setOpenIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  const filtered = useMemo(() => {
    if (filter === "week") return decisions.filter((d) => !d.ago.includes("d"));
    if (filter === "conf") return decisions.filter((d) => d.confidence >= 0.7);
    if (filter === "profit") return decisions.filter((d) => d.profitable);
    return decisions;
  }, [filter, decisions]);

  const expandAll = () => setOpenIds(new Set(filtered.map((d) => d.id)));
  const collapseAll = () => setOpenIds(new Set());

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-5">
        <SectionHead
          eyebrow="Agent Decision Log"
          title="Every move, on-chain, with rationale"
          subtitle="A complete audit trail. Each entry ties a one-line summary to the full thesis, the risk flags evaluated, Allora signals consumed, an IPFS-pinned proof, and the executing transaction."
        />

        <RiskFlagLegend />

        <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
          <div className="flex flex-wrap items-center gap-1.5 bg-ink-900 border border-ink-600/70 rounded-sm p-1">
            {(
              [
                { id: "all", label: "All", count: decisions.length },
                { id: "week", label: "This Week", count: decisions.filter((d) => !d.ago.includes("d")).length },
                {
                  id: "conf",
                  label: "High Confidence",
                  count: decisions.filter((d) => d.confidence >= 0.7).length,
                },
                { id: "profit", label: "Profitable", count: decisions.filter((d) => d.profitable).length },
              ] as const
            ).map((t) => (
              <button
                key={t.id}
                onClick={() => setFilter(t.id)}
                className={`px-3 h-8 rounded-sm text-[12px] font-mono inline-flex items-center gap-2 transition-colors
                  ${
                    filter === t.id
                      ? "bg-ink-700 text-white"
                      : "text-dim-300 hover:text-white hover:bg-ink-800"
                  }`}
              >
                {t.label}
                <span className={`text-[10.5px] tabular ${filter === t.id ? "text-neon" : "text-dim-500"}`}>
                  {t.count}
                </span>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <div className="hidden md:flex items-center gap-2 px-3 h-9 border border-ink-600/70 rounded-sm bg-ink-900 text-[12px] font-mono text-dim-400 min-w-[220px]">
              <Icon.Search />
              <input
                type="text"
                placeholder="Search by tx, asset, or rationale…"
                className="bg-transparent outline-none flex-1 text-white placeholder:text-dim-500"
              />
              <span className="text-dim-600 text-[10px]">⌘K</span>
            </div>
            <button
              onClick={expandAll}
              className="px-2.5 h-9 border border-ink-600/70 rounded-sm bg-ink-900 text-[11px] font-mono text-dim-300 hover:text-white hover:bg-ink-800"
            >
              Expand all
            </button>
            <button
              onClick={collapseAll}
              className="px-2.5 h-9 border border-ink-600/70 rounded-sm bg-ink-900 text-[11px] font-mono text-dim-300 hover:text-white hover:bg-ink-800"
            >
              Collapse
            </button>
          </div>
        </div>

        <div className="grid grid-cols-2 lg:grid-cols-5 gap-px bg-ink-600/60 border border-ink-600/70 rounded-md overflow-hidden">
          <SummaryCell label="Showing" value={`${filtered.length} / ${decisions.length}`} />
          <SummaryCell
            label="Avg confidence"
            value={(filtered.reduce((s, d) => s + d.confidence, 0) / (filtered.length || 1)).toFixed(2)}
          />
          <SummaryCell
            label="Profitable"
            value={`${filtered.filter((d) => d.profitable).length} / ${filtered.length}`}
            tone="green"
          />
          <SummaryCell
            label="Avg exec time"
            value={`${(
              filtered.reduce((s, d) => s + parseFloat(d.exec), 0) / (filtered.length || 1)
            ).toFixed(1)}s`}
          />
          <SummaryCell
            label="Risk distribution"
            value={
              <span className="font-mono tabular">
                <span className="text-neon">{filtered.filter((d) => d.risk === "LOW").length}L</span>
                <span className="text-dim-500"> · </span>
                <span className="text-warn">{filtered.filter((d) => d.risk === "MED").length}M</span>
                <span className="text-dim-500"> · </span>
                <span className="text-danger">{filtered.filter((d) => d.risk === "HIGH").length}H</span>
              </span>
            }
          />
        </div>
      </div>

      <div className="relative">
        <div className="absolute top-0 bottom-0 left-[42px] sm:left-[156px] w-px bg-ink-600/60 pointer-events-none" />

        <div className="space-y-2">
          {filtered.map((d, i) => (
            <DecisionItem
              key={d.id}
              d={d}
              first={i === 0}
              open={openIds.has(d.id)}
              onToggle={() => toggle(d.id)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function SummaryCell({
  label,
  value,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "green" | "blue";
}) {
  const t = tone === "green" ? "text-neon" : tone === "blue" ? "text-elec" : "text-white";
  return (
    <div className="bg-ink-900 px-4 py-3">
      <div className="text-[9.5px] uppercase tracking-[0.18em] font-mono text-dim-500">{label}</div>
      <div className={`font-mono tabular text-base mt-1 ${t}`}>{value}</div>
    </div>
  );
}

function DecisionItem({
  d,
  open,
  onToggle,
  first,
}: {
  d: Decision;
  open: boolean;
  onToggle: () => void;
  first: boolean;
}) {
  const riskTone: "green" | "warn" | "red" = d.risk === "LOW" ? "green" : d.risk === "MED" ? "warn" : "red";
  const summaryHasNoOp = /held|no-op/i.test(d.summary);
  // Off-chain rich detail fetched lazily — only when the row is open
  // and the join produced a cycle_ts (live row, not a mock fallback).
  const detailQuery = useCycleDetail(open && d.cycleTs ? d.cycleTs : null);
  return (
    <div className="relative flex gap-3 sm:gap-5">
      <div className="w-[42px] sm:w-[156px] shrink-0 pt-3.5 relative">
        <div className="hidden sm:block font-mono text-[11px] text-dim-400 tabular leading-tight">
          {d.ts.split(" ")[0]}
          <br />
          <span className="text-dim-500">{d.ts.split(" ")[1]}</span>
        </div>
        <div className="sm:hidden font-mono text-[10px] text-dim-500 tabular text-right pr-1">{d.ago}</div>
        <div className="absolute top-[18px] left-[42px] sm:left-[156px] -translate-x-1/2 z-10">
          <div
            className={`w-3 h-3 rounded-full border-2 ${
              open ? "bg-neon border-neon" : first ? "bg-ink-900 border-neon" : "bg-ink-900 border-ink-500"
            }`}
          />
        </div>
      </div>

      <div className="flex-1 min-w-0 pb-2">
        <button
          type="button"
          onClick={onToggle}
          className={`w-full text-left bg-ink-900 border rounded-md transition-all
            ${
              open
                ? "border-neon/40 shadow-[0_0_0_1px_rgba(0,255,136,0.15)]"
                : "border-ink-600/70 hover:border-ink-500"
            }`}
        >
          <div className="flex items-center gap-3 sm:gap-4 px-4 sm:px-5 py-3.5">
            <HashChip hash={d.full || d.id} head={6} tail={4} />
            <div className="hidden sm:block text-dim-600 font-mono text-[11px]">{d.ago}</div>
            <div className="flex-1 text-sm sm:text-[15px] text-white min-w-0 truncate">
              {summaryHasNoOp ? <span className="text-dim-300">{d.summary}</span> : d.summary}
            </div>
            <Tag tone={riskTone}>RISK: {d.risk}</Tag>
            <Tag tone="mono" className="hidden md:inline-flex">
              EXEC: {d.exec}
            </Tag>
            <ConfidenceBadge value={d.confidence} />
            <span
              className={`hidden sm:inline-flex items-center gap-1.5 text-[12px] font-mono transition-colors ${
                open ? "text-neon" : "text-dim-400"
              }`}
            >
              {open ? "Hide" : "Read"} rationale
              <Icon.Chev className={`transition-transform ${open ? "rotate-90" : ""}`} />
            </span>
          </div>
        </button>

        {open && (
          <DecisionThesis
            d={d}
            detail={detailQuery.data ?? null}
            detailLoading={detailQuery.isLoading}
          />
        )}
      </div>
    </div>
  );
}

function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = value >= 0.75 ? "#00FF88" : value >= 0.6 ? "#F7B955" : "#7A8499";
  return (
    <div className="hidden lg:flex items-center gap-2 font-mono text-[11px] text-dim-400">
      <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">conf</span>
      <div className="w-12 h-1 bg-ink-700 rounded-sm overflow-hidden">
        <div className="h-full" style={{ width: pct + "%", background: color }} />
      </div>
      <span className="tabular w-7 text-right" style={{ color }}>
        {value.toFixed(2)}
      </span>
    </div>
  );
}

type DecisionBlob = {
  thesis?: string;
  venues?: Array<{
    venue_id?: string;
    weight?: number;
    picks?: Array<{ product_id?: string; weight?: number; notes?: string[] }>;
  }>;
  risk_flags?: string[];
  notes?: string[];
  _meta?: { _validator?: { ok?: boolean; errors?: string[] } };
};

function asDecisionBlob(value: Record<string, unknown> | null | undefined): DecisionBlob | null {
  if (!value || typeof value !== "object") return null;
  return value as DecisionBlob;
}

function DecisionThesis({
  d,
  detail,
  detailLoading,
}: {
  d: Decision;
  detail?: CycleDetail | null;
  detailLoading?: boolean;
}) {
  // On-chain rows expose `cycleTs`; mock rows don't. The rich-detail
  // sections only render when we expected a cycle (`cycleTs`) and the
  // detail has either arrived or is on its way.
  const expectsDetail = Boolean(d.cycleTs);
  const blob = asDecisionBlob(detail?.decision);
  const venueRows = (blob?.venues ?? []).filter((v) => v.venue_id);
  const validator = blob?._meta?._validator;
  const events = detail?.events ?? [];
  const executions = detail?.executions ?? [];

  // Prefer the real on-chain rationale once it's loaded; otherwise fall
  // back to the row-derived placeholder / mock thesis.
  const thesisBody = blob?.thesis ?? d.thesis;

  // Risk flags: prefer the structured list from the decision blob.
  const flagKeys = blob?.risk_flags && blob.risk_flags.length > 0 ? blob.risk_flags : d.flags || [];
  const flagsMeta = flagKeys
    .map((k) => RISK_FLAGS.find((r) => r.key === k))
    .filter((f): f is NonNullable<typeof f> => Boolean(f));
  return (
    <div className="border-t border-ink-600/40 bg-ink-850/60 rounded-b-md fade-up">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-px bg-ink-600/40">
        <div className="lg:col-span-2 bg-ink-900 p-5 sm:p-6">
          {expectsDetail && events.length > 0 && (
            <div className="mb-5">
              <WatcherEventsBlock
                events={events.slice(0, 5)}
                title="Events that triggered this cycle"
              />
            </div>
          )}
          <div>
            <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-2">
              Thesis
            </div>
            <ThesisView body={thesisBody} />
          </div>
          <div className="mt-5">
            <ThesisBlock
              title="Risk flags"
              body={d.risks}
              accent={d.risks === "None." ? "green" : "warn"}
            />
            {flagsMeta.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {flagsMeta.map((f) => (
                  <span
                    key={f.key}
                    className="inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded-sm border border-warn/30 bg-warn/10 text-warn font-mono text-[10px]"
                  >
                    <span className="opacity-70">⚑</span>
                    {f.key}
                  </span>
                ))}
              </div>
            )}
          </div>
          <div className="mt-5">
            <ThesisBlock title="Allora signal used" body={d.allora} accent="elec" />
          </div>

          {expectsDetail && (
            <div className="mt-6 space-y-5">
              {detailLoading && !detail && (
                <div className="text-[11px] font-mono text-dim-500">loading off-chain detail…</div>
              )}
              {venueRows.length > 0 && <VenueAllocationsBlock venues={venueRows} />}
              {executions.length > 0 && <ExecutionsBlock executions={executions} />}
              {validator && <ValidatorStatusBlock validator={validator} />}
            </div>
          )}
        </div>
        <div className="bg-ink-900 p-5 sm:p-6 space-y-4">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-2">Confidence</div>
            <div className="flex items-baseline gap-2">
              <div className="font-mono text-3xl text-white tabular">{d.confidence.toFixed(2)}</div>
              <div className="text-[11px] text-dim-500 font-mono">/ 1.00</div>
            </div>
            <div className="mt-2 h-1.5 bg-ink-700 rounded-sm overflow-hidden">
              <div className="h-full bg-neon" style={{ width: d.confidence * 100 + "%" }} />
            </div>
            <div className="mt-2 text-[10.5px] text-dim-500 font-mono">policy threshold 0.55</div>
          </div>

          <div className="border-t border-ink-600/40 pt-4 space-y-2.5">
            <ProofRow label="IPFS proof" hash={d.ipfs} href={d.ipfs ? ipfsGateway(d.ipfs) : undefined} />
            <ProofRow
              label="On-chain tx"
              hash={d.tx}
              href={d.tx?.startsWith("0x") ? mantleExplorerTx(d.tx) : undefined}
            />
            <ProofRow label="Decision ID" hash={d.full} />
          </div>

          {d.tx?.startsWith("0x") && (
            <a
              href={mantleExplorerTx(d.tx)}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-2 inline-flex items-center gap-1.5 text-[12px] font-mono text-elec hover:text-elec-soft"
            >
              Verify on Mantle Explorer <Icon.Ext />
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

const VENUE_LABELS: Record<string, string> = {
  cash_usdc: "Cash USDC",
  aave_usdc: "Aave V3 USDC",
  aave_weth: "Aave V3 WETH",
  bybit_flex: "Bybit Flex",
  bybit_onchain: "Bybit OnChain",
  bybit_lm: "Bybit LM",
  bybit_dual_asset: "Bybit DualAsset",
  bybit_discount_buy: "Bybit DiscountBuy",
  bybit_hold_to_earn: "Bybit Hold-to-Earn",
};

function venueLabel(id: string): string {
  return VENUE_LABELS[id] ?? id;
}

function VenueAllocationsBlock({
  venues,
}: {
  venues: NonNullable<DecisionBlob["venues"]>;
}) {
  return (
    <div>
      <SubBlockTitle>Venue allocation + picks</SubBlockTitle>
      <div className="space-y-3">
        {venues.map((v, i) => {
          const id = v.venue_id ?? `venue-${i}`;
          const weight = typeof v.weight === "number" ? v.weight : 0;
          const picks = v.picks ?? [];
          return (
            <div key={id} className="bg-ink-850/60 border border-ink-600/40 rounded-sm overflow-hidden">
              <div className="flex items-center justify-between px-3 py-2 bg-ink-900/40 border-b border-ink-600/40">
                <span className="text-[12.5px] text-white">{venueLabel(id)}</span>
                <span className="font-mono text-[11px] text-neon tabular">
                  {(weight * 100).toFixed(1)}%
                </span>
              </div>
              {picks.length > 0 ? (
                <div className="px-3 py-2 space-y-1.5">
                  {picks.map((p, pi) => {
                    const pickWeight = typeof p.weight === "number" ? p.weight : 0;
                    const note = p.notes?.[0];
                    return (
                      <div key={`${id}/${p.product_id ?? pi}`} className="text-[11.5px]">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono text-dim-300">
                            #{p.product_id ?? "?"}
                          </span>
                          <span className="font-mono text-[10.5px] text-dim-400 tabular">
                            w={pickWeight.toFixed(2)}
                          </span>
                        </div>
                        {note && (
                          <div className="text-[11px] text-dim-400 leading-snug mt-0.5">{note}</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="px-3 py-2 text-[11px] text-dim-500 font-mono">no picks</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ExecutionsBlock({ executions }: { executions: ExecutionRow[] }) {
  return (
    <div>
      <SubBlockTitle>Executions</SubBlockTitle>
      <div className="space-y-1.5">
        {executions.map((e) => {
          const tone =
            e.status === "success" ? "text-neon" : e.status === "error" ? "text-danger" : "text-dim-300";
          const kind =
            typeof e.action === "object" && e.action !== null && "kind" in e.action
              ? String((e.action as { kind?: unknown }).kind ?? "action")
              : "action";
          return (
            <div
              key={e.idx}
              className="flex items-start gap-3 text-[12px] font-mono bg-ink-850/40 border border-ink-600/30 rounded-sm px-3 py-1.5"
            >
              <span className="text-dim-500 tabular w-5 text-right">{e.idx}</span>
              <span className="text-dim-300 flex-1 min-w-0 truncate">{kind}</span>
              <span className={`uppercase text-[10.5px] tracking-[0.14em] ${tone}`}>{e.status}</span>
              {e.error && (
                <span className="text-[10.5px] text-danger/80 max-w-[40%] truncate" title={e.error}>
                  {e.error}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function WatcherEventsBlock({
  events,
  title = "Watcher events that triggered this cycle",
}: {
  events: EventRow[];
  title?: string;
}) {
  return (
    <div>
      <SubBlockTitle>{title}</SubBlockTitle>
      <div className="space-y-1.5">
        {events.map((ev) => {
          const tone =
            ev.severity === "red"
              ? "text-danger border-danger/30 bg-danger/5"
              : ev.severity === "warn"
                ? "text-warn border-warn/30 bg-warn/5"
                : "text-dim-300 border-ink-600/40 bg-ink-850/40";
          return (
            <div
              key={ev.id}
              className={`flex items-center gap-3 text-[12px] font-mono border rounded-sm px-3 py-1.5 ${tone}`}
            >
              <span className="text-[10.5px] uppercase tracking-[0.14em] opacity-80">{ev.kind}</span>
              {ev.coin && <span className="text-[10.5px] opacity-80">{ev.coin}</span>}
              <span className="text-[10.5px] tabular text-dim-500 ml-auto">
                {new Date(ev.event_ts).toISOString().slice(11, 19)} UTC
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ValidatorStatusBlock({
  validator,
}: {
  validator: NonNullable<NonNullable<DecisionBlob["_meta"]>["_validator"]>;
}) {
  const ok = validator.ok === true;
  const errors = validator.errors ?? [];
  return (
    <div>
      <SubBlockTitle>Validator</SubBlockTitle>
      {ok ? (
        <div className="text-[12px] font-mono text-neon">validator passed — all hard caps respected</div>
      ) : (
        <div className="space-y-1.5">
          <div className="text-[12px] font-mono text-danger">validator rejected · {errors.length} issue(s)</div>
          {errors.map((err, i) => (
            <div
              key={i}
              className="text-[11.5px] text-danger/80 bg-danger/5 border border-danger/30 rounded-sm px-3 py-1.5 leading-snug"
            >
              {err}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SubBlockTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-2">
      {children}
    </div>
  );
}

function ThesisBlock({
  title,
  body,
  accent = "white",
}: {
  title: string;
  body: string;
  accent?: "white" | "green" | "elec" | "warn";
}) {
  const colors = {
    white: "text-white",
    green: "text-neon",
    elec: "text-elec",
    warn: "text-warn",
  };
  return (
    <div>
      <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-2">{title}</div>
      <div className={`text-[14px] leading-relaxed ${colors[accent]}`}>{body}</div>
    </div>
  );
}

function ProofRow({ label, hash, href }: { label: string; hash: string; href?: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[10.5px] font-mono uppercase tracking-[0.14em] text-dim-500">{label}</span>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-white"
        >
          <HashChip hash={hash} head={7} tail={5} />
        </a>
      ) : (
        <HashChip hash={hash} head={7} tail={5} />
      )}
    </div>
  );
}

function RiskFlagLegend() {
  const [open, setOpen] = useState(false);
  return (
    <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left hover:bg-ink-850/60"
      >
        <div className="flex items-center gap-2.5">
          <span className="text-[10px] font-mono uppercase tracking-[0.18em] text-dim-500">
            Risk flag taxonomy
          </span>
          <span className="text-[12px] text-dim-300">{RISK_FLAGS.length} flags monitored continuously</span>
        </div>
        <Icon.Chev className={`text-dim-400 transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-px bg-ink-600/40 border-t border-ink-600/60 fade-up">
          {RISK_FLAGS.map((f) => (
            <div key={f.key} className="bg-ink-900 px-4 py-3">
              <div className="flex items-start justify-between gap-3">
                <div className="font-mono text-[12px] text-white">{f.key}</div>
                <span
                  className={`text-[9.5px] font-mono uppercase tracking-[0.14em] ${
                    f.tone === "red" ? "text-danger" : "text-warn"
                  }`}
                >
                  {f.tone === "red" ? "halt" : "warn"}
                </span>
              </div>
              <div className="text-[12px] text-dim-300 mt-0.5">{f.label}</div>
              <div className="text-[10.5px] text-dim-500 font-mono mt-1">{f.thresh}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
