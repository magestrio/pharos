"use client";

import { useMemo, useState } from "react";

import {
  Card,
  Eyebrow,
  ErrorPanel,
  LineChart,
  LiveDot,
  SectionHead,
  SkeletonBox,
  Sparkline,
  fmtPct,
} from "@/components/ui";
import {
  useEarnExplorer,
  useFundingHistory,
  type EarnProductRow,
} from "@/lib/live";

const ACCENT = "#F6A94B";
const POS = "#4ADE80";
const DANGER = "#FB7185";

type SortKey = "apr" | "funding";

export function EarnExplorer() {
  const { data, isLoading, isError, error } = useEarnExplorer({ limit: 500 });
  const [category, setCategory] = useState<string>("");
  const [coin, setCoin] = useState<string>("");
  const [sortKey, setSortKey] = useState<SortKey>("apr");
  const [expanded, setExpanded] = useState<string | null>(null);

  const rows = data?.products ?? [];

  const categories = useMemo(
    () => Array.from(new Set(rows.map((r) => r.category))).sort(),
    [rows],
  );

  const filtered = useMemo(() => {
    const coinUp = coin.trim().toUpperCase();
    const out = rows.filter(
      (r) =>
        (!category || r.category === category) &&
        (!coinUp || r.coin.toUpperCase().includes(coinUp)),
    );
    out.sort((a, b) =>
      sortKey === "apr"
        ? b.effective_apr_pct - a.effective_apr_pct
        : (b.funding_annual_pct ?? -Infinity) - (a.funding_annual_pct ?? -Infinity),
    );
    return out;
  }, [rows, category, coin, sortKey]);

  return (
    <section className="space-y-6">
      <SectionHead
        eyebrow="Bybit Earn"
        title="Earn Explorer"
        subtitle="Every Bybit Earn product the agent sees each cycle — current APR, Bybit's daily APR history, and the perp funding rate for each coin."
        right={
          data?.captured_at ? (
            <div className="flex items-center gap-2 font-mono text-[11px] text-dim-400">
              <LiveDot />
              {new Date(data.captured_at).toLocaleString()}
            </div>
          ) : null
        }
      />

      <div className="flex flex-wrap items-center gap-3">
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="bg-ink-850 border border-ink-600/70 rounded-sm px-3 py-1.5 text-[12px] font-mono text-dim-200"
        >
          <option value="">All categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <input
          value={coin}
          onChange={(e) => setCoin(e.target.value)}
          placeholder="Filter coin (e.g. BTC)"
          className="bg-ink-850 border border-ink-600/70 rounded-sm px-3 py-1.5 text-[12px] font-mono text-dim-200 placeholder:text-dim-600 w-44"
        />
        <div className="flex items-center gap-1 ml-auto font-mono text-[11px] text-dim-500">
          <span>Sort:</span>
          <SortButton active={sortKey === "apr"} onClick={() => setSortKey("apr")}>
            APR
          </SortButton>
          <SortButton active={sortKey === "funding"} onClick={() => setSortKey("funding")}>
            Funding
          </SortButton>
        </div>
      </div>

      {isError ? (
        <ErrorPanel label="Failed to load Earn products" message={String(error)} />
      ) : (
        <Card className="p-0 overflow-hidden">
          <div className="grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 bg-ink-850 px-4 py-2.5">
            <div className="col-span-2">Coin</div>
            <div className="col-span-3">Category</div>
            <div className="col-span-2 text-right">APR</div>
            <div className="col-span-2">APR trend</div>
            <div className="col-span-3 text-right">Funding / yr</div>
          </div>

          {isLoading ? (
            <div className="p-4 space-y-2">
              {Array.from({ length: 8 }).map((_, i) => (
                <SkeletonBox key={i} className="h-7" />
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-dim-500 font-mono">
              No products match the current filter.
            </div>
          ) : (
            filtered.map((r) => {
              const key = `${r.category}/${r.product_id}`;
              return (
                <EarnRow
                  key={key}
                  row={r}
                  expanded={expanded === key}
                  onToggle={() => setExpanded(expanded === key ? null : key)}
                />
              );
            })
          )}
        </Card>
      )}
    </section>
  );
}

function SortButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-0.5 rounded-sm border text-[10.5px] uppercase tracking-[0.08em] ${
        active
          ? "border-accent/40 text-accent bg-accent/10"
          : "border-ink-600/60 text-dim-400 hover:text-dim-200"
      }`}
    >
      {children}
    </button>
  );
}

function fundingTone(annual: number | null): { color: string; tagTone: "pos" | "red" | "neutral" } {
  if (annual === null) return { color: "#7A8499", tagTone: "neutral" };
  return annual >= 0
    ? { color: POS, tagTone: "pos" }
    : { color: DANGER, tagTone: "red" };
}

function EarnRow({
  row,
  expanded,
  onToggle,
}: {
  row: EarnProductRow;
  expanded: boolean;
  onToggle: () => void;
}) {
  const ft = fundingTone(row.funding_annual_pct);
  return (
    <div className="border-t border-ink-600/30">
      <button
        onClick={onToggle}
        className="w-full grid grid-cols-12 items-center px-4 py-2.5 text-[12px] font-mono text-left hover:bg-ink-850/50 transition-colors"
      >
        <div className="col-span-2 text-white truncate pr-2">{row.coin}</div>
        <div className="col-span-3 text-dim-300 truncate pr-2">{row.category}</div>
        <div className="col-span-2 text-right tabular text-accent">
          {fmtPct(row.effective_apr_pct, { decimals: 2, sign: false })}
        </div>
        <div className="col-span-2 flex items-center">
          {row.apr_history_pct && row.apr_history_pct.length > 1 ? (
            <Sparkline series={row.apr_history_pct} width={90} height={22} />
          ) : (
            <span className="text-dim-600 text-[10px]">—</span>
          )}
        </div>
        <div className="col-span-3 text-right tabular" style={{ color: ft.color }}>
          {row.funding_annual_pct === null
            ? <span className="text-dim-600">n/a</span>
            : fmtPct(row.funding_annual_pct, { decimals: 1, sign: true })}
        </div>
      </button>
      {expanded && <EarnRowDetail row={row} />}
    </div>
  );
}

function EarnRowDetail({ row }: { row: EarnProductRow }) {
  const fundingQuery = useFundingHistory(coinForFunding(row.coin));
  const fundingPoints = (fundingQuery.data?.points ?? [])
    .map((p) => p.funding_annual_pct)
    .filter((v): v is number => v !== null);

  const aprSeries = row.apr_history_pct ?? [];
  const aprMean = aprSeries.length
    ? aprSeries.reduce((a, b) => a + b, 0) / aprSeries.length
    : 0;
  const fundingMean = fundingPoints.length
    ? fundingPoints.reduce((a, b) => a + b, 0) / fundingPoints.length
    : 0;

  return (
    <div className="px-4 pb-5 pt-2 bg-ink-950/40 grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div className="space-y-2">
        <Eyebrow tone="accent">APR history (Bybit daily)</Eyebrow>
        {aprSeries.length > 1 ? (
          <LineChart
            series={aprSeries}
            color={ACCENT}
            baseline={aprMean}
            baselineLabel="avg"
            width={520}
            height={180}
            yFormat={(v) => v.toFixed(2) + "%"}
            pad={{ t: 14, r: 14, b: 22, l: 52 }}
          />
        ) : (
          <EmptyChart note={`No daily APR series for ${row.category} products.`} />
        )}
        <div className="font-mono text-[10.5px] text-dim-500">
          source: {row.apr_source}
          {row.mark_price !== null && <> · mark ${row.mark_price.toLocaleString()}</>}
        </div>
      </div>

      <div className="space-y-2">
        <Eyebrow tone="accent">Funding rate / yr (per cycle)</Eyebrow>
        {fundingQuery.isLoading ? (
          <SkeletonBox className="h-[180px]" />
        ) : fundingPoints.length > 1 ? (
          <LineChart
            series={fundingPoints}
            color={fundingMean >= 0 ? POS : DANGER}
            baseline={0}
            baselineLabel="0%"
            width={520}
            height={180}
            yFormat={(v) => v.toFixed(1) + "%"}
            pad={{ t: 14, r: 14, b: 22, l: 52 }}
          />
        ) : (
          <EmptyChart note="Funding history accrues per cycle — not enough points yet." />
        )}
        <div className="font-mono text-[10.5px] text-dim-500">
          current:{" "}
          {row.funding_annual_pct === null
            ? "n/a (no perp)"
            : fmtPct(row.funding_annual_pct, { decimals: 2, sign: true }) + " / yr"}
        </div>
      </div>
    </div>
  );
}

function EmptyChart({ note }: { note: string }) {
  return (
    <div className="h-[180px] flex items-center justify-center border border-dashed border-ink-600/50 rounded-sm">
      <span className="text-[12px] text-dim-500 font-mono px-6 text-center">{note}</span>
    </div>
  );
}

// LM products carry "BASE/QUOTE"; funding tracks the base leg.
function coinForFunding(coin: string): string {
  return coin.split("/")[0]?.trim().toUpperCase() ?? coin.toUpperCase();
}
