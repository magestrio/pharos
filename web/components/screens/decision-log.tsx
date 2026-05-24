"use client";

import { useMemo, useState } from "react";

import { DECISIONS, RISK_FLAGS, type Decision } from "@/lib/data";
import { HashChip, Icon, SectionHead, Tag } from "@/components/ui";

export function DecisionLog() {
  const [filter, setFilter] = useState<"all" | "week" | "conf" | "profit">("all");
  const [openIds, setOpenIds] = useState<Set<string>>(new Set([DECISIONS[0].id]));

  const toggle = (id: string) => {
    setOpenIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  const filtered = useMemo(() => {
    if (filter === "week") return DECISIONS.filter((d) => !d.ago.includes("d"));
    if (filter === "conf") return DECISIONS.filter((d) => d.confidence >= 0.7);
    if (filter === "profit") return DECISIONS.filter((d) => d.profitable);
    return DECISIONS;
  }, [filter]);

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
                { id: "all", label: "All", count: DECISIONS.length },
                { id: "week", label: "This Week", count: DECISIONS.filter((d) => !d.ago.includes("d")).length },
                {
                  id: "conf",
                  label: "High Confidence",
                  count: DECISIONS.filter((d) => d.confidence >= 0.7).length,
                },
                { id: "profit", label: "Profitable", count: DECISIONS.filter((d) => d.profitable).length },
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
          <SummaryCell label="Showing" value={`${filtered.length} / ${DECISIONS.length}`} />
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

        {open && <DecisionThesis d={d} />}
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

function DecisionThesis({ d }: { d: Decision }) {
  const flagsMeta = (d.flags || [])
    .map((k) => RISK_FLAGS.find((r) => r.key === k))
    .filter((f): f is NonNullable<typeof f> => Boolean(f));
  return (
    <div className="border-t border-ink-600/40 bg-ink-850/60 rounded-b-md fade-up">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-px bg-ink-600/40">
        <div className="lg:col-span-2 bg-ink-900 p-5 sm:p-6">
          <ThesisBlock title="Thesis" body={d.thesis} accent="white" />
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
            <ProofRow label="IPFS proof" hash={d.ipfs} />
            <ProofRow label="On-chain tx" hash={d.tx} />
            <ProofRow label="Decision ID" hash={d.full} />
          </div>

          <a
            href="#"
            className="mt-2 inline-flex items-center gap-1.5 text-[12px] font-mono text-elec hover:text-elec-soft"
          >
            Verify on Mantle Explorer <Icon.Ext />
          </a>
        </div>
      </div>
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

function ProofRow({ label, hash }: { label: string; hash: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[10.5px] font-mono uppercase tracking-[0.14em] text-dim-500">{label}</span>
      <HashChip hash={hash} head={7} tail={5} />
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
