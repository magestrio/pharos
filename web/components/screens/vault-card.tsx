"use client";

import { useState } from "react";

import {
  ACTIVE_HEDGES,
  ALLOCATIONS,
  ATTESTOR,
  BYBIT_SUB,
  DECISIONS,
  EXCHANGE_RATE_SERIES,
  HEDGE_LIFETIME_FUNDING,
  VAULT,
  type Allocation,
} from "@/lib/data";
import { usePortfolio } from "@/lib/agent-store-context";
import type { PositionRow } from "@/lib/agent-api";
import { useAllocationStats, type AllocationStats } from "@/lib/hooks/use-allocation-stats";
import { useVaultStats, type VaultStats } from "@/lib/hooks/use-vault-stats";
import {
  Card,
  DonutChart,
  HashChip,
  Icon,
  LineChart,
  LiveDot,
  SectionHead,
  StatCard,
  Tag,
} from "@/components/ui";

export function VaultCard() {
  const stats = useVaultStats();
  const allocation = useAllocationStats();
  return (
    <div className="space-y-10 sm:space-y-12">
      <HeroBlock stats={stats} />
      <StatsRow stats={stats} />
      <ExchangeRateSection stats={stats} />
      <AllocationSection stats={stats} allocation={allocation} />
      <AttestorAndHedgesSection />
      <RecentDecisionsPreview />
    </div>
  );
}

function HeroBlock({ stats }: { stats: VaultStats }) {
  return (
    <section className="grid grid-cols-1 lg:grid-cols-3 gap-6 lg:gap-8 items-start">
      <div className="lg:col-span-2 space-y-6">
        <HeroBadgeLine />
        <div className="space-y-4">
          <h1 className="text-[40px] sm:text-[52px] lg:text-[60px] leading-[1.02] font-semibold tracking-tight text-white">
            <span className="font-mono text-neon">vUSDC</span>
            <span className="text-dim-400"> — </span>AI-Managed
            <br />
            Yield-Bearing USDC<span className="text-neon">.</span>
          </h1>
          <p className="text-[15px] sm:text-base text-dim-300 max-w-2xl leading-relaxed">
            Mint USDC, receive vUSDC. The exchange rate grows as our agent allocates across
            <span className="text-white"> Aave V3</span> and{" "}
            <span className="text-white">Bybit Earn (200+ products)</span> with delta-neutral hedging on volatile
            positions. Every decision logged on-chain. Reputation verifiable through ERC-8004.
          </p>
          <div className="flex flex-wrap items-center gap-3 pt-2">
            <button className="group inline-flex items-center gap-2 bg-neon text-black px-4 h-10 rounded-sm text-[13px] font-medium hover:bg-neon-soft transition-colors">
              Mint vUSDC
              <Icon.Arrow className="-mr-1 transition-transform group-hover:translate-x-0.5" />
            </button>
            <button className="inline-flex items-center gap-2 bg-transparent border border-ink-500 text-white px-4 h-10 rounded-sm text-[13px] font-medium hover:border-ink-400 hover:bg-ink-800 transition-colors">
              <Icon.Block /> View Live Vault
            </button>
            <div className="hidden md:flex items-center gap-2 ml-2 pl-3 border-l border-ink-600 text-[11px] text-dim-400 font-mono">
              <span>Deployed</span>
              <span className="text-white">{VAULT.inception}</span>
              <span className="text-dim-600">·</span>
              <span className="text-white">{stats.daysLive}d live</span>
            </div>
          </div>
        </div>
      </div>
      <div className="lg:col-span-1">
        <div className="lg:sticky lg:top-24">
          <ReputationNFTCard />
        </div>
      </div>
    </section>
  );
}

function HeroBadgeLine() {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 font-mono text-[11.5px] uppercase tracking-[0.14em]">
      <span className="text-white font-semibold">{VAULT.id}</span>
      <span className="text-dim-600">│</span>
      <span className="text-dim-300">
        ERC-8004 <span className="text-white">{VAULT.erc8004}</span>
      </span>
      <span className="text-dim-600">│</span>
      <span className="text-dim-300">
        Reputation <span className="text-white">{VAULT.reputation}</span>
      </span>
      <span className="text-dim-600">│</span>
      <span className="inline-flex items-center gap-2 text-neon">
        <LiveDot /> {VAULT.status}
      </span>
      <span className="text-dim-600">│</span>
      <span className="text-dim-300">Mantle Mainnet</span>
    </div>
  );
}

type Phase = "idle" | "pulling" | "computing" | "success";

function ReputationNFTCard() {
  const [score, setScore] = useState(VAULT.reputation);
  const [phase, setPhase] = useState<Phase>("idle");
  const [lastTx, setLastTx] = useState<string | null>(null);

  const onUpdate = () => {
    if (phase !== "idle") return;
    setPhase("pulling");
    setTimeout(() => setPhase("computing"), 900);
    setTimeout(() => {
      const bump = 1 + Math.floor(Math.random() * 4);
      setScore((s) => Math.min(1000, s + bump));
      const rand = () => Math.floor(Math.random() * 16).toString(16);
      const hex = Array.from({ length: 40 }, rand).join("");
      setLastTx("0x" + hex);
      setPhase("success");
      setTimeout(() => setPhase("idle"), 4500);
    }, 2100);
  };

  const pct = (score / VAULT.reputationMax) * 100;

  return (
    <div className="relative">
      <div
        className="absolute -inset-[1px] rounded-md pointer-events-none"
        style={{ background: "radial-gradient(120% 80% at 50% 0%, rgba(0,255,136,0.18), transparent 60%)" }}
      />
      <div className="relative bg-ink-900 border border-ink-500/80 rounded-md overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-600/70 bg-ink-850">
          <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400">
            <span className="w-1.5 h-1.5 rounded-sm bg-neon"></span>
            ERC-8004 Reputation
          </div>
          <div className="text-[10px] font-mono text-dim-500">TOKEN #001</div>
        </div>

        <div className="p-5 pb-4 bg-dots">
          <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-3">Score</div>
          <div className="flex items-baseline gap-2">
            <div className="font-mono text-[64px] leading-none font-semibold text-white tabular tracking-tight">
              {score}
            </div>
            <div className="text-dim-500 font-mono text-lg tabular">/ 1000</div>
          </div>
          <div className="mt-4 h-[3px] bg-ink-700 overflow-hidden rounded-sm">
            <div className="h-full bg-neon transition-all duration-700" style={{ width: pct + "%" }} />
          </div>
          <div className="mt-2 flex items-center justify-between text-[10.5px] font-mono">
            <span className="text-dim-500">Decile rank</span>
            <span className="text-neon">TOP 16%</span>
          </div>
        </div>

        <div className="grid grid-cols-2 border-t border-ink-600/70">
          <MetricCell label="Sharpe" value={VAULT.sharpe.toFixed(2)} />
          <MetricCell label="Max DD" value={VAULT.maxDD.toFixed(1) + "%"} border="left" />
          <MetricCell label="Win Rate" value={VAULT.winRate + "%"} border="top" />
          <MetricCell label="Decisions" value={String(VAULT.decisions)} border="top left" />
        </div>

        <div className="border-t border-ink-600/70 bg-ink-850 p-4 space-y-3">
          <button
            onClick={onUpdate}
            disabled={phase !== "idle"}
            className={`w-full inline-flex items-center justify-center gap-2 px-3 h-10 rounded-sm text-[12.5px] font-mono tracking-[0.08em] uppercase font-medium transition-all
              ${
                phase === "idle"
                  ? "bg-neon/10 border border-neon/40 text-neon hover:bg-neon/20"
                  : phase === "success"
                    ? "bg-neon text-black border border-neon"
                    : "bg-ink-700 border border-ink-500 text-dim-300 cursor-wait"
              }`}
          >
            {phase === "idle" && <>[ Update Reputation ]</>}
            {(phase === "pulling" || phase === "computing") && (
              <>
                <Icon.Spinner className="animate-spin" />
                {phase === "pulling" ? "Pulling vault.totalAssets()…" : "Computing Sharpe + signing…"}
              </>
            )}
            {phase === "success" && (
              <>
                <Icon.Check /> Score updated on-chain
              </>
            )}
          </button>

          <div className="min-h-[42px] text-[11px] font-mono">
            {phase === "idle" && !lastTx && (
              <div className="text-dim-500 leading-relaxed">
                Recomputes Sharpe, max-DD, win-rate from on-chain history. Updates token URI. Gas ≈ 0.012 MNT.
              </div>
            )}
            {phase === "pulling" && (
              <div className="space-y-1.5 fade-up">
                <ProgressLine label="vault.totalAssets()" />
                <div className="text-dim-500">Reading state from block #4,219,847…</div>
              </div>
            )}
            {phase === "computing" && (
              <div className="space-y-1.5 fade-up">
                <ProgressLine label="computeSharpe(returns[])" />
                <div className="text-dim-500">Hashing metrics, signing tx…</div>
              </div>
            )}
            {phase === "success" && lastTx && (
              <div className="space-y-1 fade-up">
                <div className="flex items-center gap-2 text-neon">
                  <LiveDot /> Confirmed in 2 blocks
                </div>
                <div className="text-dim-400">
                  Score {VAULT.reputation} → <span className="text-white">{score}</span>
                </div>
                <HashChip hash={lastTx} label="tx:" />
              </div>
            )}
            {phase === "idle" && lastTx && (
              <div className="text-dim-500">
                Last update: <HashChip hash={lastTx} label="tx:" className="!text-[11px]" />
              </div>
            )}
          </div>

          <div className="flex items-center justify-between pt-1 border-t border-ink-600/60 -mx-4 px-4 -mb-4 pb-4 text-[11px] font-mono">
            <a className="text-dim-400 hover:text-white inline-flex items-center gap-1.5 transition-colors" href="#">
              View NFT on Mantle Explorer <Icon.Ext />
            </a>
            <span className="text-dim-600">v1.0.4</span>
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
        <div className="h-full bg-neon bar-fill"></div>
      </div>
    </div>
  );
}

function MetricCell({ label, value, border = "" }: { label: string; value: string; border?: string }) {
  const borderCls = [
    border.includes("top") ? "border-t" : "",
    border.includes("left") ? "border-l" : "",
  ].join(" ");
  return (
    <div className={`p-3.5 ${borderCls} border-ink-600/70`}>
      <div className="text-[9.5px] font-mono uppercase tracking-[0.18em] text-dim-500">{label}</div>
      <div className="font-mono text-xl text-white mt-1 tabular">{value}</div>
    </div>
  );
}

function StatsRow({ stats }: { stats: VaultStats }) {
  const exchangeRate = stats.exchangeRate ?? VAULT.exchangeRate;
  const cumReturnPct = stats.cumReturnPct ?? (VAULT.exchangeRate / VAULT.exchangeRateStart - 1) * 100;
  const tvlUsdc = stats.tvlUsdc ?? VAULT.tvlUsdc;
  return (
    <section className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
      <StatCard
        label="vUSDC Exchange Rate"
        value={exchangeRate.toFixed(5)}
        tone="green"
        sub={<span>+{cumReturnPct.toFixed(3)}% since inception</span>}
      />
      <StatCard
        label="Total Value Locked"
        value={"$" + (tvlUsdc / 1_000_000).toFixed(3) + "M"}
        sub={<span className="text-neon">+${(VAULT.tvlDelta / 1000).toFixed(0)}k since launch</span>}
      />
      <StatCard
        label="Effective APY"
        value={VAULT.apyEffective.toFixed(1) + "%"}
        sub={<span>annualised from 21-day window</span>}
      />
      <StatCard
        label="Decisions Logged"
        value={String(VAULT.decisions)}
        sub={<span>Event-driven · 4h cron fallback</span>}
      />
    </section>
  );
}

function ExchangeRateSection({ stats }: { stats: VaultStats }) {
  const exchangeRate = stats.exchangeRate ?? VAULT.exchangeRate;
  return (
    <section>
      <SectionHead
        eyebrow="vUSDC / USDC Exchange Rate"
        title="21-day monotonic appreciation"
        subtitle="Every share of vUSDC redeems for more USDC than the day before. The rate only moves one direction by design — yield is realised, never marked-to-market."
        right={
          <div className="hidden md:flex items-center gap-3 font-mono text-[12px]">
            <div className="flex items-center gap-2">
              <span className="text-dim-500 uppercase tracking-[0.14em] text-[10px]">D0</span>
              <span className="text-dim-300 tabular">{EXCHANGE_RATE_SERIES[0].toFixed(5)}</span>
            </div>
            <span className="text-dim-600">→</span>
            <div className="flex items-center gap-2">
              <span className="text-dim-500 uppercase tracking-[0.14em] text-[10px]">
                D{EXCHANGE_RATE_SERIES.length - 1}
              </span>
              <span className="text-neon tabular">{exchangeRate.toFixed(5)}</span>
            </div>
            <span className="text-dim-600">·</span>
            <span className="text-neon tabular">+{((exchangeRate - 1) * 10000).toFixed(0)} bps</span>
          </div>
        }
      />
      <Card className="p-5 sm:p-6">
        <div className="h-[240px] sm:h-[280px] -mx-2">
          <LineChart
            series={EXCHANGE_RATE_SERIES}
            color="#00FF88"
            fillColor="rgba(0,255,136,0.12)"
            label="vusdcrate"
            baseline={1.0}
            width={1200}
            height={280}
            pad={{ t: 18, r: 16, b: 26, l: 56 }}
          />
        </div>
      </Card>
    </section>
  );
}

const BYBIT_SUB_PALETTE = ["#5B8FF9", "#7AA5FB", "#A6BEFC", "#345FC2"] as const;

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

function AllocationSection({
  stats,
  allocation,
}: {
  stats: VaultStats;
  allocation: AllocationStats;
}) {
  const [bybitOpen, setBybitOpen] = useState(true);
  const portfolio = usePortfolio();
  const rows = allocation.rows;
  const weightedApy = rows.reduce((s, a) => s + a.pct * a.apy, 0) / 100;
  // Prefer on-chain TVL when present; otherwise fall back to allocation
  // total (mock or live), then the legacy vault stat.
  const tvlUsdc = stats.tvlUsdc ?? allocation.totalUsdc ?? VAULT.tvlUsdc;
  const bybitSubLive = portfolioToBybitSubRows(portfolio.data?.positions ?? []);
  const bybitSubRows = bybitSubLive.length > 0 ? bybitSubLive : BYBIT_SUB;

  return (
    <section>
      <SectionHead
        eyebrow="Current Allocation"
        title="Capital distribution across whitelisted venues"
        subtitle="Live on-chain balances for Aave legs and cash. Bybit-side balances pushed every ~5 minutes by a 2-of-3 Gnosis Safe attestor. Sub-allocation inside Bybit selects from 200+ Earn products + delta-neutral basis trades."
        right={
          <div className="hidden md:flex items-center gap-3 text-[11px] font-mono">
            <span className="text-dim-500 uppercase tracking-[0.14em]">Blended APY</span>
            <span className="text-neon text-lg tabular">{weightedApy.toFixed(2)}%</span>
          </div>
        }
      />
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <div className="lg:col-span-2 bg-ink-900 border border-ink-600/70 rounded-md p-6 flex flex-col items-center justify-center">
          <DonutChart
            data={rows.map((a) => ({ pct: a.pct, color: a.color, label: a.key }))}
            size={220}
            thickness={18}
            centerValue={"$" + (tvlUsdc / 1_000_000).toFixed(2) + "M"}
            centerLabel="TVL · USDC"
          />
          <div className="mt-5 grid grid-cols-2 gap-2 w-full">
            {rows.map((a) => (
              <div key={a.key} className="flex items-center gap-2 text-[11px] font-mono">
                <span className="w-2 h-2 rounded-sm shrink-0" style={{ background: a.color }} />
                <span className="text-dim-300 truncate">{a.label.replace("Aave V3 ", "Aave ")}</span>
                <span className="text-white tabular ml-auto">{a.pct}%</span>
              </div>
            ))}
          </div>
        </div>

        <div className="lg:col-span-3 bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden">
          <div className="grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 border-b border-ink-600/70 px-4 py-2.5 bg-ink-850">
            <div className="col-span-4">Venue</div>
            <div className="col-span-2 text-right">Weight</div>
            <div className="col-span-2 text-right">APY</div>
            <div className="col-span-3 text-right">Notional</div>
            <div className="col-span-1 text-right">Tx</div>
          </div>
          {rows.map((a, i) => {
            const isBybit = a.key === "BYBIT";
            return (
              <div key={a.key} style={{ display: "contents" }}>
                <div
                  className={`grid grid-cols-12 items-center px-4 py-3.5 ${
                    isBybit ? "cursor-pointer hover:bg-ink-850/60" : ""
                  } ${i !== rows.length - 1 || (isBybit && bybitOpen) ? "border-b border-ink-600/40" : ""}`}
                  onClick={isBybit ? () => setBybitOpen((o) => !o) : undefined}
                >
                  <div className="col-span-4 flex items-center gap-3">
                    <span className="w-1.5 h-8 rounded-sm shrink-0" style={{ background: a.color }} />
                    <div className="min-w-0">
                      <div className="text-white text-sm font-medium flex items-center gap-2">
                        {a.label}
                        {isBybit && (
                          <span
                            className={`inline-flex items-center justify-center w-4 h-4 rounded-sm border border-ink-500 text-dim-300 text-[9px] transition-transform ${
                              bybitOpen ? "rotate-90" : ""
                            }`}
                          >
                            <Icon.Chev />
                          </span>
                        )}
                      </div>
                      <div className="text-[11px] text-dim-500 font-mono">{a.sub}</div>
                    </div>
                  </div>
                  <div className="col-span-2 text-right">
                    <div className="font-mono text-white text-sm tabular">{a.pct}%</div>
                    <div className="mt-1 h-[2px] bg-ink-700 rounded-sm overflow-hidden">
                      <div className="h-full" style={{ width: a.pct + "%", background: a.color }} />
                    </div>
                  </div>
                  <div className="col-span-2 text-right font-mono text-sm tabular">
                    <span className={a.apy > 8 ? "text-neon" : a.apy > 0 ? "text-white" : "text-dim-500"}>
                      {a.apy.toFixed(2)}%
                    </span>
                  </div>
                  <div className="col-span-3 text-right font-mono text-sm text-white tabular">
                    ${a.notional.toLocaleString("en-US", { maximumFractionDigits: 0 })}
                  </div>
                  <div className="col-span-1 text-right">
                    <a
                      href="#"
                      className="inline-flex items-center justify-end text-dim-400 hover:text-white"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <Icon.Ext />
                    </a>
                  </div>
                </div>

                {isBybit && bybitOpen && (
                  <div
                    className={`bg-ink-850/40 border-l-2 border-elec/40 ${
                      i !== rows.length - 1 ? "border-b border-ink-600/40" : ""
                    }`}
                  >
                    <div className="px-4 py-2 text-[9.5px] uppercase tracking-[0.18em] font-mono text-elec/80 flex items-center gap-2">
                      <span>↳ inside Bybit Attestor</span>
                      <span className="text-dim-600">·</span>
                      <span className="text-dim-500">via 0x4dc4…a037 (2-of-3 Safe)</span>
                    </div>
                    {bybitSubRows.map((b, j) => (
                      <div
                        key={b.key}
                        className={`grid grid-cols-12 items-center px-4 py-2.5 ${
                          j !== bybitSubRows.length - 1 ? "border-b border-ink-600/30" : ""
                        }`}
                      >
                        <div className="col-span-4 flex items-center gap-3 pl-6">
                          <span className="w-1 h-6 rounded-sm shrink-0" style={{ background: b.color }} />
                          <div className="min-w-0">
                            <div className="text-dim-300 text-[13px]">{b.label}</div>
                            <div className="text-[10.5px] text-dim-500 font-mono">{b.sub}</div>
                          </div>
                        </div>
                        <div className="col-span-2 text-right font-mono text-[12.5px] text-dim-300 tabular">
                          {b.pct}%
                        </div>
                        <div className="col-span-2 text-right font-mono text-[12.5px] tabular">
                          <span
                            className={
                              b.apy > 8 ? "text-neon" : b.apy > 0 ? "text-dim-300" : "text-dim-500"
                            }
                          >
                            {b.apy.toFixed(2)}%
                          </span>
                        </div>
                        <div className="col-span-3 text-right font-mono text-[12.5px] text-dim-300 tabular">
                          ${b.notional.toLocaleString("en-US", { maximumFractionDigits: 0 })}
                        </div>
                        <div className="col-span-1"></div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function AttestorAndHedgesSection() {
  return (
    <section>
      <SectionHead
        eyebrow="Off-Chain Trust Surface"
        title="Attestor health & active hedges"
        subtitle="The Bybit-side balance enters on-chain accounting through a 2-of-3 Gnosis Safe attestor. Below: liveness status of that push, plus every delta-neutral position currently open so anyone can verify the hedge is real, not asserted."
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
  return (
    <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden h-full flex flex-col">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-600/70 bg-ink-850">
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400">
          <span className="w-1.5 h-1.5 rounded-sm bg-elec"></span>
          Bybit Attestor Health
        </div>
        <span className="inline-flex items-center gap-1.5 text-[10px] font-mono text-neon">
          <LiveDot size={6} /> {ATTESTOR.status}
        </span>
      </div>
      <div className="p-5 space-y-4 flex-1">
        <div>
          <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500">Last attestation</div>
          <div className="flex items-baseline gap-2 mt-1">
            <div className="font-mono text-4xl text-white tabular leading-none">{ATTESTOR.lastPushMin}</div>
            <div className="text-dim-400 font-mono text-sm">min ago</div>
          </div>
          <div className="mt-3 relative">
            <div className="h-1.5 bg-ink-700 rounded-sm overflow-hidden">
              <div
                className="h-full bg-neon transition-all"
                style={{
                  width: Math.min(100, (ATTESTOR.lastPushMin / ATTESTOR.criticalThreshold) * 100) + "%",
                }}
              />
            </div>
            <div className="flex items-center justify-between mt-1.5 text-[9.5px] font-mono">
              <span className="text-dim-600">0m</span>
              <span className="text-warn">{ATTESTOR.warningThreshold}m warn</span>
              <span className="text-danger">{ATTESTOR.criticalThreshold}m halt</span>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-px bg-ink-600/40 border border-ink-600/60 rounded-sm overflow-hidden">
          <div className="bg-ink-900 px-3 py-2.5">
            <div className="text-[9.5px] font-mono uppercase tracking-[0.16em] text-dim-500">Push streak</div>
            <div className="font-mono text-base text-white tabular mt-1">{ATTESTOR.consecutivePushes}</div>
          </div>
          <div className="bg-ink-900 px-3 py-2.5">
            <div className="text-[9.5px] font-mono uppercase tracking-[0.16em] text-dim-500">Lagged 24h</div>
            <div className="font-mono text-base text-neon tabular mt-1">{ATTESTOR.laggedPushesLast24h}</div>
          </div>
        </div>

        <div className="space-y-2 text-[11px] font-mono pt-1">
          <div className="flex items-center justify-between">
            <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Attestor</span>
            <HashChip hash={ATTESTOR.safeAddress} head={6} tail={4} />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Control</span>
            <span className="text-dim-300">{ATTESTOR.multisig}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Cadence</span>
            <span className="text-dim-300">{ATTESTOR.pushFrequency}</span>
          </div>
        </div>
      </div>
      <div className="border-t border-ink-600/70 bg-ink-850 px-4 py-2.5 flex items-center justify-between text-[11px] font-mono">
        <a href="#" className="text-elec hover:text-elec-soft inline-flex items-center gap-1.5">
          View Safe <Icon.Ext />
        </a>
        <span className="text-dim-500">If lag &gt; 60m, vault freezes new allocations.</span>
      </div>
    </div>
  );
}

function HedgeTransparencyCard() {
  return (
    <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden h-full flex flex-col">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-600/70 bg-ink-850">
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400">
          <span className="w-1.5 h-1.5 rounded-sm bg-neon"></span>
          Active Hedges · Delta-Neutral
        </div>
        <span className="text-[10px] font-mono text-dim-500">{ACTIVE_HEDGES.length} open positions</span>
      </div>
      <div className="divide-y divide-ink-600/40 flex-1">
        {ACTIVE_HEDGES.map((h) => (
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
                  {h.blendedApr.toFixed(1)}%
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
                <div className="text-neon tabular mt-0.5 inline-flex items-center gap-1">
                  0 <Icon.Check className="w-3 h-3" />
                </div>
              </div>
              <div className="bg-ink-900 px-2.5 py-2">
                <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">Spot APR</div>
                <div className="text-white tabular mt-0.5">{h.spotApr.toFixed(1)}%</div>
              </div>
              <div className="bg-ink-900 px-2.5 py-2">
                <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">Funding APR</div>
                <div className="text-white tabular mt-0.5">{h.fundingApr.toFixed(1)}%</div>
              </div>
              <div className="bg-ink-900 px-2.5 py-2">
                <div className="text-[9px] uppercase tracking-[0.14em] text-dim-500">Earned 24h</div>
                <div className="text-neon tabular mt-0.5">+${h.fundingEarned24h.toFixed(2)}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="border-t border-ink-600/70 bg-ink-850 px-4 py-2.5 flex items-center justify-between text-[11px] font-mono">
        <span className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Lifetime funding harvested</span>
        <span className="text-neon tabular">+${HEDGE_LIFETIME_FUNDING.toFixed(2)}</span>
      </div>
    </div>
  );
}

function RecentDecisionsPreview() {
  const recent = DECISIONS.slice(0, 4);
  return (
    <section>
      <SectionHead
        eyebrow="Latest Agent Activity"
        title="Recent decisions"
        subtitle="Last 4 actions from the live log. Every entry includes rationale, Allora signals used, IPFS proof, and on-chain transaction."
        right={
          <a href="#" className="text-[12px] font-mono text-elec hover:text-elec-soft inline-flex items-center gap-1.5">
            View full log <Icon.Arrow />
          </a>
        }
      />
      <Card className="overflow-hidden">
        {recent.map((d, i) => (
          <div
            key={d.id}
            className={`flex items-center gap-4 px-4 sm:px-5 py-3.5 ${
              i !== recent.length - 1 ? "border-b border-ink-600/40" : ""
            }`}
          >
            <div className="font-mono text-[11px] text-dim-500 tabular w-20 hidden sm:block">{d.ago}</div>
            <HashChip hash={d.full} className="hidden md:inline-flex w-28" />
            <div className="flex-1 text-sm text-white min-w-0 truncate">{d.summary}</div>
            <Tag tone={d.risk === "LOW" ? "green" : d.risk === "MED" ? "warn" : "red"}>
              RISK: {d.risk}
            </Tag>
            <span className="font-mono text-[11px] text-dim-400 hidden lg:inline tabular">
              conf {d.confidence.toFixed(2)}
            </span>
            <Icon.Chev className="text-dim-500" />
          </div>
        ))}
      </Card>
    </section>
  );
}
