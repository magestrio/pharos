"use client";

import { ErrorPanel, SkeletonBox } from "@/components/ui";
import {
  IDENTITY_REGISTRY_ADDRESS,
  REPUTATION_ORACLE_ADDRESS,
  REPUTATION_REGISTRY_ADDRESS,
  VAULT_AGENT_ID,
} from "@/lib/contracts";
import { mantleExplorerAddress, mantleExplorerTx } from "@/lib/explorer";
import {
  formatBpsAsPct,
  formatCountdown,
  useReputation,
} from "@/lib/hooks/use-reputation";
import {
  useReputationHistory,
  type ReputationHistoryPoint,
} from "@/lib/hooks/use-reputation-history";

const USDC_SCALE_F = 1_000_000;

export function ReputationPage({ tokenId }: { tokenId: bigint }) {
  const history = useReputationHistory();
  const isVaultAgent = tokenId === VAULT_AGENT_ID;

  return (
    <div className="space-y-8">
      <HeaderBlock tokenId={tokenId} isVaultAgent={isVaultAgent} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <CurrentScoreCard />
        <HistorySparklineCard
          points={history.points}
          isLive={history.isLive}
          isLoading={history.isLoading}
          isError={history.isError}
        />
      </div>

      <RegistryLinksCard tokenId={tokenId} />

      <HistoryTable points={history.points} />

      <LeaderboardPlaceholder />
    </div>
  );
}

function HeaderBlock({ tokenId, isVaultAgent }: { tokenId: bigint; isVaultAgent: boolean }) {
  return (
    <div>
      <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-2">
        ERC-8004 Reputation
      </div>
      <h1 className="text-3xl sm:text-4xl font-semibold text-white">
        Token <span className="font-mono text-neon">#{tokenId.toString()}</span>
      </h1>
      <p className="mt-3 text-[14px] text-dim-300 max-w-2xl leading-relaxed">
        {isVaultAgent ? (
          <>
            Canonical Pharos agent. Reputation = annualized APR in basis points, computed
            on-chain from <code className="font-mono text-white">vault.totalAssets()</code> growth
            since the baseline. Every update is verifiable in the Mantle Explorer + Reputation
            Registry.
          </>
        ) : (
          <>
            Generic ERC-8004 agent view. Live data is Pharos-scoped — other agents would render
            their own oracle reads through the same component once wired.
          </>
        )}
      </p>
    </div>
  );
}

function CurrentScoreCard() {
  const rep = useReputation();
  if (!rep.isLive) {
    return (
      <Card title="Current Score">
        <div className="text-warn text-[12px] font-mono">
          ReputationOracle not deployed yet.
        </div>
        <p className="mt-2 text-[11px] text-dim-500 font-mono leading-relaxed">
          Wire <code>NEXT_PUBLIC_REPUTATION_ORACLE_ADDRESS</code> after the mainnet-deploy epic to
          render live score + countdown.
        </p>
      </Card>
    );
  }
  return (
    <Card title="Current Score">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-5xl text-white tabular leading-none">
          {formatBpsAsPct(rep.lastScoreBps)}
        </span>
        <span className="text-dim-500 font-mono text-[12px]">annualized APR</span>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-2 text-[11.5px] font-mono">
        <Cell label="Updates" value={String(rep.updateCount ?? 0)} />
        <Cell
          label="Next update"
          value={rep.canUpdate ? "ready" : formatCountdown(rep.secondsUntilNext)}
          tone={rep.canUpdate ? "neon" : "dim"}
        />
      </div>
      {rep.previewScoreBps !== null && (
        <div className="mt-4 text-[11px] text-dim-400 font-mono">
          Preview next call:{" "}
          <span className="text-neon">{formatBpsAsPct(rep.previewScoreBps)}</span>{" "}
          (annualized over {Math.round((rep.previewElapsedSec ?? 0) / 86400)}d)
        </div>
      )}
    </Card>
  );
}

function HistorySparklineCard({
  points,
  isLive,
  isLoading,
  isError,
}: {
  points: ReputationHistoryPoint[];
  isLive: boolean;
  isLoading: boolean;
  isError: boolean;
}) {
  if (!isLive) {
    return (
      <Card title="Score History" wide>
        <div className="text-warn text-[12px] font-mono">
          ReputationOracle not deployed yet — history will populate as updates accumulate.
        </div>
      </Card>
    );
  }
  if (isError && points.length === 0) {
    return (
      <Card title="Score History" wide>
        <ErrorPanel label="Couldn't fetch ReputationUpdated events from Mantle RPC." />
      </Card>
    );
  }
  if (isLoading && points.length === 0) {
    return (
      <Card title="Score History" wide>
        <SkeletonBox className="h-32" />
        <div className="mt-3 flex items-center justify-between gap-3">
          <SkeletonBox className="h-3 w-1/3" />
          <SkeletonBox className="h-3 w-1/4" />
        </div>
      </Card>
    );
  }
  if (points.length === 0) {
    return (
      <Card title="Score History" wide>
        <div className="text-dim-500 text-[12px] font-mono">
          No `ReputationUpdated` events yet. First call to{" "}
          <code className="text-white">updateReputation()</code> seeds the chart.
        </div>
      </Card>
    );
  }
  return (
    <Card title="Score History" wide>
      <Sparkline points={points} />
      <div className="mt-3 flex items-center justify-between text-[10.5px] font-mono text-dim-500">
        <span>{points.length} updates · annualized APR (bps)</span>
        <span>
          {formatBpsAsPct(points[0].scoreBps)} →{" "}
          <span className="text-neon">{formatBpsAsPct(points[points.length - 1].scoreBps)}</span>
        </span>
      </div>
    </Card>
  );
}

function Sparkline({ points }: { points: ReputationHistoryPoint[] }) {
  if (points.length === 0) return null;
  const width = 540;
  const height = 120;
  const padX = 6;
  const padY = 8;
  const scores = points.map((p) => p.scoreBps);
  const min = Math.min(0, ...scores);
  const max = Math.max(...scores, min + 1);
  const range = max - min;
  const stepX = points.length > 1 ? (width - padX * 2) / (points.length - 1) : 0;
  const yFor = (v: number) => padY + (height - padY * 2) * (1 - (v - min) / range);
  const pathD = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${padX + i * stepX},${yFor(p.scoreBps)}`)
    .join(" ");
  const areaD = `${pathD} L${padX + (points.length - 1) * stepX},${height - padY} L${padX},${height - padY} Z`;
  const zeroY = yFor(0);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-32" preserveAspectRatio="none">
      {min < 0 && (
        <line x1={padX} x2={width - padX} y1={zeroY} y2={zeroY} stroke="#3F4860" strokeDasharray="2 3" />
      )}
      <path d={areaD} fill="rgba(246,169,75,0.12)" />
      <path d={pathD} fill="none" stroke="#F6A94B" strokeWidth={1.5} />
      {points.map((p, i) => (
        <circle key={p.updateIndex} cx={padX + i * stepX} cy={yFor(p.scoreBps)} r={2.5} fill="#F6A94B" />
      ))}
    </svg>
  );
}

function RegistryLinksCard({ tokenId }: { tokenId: bigint }) {
  return (
    <Card title="Verification Surface">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <RegistryLink
          label="ERC-8004 Identity Registry"
          sub={`tokenURI(${tokenId.toString()}) holds the agent.json pinned to IPFS`}
          href={mantleExplorerAddress(IDENTITY_REGISTRY_ADDRESS)}
        />
        <RegistryLink
          label="ERC-8004 Reputation Registry"
          sub="Canonical score store. ReputationOracle writes here."
          href={mantleExplorerAddress(REPUTATION_REGISTRY_ADDRESS)}
        />
        <RegistryLink
          label="ReputationOracle"
          sub="View, baseline, and write logic. Source of truth for previewScore()."
          href={mantleExplorerAddress(REPUTATION_ORACLE_ADDRESS)}
        />
        <RegistryLink
          label="NFT image"
          sub="agent.json image field is the rendered token graphic — wire IPFS fetch next iter."
        />
      </div>
    </Card>
  );
}

function RegistryLink({
  label,
  sub,
  href,
}: {
  label: string;
  sub: string;
  href?: string;
}) {
  const content = (
    <div className="bg-ink-900 border border-ink-600/70 rounded-md p-4 hover:border-ink-500 transition-colors h-full">
      <div className="text-[10px] font-mono uppercase tracking-[0.16em] text-dim-500">{label}</div>
      <div className="mt-1.5 text-[12.5px] text-dim-300 leading-relaxed">{sub}</div>
      {href && (
        <div className="mt-3 text-[11px] font-mono text-elec hover:text-elec-soft inline-flex items-center gap-1">
          Open on Mantle Explorer →
        </div>
      )}
    </div>
  );
  return href ? (
    <a href={href} target="_blank" rel="noopener noreferrer" className="block">
      {content}
    </a>
  ) : (
    content
  );
}

function HistoryTable({ points }: { points: ReputationHistoryPoint[] }) {
  if (points.length === 0) return null;
  return (
    <Card title="Updates">
      <div className="overflow-hidden border border-ink-600/40 rounded-sm">
        <div className="grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 bg-ink-850 px-3 py-2">
          <div className="col-span-1 text-right">#</div>
          <div className="col-span-3">Score (bps)</div>
          <div className="col-span-3">Vault assets (USDC)</div>
          <div className="col-span-2">Elapsed</div>
          <div className="col-span-3 text-right">Tx</div>
        </div>
        {[...points].reverse().map((p) => (
          <div
            key={p.updateIndex}
            className="grid grid-cols-12 items-center px-3 py-2 border-t border-ink-600/30 text-[12px] font-mono"
          >
            <div className="col-span-1 text-right text-dim-400">{p.updateIndex}</div>
            <div className="col-span-3 text-white tabular">{formatBpsAsPct(p.scoreBps)}</div>
            <div className="col-span-3 text-dim-300 tabular">
              ${(Number(p.currentAssets) / USDC_SCALE_F).toLocaleString("en-US", { maximumFractionDigits: 0 })}
            </div>
            <div className="col-span-2 text-dim-400">{formatCountdown(p.elapsedSeconds)}</div>
            <div className="col-span-3 text-right">
              <a
                href={mantleExplorerTx(p.txHash)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-elec hover:text-elec-soft"
              >
                {p.txHash.slice(0, 6)}…{p.txHash.slice(-4)}
              </a>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function LeaderboardPlaceholder() {
  return (
    <Card title="Leaderboard">
      <div className="text-[12px] text-dim-300 leading-relaxed">
        Pharos is the first agent registered against the canonical ERC-8004 registries on Mantle.
        As other agents register, their token ids will appear here sorted by current score —
        provided the same `ReputationOracle` write pattern is used. A multi-agent leaderboard view
        is filed under the judge verification surface (`.18`).
      </div>
    </Card>
  );
}

function Card({
  title,
  children,
  wide,
}: {
  title: string;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div
      className={`bg-ink-900 border border-ink-600/70 rounded-md p-5 ${wide ? "lg:col-span-2" : ""}`}
    >
      <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-3">
        {title}
      </div>
      {children}
    </div>
  );
}

function Cell({
  label,
  value,
  tone = "white",
}: {
  label: string;
  value: string;
  tone?: "white" | "neon" | "dim";
}) {
  const t = tone === "neon" ? "text-neon" : tone === "dim" ? "text-dim-400" : "text-white";
  return (
    <div className="bg-ink-850/60 border border-ink-600/40 rounded-sm px-2.5 py-2">
      <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">{label}</div>
      <div className={`tabular mt-0.5 ${t}`}>{value}</div>
    </div>
  );
}
