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
  type ProfitHorizon,
} from "@/lib/live";

const ACCENT = "#F6A94B";
const POS = "#4ADE80";
const DANGER = "#FB7185";
const WARN = "#FBBF6B";

type SortKey = "quality" | "net" | "apr" | "profit";
type ProfitKey = "profit_1d" | "profit_7d" | "profit_30d";

const PROFIT_OPTS: Array<{ key: ProfitKey; short: string; label: string }> = [
  { key: "profit_1d", short: "Day", label: "1d" },
  { key: "profit_7d", short: "Week", label: "7d" },
  { key: "profit_30d", short: "Month", label: "30d" },
];

export function EarnExplorer() {
  const { data, isLoading, isError, error } = useEarnExplorer({ limit: 500 });
  const [category, setCategory] = useState<string>("");
  const [coin, setCoin] = useState<string>("");
  const [sortKey, setSortKey] = useState<SortKey>("quality");
  const [profitKey, setProfitKey] = useState<ProfitKey>("profit_7d");
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
    const keyOf = (r: EarnProductRow) =>
      sortKey === "quality"
        ? r.quality_score ?? -1
        : sortKey === "net"
          ? r.net_apr_pct ?? -Infinity
          : sortKey === "profit"
            ? r[profitKey]?.total_pct ?? -Infinity
            : r.effective_apr_pct;
    out.sort((a, b) => keyOf(b) - keyOf(a));
    return out;
  }, [rows, category, coin, sortKey, profitKey]);

  return (
    <section className="space-y-6">
      <SectionHead
        eyebrow="Bybit Earn"
        title="Earn Explorer"
        subtitle="Every Bybit Earn coin scored for real earnability. Profit = gross yield per $100 (APR + funding) over the chosen horizon; the one-time Bybit round-trip fee is shown as break-even days (hold past it to profit). Negative profit means the position itself loses (e.g. negative funding), not the fee. ~ marks projected when history is short. Expand a row for the breakdown."
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
        <div className="flex items-center gap-1 font-mono text-[11px] text-dim-500">
          <span>Profit:</span>
          {PROFIT_OPTS.map((o) => (
            <SortButton
              key={o.key}
              active={profitKey === o.key}
              onClick={() => setProfitKey(o.key)}
            >
              {o.short}
            </SortButton>
          ))}
        </div>
        <div className="flex items-center gap-1 ml-auto font-mono text-[11px] text-dim-500">
          <span>Sort:</span>
          <SortButton active={sortKey === "quality"} onClick={() => setSortKey("quality")}>
            Quality
          </SortButton>
          <SortButton active={sortKey === "profit"} onClick={() => setSortKey("profit")}>
            Profit
          </SortButton>
          <SortButton active={sortKey === "net"} onClick={() => setSortKey("net")}>
            Net APR
          </SortButton>
          <SortButton active={sortKey === "apr"} onClick={() => setSortKey("apr")}>
            APR
          </SortButton>
        </div>
      </div>

      {isError ? (
        <ErrorPanel label="Failed to load Earn products" message={String(error)} />
      ) : (
        <Card className="p-0 overflow-hidden">
          <div className="grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 bg-ink-850 px-4 py-2.5">
            <div className="col-span-2">Quality</div>
            <div className="col-span-2">Coin</div>
            <div className="col-span-2 text-right">Net APR</div>
            <div className="col-span-2 text-right text-accent">
              Profit {PROFIT_OPTS.find((o) => o.key === profitKey)?.short}
            </div>
            <div className="col-span-2 text-right">Fee break-even</div>
            <div className="col-span-2">Stability</div>
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
                  profitKey={profitKey}
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

function qualityColor(score: number | null | undefined): string {
  if (score === null || score === undefined) return "#7A8499";
  if (score >= 70) return POS;
  if (score >= 40) return WARN;
  return DANGER;
}

function QualityBadge({ score }: { score: number | null | undefined }) {
  const color = qualityColor(score);
  return (
    <span
      className="inline-flex items-center justify-center min-w-[2.6rem] px-2 py-0.5 rounded-sm text-[12px] tabular font-semibold border"
      style={{ color, borderColor: `${color}55`, backgroundColor: `${color}14` }}
    >
      {score === null || score === undefined ? "—" : Math.round(score)}
    </span>
  );
}

function Bar({ value, color = ACCENT }: { value: number | null | undefined; color?: string }) {
  // value in 0..100
  if (value === null || value === undefined) {
    return <span className="text-dim-600 text-[10px]">—</span>;
  }
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="flex items-center gap-2 w-full">
      <div className="flex-1 h-1.5 rounded-full bg-ink-700/70 overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
      <span className="tabular text-[11px] text-dim-300 w-7 text-right">{Math.round(pct)}</span>
    </div>
  );
}

function EarnRow({
  row,
  profitKey,
  expanded,
  onToggle,
}: {
  row: EarnProductRow;
  profitKey: ProfitKey;
  expanded: boolean;
  onToggle: () => void;
}) {
  const profit = row[profitKey] ?? null;
  return (
    <div className="border-t border-ink-600/30">
      <button
        onClick={onToggle}
        className="w-full grid grid-cols-12 items-center px-4 py-2.5 text-[12px] font-mono text-left hover:bg-ink-850/50 transition-colors"
      >
        <div className="col-span-2">
          <QualityBadge score={row.quality_score} />
        </div>
        <div className="col-span-2 min-w-0 pr-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-white truncate">{row.coin}</span>
            {row.is_stable && (
              <span className="text-[8.5px] uppercase tracking-[0.08em] text-pos border border-pos/30 bg-pos/10 rounded-sm px-1 py-px shrink-0">
                stable
              </span>
            )}
          </div>
          <div className="text-dim-500 truncate text-[10px]">{row.category}</div>
        </div>
        <div className="col-span-2 text-right tabular text-accent">
          {row.net_apr_pct === null || row.net_apr_pct === undefined
            ? fmtPct(row.effective_apr_pct, { decimals: 2, sign: false })
            : fmtPct(row.net_apr_pct, { decimals: 2, sign: false })}
        </div>
        <ProfitCell profit={profit} />
        <BreakEvenCell profit={profit} />
        <div className="col-span-2 pl-3">
          <Bar value={row.stability_score} color={ACCENT} />
        </div>
      </button>
      {expanded && <EarnRowDetail row={row} />}
    </div>
  );
}

function ProfitCell({ profit }: { profit: ProfitHorizon | null }) {
  const total = profit?.total_pct ?? null;
  const realized = profit?.basis === "realized";
  return (
    <div className="col-span-2 text-right">
      {total === null ? (
        <span className="text-dim-600 text-[11px]">no history</span>
      ) : (
        <div className="flex items-center justify-end gap-1.5">
          <span
            className="tabular font-semibold"
            style={{ color: total >= 0 ? POS : DANGER }}
            title={profit?.note ?? undefined}
          >
            {fmtPct(total, { decimals: total !== 0 && Math.abs(total) < 1 ? 3 : 2, sign: true })}
          </span>
          {!realized && (
            <span
              className="text-[8px] uppercase tracking-[0.06em] text-warn shrink-0"
              title={profit?.note ?? "projected"}
            >
              ~
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// Hours-to-recoup the round-trip fee at the current yield. Stable (no fee) →
// "no fee"; a losing position (yield ≤ 0) → "never".
function BreakEvenCell({ profit }: { profit: ProfitHorizon | null }) {
  if (!profit || profit.total_pct === null) {
    return <div className="col-span-2 text-right text-dim-600 text-[11px]">—</div>;
  }
  if (profit.fee_pct !== null && profit.fee_pct <= 0) {
    return <div className="col-span-2 text-right text-pos text-[11px]">no fee</div>;
  }
  if (profit.break_even_days === null) {
    return <div className="col-span-2 text-right text-danger text-[11px]">never</div>;
  }
  const hours = profit.break_even_days * 24;
  const text = hours < 48 ? `${Math.round(hours)}h` : `${profit.break_even_days.toFixed(1)}d`;
  const color = profit.break_even_days > 30 ? WARN : "#9aa3b2";
  return (
    <div
      className="col-span-2 text-right tabular text-[12px]"
      style={{ color }}
      title={`${Math.round(hours)}h to recoup the ${profit.fee_pct?.toFixed(2)}% round-trip fee`}
    >
      {text}
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
    <div className="px-4 pb-5 pt-3 bg-ink-950/40 space-y-5">
      <ProfitPanel row={row} />
      <QualityBreakdown row={row} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
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
    </div>
  );
}

const PROFIT_HORIZONS: Array<{ key: "profit_1d" | "profit_7d" | "profit_30d"; label: string }> = [
  { key: "profit_1d", label: "1 day" },
  { key: "profit_7d", label: "7 days" },
  { key: "profit_30d", label: "30 days" },
];

function basisBadge(basis: string | undefined): { text: string; color: string } {
  if (basis === "realized") return { text: "realized", color: POS };
  if (basis === "projected") return { text: "projected", color: WARN };
  return { text: "no history", color: "#7A8499" };
}

function ProfitPanel({ row }: { row: EarnProductRow }) {
  return (
    <div className="space-y-2">
      <Eyebrow tone="accent">
        Profit per $100 — yield (earn + funding); fee shown as break-even
      </Eyebrow>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {PROFIT_HORIZONS.map(({ key, label }) => (
          <ProfitCard key={key} label={label} h={row[key] ?? null} />
        ))}
      </div>
    </div>
  );
}

function ProfitCard({ label, h }: { label: string; h: ProfitHorizon | null }) {
  const badge = basisBadge(h?.basis);
  const total = h?.total_pct ?? null;
  const usd = total === null ? null : (total / 100) * 100; // per $100 notional
  return (
    <div className="border border-ink-600/40 rounded-sm p-3 space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-dim-400">
          {label}
        </span>
        <span
          className="text-[8.5px] uppercase tracking-[0.08em] rounded-sm px-1 py-px border"
          style={{ color: badge.color, borderColor: `${badge.color}55`, backgroundColor: `${badge.color}14` }}
        >
          {badge.text}
        </span>
      </div>
      {total === null ? (
        <div className="text-[15px] text-dim-500 font-mono">—</div>
      ) : (
        <div className="flex items-baseline gap-2">
          <span
            className="text-[20px] font-serif leading-none tabular"
            style={{ color: total >= 0 ? POS : DANGER }}
          >
            {fmtPct(total, { decimals: total < 1 ? 3 : 2, sign: true })}
          </span>
          <span className="text-[10.5px] text-dim-500 font-mono tabular">
            ${usd!.toFixed(usd! < 1 ? 3 : 2)}
          </span>
        </div>
      )}
      {h && (h.earn_pct !== null || h.funding_pct !== null) && (
        <div className="font-mono text-[10px] text-dim-500 tabular">
          earn {h.earn_pct === null ? "—" : h.earn_pct.toFixed(3) + "%"}
          {" · "}
          funding {h.funding_pct === null ? "n/a" : h.funding_pct.toFixed(3) + "%"}
        </div>
      )}
      {h && h.fee_pct !== null && h.fee_pct > 0 && (
        <div className="font-mono text-[10px] text-dim-500 tabular">
          fee {h.fee_pct.toFixed(2)}% ·{" "}
          {h.break_even_days === null ? (
            <span className="text-danger">never breaks even</span>
          ) : (
            <span className={h.break_even_days > 30 ? "text-warn" : "text-dim-400"}>
              break-even {h.break_even_days.toFixed(0)}d
            </span>
          )}
        </div>
      )}
      {h?.note && <div className="text-[9.5px] font-mono text-dim-600 leading-snug">{h.note}</div>}
    </div>
  );
}

function QualityBreakdown({ row }: { row: EarnProductRow }) {
  const grossApr = row.avg_apr_7d_pct;
  const netApr = row.net_apr_pct;
  const hedgeCost =
    grossApr !== null && grossApr !== undefined && netApr !== null && netApr !== undefined
      ? grossApr - netApr
      : null;
  const penalties: string[] = [];
  if (netApr !== null && netApr !== undefined && netApr < 0) penalties.push("net APR negative");
  if (
    row.price_volatility_pct !== null &&
    row.price_volatility_pct !== undefined &&
    row.price_volatility_pct >= 40
  )
    penalties.push("high volatility");

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {/* Yield */}
      <div className="space-y-2 border border-ink-600/40 rounded-sm p-3">
        <Eyebrow tone="accent">Realizable yield</Eyebrow>
        <Metric label="Avg APR 7d (gross)" value={pct(grossApr)} />
        <Metric label="Net APR (after hedge)" value={pct(netApr)} strong />
        {hedgeCost !== null && (
          <Metric
            label="Hedge cost"
            value={fmtPct(hedgeCost, { decimals: 2, sign: true })}
            tone={hedgeCost > 0 ? "neg" : "pos"}
          />
        )}
        <div className="font-mono text-[10px] text-dim-500 pt-1">source: {row.apr_source}</div>
      </div>

      {/* Stability */}
      <div className="space-y-2 border border-ink-600/40 rounded-sm p-3">
        <Eyebrow tone="accent">Stability {row.is_stable ? "(stablecoin)" : ""}</Eyebrow>
        <LabeledBar label="APR steadiness · 40%" value={unit(row.apr_stability)} />
        <LabeledBar label="Price calm · 60%" value={unit(row.price_stability)} />
        <div className="pt-1">
          <LabeledBar label="Combined" value={row.stability_score} color={POS} />
        </div>
        {row.price_volatility_pct !== null && row.price_volatility_pct !== undefined && (
          <div className="font-mono text-[10px] text-dim-500">
            7d price move: ±{row.price_volatility_pct.toFixed(1)}%
          </div>
        )}
      </div>

      {/* Quality */}
      <div className="space-y-2 border border-ink-600/40 rounded-sm p-3">
        <Eyebrow tone="accent">Quality score</Eyebrow>
        <div className="flex items-baseline gap-2">
          <span
            className="text-[28px] font-serif leading-none"
            style={{ color: qualityColor(row.quality_score) }}
          >
            {row.quality_score === null || row.quality_score === undefined
              ? "—"
              : Math.round(row.quality_score)}
          </span>
          <span className="text-[11px] text-dim-500 font-mono">/ 100</span>
        </div>
        <div className="font-mono text-[10px] text-dim-400 leading-relaxed">
          0.45·yield + 0.40·stability + 0.15·source confidence
        </div>
        {penalties.length > 0 && (
          <div className="text-[10px] font-mono text-danger">
            penalty: {penalties.join(", ")}
          </div>
        )}
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  strong,
  tone,
}: {
  label: string;
  value: string;
  strong?: boolean;
  tone?: "pos" | "neg";
}) {
  const color = tone === "pos" ? POS : tone === "neg" ? DANGER : undefined;
  return (
    <div className="flex items-center justify-between gap-2 text-[11px] font-mono">
      <span className="text-dim-400">{label}</span>
      <span
        className={`tabular ${strong ? "text-white font-semibold" : "text-dim-200"}`}
        style={color ? { color } : undefined}
      >
        {value}
      </span>
    </div>
  );
}

function LabeledBar({
  label,
  value,
  color = ACCENT,
}: {
  label: string;
  value: number | null | undefined;
  color?: string;
}) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] font-mono text-dim-400">{label}</div>
      <Bar value={value} color={color} />
    </div>
  );
}

// 0..1 → 0..100 for bar display.
function unit(v: number | null | undefined): number | null {
  return v === null || v === undefined ? null : v * 100;
}

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : fmtPct(v, { decimals: 2, sign: false });
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
