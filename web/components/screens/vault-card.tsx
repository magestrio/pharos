"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useWaitForTransactionReceipt, useWriteContract } from "wagmi";

import { type Allocation } from "@/lib/data";
import Link from "next/link";
import { useCycles, usePortfolio, useRecentEvents } from "@/lib/agent-store-context";
import { usePortfolio as useLivePortfolio } from "@/lib/live";
import type { LivePortfolio } from "@/lib/live";
import type { CycleSummary, EventRow, PositionRow } from "@/lib/agent-api";
import {
  REPUTATION_ORACLE_ADDRESS,
  VAULT_AGENT_ID,
  VUSDC_CHAIN_ID,
  reputationOracleContract,
} from "@/lib/contracts";
import { mantleExplorerAddress, mantleExplorerTx } from "@/lib/explorer";
import { formatDateTime } from "@/lib/datetime";
import { BRAND } from "@/lib/brand";
import { useActiveHedges } from "@/lib/hooks/use-active-hedges";
import {
  useAlloraForecast,
  type AlloraForecast,
  type MarketBias,
} from "@/lib/hooks/use-allora-forecast";
import { useIsMounted } from "@/lib/hooks/use-is-mounted";
import {
  useAllocationStats,
  usePlannedVsActual,
  type AllocationRow,
  type AllocationStats,
} from "@/lib/hooks/use-allocation-stats";
import {
  formatHeartbeatShort,
  useAttestorHealth,
} from "@/lib/hooks/use-attestor-health";
import {
  formatBpsAsPct,
  formatCountdown,
  useReputation,
} from "@/lib/hooks/use-reputation";
import { useVaultStats, type VaultStats } from "@/lib/hooks/use-vault-stats";
import { MINT_REDEEM_ANCHOR, MintRedeemPanel } from "@/components/mint-redeem-panel";
import {
  Button,
  Card,
  DonutChart,
  ErrorPanel,
  Eyebrow,
  HashChip,
  Icon,
  LineChart,
  LiveDot,
  SectionHead,
  SkeletonRow,
  StatCard,
  Tag,
} from "@/components/ui";

// 2-of-3 Gnosis Safe owner / attestor signer - canonical address from
// CLAUDE.md, a real static fact (not an env-driven deploy artifact).
const SAFE_OWNER_ADDRESS = "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037";

export function VaultCard() {
  const stats = useVaultStats();
  const allocation = useAllocationStats();
  return (
    <div className="space-y-10 sm:space-y-12">
      <HeroBlock stats={stats} />
      <StatsRow stats={stats} />
      <AlloraForecastSection />
      <MintRedeemPanel />
      {/* Capital-growth section pulls its own series from
          /api/capital-history - real per-cycle equity reconstructed
          from store positions, falls back to a clear empty state when
          there's not enough history yet. */}
      <CapitalGrowthSection stats={stats} />
      <AllocationSection stats={stats} allocation={allocation} />
      <PlannedVsActualSection />
      <AttestorAndHedgesSection />
      <RecentWatcherEventsWidget />
      <RecentDecisionsPreview />
    </div>
  );
}

function HeroBlock({ stats }: { stats: VaultStats }) {
  return (
    <section className="relative grid grid-cols-1 lg:grid-cols-3 gap-8 lg:gap-10 items-start">
      <div
        aria-hidden
        className="glow-amber-soft pointer-events-none absolute -top-24 -left-24 w-[640px] h-[420px]"
      />
      <div className="relative lg:col-span-2 space-y-7">
        <HeroBadgeLine />
        <div className="space-y-5">
          <h1 className="font-serif text-[44px] sm:text-[60px] lg:text-[76px] leading-[0.98] tracking-[-0.025em] text-white">
            <span className="font-mono text-accent align-middle text-[0.72em] mr-2">
              vUSDC
            </span>
            <span className="text-dim-500 font-normal">-</span> AI-Managed
            <br />
            Yield-Bearing USDC
            <span className="font-mono text-accent">.</span>
          </h1>
          <p className="text-[16px] sm:text-[17px] text-dim-300 max-w-[58ch] leading-[1.55]">
            Mint USDC, receive vUSDC. The exchange rate grows as our agent allocates across{" "}
            <span className="font-serif italic text-white">Aave V3</span> and{" "}
            <span className="font-serif italic text-white">Bybit Earn</span> (200+ products) with
            delta-neutral hedging on volatile positions. Every decision logged on-chain. Reputation
            verifiable through ERC-8004.
          </p>
          <div className="flex flex-wrap items-center gap-3 pt-3">
            <Button variant="primary" href={MINT_REDEEM_ANCHOR}>
              Mint vUSDC
              <Icon.Arrow className="transition-transform group-hover:translate-x-0.5" />
            </Button>
            <Button variant="secondary" href={MINT_REDEEM_ANCHOR}>
              <Icon.Block /> View Live Vault
            </Button>
          </div>
          <div className="hidden md:flex items-center gap-3 pt-1 font-mono text-[10.5px] uppercase tracking-[0.18em] text-dim-500">
            <span>Agent</span>
            <span className="text-white tabular">Claude Sonnet 4.6</span>
            <span className="h-2.5 w-px bg-dim-600/70" />
            <span className="text-white tabular">
              {stats.daysLive > 0 ? `${stats.daysLive}d live` : "-"}
            </span>
            <span className="h-2.5 w-px bg-dim-600/70" />
            <span>Mantle Mainnet</span>
          </div>
        </div>
      </div>
      <div className="relative lg:col-span-1">
        <div className="lg:sticky lg:top-24">
          {/* ReputationNFTCard reads ReputationOracle on-chain; the oracle
              is deferred to Phase B, and rendering the mock score (1247
              / "Decile rank: TOP 16%") here would be the most visually
              prominent fake on the page. Hide until the contract is wired. */}
          <ReputationNFTCardLive />
        </div>
      </div>
    </section>
  );
}

function HeroBadgeLine() {
  // ERC-8004 AGENT_ID=99 is the only on-chain identity actually wired
  // (registered 2026-05-24, owner = SAFE). Reputation NFT score is
  // not yet readable - ReputationOracle deferred to Phase B. Showing
  // a mock "1247" was the bug; just omit it until live.
  const rep = useReputation();
  const repLabel = rep.isLive && rep.lastScoreBps !== null
    ? formatBpsAsPct(rep.lastScoreBps)
    : null;

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 font-mono text-[11px] uppercase tracking-[0.16em]">
      <span className="text-white font-semibold">{BRAND.wordmark}</span>
      <span className="h-2.5 w-px bg-dim-600/70" />
      <span className="text-dim-400">
        ERC-8004 <span className="text-accent">#{VAULT_AGENT_ID}</span>
      </span>
      {repLabel !== null && (
        <>
          <span className="h-2.5 w-px bg-dim-600/70" />
          <span className="text-dim-400">
            Reputation <span className="text-white tabular">{repLabel}</span>
          </span>
        </>
      )}
      <span className="h-2.5 w-px bg-dim-600/70" />
      <span className="inline-flex items-center gap-2 text-accent">
        <LiveDot /> Live
      </span>
      <span className="h-2.5 w-px bg-dim-600/70" />
      <span className="text-dim-400">Mantle Mainnet</span>
    </div>
  );
}

/**
 * Wraps ReputationNFTCard with a live-data gate: when the on-chain
 * ReputationOracle isn't deployed, render a compact placeholder instead
 * of the legacy mock-score card.
 */
function ReputationNFTCardLive() {
  const rep = useReputation();
  if (!rep.isLive) {
    return (
      <div className="bg-ink-900 border border-ink-600/70 rounded-md p-5">
        <div className="text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400 mb-3 flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-sm bg-warn/70"></span>
          ERC-8004 Reputation
        </div>
        <div className="text-sm text-dim-200 leading-relaxed">
          Agent identity is live on-chain (
          <span className="font-mono text-white">AGENT_ID=#{VAULT_AGENT_ID}</span>
          ). Score-update path through{" "}
          <span className="font-mono text-white">ReputationOracle</span> ships
          with the Phase B vUSDC contract deploy.
        </div>
        <div className="text-[11px] font-mono text-dim-500 mt-3">
          [mainnet pending]
        </div>
      </div>
    );
  }
  return <ReputationNFTCard />;
}

function ReputationNFTCard() {
  const rep = useReputation();
  const decisionsCount = useCycles({ limit: 50 }).data?.length ?? 0;
  const update = useWriteContract();
  const updateReceipt = useWaitForTransactionReceipt({
    hash: update.data,
    chainId: VUSDC_CHAIN_ID,
  });

  // This card only renders when `rep.isLive` (the non-live branch shows
  // ReputationNFTCardLive's placeholder), so every value here is real
  // on-chain data - no mock fallback.
  const liveScoreBps = rep.lastScoreBps;
  const liveScoreLabel = liveScoreBps !== null ? formatBpsAsPct(liveScoreBps) : null;
  // Cap live bar at +50% APR (5000 bps) for visual scaling; anything
  // above pegs. Clamp at 0 for negative (underwater) scores.
  const pct =
    liveScoreBps !== null ? Math.max(0, Math.min(100, (liveScoreBps / 5000) * 100)) : 0;

  const previewBps = rep.previewScoreBps;
  const previewLabel = previewBps !== null ? formatBpsAsPct(previewBps) : null;

  const onUpdate = () => {
    update.writeContract({
      ...reputationOracleContract,
      functionName: "updateReputation",
    });
  };

  const isSubmitting = update.isPending || updateReceipt.isLoading;
  const buttonDisabled =
    !rep.isLive || !rep.canUpdate || isSubmitting || rep.secondsUntilNext > 0;

  return (
    <div className="relative">
      <div
        className="absolute -inset-[2px] rounded-lg pointer-events-none"
        style={{
          background:
            "radial-gradient(120% 80% at 50% 0%, rgba(246,169,75,0.25), transparent 60%)",
        }}
      />
      <div className="relative bg-gradient-to-b from-ink-850 to-ink-900 border border-ink-500/70 rounded-md overflow-hidden shadow-card-premium ring-1 ring-inset ring-white/[0.04]">
        <div
          aria-hidden
          className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/40 to-transparent pointer-events-none"
        />
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-600/60 bg-ink-900/50">
          <Eyebrow tone="dim" className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-sm bg-accent shadow-[0_0_8px_rgba(246,169,75,0.6)]"></span>
            ERC-8004 Reputation
          </Eyebrow>
          <div className="text-[10px] font-mono text-dim-500 tracking-[0.16em]">TOKEN #001</div>
        </div>

        <div className="p-6 pb-5 bg-dots relative">
          <Eyebrow tone="dim" className="mb-4">
            Annualized APR
          </Eyebrow>
          <div className="flex items-baseline gap-2">
            <div className="font-serif text-[88px] leading-[0.85] font-semibold text-white tabular tracking-[-0.04em]">
              {liveScoreLabel ?? "-"}
            </div>
            <div className="text-dim-500 font-mono text-[18px] tabular self-start mt-3">
              ERC-8004
            </div>
          </div>
          <div className="mt-5 h-[5px] bg-ink-700 overflow-hidden rounded-full">
            <div
              className="h-full rounded-full bg-gradient-to-r from-accent-dim via-accent to-accent-soft transition-all duration-700 shadow-[0_0_12px_rgba(246,169,75,0.45)]"
              style={{ width: pct + "%" }}
            />
          </div>
          <div className="mt-3 flex items-center justify-between text-[10.5px] font-mono">
            <span className="text-dim-500 uppercase tracking-[0.18em]">Updates</span>
            <Tag tone="accent">{rep.updateCount ?? 0}</Tag>
          </div>
        </div>

        {/* Sharpe / Max DD / Win Rate are realized-performance metrics -
            no live source exists yet (needs vUSDC exchangeRate history),
            so they render "-" rather than fabricated numbers. Decisions
            is the real cycle count. */}
        <div className="grid grid-cols-2 gap-px bg-ink-600/40 border-t border-ink-600/60">
          <MetricCell label="Sharpe" value="-" />
          <MetricCell label="Max DD" value="-" />
          <MetricCell label="Win Rate" value="-" />
          <MetricCell label="Decisions" value={String(decisionsCount)} />
        </div>

        <div className="border-t border-ink-600/60 bg-ink-900/40 p-4 space-y-3">
          <button
            onClick={onUpdate}
            disabled={buttonDisabled}
            className={`w-full inline-flex items-center justify-center gap-2 px-4 h-11 rounded-[3px] font-mono tracking-[0.14em] uppercase text-[12px] font-semibold transition-all
              ${
                updateReceipt.isSuccess
                  ? "bg-accent text-[#1B1300] shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_0_0_1px_rgba(246,169,75,0.6)]"
                  : buttonDisabled
                    ? "bg-ink-800 border border-ink-600 text-dim-400 cursor-not-allowed"
                    : "bg-accent text-[#1B1300] shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_0_0_1px_rgba(246,169,75,0.6),0_8px_24px_-10px_rgba(246,169,75,0.45)] hover:bg-accent-soft active:translate-y-px"
              }`}
          >
            {!rep.isLive && <span className="btn-bracket">Update Reputation</span>}
            {rep.isLive && !isSubmitting && !updateReceipt.isSuccess && rep.canUpdate && (
              <span className="btn-bracket">Update Reputation</span>
            )}
            {rep.isLive && !isSubmitting && !updateReceipt.isSuccess && !rep.canUpdate && (
              <>Next update in {formatCountdown(rep.secondsUntilNext)}</>
            )}
            {update.isPending && (
              <>
                <Icon.Spinner className="animate-spin" /> Confirm in wallet…
              </>
            )}
            {updateReceipt.isLoading && (
              <>
                <Icon.Spinner className="animate-spin" /> Waiting for confirmation…
              </>
            )}
            {updateReceipt.isSuccess && (
              <>
                <Icon.Check /> Score updated on-chain
              </>
            )}
          </button>

          <div className="min-h-[42px] text-[11px] font-mono">
            {!rep.isLive && (
              <div className="text-warn leading-relaxed">
                ReputationOracle not deployed yet - wire `NEXT_PUBLIC_REPUTATION_ORACLE_ADDRESS` after the mainnet-deploy epic.
              </div>
            )}
            {rep.isLive && !update.data && previewLabel && rep.canUpdate && (
              <div className="text-dim-300 leading-relaxed">
                Next call sets score to <span className="text-accent">{previewLabel}</span>{" "}
                from {liveScoreLabel ?? "-"} (annualized over {Math.round((rep.previewElapsedSec ?? 0) / 86400)}d).
              </div>
            )}
            {rep.isLive && !update.data && !rep.canUpdate && (
              <div className="text-dim-500 leading-relaxed">
                Throttle: 1 update / hour. Last call was{" "}
                {rep.lastUpdateTimestamp
                  ? formatCountdown(rep.minIntervalSec - rep.secondsUntilNext)
                  : "-"}{" "}
                ago.
              </div>
            )}
            {update.error && !update.data && (
              <div className="text-danger">
                {update.error.message?.slice(0, 120) ?? "transaction failed"}
              </div>
            )}
            {update.data && updateReceipt.isLoading && (
              <div className="space-y-1 fade-up">
                <div className="flex items-center gap-2 text-elec">
                  <LiveDot /> Submitted to Mantle
                </div>
                <a
                  href={mantleExplorerTx(update.data)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-elec hover:text-elec-soft inline-flex items-center gap-1"
                >
                  <HashChip hash={update.data} label="tx:" />
                </a>
              </div>
            )}
            {updateReceipt.isSuccess && update.data && (
              <div className="space-y-1 fade-up">
                <div className="flex items-center gap-2 text-accent">
                  <LiveDot /> Confirmed
                </div>
                <div className="text-dim-400">
                  Score → <span className="text-white">{liveScoreLabel ?? "-"}</span>
                </div>
                <a
                  href={mantleExplorerTx(update.data)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-elec hover:text-elec-soft inline-flex items-center gap-1"
                >
                  <HashChip hash={update.data} label="tx:" />
                </a>
              </div>
            )}
          </div>

          <div className="flex items-center justify-between pt-1 border-t border-ink-600/60 -mx-4 px-4 -mb-4 pb-4 text-[11px] font-mono">
            <Link
              href={`/reputation/${VAULT_AGENT_ID.toString()}`}
              className="text-dim-400 hover:text-white inline-flex items-center gap-1.5 transition-colors"
            >
              Reputation history <Icon.Arrow />
            </Link>
            {rep.isLive ? (
              <a
                href={mantleExplorerAddress(REPUTATION_ORACLE_ADDRESS)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-dim-400 hover:text-white inline-flex items-center gap-1.5"
              >
                Explorer <Icon.Ext />
              </a>
            ) : (
              <span className="text-dim-600">v1.0.4</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function ProgressLine({ label }: { label: string }) {
  return (
    <div>
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-white">{label}</span>
        <span className="text-dim-500">RPC →</span>
      </div>
      <div className="mt-1 h-[2px] bg-ink-700 overflow-hidden rounded-sm">
        <div className="h-full bg-accent bar-fill"></div>
      </div>
    </div>
  );
}

function MetricCell({ label, value }: { label: string; value: string; border?: string }) {
  return (
    <div className="p-4 bg-ink-900/60">
      <Eyebrow tone="dim">{label}</Eyebrow>
      <div className="font-serif text-[28px] leading-none text-white mt-2 tabular tracking-[-0.02em]">
        {value}
      </div>
    </div>
  );
}

function formatTvl(tvlUsdc: number | undefined): string {
  if (tvlUsdc === undefined) return "-";
  if (tvlUsdc >= 1_000_000) return "$" + (tvlUsdc / 1_000_000).toFixed(3) + "M";
  if (tvlUsdc >= 1_000) return "$" + (tvlUsdc / 1_000).toFixed(2) + "k";
  return "$" + tvlUsdc.toFixed(2);
}

function StatsRow({ stats }: { stats: VaultStats }) {
  // Live data - derive APY + decisions count from the cycle history so
  // numbers reflect what the agent actually did (vs. mock VAULT consts).
  const cyclesQuery = useCycles({ limit: 50 });
  const cycles = cyclesQuery.data ?? [];
  const aprSamples = cycles
    .map((c) => c.expected_apr_pct)
    .filter((x): x is number => x !== null && Number.isFinite(x));
  const avgApr =
    aprSamples.length > 0
      ? aprSamples.reduce((s, v) => s + v, 0) / aprSamples.length
      : null;

  return (
    <section className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
      <StatCard
        label="vUSDC Exchange Rate"
        value={stats.exchangeRate !== undefined ? stats.exchangeRate.toFixed(5) : "-"}
        tone="green"
        sub={
          stats.cumReturnPct !== undefined ? (
            <span>+{stats.cumReturnPct.toFixed(3)}% since inception</span>
          ) : (
            <span className="text-dim-500">vUSDC contract not deployed yet</span>
          )
        }
      />
      <StatCard
        label="Total Value Locked"
        value={formatTvl(stats.tvlUsdc)}
        sub={
          stats.tvlUsdc !== undefined ? (
            <span className="text-dim-400">live · off-chain sandbox</span>
          ) : (
            <span className="text-dim-500">no data</span>
          )
        }
      />
      <StatCard
        label="Avg Expected APR"
        value={avgApr !== null ? avgApr.toFixed(1) + "%" : "-"}
        sub={
          avgApr !== null ? (
            <span>mean across {aprSamples.length} cycles</span>
          ) : (
            <span className="text-dim-500">no cycles yet</span>
          )
        }
      />
      <StatCard
        label="Decisions Logged"
        value={String(cycles.length)}
        sub={<span>Event-driven · 4h cron fallback</span>}
      />
    </section>
  );
}

// ─── Allora price forecast ───────────────────────────────────────────
//
// The agent pulls Allora's 8h directional price forecast per coin into
// each snapshot; this surfaces the latest one as a market-context card.
// Spot exists only for BTC/ETH in the snapshot, so SOL renders
// price-only (no delta). The headline bias is read off the BTC forecast.

const BIAS_META: Record<MarketBias, { label: string; tone: "pos" | "red" | "neutral" }> = {
  bullish: { label: "Market · Bullish", tone: "pos" },
  bearish: { label: "Market · Bearish", tone: "red" },
  neutral: { label: "Market · Neutral", tone: "neutral" },
};

function fmtPrice(usd: number): string {
  const digits = usd >= 1000 ? 0 : usd >= 1 ? 2 : 4;
  return (
    "$" +
    usd.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })
  );
}

function AlloraForecastRow({ f }: { f: AlloraForecast }) {
  const arrow = f.direction === "up" ? "▲" : f.direction === "down" ? "▼" : "≈";
  const tone: "pos" | "red" | "neutral" =
    f.direction === "up" ? "pos" : f.direction === "down" ? "red" : "neutral";
  const accent =
    f.direction === "up"
      ? "border-l-pos/50"
      : f.direction === "down"
        ? "border-l-danger/50"
        : "border-l-ink-500";

  return (
    <div
      className={`flex items-center gap-4 px-4 py-4 bg-ink-900 border border-ink-600/60 border-l-2 ${accent} rounded-md`}
    >
      <span className="inline-flex items-center justify-center min-w-[48px] h-[30px] px-2 rounded-sm bg-accent/[0.10] border border-accent/30 text-accent font-mono text-[12px] tracking-[0.06em]">
        {f.token}
      </span>
      <div className="flex-1 min-w-0">
        <Eyebrow tone="dim" className="!text-[9.5px] mb-1">
          Allora expects · 8h
        </Eyebrow>
        <div className="font-serif text-[24px] leading-none text-white tabular tracking-[-0.02em]">
          {fmtPrice(f.inferenceUsd)}
        </div>
      </div>
      <div className="text-right shrink-0">
        {f.deltaPct !== null ? (
          <Tag tone={tone} className="!text-[12px]">
            {arrow} {f.deltaPct >= 0 ? "+" : ""}
            {f.deltaPct.toFixed(2)}%
          </Tag>
        ) : (
          <Tag tone="neutral" className="!text-[10px]">
            no spot
          </Tag>
        )}
        <div className="text-[11px] font-mono text-dim-500 mt-1.5 tabular">
          {f.spotUsd !== null ? <>spot {fmtPrice(f.spotUsd)}</> : <>spot n/a</>}
        </div>
      </div>
    </div>
  );
}

function AlloraForecastSection() {
  const mounted = useIsMounted();
  const { forecasts, marketBias, isLoading, isError, isLive } = useAlloraForecast();
  const asOf = forecasts.length > 0 ? Math.max(...forecasts.map((f) => f.asOf)) : 0;
  const bias = BIAS_META[marketBias];

  // Gate all data-dependent rendering behind mount: the forecast comes
  // from a client-only detail fetch, so server + first-client paint must
  // render the same stable skeleton to avoid a hydration mismatch.
  const ready = mounted && isLive;

  return (
    <section>
      <SectionHead
        eyebrow="Allora · decentralized AI oracle"
        title="Price forecast · 8h"
        subtitle="Allora's on-chain AI oracle predicts each coin's price 8 hours out - the directional signal the agent factors into every decision. 8h is the longest window the oracle exposes for these assets."
        right={
          ready && (
            <div className="flex items-center gap-3">
              <Tag tone={bias.tone}>{bias.label}</Tag>
              {asOf > 0 && (
                <span className="hidden sm:inline font-mono text-[11px] text-dim-500">
                  as of {formatDateTime(asOf * 1000)}
                </span>
              )}
            </div>
          )
        }
      />
      <Card className="p-5 sm:p-6">
        {!mounted || (isLoading && !isLive) ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5">
            <SkeletonRow width="100%" />
            <SkeletonRow width="100%" />
            <SkeletonRow width="100%" />
          </div>
        ) : isError && !isLive ? (
          <div className="font-mono text-[12px] text-dim-500">
            Could not load Allora forecast.
          </div>
        ) : !isLive ? (
          <div className="flex flex-col items-center justify-center gap-2 py-6 font-mono text-[12px] text-dim-400 text-center">
            <span className="text-dim-500 uppercase tracking-[0.16em] text-[10px]">
              no allora signal this cycle
            </span>
            <span>The latest cycle carries no 8h price inference.</span>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5">
            {forecasts.map((f) => (
              <AlloraForecastRow key={f.token} f={f} />
            ))}
          </div>
        )}
      </Card>
    </section>
  );
}

type CapitalPoint = { ts: string; equityUsd: number };

function useCapitalHistory(limit = 60) {
  return useQuery<{ points: CapitalPoint[] }>({
    queryKey: ["capital-history", limit],
    queryFn: async () => {
      const res = await fetch(`/api/capital-history?limit=${limit}`, {
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`capital-history → ${res.status}`);
      return res.json();
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

function CapitalGrowthSection({ stats: _stats }: { stats: VaultStats }) {
  // Real series - each point is `Σ positions[cycle].amount_usd` for one
  // historical cycle. No mock fallback: when the agent hasn't logged
  // enough cycles yet, the chart renders an honest empty state instead
  // of a fabricated curve.
  const live = useLivePortfolio();
  const history = useCapitalHistory(60);
  const points = history.data?.points ?? [];
  const fmtUsd = (v: number) => {
    if (Math.abs(v) >= 1_000_000) return "$" + (v / 1_000_000).toFixed(4) + "M";
    if (Math.abs(v) >= 1_000) return "$" + (v / 1_000).toFixed(2) + "k";
    return "$" + v.toFixed(2);
  };

  // Splice the live current-equity point onto the end of the series so
  // the curve always lands on the snapshot the dashboard headline shows.
  const liveEquity = live.data?.total_equity_usd;
  const lastHistoryTs = points.length > 0 ? points[points.length - 1].ts : null;
  const series: CapitalPoint[] = [...points];
  if (
    Number.isFinite(liveEquity) &&
    (lastHistoryTs === null ||
      lastHistoryTs !== new Date().toISOString().slice(0, 19) + "Z")
  ) {
    series.push({ ts: new Date().toISOString(), equityUsd: liveEquity ?? 0 });
  }

  const startUsd = series.length > 0 ? series[0].equityUsd : 0;
  const endUsd = series.length > 0 ? series[series.length - 1].equityUsd : 0;
  const gainUsd = endUsd - startUsd;
  const gainPct = startUsd > 0 ? (gainUsd / startUsd) * 100 : 0;
  const startDate = series.length > 0 ? series[0].ts.slice(0, 10) : "-";

  const numericSeries = series.map((p) => p.equityUsd);
  const enoughForChart = numericSeries.length >= 3;

  return (
    <section>
      <SectionHead
        eyebrow="Vault Capital · USD"
        title="Equity over time"
        subtitle="Each point is the total USD value of positions held at that cycle, reconstructed from the on-chain decision log. The latest point is the live snapshot."
        right={
          enoughForChart && (
            <div className="hidden md:flex items-center gap-3 font-mono text-[12px]">
              <div className="flex items-center gap-2">
                <span className="text-dim-500 uppercase tracking-[0.14em] text-[10px]">
                  {startDate}
                </span>
                <span className="text-dim-300 tabular">{fmtUsd(startUsd)}</span>
              </div>
              <span className="text-dim-600">→</span>
              <div className="flex items-center gap-2">
                <span className="text-dim-500 uppercase tracking-[0.14em] text-[10px]">
                  now
                </span>
                <span
                  className={`tabular ${gainUsd >= 0 ? "text-accent" : "text-danger"}`}
                >
                  {fmtUsd(endUsd)}
                </span>
              </div>
              <span className="text-dim-600">·</span>
              <span
                className={`tabular ${gainUsd >= 0 ? "text-accent" : "text-danger"}`}
              >
                {gainUsd >= 0 ? "+" : ""}
                {fmtUsd(gainUsd)} ({gainPct >= 0 ? "+" : ""}
                {gainPct.toFixed(2)}%)
              </span>
            </div>
          )
        }
      />
      <Card className="p-5 sm:p-6">
        {history.isLoading && !history.data && (
          <div className="h-[240px] flex items-center justify-center font-mono text-[12px] text-dim-500">
            loading capital history…
          </div>
        )}
        {!history.isLoading && !enoughForChart && (
          <div className="h-[240px] flex flex-col items-center justify-center gap-2 font-mono text-[12px] text-dim-400 text-center px-6">
            <span className="text-dim-500 uppercase tracking-[0.16em] text-[10px]">
              not enough cycles yet
            </span>
            <span>
              {points.length} historical {points.length === 1 ? "point" : "points"} ·
              live equity {liveEquity !== undefined ? fmtUsd(liveEquity) : "-"}
            </span>
            <span className="text-dim-500">
              Chart fills in once the agent logs at least 3 cycles with positions.
            </span>
          </div>
        )}
        {enoughForChart && (
          <div className="h-[240px] sm:h-[280px] -mx-2">
            <LineChart
              series={numericSeries}
              color="#F6A94B"
              label="capitalusd"
              baseline={startUsd}
              width={1200}
              height={280}
              pad={{ t: 18, r: 16, b: 26, l: 72 }}
              yFormat={fmtUsd}
            />
          </div>
        )}
      </Card>
    </section>
  );
}


const BYBIT_SUB_PALETTE = ["#A78BFA", "#C4B5FD", "#A6BEFC", "#7C5CD6"] as const;

function portfolioToBybitSubRows(positions: PositionRow[]): Allocation[] {
  const filtered = positions.filter((p) => p.venue.startsWith("bybit_"));
  if (filtered.length === 0) return [];
  const totals = filtered.map((p) => Number(p.amount_usd ?? "0"));
  const total = totals.reduce((s, v) => s + v, 0);
  if (total === 0) return [];
  return filtered.map((p, i) => {
    const notional = totals[i];
    const venueLabel = p.venue.replace(/^bybit_/, "Bybit ");
    return {
      key: `${p.venue}/${p.product_id}`,
      label: p.product_id || venueLabel,
      sub: p.coin ? `${venueLabel} · ${p.coin}` : venueLabel,
      pct: Math.round((notional / total) * 1000) / 10,
      apy: 0,
      color: BYBIT_SUB_PALETTE[i % BYBIT_SUB_PALETTE.length],
      notional: Math.round(notional),
    };
  });
}

// ─── Grouped allocation: trades vs passive earn vs cash ──────────────
//
// Positions arrive as a flat venue-level list. For the dashboard the
// useful unit is a "trade" - a base leg + its hedge - not a venue. We
// pair perp shorts with same-coin earn legs into one delta-neutral
// trade card; stable earns with no hedge become "passive earn";
// idle USDC becomes the cash buffer.
//
// Data sources (merged):
//   • `/api/portfolio` (live Bybit snapshot) - every active earn
//     position row + Spot/Funding holdings. The store-cycle row often
//     only saves a single OnChain Earn record per coin even when Bybit
//     holds three sub-orders - live is the only honest source for the
//     full earn breakdown.
//   • agent-store `/portfolio/current` - perp positions (hedge legs).
//     Live `/api/portfolio` doesn't include perp side; the store does.
// We rebuild `positions[]` from both so the grouping operates on the
// real, complete set.

const EARN_CATEGORY_TO_VENUE: Record<string, string> = {
  FlexibleSaving: "bybit_flex",
  Flexible: "bybit_flex",
  OnChain: "bybit_onchain",
  "On-Chain Earn": "bybit_onchain",
  LiquidityMining: "bybit_lm",
  LM: "bybit_lm",
  DiscountBuy: "bybit_discount_buy",
  DualAsset: "bybit_dual_asset",
  HoldToEarn: "bybit_hold_to_earn",
  "Easy Earn": "bybit_flex",
};

function categoryToVenue(category: string | undefined | null): string {
  if (!category) return "bybit_flex";
  return EARN_CATEGORY_TO_VENUE[category] ?? "bybit_flex";
}

// Bybit's `holdings[].equity` is the COIN AMOUNT, not USD - even when
// `valuationCurrency: USD` sits on the parent account. For non-stables
// we have to derive the unit price ourselves. We do it per-account:
//   USD-priced totalEquity − Σ(stable amounts at $1)
//     ÷ amount of the remaining non-stable coin
// works exactly when an account has zero/one non-stable bucket.
const STABLE_COINS = new Set<string>([
  "USDC",
  "USDT",
  "USD1",
  "USDE",
  "USDTB",
  "USDP",
  "BUSD",
  "DAI",
  "FDUSD",
  "USDR",
  "PYUSD",
]);

function isStable(coin: string): boolean {
  return STABLE_COINS.has(coin.toUpperCase());
}

type AccountHolding = { coin: string; equity: number; category?: string };
type AccountLike = {
  accountType: string;
  totalEquity: number;
  holdings?: AccountHolding[];
};

/**
 * Best-effort coin → USD price map derived from per-account totalEquity.
 * Stables are pinned at $1. For each account we deduct the stable
 * portion from `totalEquity` and divide the remainder across the
 * non-stable holdings. The single-non-stable case (Earn account holding
 * just TON next to a stable) yields an exact price; the multi-coin case
 * falls back to an amount-weighted average - better than zero.
 */
function derivePrices(accounts: AccountLike[]): Map<string, number> {
  const prices = new Map<string, number>();
  // Pin stables.
  for (const acct of accounts) {
    for (const h of acct.holdings ?? []) {
      if (isStable(h.coin)) prices.set(h.coin, 1);
    }
  }
  for (const acct of accounts) {
    const holdings = acct.holdings ?? [];
    if (holdings.length === 0) continue;
    const stableUsd = holdings
      .filter((h) => isStable(h.coin))
      .reduce((s, h) => s + (Number.isFinite(h.equity) ? h.equity : 0), 0);
    const nonStables = holdings.filter(
      (h) => !isStable(h.coin) && Number.isFinite(h.equity) && h.equity > 0,
    );
    const remainder = (acct.totalEquity ?? 0) - stableUsd;
    if (remainder <= 0 || nonStables.length === 0) continue;
    if (nonStables.length === 1) {
      const h = nonStables[0];
      if (h.equity > 0) prices.set(h.coin, remainder / h.equity);
    } else {
      // Multi-coin non-stable bucket - fall back to a single
      // amount-weighted price applied uniformly. Imprecise but better
      // than 0; only kicks in for messy accounts (Unified spot dust).
      const totalAmt = nonStables.reduce((s, h) => s + h.equity, 0);
      const avg = remainder / totalAmt;
      for (const h of nonStables) {
        if (!prices.has(h.coin)) prices.set(h.coin, avg);
      }
    }
  }
  return prices;
}

/**
 * Rebuild a complete `positions[]` from the live Bybit snapshot, with
 * accurate USD per position. Bybit returns `active_earn_positions[]`
 * with coin amounts but no per-row USD; we infer it from the Earn
 * account's `holdings[coin].equity` - the authoritative USD figure -
 * and split it across same-coin rows proportionally to `amount`. Spot
 * balances (USDT/USDC in UnifiedTrading + Funding) become cash rows.
 * Perp legs are taken from the agent-store positions because Bybit's
 * portfolio endpoint omits the derivatives side.
 */
function buildPositionsFromLive(
  live: LivePortfolio | undefined,
  storePositions: PositionRow[],
): PositionRow[] {
  const out: PositionRow[] = [];
  if (!live) return storePositions;

  const accounts = (live.accounts ?? []) as AccountLike[];
  const prices = derivePrices(accounts);
  const priceOf = (coin: string): number => prices.get(coin) ?? (isStable(coin) ? 1 : 0);

  // 1. Earn positions - every active_earn_positions row gets its own
  //    PositionRow. USD = amount × derived price. Stables resolve
  //    exactly; non-stables (TON) use the per-account price inferred
  //    from `accounts[Earn].totalEquity`.
  for (const ep of live.active_earn_positions ?? []) {
    const usd = ep.amount * priceOf(ep.coin);
    out.push({
      venue: categoryToVenue(ep.category),
      product_id: ep.productId,
      coin: ep.coin,
      amount: String(ep.amount),
      amount_usd: usd.toFixed(4),
    });
  }

  // 2. Cash buffer = stable holdings from non-Earn accounts. We skip
  //    non-stables here because (a) their USD value is approximate at
  //    best for spot dust, and (b) "cash" semantically means liquidity
  //    ready to deploy into yield, which is only stables.
  const cashByCoin = new Map<string, number>();
  for (const acct of accounts) {
    if (acct.accountType.toLowerCase().includes("earn")) continue;
    for (const h of acct.holdings ?? []) {
      if (!isStable(h.coin)) continue;
      const prev = cashByCoin.get(h.coin) ?? 0;
      cashByCoin.set(h.coin, prev + (Number.isFinite(h.equity) ? h.equity : 0));
    }
  }
  for (const [coin, amount] of cashByCoin.entries()) {
    if (amount < 0.5) continue;
    const venue = coin === "USDC" ? "cash_usdc" : `cash_${coin.toLowerCase()}`;
    const usd = amount * priceOf(coin); // stables → 1:1 USD
    out.push({
      venue,
      product_id: coin,
      coin,
      amount: String(amount),
      amount_usd: usd.toFixed(4),
    });
  }

  // 3. Perp legs - only the store knows about derivatives. Live
  //    `/api/portfolio` omits the perp side entirely.
  for (const p of storePositions) {
    if (p.venue === "perp") out.push(p);
  }

  return out;
}

// Donut palette - aligned with the per-leg colours used inside trade
// cards: gold/amber for base earn venues, blue for perp hedge legs (we
// no longer show them in the donut but the legend item uses the same
// hue), neutral grey for cash buffer.
const ALLOC_VENUE_META: Record<
  string,
  { label: string; sub: string; color: string }
> = {
  aave_v3_usdc: { label: "Aave V3 USDC", sub: "lending · on-chain", color: "#F6A94B" },
  aave_v3_weth: { label: "Aave V3 WETH", sub: "lending · on-chain", color: "#FFC97A" },
  bybit_flex: { label: "Bybit Flexible Earn", sub: "stable yield", color: "#F6A94B" },
  bybit_onchain: { label: "Bybit OnChain Earn", sub: "staked / hedged", color: "#D9A005" },
  bybit_lm: { label: "Bybit Liquidity Mining", sub: "CPMM LP", color: "#FFC97A" },
  bybit_discount_buy: { label: "Bybit DiscountBuy", sub: "range payoff", color: "#E8B428" },
  bybit_dual_asset: { label: "Bybit DualAsset", sub: "either-side delivery", color: "#C99500" },
  bybit_hold_to_earn: { label: "Bybit Hold-to-Earn", sub: "stables", color: "#B89800" },
  bybit_alpha: { label: "Bybit Alpha Farm", sub: "DEX exposure", color: "#A88500" },
  perp: { label: "Bybit USDT-Perp", sub: "hedge leg · off-book", color: "#A78BFA" },
  cash_usdc: { label: "Cash buffer", sub: "idle · liquidity", color: "#3F4860" },
};

const ALLOC_FALLBACK_PALETTE = [
  "#A78BFA",
  "#C4B5FD",
  "#A6BEFC",
  "#9C7BFB",
  "#B488FB",
  "#7BCBFB",
  "#FBBF24",
  "#7C5CD6",
];

const VENUE_KIND_LABELS: Record<string, string> = {
  bybit_onchain: "OnChain Stake",
  bybit_flex: "Flexible Earn",
  bybit_lm: "Liquidity Mining",
  bybit_discount_buy: "DiscountBuy",
  bybit_dual_asset: "DualAsset",
  bybit_hold_to_earn: "Hold-to-Earn",
  bybit_alpha: "Alpha Farm",
  aave_v3_usdc: "Aave Supply",
  aave_v3_weth: "Aave Supply",
};

type HedgedTrade = {
  coin: string;
  baseLegs: PositionRow[];
  perp: PositionRow;
  baseUsd: number;
  perpUsd: number;
  baseQty: number;
  perpQty: number;
};

type PassivePosition = {
  key: string;
  coin: string;
  venue: string;
  productId: string;
  amountUsd: number;
  amount: number;
};

type CashRow = {
  coin: string;
  amountUsd: number;
  amount: number;
};

function numStr(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function groupPositions(positions: PositionRow[]): {
  trades: HedgedTrade[];
  passive: PassivePosition[];
  cash: CashRow[];
} {
  const byCoin = new Map<string, PositionRow[]>();
  const cashMap = new Map<string, { amount: number; amountUsd: number }>();

  for (const p of positions) {
    if (p.venue.startsWith("cash_")) {
      const coin = p.coin ?? p.venue.replace(/^cash_/, "").toUpperCase();
      const prev = cashMap.get(coin) ?? { amount: 0, amountUsd: 0 };
      prev.amount += numStr(p.amount);
      prev.amountUsd += numStr(p.amount_usd);
      cashMap.set(coin, prev);
      continue;
    }
    const key = p.coin ?? "_nocoin";
    if (!byCoin.has(key)) byCoin.set(key, []);
    byCoin.get(key)!.push(p);
  }

  const trades: HedgedTrade[] = [];
  const passive: PassivePosition[] = [];

  for (const [coin, rows] of byCoin.entries()) {
    const perp = rows.find((r) => r.venue === "perp");
    const earns = rows.filter((r) => r.venue !== "perp");
    if (perp && earns.length > 0) {
      const baseUsd = earns.reduce((s, e) => s + numStr(e.amount_usd), 0);
      const baseQty = earns.reduce((s, e) => s + numStr(e.amount), 0);
      const perpUsd = Math.abs(numStr(perp.amount_usd));
      const perpQty = Math.abs(numStr(perp.amount));
      trades.push({
        coin,
        baseLegs: earns,
        perp,
        baseUsd,
        perpUsd,
        baseQty,
        perpQty,
      });
    } else {
      for (const e of earns) {
        passive.push({
          key: `${e.venue}/${e.product_id}/${e.coin ?? ""}`,
          coin: coin === "_nocoin" ? (e.coin ?? "-") : coin,
          venue: e.venue,
          productId: e.product_id,
          amountUsd: numStr(e.amount_usd),
          amount: numStr(e.amount),
        });
      }
    }
  }

  trades.sort((a, b) => b.baseUsd + b.perpUsd - (a.baseUsd + a.perpUsd));
  passive.sort((a, b) => b.amountUsd - a.amountUsd);
  const cash: CashRow[] = Array.from(cashMap.entries())
    .map(([coin, v]) => ({ coin, amount: v.amount, amountUsd: v.amountUsd }))
    .sort((a, b) => b.amountUsd - a.amountUsd);

  return { trades, passive, cash };
}

function kindLabel(venue: string): string {
  return VENUE_KIND_LABELS[venue] ?? venue.replace(/^bybit_/, "Bybit ");
}

function fmtQty(qty: number, coin: string): string {
  const digits = qty >= 100 ? 2 : qty >= 1 ? 3 : 6;
  return `${qty.toFixed(digits)} ${coin}`;
}

function fmtUsdShort(usd: number, opts: { sign?: boolean } = {}): string {
  const sign = opts.sign && usd > 0 ? "+" : usd < 0 ? "-" : "";
  const abs = Math.abs(usd);
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 10_000) return `${sign}$${(abs / 1_000).toFixed(1)}k`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(2)}k`;
  return `${sign}$${abs.toFixed(2)}`;
}

// Hedge tolerance band for the Net-Δ gauge. Inside ±2% the trade is
// treated as effectively neutral; ±2-10% is the operator-acceptable
// drift band (next cycle will re-hedge); beyond ±10% the position is
// exposed and the gauge turns red.
const NEUTRAL_BAND = 0.02;
const DRIFT_BAND = 0.1;

function TradeCard({ trade, totalUsd }: { trade: HedgedTrade; totalUsd: number }) {
  // Headline = base leg USD only. The hedge perp lives off-book as
  // margin against the base, so adding them would double-count and the
  // percent-of-book figure would diverge from the dollar figure.
  const pctOfBook = totalUsd > 0 ? (trade.baseUsd / totalUsd) * 100 : 0;
  const venues = Array.from(new Set(trade.baseLegs.map((l) => l.venue)));
  const baseLabel = venues.length === 1 ? kindLabel(venues[0]) : "Multi-leg base";
  // Same `product_id` repeated across sub-orders just means one
  // product, multiple top-ups. Deduplicate and show count instead.
  const uniqueProductIds = Array.from(
    new Set(trade.baseLegs.map((l) => l.product_id).filter(Boolean)),
  );
  const subOrderCount = trade.baseLegs.length;

  // Net delta in coin units. Store rows may save perp.amount as the
  // raw notional (always positive) instead of a signed short, so we
  // force the hedge leg negative here: net = base − |perp|.
  const netDelta = trade.baseQty - Math.abs(numStr(trade.perp.amount));
  const driftFrac = Math.abs(netDelta) / Math.max(trade.baseQty, 0.0001);
  const driftPct = driftFrac * 100;
  const status: "neutral" | "drift" | "exposed" =
    driftFrac < NEUTRAL_BAND
      ? "neutral"
      : driftFrac < DRIFT_BAND
        ? "drift"
        : "exposed";
  const statusLabel = {
    neutral: "Δ NEUTRAL",
    drift: "Δ DRIFT",
    exposed: "Δ EXPOSED",
  }[status];
  const statusTone: "pos" | "warn" | "red" = {
    neutral: "pos" as const,
    drift: "warn" as const,
    exposed: "red" as const,
  }[status];
  const statusTitle = {
    neutral: `Within ±${(NEUTRAL_BAND * 100).toFixed(0)}% tolerance - treated as delta-neutral.`,
    drift: `Drift ${driftPct.toFixed(1)}% - within operator band (±${(DRIFT_BAND * 100).toFixed(0)}%). Re-hedge next cycle.`,
    exposed: `Drift ${driftPct.toFixed(1)}% - beyond ±${(DRIFT_BAND * 100).toFixed(0)}% band. Position is directionally exposed until re-hedge.`,
  }[status];

  const netUsd =
    Math.abs(netDelta) * (trade.baseUsd / Math.max(trade.baseQty, 0.0001));

  return (
    <div className="relative bg-gradient-to-b from-ink-850 to-ink-900 border border-ink-600/70 rounded-md overflow-hidden shadow-card-lift">
      <div
        aria-hidden
        className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/30 to-transparent pointer-events-none"
      />
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-ink-600/50 bg-ink-900/40">
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center justify-center min-w-[44px] h-[26px] px-2 rounded-sm bg-accent/[0.10] border border-accent/30 text-accent font-mono text-[11px] tracking-[0.06em]">
            {trade.coin}
          </span>
          <Tag tone={statusTone} className="cursor-help">
            <span title={statusTitle}>{statusLabel}</span>
          </Tag>
        </div>
        <div className="text-right">
          <div className="font-serif tabular text-[20px] text-white leading-none tracking-[-0.02em]">
            {fmtUsdShort(trade.baseUsd)}
          </div>
          <Eyebrow tone="dim" className="mt-1">
            {pctOfBook.toFixed(1)}% of book
          </Eyebrow>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-ink-600/30">
        <div className="bg-ink-900 px-4 py-3.5 border-l-2 border-accent/40">
          <Eyebrow tone="accent" className="mb-1.5">
            Base leg · in book
          </Eyebrow>
          <div className="text-[13px] text-white">{baseLabel}</div>
          <div className="text-[11px] font-mono text-dim-500 mt-0.5">
            {uniqueProductIds.length > 0 && (
              <>
                product {uniqueProductIds.map((id) => `#${id}`).join(", ")}
                {subOrderCount > uniqueProductIds.length && (
                  <span className="text-dim-600">
                    {" "}
                    · {subOrderCount} sub-orders
                  </span>
                )}
              </>
            )}
          </div>
          <div className="mt-2 flex items-baseline justify-between gap-2">
            <span className="font-mono text-[12px] text-dim-300 tabular">
              {fmtQty(trade.baseQty, trade.coin)}
            </span>
            <span className="font-mono text-[13px] text-white tabular">
              {fmtUsdShort(trade.baseUsd)}
            </span>
          </div>
        </div>

        <div className="bg-ink-900 px-4 py-3.5 border-l-2 border-elec/40">
          <Eyebrow tone="dim" className="mb-1.5 text-elec">
            Hedge leg · off-book
          </Eyebrow>
          <div className="text-[13px] text-white">USDT-Perp short</div>
          <div className="text-[11px] font-mono text-dim-500 mt-0.5">
            {trade.coin}USDT · isolated · perp margin
          </div>
          <div className="mt-2 flex items-baseline justify-between gap-2">
            <span className="font-mono text-[12px] text-dim-300 tabular">
              -{fmtQty(trade.perpQty, trade.coin)}
            </span>
            <span className="font-mono text-[13px] text-white tabular">
              {fmtUsdShort(trade.perpUsd)}
            </span>
          </div>
        </div>
      </div>

      <div className="px-4 py-3 bg-ink-900/40 border-t border-ink-600/40 space-y-2">
        <div className="flex items-center justify-between">
          <Eyebrow tone="dim">
            Net delta · |drift| {driftPct.toFixed(1)}%
          </Eyebrow>
          <span
            className={`font-mono text-[12px] tabular ${
              status === "neutral"
                ? "text-pos"
                : status === "drift"
                  ? "text-warn"
                  : "text-danger"
            }`}
          >
            {netDelta >= 0 ? "+" : ""}
            {netDelta.toFixed(3)} {trade.coin} ≈ {fmtUsdShort(netUsd)}
          </span>
        </div>
        <ToleranceGauge driftFrac={driftFrac} signed={netDelta} />
      </div>
    </div>
  );
}

/**
 * Visual hedge-tolerance gauge. The bar is split into three zones:
 *   • green band:  ±NEUTRAL_BAND (treated as neutral)
 *   • amber band:  NEUTRAL_BAND → DRIFT_BAND (operator-acceptable drift)
 *   • red zone:    beyond DRIFT_BAND (directionally exposed)
 * A vertical needle marks the current drift; the tick at center is
 * the perfectly-hedged 0.0 reference.
 */
function ToleranceGauge({
  driftFrac,
  signed,
}: {
  driftFrac: number;
  signed: number;
}) {
  // Map ±DRIFT_BAND (10%) to ±45% of bar width; beyond that we clamp
  // to the outer 5% so the needle stays visible in the "exposed" zone.
  const direction = signed >= 0 ? 1 : -1;
  const clamped = Math.min(driftFrac, DRIFT_BAND * 1.5);
  const halfPct = Math.min((clamped / (DRIFT_BAND * 1.5)) * 50, 50);
  const needleLeft = 50 + direction * halfPct;
  // Zone widths as percentages of bar width.
  const neutralHalf = (NEUTRAL_BAND / (DRIFT_BAND * 1.5)) * 50;
  const driftHalf = (DRIFT_BAND / (DRIFT_BAND * 1.5)) * 50;

  return (
    <div className="relative h-2 rounded-full overflow-hidden bg-ink-700">
      {/* Red zones (extremes) */}
      <span className="absolute inset-y-0 left-0 bg-danger/30" style={{ width: `${50 - driftHalf}%` }} />
      <span className="absolute inset-y-0 right-0 bg-danger/30" style={{ width: `${50 - driftHalf}%` }} />
      {/* Amber drift band */}
      <span
        className="absolute inset-y-0 bg-warn/30"
        style={{ left: `${50 - driftHalf}%`, width: `${driftHalf - neutralHalf}%` }}
      />
      <span
        className="absolute inset-y-0 bg-warn/30"
        style={{ left: `${50 + neutralHalf}%`, width: `${driftHalf - neutralHalf}%` }}
      />
      {/* Neutral band */}
      <span
        className="absolute inset-y-0 bg-pos/40"
        style={{ left: `${50 - neutralHalf}%`, width: `${neutralHalf * 2}%` }}
      />
      {/* Center reference tick */}
      <span className="absolute top-0 bottom-0 left-1/2 w-px bg-white/30" />
      {/* Needle */}
      <span
        className="absolute top-[-2px] bottom-[-2px] w-[2px] bg-white shadow-[0_0_6px_rgba(255,255,255,0.6)]"
        style={{ left: `calc(${needleLeft}% - 1px)` }}
      />
    </div>
  );
}

function SummaryStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "accent" | "pos" | "neutral";
}) {
  const toneCls = {
    accent: "text-accent",
    pos: "text-pos",
    neutral: "text-white",
  }[tone];
  return (
    <div className="bg-ink-900 px-3 py-2.5 text-center">
      <Eyebrow tone="dim" className="!text-[9.5px]">
        {label}
      </Eyebrow>
      <div className={`font-mono tabular text-[13px] mt-1 ${toneCls}`}>{value}</div>
    </div>
  );
}

function PassiveRow({
  pos,
  totalUsd,
}: {
  pos: PassivePosition;
  totalUsd: number;
}) {
  const pct = totalUsd > 0 ? (pos.amountUsd / totalUsd) * 100 : 0;
  return (
    <div className="flex items-center gap-4 px-4 py-3 bg-ink-900 border border-ink-600/60 rounded-md hover:border-ink-500 transition-colors">
      <span className="inline-flex items-center justify-center min-w-[44px] h-[24px] px-2 rounded-sm bg-pos/[0.10] border border-pos/30 text-pos font-mono text-[10.5px] tracking-[0.06em]">
        {pos.coin}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-[13.5px] text-white">{kindLabel(pos.venue)}</div>
        <div className="text-[11px] font-mono text-dim-500 mt-0.5 truncate">
          #{pos.productId} · {fmtQty(pos.amount, pos.coin)}
        </div>
      </div>
      <div className="text-right shrink-0">
        <div className="font-mono text-[13px] text-white tabular">
          {fmtUsdShort(pos.amountUsd)}
        </div>
        <div className="font-mono text-[10.5px] text-dim-500 tabular">
          {pct.toFixed(1)}%
        </div>
      </div>
    </div>
  );
}

function AllocationSection({
  stats,
  allocation,
}: {
  stats: VaultStats;
  allocation: AllocationStats;
}) {
  const storeQuery = usePortfolio();
  const liveQuery = useLivePortfolio();
  const positions = buildPositionsFromLive(
    liveQuery.data,
    storeQuery.data?.positions ?? [],
  );
  // Live snapshot total is authoritative when present (it sums every
  // account; store rows can omit sub-positions). Fall back to the store
  // allocation total, then vault-stats equity, in that order.
  const tvlUsdc =
    liveQuery.data?.total_equity_usd ??
    allocation.totalUsdc ??
    stats.tvlUsdc ??
    0;
  const { trades, passive, cash } = groupPositions(positions);
  const cashUsdTotal = cash.reduce((s, c) => s + c.amountUsd, 0);
  const empty = trades.length === 0 && passive.length === 0 && cashUsdTotal === 0;

  // Donut + legend share the merged `positions[]` so the chart never
  // diverges from the grouped cards on the right. Aggregate USD by
  // venue (cash_* coins fold into one "Cash buffer" bucket) and derive
  // percentages off `tvlUsdc` so the slices match the snapshot total.
  const donutSource: AllocationRow[] = (() => {
    if (positions.length === 0) return allocation.rows;
    const usdByVenue = new Map<string, number>();
    for (const p of positions) {
      // Perp is the hedge leg, not a capital allocation - including it
      // would double-count the base leg and overstate TVL.
      if (p.venue === "perp") continue;
      const usd = numStr(p.amount_usd);
      if (usd <= 0) continue;
      const k = p.venue.startsWith("cash_") ? "cash_usdc" : p.venue;
      usdByVenue.set(k, (usdByVenue.get(k) ?? 0) + usd);
    }
    const total = Array.from(usdByVenue.values()).reduce((s, v) => s + v, 0) || tvlUsdc;
    return Array.from(usdByVenue.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([venue, valueUsdc], idx) => {
        const meta = ALLOC_VENUE_META[venue] ?? {
          label: venue,
          sub: "off-chain",
          color: ALLOC_FALLBACK_PALETTE[idx % ALLOC_FALLBACK_PALETTE.length],
        };
        const pct = total > 0 ? Math.round((valueUsdc / total) * 1000) / 10 : 0;
        return {
          key: venue,
          label: meta.label,
          sub: meta.sub,
          color: meta.color,
          pct,
          apy: 0,
          notional: Math.round(valueUsdc),
          valueUsdc,
        };
      });
  })();

  return (
    <section>
      <SectionHead
        eyebrow="Current Allocation · live portfolio"
        title="Capital distribution"
        subtitle={
          <>
            TVL = base earn + cash buffer. Perp hedge margin is shown paired
            with its base inside each trade (off-book - adding it would
            double-count exposure).
          </>
        }
      />
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <div className="lg:col-span-2 bg-gradient-to-b from-ink-850 to-ink-900 border border-ink-600/70 rounded-md p-6 flex flex-col items-center shadow-card-lift">
          <DonutChart
            data={donutSource.map((a) => ({ pct: a.pct, color: a.color, label: a.key }))}
            size={220}
            thickness={18}
            centerValue={tvlUsdc ? formatTvl(tvlUsdc) : "-"}
            centerLabel="TVL · USD"
          />
          <div className="mt-5 grid grid-cols-1 gap-2 w-full">
            {donutSource.length === 0 && (
              <div className="text-[11px] font-mono text-dim-500 text-center py-4">
                No positions held - agent fully in cash this cycle.
              </div>
            )}
            {donutSource.map((a) => (
              <div key={a.key} className="flex items-center gap-2 text-[11px] font-mono">
                <span
                  className="w-2 h-2 rounded-sm shrink-0"
                  style={{ background: a.color }}
                />
                <span className="text-dim-300 truncate">{a.label}</span>
                <span className="text-white tabular ml-auto">{a.pct}%</span>
              </div>
            ))}
            {/* Hedge margin sits OUTSIDE the donut percentages because it
                represents perp collateral, not capital exposure. Show it
                explicitly so the reader sees it isn't missing - just
                accounted for separately. */}
            {trades.length > 0 && (
              <div className="flex items-center gap-2 text-[11px] font-mono pt-2 mt-1 border-t border-ink-600/40">
                <span className="w-2 h-2 rounded-sm shrink-0 bg-elec border border-dashed border-elec/60" />
                <span className="text-dim-300 truncate">Perp hedge margin</span>
                <span className="text-elec tabular ml-auto">
                  {fmtUsdShort(
                    trades.reduce((s, t) => s + t.perpUsd, 0),
                  )}{" "}
                  off-book
                </span>
              </div>
            )}
          </div>

          <div className="mt-6 w-full grid grid-cols-3 gap-px bg-ink-600/40 rounded-md overflow-hidden border border-ink-600/60">
            {(() => {
              const hedgedUsd = trades.reduce((s, t) => s + t.baseUsd, 0);
              const passiveUsd = passive.reduce((s, p) => s + p.amountUsd, 0);
              return (
                <>
                  <SummaryStat label="Hedged" value={fmtUsdShort(hedgedUsd)} tone="accent" />
                  <SummaryStat label="Passive" value={fmtUsdShort(passiveUsd)} tone="pos" />
                  <SummaryStat label="Cash" value={fmtUsdShort(cashUsdTotal)} tone="neutral" />
                </>
              );
            })()}
          </div>
        </div>

        <div className="lg:col-span-3 space-y-5">
          {empty && (
            <div className="bg-ink-900 border border-ink-600/70 rounded-md px-4 py-8 text-center text-[12px] font-mono text-dim-500">
              No live positions yet.
            </div>
          )}

          {trades.length > 0 && (
            <div className="space-y-3">
              <div className="flex items-baseline justify-between">
                <Eyebrow tone="accent">
                  Delta-neutral trades · {trades.length}
                </Eyebrow>
                <span className="text-[11px] font-mono text-dim-500 tabular">
                  base + hedge per row
                </span>
              </div>
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
                {trades.map((t) => (
                  <TradeCard key={t.coin} trade={t} totalUsd={tvlUsdc} />
                ))}
              </div>
            </div>
          )}

          {passive.length > 0 && (
            <div className="space-y-3">
              <div className="flex items-baseline justify-between">
                <Eyebrow tone="accent">
                  Passive earn · {passive.length}
                </Eyebrow>
                <span className="text-[11px] font-mono text-dim-500 tabular">
                  stable yield, no hedge required
                </span>
              </div>
              <div className="space-y-2">
                {passive.map((p) => (
                  <PassiveRow key={p.key} pos={p} totalUsd={tvlUsdc} />
                ))}
              </div>
            </div>
          )}

          {cash.length > 0 && (
            <div className="space-y-3">
              <div className="flex items-baseline justify-between">
                <Eyebrow tone="accent">
                  Cash buffer · {cash.length} coin{cash.length === 1 ? "" : "s"}
                </Eyebrow>
                <span className="text-[11px] font-mono text-dim-500 tabular">
                  idle liquidity, instantly deployable
                </span>
              </div>
              <div className="space-y-2">
                {cash.map((c) => (
                  <div
                    key={c.coin}
                    className="flex items-center gap-4 px-4 py-3 bg-ink-900 border border-ink-600/60 rounded-md"
                  >
                    <span className="inline-flex items-center justify-center min-w-[52px] h-[26px] px-2 rounded-sm bg-dim-600/30 border border-dim-500/40 text-dim-300 font-mono text-[10.5px] tracking-[0.06em]">
                      {c.coin}
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="text-[13.5px] text-white">Cash {c.coin}</div>
                      <div className="text-[11px] font-mono text-dim-500 mt-0.5 truncate">
                        Bybit wallet · {fmtQty(c.amount, c.coin)}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="font-mono text-[15px] text-white tabular">
                        {fmtUsdShort(c.amountUsd)}
                      </div>
                      <div className="font-mono text-[10.5px] text-dim-500 tabular mt-0.5">
                        {tvlUsdc > 0
                          ? `${((c.amountUsd / tvlUsdc) * 100).toFixed(1)}%`
                          : "-"}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function AttestorAndHedgesSection() {
  const health = useAttestorHealth();
  const hedges = useActiveHedges();
  // Both legs need real signal to keep this section visible. Mock-only
  // attestor + zero hedges is not "what the agent does today" - hide.
  if (!health.isLive && !hedges.isLive) return null;
  return (
    <section>
      <SectionHead
        eyebrow="Off-Chain Trust Surface"
        title="Attestor health & active hedges"
        subtitle="The Bybit-side balance enters on-chain accounting through a 2-of-3 Gnosis Safe attestor. Below: liveness status of that push, plus every delta-neutral position currently open so anyone can verify the hedge is real, not asserted."
        right={
          <Link
            href="/verify"
            className="text-[12px] font-mono text-elec hover:text-elec-soft inline-flex items-center gap-1.5"
          >
            Verify on-chain <Icon.Arrow />
          </Link>
        }
      />
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <div className="lg:col-span-2">
          <AttestorHealthCard />
        </div>
        <div className="lg:col-span-3">
          <HedgeTransparencyCard />
        </div>
      </div>
    </section>
  );
}

function AttestorHealthCard() {
  const health = useAttestorHealth();

  // No on-chain attestor contract wired yet → show an honest placeholder
  // rather than mock lag/push numbers. The attestor address itself is a
  // real static fact (the 2-of-3 Safe), so we still surface it.
  if (!health.isLive) {
    return (
      <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden h-full flex flex-col">
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-600/70 bg-ink-850">
          <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400">
            <span className="w-1.5 h-1.5 rounded-sm bg-warn/70"></span>
            Bybit Attestor Health
          </div>
          <span className="text-[10px] font-mono text-dim-400">NO DATA</span>
        </div>
        <div className="p-5 space-y-4 flex-1 text-[12px] text-dim-300 leading-relaxed">
          <p>
            The BybitAttestor contract isn&apos;t deployed on mainnet yet - once
            live, this panel shows the real attestation lag, push count, and
            freeze status read straight from the chain.
          </p>
          <div className="space-y-2 text-[11px] font-mono pt-1">
            <div className="flex items-center justify-between">
              <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Attestor (Safe)</span>
              <HashChip hash={SAFE_OWNER_ADDRESS} head={6} tail={4} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Control</span>
              <span className="text-dim-300">2-of-3 Gnosis Safe</span>
            </div>
          </div>
        </div>
        <div className="border-t border-ink-600/70 bg-ink-850 px-4 py-2.5 text-[11px] font-mono text-dim-500">
          If lag &gt; 60m, vault freezes new allocations.
        </div>
      </div>
    );
  }

  // Live path - every value below is read from the on-chain attestor.
  const lagMin = Math.floor(health.lagSec / 60);
  const lagSubMin = health.lagSec < 60;
  const warnMin = Math.floor(health.warnThresholdSec / 60);
  const criticalMin = Math.floor(health.criticalThresholdSec / 60);
  const pushStreak = health.pushCount ?? "-";
  const attestorAddr = health.attestorAddress ?? SAFE_OWNER_ADDRESS;
  const cadence = formatHeartbeatShort(health.heartbeatSec);
  const status = health.status;
  const statusTone =
    status === "HEALTHY"
      ? "text-neon"
      : status === "DEGRADED"
        ? "text-warn"
        : status === "CRITICAL"
          ? "text-danger"
          : "text-dim-300";
  const barTone =
    status === "HEALTHY"
      ? "bg-neon"
      : status === "DEGRADED"
        ? "bg-warn"
        : status === "CRITICAL"
          ? "bg-danger"
          : "bg-dim-500";
  const lagPct = Math.min(100, (health.lagSec / health.criticalThresholdSec) * 100);

  return (
    <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden h-full flex flex-col">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-600/70 bg-ink-850">
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400">
          <span className="w-1.5 h-1.5 rounded-sm bg-elec"></span>
          Bybit Attestor Health
        </div>
        <span className={`inline-flex items-center gap-1.5 text-[10px] font-mono ${statusTone}`}>
          <LiveDot size={6} /> {status}
        </span>
      </div>
      <div className="p-5 space-y-4 flex-1">
        <div>
          <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500">Last attestation</div>
          <div className="flex items-baseline gap-2 mt-1">
            <div className="font-mono text-4xl text-white tabular leading-none">
              {lagSubMin ? health.lagSec : lagMin}
            </div>
            <div className="text-dim-400 font-mono text-sm">
              {lagSubMin ? "sec ago" : "min ago"}
            </div>
          </div>
          <div className="mt-3 relative">
            <div className="h-1.5 bg-ink-700 rounded-sm overflow-hidden">
              <div
                className={`h-full transition-all ${barTone}`}
                style={{ width: lagPct + "%" }}
              />
            </div>
            <div className="flex items-center justify-between mt-1.5 text-[9.5px] font-mono">
              <span className="text-dim-600">0m</span>
              <span className="text-warn">{warnMin}m warn</span>
              <span className="text-danger">{criticalMin}m halt</span>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-px bg-ink-600/40 border border-ink-600/60 rounded-sm overflow-hidden">
          <div className="bg-ink-900 px-3 py-2.5">
            <div className="text-[9.5px] font-mono uppercase tracking-[0.16em] text-dim-500">Push count</div>
            <div className="font-mono text-base text-white tabular mt-1">{pushStreak}</div>
          </div>
          <div className="bg-ink-900 px-3 py-2.5">
            <div className="text-[9.5px] font-mono uppercase tracking-[0.16em] text-dim-500">Lagged 24h</div>
            <div className="font-mono text-base text-neon tabular mt-1">-</div>
          </div>
        </div>

        <div className="space-y-2 text-[11px] font-mono pt-1">
          <div className="flex items-center justify-between">
            <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Attestor</span>
            <HashChip hash={attestorAddr} head={6} tail={4} />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Control</span>
            <span className="text-dim-300">2-of-3 Gnosis Safe</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Cadence</span>
            <span className="text-dim-300">{cadence}</span>
          </div>
        </div>
      </div>
      <div className="border-t border-ink-600/70 bg-ink-850 px-4 py-2.5 flex items-center justify-between text-[11px] font-mono">
        <a
          href={mantleExplorerAddress(attestorAddr)}
          target="_blank"
          rel="noopener noreferrer"
          className="text-elec hover:text-elec-soft inline-flex items-center gap-1.5"
        >
          View Safe <Icon.Ext />
        </a>
        <span className="text-dim-500">If lag &gt; 60m, vault freezes new allocations.</span>
      </div>
    </div>
  );
}

function HedgeTransparencyCard() {
  const { hedges, isLive } = useActiveHedges();
  const dash = (live: boolean) => (live ? "-" : null);
  return (
    <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden h-full flex flex-col">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-600/70 bg-ink-850">
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400">
          <span className="w-1.5 h-1.5 rounded-sm bg-neon"></span>
          Active Hedges · Delta-Neutral
        </div>
        <span className="text-[10px] font-mono text-dim-500">{hedges.length} open positions</span>
      </div>
      <div className="divide-y divide-ink-600/40 flex-1">
        {hedges.map((h) => {
          const neutral = Math.abs(h.netDelta) < 0.01 * Math.max(Math.abs(h.spotUsd) / 1000, 1);
          return (
            <div key={h.key} className="p-5">
              <div className="flex items-start justify-between gap-3 mb-3">
                <div>
                  <div className="text-white text-[15px] font-medium">{h.label}</div>
                  <div className="text-[10.5px] text-dim-500 font-mono mt-0.5">
                    Opened {h.openedAgo} · close trigger: {h.closeTrigger}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-[10px] font-mono uppercase tracking-[0.14em] text-dim-500">Blended APR</div>
                  <div className="font-mono text-neon text-lg tabular leading-none mt-0.5">
                    {isLive ? dash(true) : `${h.blendedApr.toFixed(1)}%`}
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-2 mb-3">
                <div className="bg-ink-850/70 border border-ink-600/50 rounded-sm px-3 py-2">
                  <div className="text-[9.5px] font-mono uppercase tracking-[0.16em] text-dim-500">Spot</div>
                  <div className="text-white text-[13px] tabular font-mono mt-0.5">{h.spotQty}</div>
                  <div className="text-dim-400 text-[11px] font-mono tabular">
                    ${h.spotUsd.toLocaleString()}
                  </div>
                  <div className="text-[10px] text-dim-500 font-mono mt-1">{h.venueSpot}</div>
                </div>
                <div className="bg-ink-850/70 border border-ink-600/50 rounded-sm px-3 py-2">
                  <div className="text-[9.5px] font-mono uppercase tracking-[0.16em] text-dim-500">Hedge</div>
                  <div className="text-white text-[13px] tabular font-mono mt-0.5">{h.hedgeQty}</div>
                  <div className="text-dim-400 text-[11px] font-mono tabular">
                    ${h.hedgeUsd.toLocaleString()} notional
                  </div>
                  <div className="text-[10px] text-dim-500 font-mono mt-1">{h.venuePerp}</div>
                </div>
              </div>

              <div className="grid grid-cols-4 gap-px bg-ink-600/40 rounded-sm overflow-hidden text-[11px] font-mono">
                <div className="bg-ink-900 px-2.5 py-2">
                  <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">Net Δ</div>
                  <div
                    className={`tabular mt-0.5 inline-flex items-center gap-1 ${
                      neutral ? "text-neon" : "text-warn"
                    }`}
                  >
                    {h.netDelta.toFixed(neutral ? 0 : 3)}{" "}
                    {neutral && <Icon.Check className="w-3 h-3" />}
                  </div>
                </div>
                <div className="bg-ink-900 px-2.5 py-2">
                  <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">Spot APR</div>
                  <div className="text-white tabular mt-0.5">
                    {isLive ? dash(true) : `${h.spotApr.toFixed(1)}%`}
                  </div>
                </div>
                <div className="bg-ink-900 px-2.5 py-2">
                  <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">Funding APR</div>
                  <div className="text-white tabular mt-0.5">
                    {isLive ? dash(true) : `${h.fundingApr.toFixed(1)}%`}
                  </div>
                </div>
                <div className="bg-ink-900 px-2.5 py-2">
                  <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">Earned 24h</div>
                  <div className="text-neon tabular mt-0.5">
                    {isLive ? dash(true) : `+$${h.fundingEarned24h.toFixed(2)}`}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <div className="border-t border-ink-600/70 bg-ink-850 px-4 py-2.5 flex items-center justify-between text-[11px] font-mono">
        <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Lifetime funding harvested</span>
        <span className="text-dim-400 tabular">-</span>
      </div>
    </div>
  );
}

type RecentRow = {
  key: string;
  href: string | null;
  ago: string;
  summary: string;
  risk: "LOW" | "MED" | "HIGH";
  confidence: number;
};

function formatAgoSec(unixSec: number, nowMs: number = Date.now()): string {
  const diffSec = Math.max(0, Math.floor(nowMs / 1000 - unixSec));
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  const diffD = Math.floor(diffH / 24);
  const remH = diffH - diffD * 24;
  return remH > 0 ? `${diffD}d ${remH}h ago` : `${diffD}d ago`;
}

function cycleToRecentRow(cycle: CycleSummary): RecentRow {
  const startedSec = Math.floor(new Date(cycle.started_at).getTime() / 1000);
  const acted = cycle.actions_executed ?? 0;
  const summary = cycle.error
    ? `Cycle errored: ${cycle.error.slice(0, 80)}`
    : acted === 0 || cycle.result === "no_change"
      ? `Held - ${cycle.wake_reason}`
      : `${acted} action${acted === 1 ? "" : "s"} executed (${cycle.wake_reason})`;
  const risk: "LOW" | "MED" | "HIGH" = cycle.error
    ? "HIGH"
    : (cycle.confidence ?? 1) < 0.6
      ? "MED"
      : "LOW";
  return {
    key: cycle.cycle_ts,
    href: `/history/${encodeURIComponent(cycle.cycle_ts)}`,
    ago: formatAgoSec(startedSec),
    summary,
    risk,
    confidence: cycle.confidence ?? 0,
  };
}

function eventToneClasses(severity: string): string {
  switch (severity) {
    case "red":
    case "critical":
      return "text-danger border-danger/30 bg-danger/5";
    case "warn":
    case "warning":
      return "text-warn border-warn/30 bg-warn/10";
    default:
      return "text-dim-300 border-ink-600/40 bg-ink-850/40";
  }
}

function eventRowInner(ev: EventRow): React.ReactNode {
  return (
    <>
      <span className="text-[10.5px] uppercase tracking-[0.14em] opacity-80">{ev.kind}</span>
      {ev.coin && <span className="text-[10.5px] opacity-80">{ev.coin}</span>}
      {ev.position_id && (
        <span className="text-[10.5px] opacity-60 hidden md:inline truncate max-w-[40%]">
          {ev.position_id}
        </span>
      )}
      <span className="text-[10.5px] tabular text-dim-500 ml-auto">
        {formatDateTime(ev.event_ts)}
      </span>
      {ev.triggered_cycle_ts && <Icon.Chev className="text-dim-500" />}
    </>
  );
}

function PlannedVsActualSection() {
  const data = usePlannedVsActual();
  // Hide when there's nothing meaningful to compare (no decision, or
  // plan exactly matches reality within rounding).
  if (data.rows.length === 0) return null;
  if (!data.hasGap && data.actionsPlanned === data.actionsExecuted) return null;

  const fillStatus =
    data.actionsExecuted !== null && data.actionsPlanned !== null
      ? `${data.actionsExecuted} / ${data.actionsPlanned} actions filled`
      : "executor status unknown";
  const fillTone =
    data.actionsExecuted === 0 && (data.actionsPlanned ?? 0) > 0
      ? "text-warn"
      : data.actionsExecuted === data.actionsPlanned
      ? "text-neon"
      : "text-warn";

  return (
    <section>
      <SectionHead
        eyebrow="Planned vs Actual"
        title="Decision target vs what's actually held"
        subtitle="Latest cycle's allocation intent (left) compared to the snapshot's real positions (right). Non-zero `Δ` means the executor hasn't filled the rebalance yet - happens in dry-run, mid-cycle, or when a rate-limited subscribe is pending."
        right={
          <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm border border-ink-500 bg-ink-900 font-mono text-[10.5px] uppercase tracking-[0.14em] ${fillTone}`}>
            {fillStatus}
          </span>
        }
      />
      <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden">
        <div className="grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 border-b border-ink-600/70 px-4 py-2.5 bg-ink-850">
          <div className="col-span-5">Venue</div>
          <div className="col-span-2 text-right">Planned</div>
          <div className="col-span-2 text-right">Actual</div>
          <div className="col-span-3 text-right">Δ (plan − real)</div>
        </div>
        {data.rows.map((r, i) => {
          const diffTone =
            Math.abs(r.diffPct) < 0.5
              ? "text-dim-300"
              : r.diffPct > 0
              ? "text-warn"
              : "text-elec";
          const sign = r.diffPct > 0 ? "+" : "";
          return (
            <div
              key={r.venue}
              className={`grid grid-cols-12 items-center px-4 py-2.5 ${
                i !== data.rows.length - 1 ? "border-b border-ink-600/40" : ""
              }`}
            >
              <div className="col-span-5 font-mono text-[12.5px] text-white">
                {r.label}
              </div>
              <div className="col-span-2 text-right font-mono text-[12.5px] text-dim-200 tabular">
                {r.plannedPct.toFixed(1)}%
              </div>
              <div className="col-span-2 text-right font-mono text-[12.5px] text-dim-200 tabular">
                {r.actualPct.toFixed(1)}%
              </div>
              <div className={`col-span-3 text-right font-mono text-[12.5px] tabular ${diffTone}`}>
                {sign}{r.diffPct.toFixed(1)}%
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function RecentWatcherEventsWidget() {
  const eventsQuery = useRecentEvents(5);
  const events = eventsQuery.data ?? [];
  // No data + not loading + no error → nothing to surface (pre-deploy
  // FastAPI or empty stream); collapsing the section keeps the homepage
  // tight rather than showing an always-empty box.
  if (events.length === 0 && !eventsQuery.isLoading && !eventsQuery.isError) {
    return null;
  }
  return (
    <section>
      <SectionHead
        eyebrow="Watcher Feed"
        title="Recent watcher events"
        subtitle="Position-level watchers that wake the agent off the 4h cron cadence - funding flips, attestor lag, peg deviations, pending redemptions. Each event links to the cycle it triggered."
        right={
          <Link
            href="/history"
            className="text-[12px] font-mono text-elec hover:text-elec-soft inline-flex items-center gap-1.5"
          >
            View all cycles <Icon.Arrow />
          </Link>
        }
      />
      {eventsQuery.isError && events.length === 0 ? (
        <ErrorPanel
          label="Couldn't reach the agent watcher feed."
          message={eventsQuery.error?.message}
        />
      ) : (
        <Card className="overflow-hidden">
          {eventsQuery.isLoading && events.length === 0 && (
            <div className="px-4 py-3 space-y-2">
              <SkeletonRow width="70%" />
              <SkeletonRow width="55%" />
              <SkeletonRow width="65%" />
            </div>
          )}
          {events.map((ev, i) => {
          const className = `flex items-center gap-3 px-4 sm:px-5 py-3 font-mono ${
            i !== events.length - 1 ? "border-b border-ink-600/40" : ""
          } ${eventToneClasses(ev.severity)} ${ev.triggered_cycle_ts ? "hover:bg-ink-850/60 transition-colors" : ""}`;
          return ev.triggered_cycle_ts ? (
            <Link
              key={ev.id}
              href={`/history/${encodeURIComponent(ev.triggered_cycle_ts)}`}
              className={className}
            >
              {eventRowInner(ev)}
            </Link>
          ) : (
            <div key={ev.id} className={className}>
              {eventRowInner(ev)}
            </div>
          );
        })}
        </Card>
      )}
    </section>
  );
}

function RecentDecisionsPreview() {
  const cyclesQuery = useCycles({ limit: 5 });
  const cycles = cyclesQuery.data ?? [];
  const recent: RecentRow[] = cycles.slice(0, 5).map(cycleToRecentRow);
  const showError = cyclesQuery.isError && cycles.length === 0;
  const showLoading = cyclesQuery.isLoading && recent.length === 0;
  const showEmpty =
    !showLoading && !showError && recent.length === 0;

  return (
    <section>
      <SectionHead
        eyebrow="Latest Agent Activity"
        title="Recent decisions"
        subtitle="Last 5 cycles from the live agent. Click through for the full off-chain rationale, validator outcome, and watcher events."
        right={
          <Link
            href="/history"
            className="text-[12px] font-mono text-elec hover:text-elec-soft inline-flex items-center gap-1.5"
          >
            View full log <Icon.Arrow />
          </Link>
        }
      />
      {showError && (
        <ErrorPanel
          label="Couldn't reach the agent cycle store."
          message={cyclesQuery.error?.message}
          className="mb-3"
        />
      )}
      <Card className="overflow-hidden">
        {showLoading && (
          <div className="px-4 sm:px-5 py-3.5 space-y-2">
            <SkeletonRow width="70%" />
            <SkeletonRow width="60%" />
            <SkeletonRow width="65%" />
            <SkeletonRow width="55%" />
            <SkeletonRow width="65%" />
          </div>
        )}
        {showEmpty && (
          <div className="px-4 sm:px-5 py-5 text-center text-[12px] font-mono text-dim-400">
            No cycles recorded yet - the agent will populate this once the first loop completes.
          </div>
        )}
        {recent.map((r, i) => {
          const className = `flex items-center gap-4 px-4 sm:px-5 py-3.5 ${
            i !== recent.length - 1 ? "border-b border-ink-600/40" : ""
          } ${r.href ? "hover:bg-ink-850/60 transition-colors" : ""}`;
          const inner = (
            <>
              <div className="font-mono text-[11px] text-dim-500 tabular w-20 hidden sm:block">{r.ago}</div>
              <div className="flex-1 text-sm text-white min-w-0 truncate">{r.summary}</div>
              <Tag tone={r.risk === "LOW" ? "green" : r.risk === "MED" ? "warn" : "red"}>
                RISK: {r.risk}
              </Tag>
              <span className="font-mono text-[11px] text-dim-400 hidden lg:inline tabular">
                conf {r.confidence.toFixed(2)}
              </span>
              <Icon.Chev className="text-dim-500" />
            </>
          );
          return r.href ? (
            <Link key={r.key} href={r.href} className={className}>
              {inner}
            </Link>
          ) : (
            <div key={r.key} className={className}>
              {inner}
            </div>
          );
        })}
      </Card>
    </section>
  );
}
