"use client";

import { useMemo, useState } from "react";

import { RISK_FLAGS, type Decision } from "@/lib/data";
import { formatTime } from "@/lib/datetime";
import { useDecisions } from "@/lib/hooks/use-decisions";
import { useCycleDetail } from "@/lib/agent-store-context";
import type { CycleDetail, EventRow, ExecutionRow } from "@/lib/agent-api";
import { ipfsGateway, mantleExplorerTx } from "@/lib/explorer";
import { Button, Eyebrow, HashChip, Icon, SectionHead, Tag } from "@/components/ui";
import { ThesisView } from "@/components/thesis-view";

export function DecisionLog() {
  const { decisions, isLoading } = useDecisions();
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
          <div className="flex flex-wrap items-center gap-2">
            {(
              [
                { id: "all", label: "All", count: decisions.length },
                { id: "week", label: "This Week", count: decisions.filter((d) => !d.ago.includes("d")).length },
                {
                  id: "conf",
                  label: "High Conf",
                  count: decisions.filter((d) => d.confidence >= 0.7).length,
                },
                { id: "profit", label: "Profitable", count: decisions.filter((d) => d.profitable).length },
              ] as const
            ).map((t) => (
              <Button
                key={t.id}
                variant="terminal"
                size="sm"
                active={filter === t.id}
                onClick={() => setFilter(t.id)}
              >
                {t.label}
                <span
                  className={`text-[10px] tabular tracking-normal ${filter === t.id ? "text-accent" : "text-dim-500"}`}
                >
                  · {t.count}
                </span>
              </Button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <div className="hidden md:flex items-center gap-2 px-3 h-9 border border-ink-600/70 rounded-[3px] bg-ink-900 text-[12px] font-mono text-dim-400 min-w-[220px]">
              <Icon.Search />
              <input
                type="text"
                placeholder="Search by tx, asset, or rationale…"
                className="bg-transparent outline-none flex-1 text-white placeholder:text-dim-500"
              />
              <span className="text-dim-600 text-[10px]">⌘K</span>
            </div>
            <Button variant="terminal" size="sm" onClick={expandAll}>
              Expand all
            </Button>
            <Button variant="terminal" size="sm" onClick={collapseAll}>
              Collapse
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-2 lg:grid-cols-5 gap-px bg-ink-600/40 border border-ink-600/70 rounded-md overflow-hidden">
          <SummaryCell label="Showing" value={`${filtered.length} / ${decisions.length}`} />
          <SummaryCell
            label="Avg confidence"
            value={(filtered.reduce((s, d) => s + d.confidence, 0) / (filtered.length || 1)).toFixed(2)}
          />
          <SummaryCell
            label="Profitable"
            value={`${filtered.filter((d) => d.profitable).length} / ${filtered.length}`}
            tone="pos"
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
              <span className="font-serif tabular">
                <span className="text-pos">{filtered.filter((d) => d.risk === "LOW").length}L</span>
                <span className="text-dim-500"> · </span>
                <span className="text-accent">{filtered.filter((d) => d.risk === "MED").length}M</span>
                <span className="text-dim-500"> · </span>
                <span className="text-danger">{filtered.filter((d) => d.risk === "HIGH").length}H</span>
              </span>
            }
          />
        </div>
      </div>

      <div className="relative">
        <div className="absolute top-0 bottom-0 left-[42px] sm:left-[156px] w-px bg-gradient-to-b from-accent/40 via-ink-600/60 to-transparent pointer-events-none" />

        <div className="space-y-2">
          {filtered.length === 0 && (
            <div className="ml-[42px] sm:ml-[156px] bg-ink-900 border border-ink-600/70 rounded-md px-5 py-8 text-center text-[12px] font-mono text-dim-400">
              {isLoading
                ? "Loading agent decisions…"
                : decisions.length === 0
                  ? "No decisions recorded yet — the log fills in once the agent completes its first cycle."
                  : "No decisions match this filter."}
            </div>
          )}
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
  tone?: "pos" | "accent" | "blue";
}) {
  const t =
    tone === "pos"
      ? "text-pos"
      : tone === "accent"
        ? "text-accent"
        : tone === "blue"
          ? "text-elec"
          : "text-white";
  return (
    <div className="bg-ink-900 px-4 py-4">
      <Eyebrow tone="dim">{label}</Eyebrow>
      <div className={`font-serif tabular text-[22px] leading-none mt-2 tracking-[-0.02em] ${t}`}>
        {value}
      </div>
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
  // Risk LOW reads better as teal `pos` — green = "low risk, safe" stays
  // intuitive, and we free amber up for brand accents.
  const riskTone: "pos" | "warn" | "red" =
    d.risk === "LOW" ? "pos" : d.risk === "MED" ? "warn" : "red";
  const summaryHasNoOp = /held|no-op/i.test(d.summary);
  // Off-chain rich detail fetched lazily — only when the row is open
  // and the join produced a cycle_ts (live row, not a mock fallback).
  const detailQuery = useCycleDetail(open && d.cycleTs ? d.cycleTs : null);
  return (
    <div className="relative flex gap-3 sm:gap-5">
      <div className="w-[42px] sm:w-[156px] shrink-0 pt-3.5 relative">
        <div className="hidden sm:block font-mono text-[11px] text-dim-400 tabular leading-tight">
          {d.dateLabel}
          <br />
          <span className="text-dim-500">{d.timeLabel}</span>
        </div>
        <div className="sm:hidden font-mono text-[10px] text-dim-500 tabular text-right pr-1">{d.ago}</div>
        <div className="absolute top-[20px] left-[42px] sm:left-[156px] -translate-x-1/2 z-10">
          <div
            className={`w-3 h-3 rounded-full border-2 transition-all ${
              open
                ? "bg-accent border-accent shadow-[0_0_12px_rgba(245,180,0,0.6)]"
                : first
                  ? "bg-ink-900 border-accent"
                  : "bg-ink-900 border-ink-500"
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
                ? "border-accent/40 shadow-[0_0_0_1px_rgba(245,180,0,0.18),0_8px_24px_-12px_rgba(245,180,0,0.30)] bg-gradient-to-b from-ink-850/80 to-ink-900"
                : "border-ink-600/70 hover:border-ink-500 hover:bg-ink-900/80"
            }`}
        >
          <div className="flex items-center gap-3 sm:gap-4 px-4 sm:px-5 py-4">
            <HashChip hash={d.full || d.id} head={6} tail={4} />
            <div className="hidden sm:block text-dim-600 font-mono text-[10.5px] tabular">{d.ago}</div>
            <div className="flex-1 text-[15px] sm:text-[17px] leading-snug text-white min-w-0 line-clamp-2">
              {summaryHasNoOp ? <span className="text-dim-300">{d.summary}</span> : d.summary}
            </div>
            <Tag tone={riskTone}>RISK · {d.risk}</Tag>
            <Tag tone="mono" className="hidden md:inline-flex">
              EXEC · {d.exec}
            </Tag>
            <ConfidenceBadge value={d.confidence} />
            <span
              className={`hidden sm:inline-flex items-center gap-1.5 text-[11px] font-mono uppercase tracking-[0.14em] transition-colors ${
                open ? "text-accent" : "text-dim-400"
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
  // High-confidence = brand amber (this is the agent's "I'm sure"),
  // medium = warn ochre, low = dim. Threshold 0.6/0.75 matches the
  // legend the validator uses internally.
  const color = value >= 0.75 ? "#F5B400" : value >= 0.6 ? "#F7B955" : "#7A8499";
  return (
    <div className="hidden lg:flex items-center gap-2.5 font-mono text-[11px] text-dim-400">
      <span className="text-dim-500 uppercase tracking-[0.16em] text-[9.5px]">conf</span>
      <div className="w-20 h-1.5 bg-ink-700 rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{
            width: pct + "%",
            background: `linear-gradient(90deg, ${color}55, ${color})`,
            boxShadow: `0 0 8px ${color}55`,
          }}
        />
      </div>
      <span className="tabular text-[12px] w-8 text-right" style={{ color }}>
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

/**
 * Extract product_ids that the deterministic cooldown filter stripped
 * post-LLM (`bybit-sandbox.61`). The filter annotates `decision.notes`
 * with `"cooldown_filter dropped re-picked pids: 497,123"` whenever it
 * fires. Returns [] when no such note is present.
 */
function cooldownPidsFromNotes(notes: string[] | undefined): string[] {
  if (!notes) return [];
  for (const n of notes) {
    if (typeof n !== "string") continue;
    const m = /^cooldown_filter dropped re-picked pids:\s*(.+)$/.exec(n);
    if (m) {
      return m[1]
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    }
  }
  return [];
}

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
  const cooldownPids = cooldownPidsFromNotes(blob?.notes);
  return (
    <div className="border-t border-ink-600/40 bg-ink-850/60 rounded-b-md fade-up">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-px bg-ink-600/30">
        <div className="lg:col-span-2 bg-ink-900 p-6 sm:p-8 space-y-7">
          {expectsDetail && events.length > 0 && (
            <WatcherEventsBlock
              events={events.slice(0, 5)}
              title="Events that triggered this cycle"
            />
          )}
          <div className="space-y-2.5">
            <Eyebrow tone="accent">TL;DR</Eyebrow>
            <p className="font-serif text-[18px] leading-[1.4] text-white border-l-2 border-accent pl-4 max-w-[68ch]">
              {d.summary}
            </p>
          </div>
          <div className="max-w-[68ch]">
            <ThesisView body={thesisBody} />
          </div>
          <div>
            <ThesisBlock
              title="Risk flags"
              body={d.risks}
              accent={d.risks === "None." ? "pos" : "warn"}
            />
            {flagsMeta.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {flagsMeta.map((f) => (
                  <Tag key={f.key} tone="warn">
                    <span className="opacity-70">⚑</span>
                    {f.key}
                  </Tag>
                ))}
              </div>
            )}
          </div>
          {d.allora && d.allora.trim() !== "—" && d.allora.trim() !== "" && (
            <div>
              <ThesisBlock title="Allora signal used" body={d.allora} accent="elec" />
            </div>
          )}

          {cooldownPids.length > 0 && (
            <div>
              <Eyebrow tone="accent">Cooldown skip</Eyebrow>
              <p className="mt-2 text-[13px] text-dim-200 leading-relaxed max-w-[68ch]">
                The LLM tried to re-pick {cooldownPids.length === 1 ? "a product" : "products"} that
                the watcher just auto-closed. The deterministic filter dropped{" "}
                {cooldownPids.length === 1 ? "it" : "them"} and rolled the weight to
                cash to avoid ping-pong on Bybit fees + slippage.
              </p>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {cooldownPids.map((pid) => (
                  <Tag key={pid} tone="warn">
                    <span className="opacity-70">⏱</span>
                    COOLDOWN · {pid}
                  </Tag>
                ))}
              </div>
            </div>
          )}

          {expectsDetail && (
            <div className="space-y-6 pt-2">
              {detailLoading && !detail && (
                <div className="text-[11px] font-mono text-dim-500">loading off-chain detail…</div>
              )}
              {venueRows.length > 0 && <VenueAllocationsBlock venues={venueRows} />}
              {executions.length > 0 && <ExecutionsBlock executions={executions} />}
              {validator && <ValidatorStatusBlock validator={validator} />}
            </div>
          )}
        </div>
        <div className="bg-ink-900 p-6 sm:p-7 space-y-5">
          <div>
            <Eyebrow tone="dim">Confidence</Eyebrow>
            <div className="flex items-baseline gap-2 mt-2">
              <div className="font-serif text-[44px] leading-none text-white tabular tracking-[-0.03em]">
                {d.confidence.toFixed(2)}
              </div>
              <div className="text-[12px] text-dim-500 font-mono">/ 1.00</div>
            </div>
            <div className="mt-3 h-2 bg-ink-700 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full bg-gradient-to-r from-accent-dim via-accent to-accent-soft shadow-[0_0_10px_rgba(245,180,0,0.45)]"
                style={{ width: d.confidence * 100 + "%" }}
              />
            </div>
            <div className="mt-2 text-[10.5px] text-dim-500 font-mono uppercase tracking-[0.16em]">
              policy threshold 0.55
            </div>
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
              className="mt-2 inline-flex items-center gap-1.5 text-[12px] font-mono text-elec hover:text-elec-soft uppercase tracking-[0.14em]"
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
                <span className="font-mono text-[11px] text-accent tabular">
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
            e.status === "success" ? "text-pos" : e.status === "error" ? "text-danger" : "text-dim-300";
          const kind =
            typeof e.action === "object" && e.action !== null && "kind" in e.action
              ? String((e.action as { kind?: unknown }).kind ?? "action")
              : "action";
          return (
            <div
              key={e.idx}
              className="flex items-start gap-3 text-[12px] font-mono bg-ink-850/40 border border-ink-600/30 rounded-sm px-3 py-2"
            >
              <span className="text-dim-500 tabular w-5 text-right">{e.idx}</span>
              <span className="text-dim-300 flex-1 min-w-0 truncate">{kind}</span>
              <span className={`uppercase text-[10px] tracking-[0.16em] ${tone}`}>{e.status}</span>
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
                {formatTime(ev.event_ts)}
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
        <div className="text-[12px] font-mono text-pos">validator passed — all hard caps respected</div>
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
  return <Eyebrow tone="accent" className="mb-3">{children}</Eyebrow>;
}

function ThesisBlock({
  title,
  body,
  accent = "white",
}: {
  title: string;
  body: string;
  accent?: "white" | "pos" | "elec" | "warn";
}) {
  const colors = {
    white: "text-white",
    pos: "text-pos",
    elec: "text-elec",
    warn: "text-warn",
  };
  return (
    <div>
      <Eyebrow tone="dim" className="mb-2">{title}</Eyebrow>
      <div className={`text-[14px] leading-[1.55] ${colors[accent]}`}>{body}</div>
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
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-ink-850/60"
      >
        <div className="flex items-center gap-3">
          <Eyebrow tone="accent">Risk flag taxonomy</Eyebrow>
          <span className="text-[12.5px] text-dim-300">
            {RISK_FLAGS.length} flags monitored continuously
          </span>
        </div>
        <Icon.Chev className={`text-dim-400 transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-px bg-ink-600/40 border-t border-ink-600/60 fade-up">
          {RISK_FLAGS.map((f) => (
            <div key={f.key} className="bg-ink-900 px-4 py-3.5">
              <div className="flex items-start justify-between gap-3">
                <div className="font-mono text-[12px] text-white">{f.key}</div>
                <Tag tone={f.tone === "red" ? "red" : "warn"}>
                  {f.tone === "red" ? "halt" : "warn"}
                </Tag>
              </div>
              <div className="text-[13px] text-dim-300 mt-1.5 leading-snug">{f.label}</div>
              <div className="text-[10.5px] text-dim-500 font-mono mt-1.5">{f.thresh}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
