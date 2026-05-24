"use client";

import type { ReactNode } from "react";

import {
  AI_SERIES,
  AI_STATS,
  CIAN_COMPARE,
  HUMAN_SERIES,
  HUMAN_STATS,
  VAULT,
} from "@/lib/data";
import {
  Card,
  HashChip,
  Icon,
  LineChart,
  LiveDot,
  SectionHead,
  Tag,
  fmtPct,
  fmtUsd,
} from "@/components/ui";

type Stats = {
  return: number;
  sharpe: number;
  dd: number;
  rebalances?: number;
  decisions?: number;
  strategy: string;
  rationale: string;
  final: number;
  apyAnnualized: number;
};

export function HumanVsAi() {
  return (
    <div className="space-y-8">
      <SectionHead
        eyebrow="Live Strategy Comparison · 21d window"
        title="Human PM vs Vault8004 Agent"
        subtitle="Two identical $1,000,000 USDC vaults deployed side-by-side on Mantle Mainnet. Same starting capital, same chain. The Human PM is restricted to on-chain venues (retail-realistic). The agent has multi-venue access including Bybit Earn via attestor. No backtests — every dollar is live."
        right={
          <div className="hidden lg:flex items-center gap-2 text-[11px] font-mono">
            <LiveDot />
            <span className="text-neon">RACE LIVE</span>
            <span className="text-dim-500">· D21 / ∞</span>
          </div>
        }
      />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-px bg-ink-600/60 border border-ink-600/70 rounded-md overflow-hidden">
        <ContestantPanel
          side="human"
          title="Human PM"
          subtitle="On-chain baseline"
          icon={<Icon.User className="text-dim-300" />}
          strategy={HUMAN_STATS.strategy}
          rationale={HUMAN_STATS.rationale}
          series={HUMAN_SERIES}
          stats={HUMAN_STATS}
          color="#7A8499"
          fillColor="rgba(122,132,153,0.12)"
        />
        <ContestantPanel
          side="ai"
          title="Vault8004 Agent"
          subtitle={`Powered by ${VAULT.model}`}
          icon={<Icon.Robot className="text-neon" />}
          strategy={AI_STATS.strategy}
          rationale={AI_STATS.rationale}
          series={AI_SERIES}
          stats={AI_STATS}
          color="#00FF88"
          fillColor="rgba(0,255,136,0.16)"
          highlight
        />
      </div>

      <OverlayChart />
      <WinnerBars />
      <HowItWorks />
      <CianComparison />
    </div>
  );
}

function ContestantPanel({
  side,
  title,
  subtitle,
  icon,
  strategy,
  rationale,
  series,
  stats,
  color,
  fillColor,
  highlight,
}: {
  side: "human" | "ai";
  title: string;
  subtitle: string;
  icon: ReactNode;
  strategy: string;
  rationale: string;
  series: number[];
  stats: Stats;
  color: string;
  fillColor: string;
  highlight?: boolean;
}) {
  const start = series[0];
  const final = series[series.length - 1];
  const totalPnl = final - start;
  const dailyReturns = series.slice(1).map((v, i) => ((v - series[i]) / series[i]) * 100);
  const bestDay = Math.max(...dailyReturns).toFixed(3);
  const worstDay = Math.min(...dailyReturns).toFixed(3);

  return (
    <div className="bg-ink-900 p-5 sm:p-6 relative">
      {highlight && (
        <div
          className="absolute inset-0 pointer-events-none"
          style={{ background: "radial-gradient(120% 80% at 100% 0%, rgba(0,255,136,0.06), transparent 55%)" }}
        />
      )}
      <div className="relative">
        <div className="flex items-start justify-between mb-1">
          <div className="flex items-center gap-2.5">
            <div
              className={`w-8 h-8 rounded-sm grid place-items-center border ${
                highlight ? "border-neon/40 bg-neon/10" : "border-ink-600 bg-ink-800"
              }`}
            >
              {icon}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-white font-semibold text-lg">{title}</span>
                {highlight && <Tag tone="green">LEADING</Tag>}
              </div>
              <div className="text-[12px] text-dim-400">{subtitle}</div>
            </div>
          </div>
          <div className={`text-right font-mono ${stats.return >= 0 ? "text-neon" : "text-danger"}`}>
            <div className="text-3xl tabular leading-none">{fmtPct(stats.return, { decimals: 3 })}</div>
            <div className="text-[10.5px] text-dim-500 mt-1 uppercase tracking-[0.14em]">21d return</div>
          </div>
        </div>

        <div className="mt-4 p-3 bg-ink-850 border border-ink-600/50 rounded-sm">
          <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-1">Strategy</div>
          <div className="text-[13px] text-dim-300 leading-snug">{strategy}</div>
          {rationale && (
            <div className="mt-2 pt-2 border-t border-ink-600/40 text-[11.5px] text-dim-500 italic leading-snug">
              {rationale}
            </div>
          )}
        </div>

        <div className="mt-5 -mx-2 h-[200px] sm:h-[230px]">
          <LineChart
            series={series}
            color={color}
            fillColor={fillColor}
            label={side}
            baseline={start}
            height={230}
            width={560}
            pad={{ t: 16, r: 12, b: 24, l: 44 }}
          />
        </div>

        <div className="mt-4 grid grid-cols-4 gap-px bg-ink-600/40 border border-ink-600/60 rounded-sm overflow-hidden">
          <RaceStat
            label="Return"
            value={fmtPct(stats.return, { decimals: 3 })}
            tone={stats.return >= 0 ? "green" : "red"}
          />
          <RaceStat label="Sharpe" value={stats.sharpe.toFixed(2)} tone="white" />
          <RaceStat
            label="Max DD"
            value={fmtPct(stats.dd, { decimals: 1 })}
            tone={stats.dd < 0 ? "red" : "green"}
          />
          <RaceStat
            label={side === "ai" ? "Decisions" : "Rebalances"}
            value={String(side === "ai" ? stats.decisions : stats.rebalances)}
            tone="white"
          />
        </div>

        <div className="mt-3 grid grid-cols-3 text-[11px] font-mono">
          <div>
            <div className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Best day</div>
            <div className="text-neon tabular mt-0.5">+{bestDay}%</div>
          </div>
          <div>
            <div className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Worst day</div>
            <div className={`tabular mt-0.5 ${parseFloat(worstDay) < 0 ? "text-danger" : "text-neon"}`}>
              {parseFloat(worstDay) >= 0 ? "+" : ""}
              {worstDay}%
            </div>
          </div>
          <div className="text-right">
            <div className="text-dim-500 uppercase tracking-[0.14em] text-[9.5px]">Net P&amp;L</div>
            <div className="text-white tabular mt-0.5">{fmtUsd(totalPnl, { sign: true, decimals: 0 })}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

function RaceStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "green" | "red" | "white";
}) {
  const t = tone === "green" ? "text-neon" : tone === "red" ? "text-danger" : "text-white";
  return (
    <div className="bg-ink-900 px-3 py-3">
      <div className="text-[9.5px] font-mono uppercase tracking-[0.18em] text-dim-500">{label}</div>
      <div className={`font-mono text-lg tabular mt-1 ${t}`}>{value}</div>
    </div>
  );
}

function OverlayChart() {
  return (
    <Card className="p-5 sm:p-6">
      <div className="flex items-end justify-between mb-3">
        <div>
          <div className="text-[10.5px] uppercase tracking-[0.18em] text-dim-500 font-mono mb-1">P&amp;L Overlay</div>
          <div className="text-white font-semibold">Both strategies, same axis</div>
        </div>
        <div className="flex items-center gap-4 text-[11px] font-mono">
          <div className="flex items-center gap-2">
            <span className="w-4 h-[2px] bg-neon"></span>
            <span className="text-dim-300">AI Agent</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-4 h-[2px] bg-dim-400 border-t border-dashed"></span>
            <span className="text-dim-300">Human PM</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-4 h-[2px] bg-ink-500 border-t border-dashed"></span>
            <span className="text-dim-500">Start ($1M)</span>
          </div>
        </div>
      </div>
      <div className="h-[260px] sm:h-[320px]">
        <LineChart
          series={AI_SERIES}
          compareSeries={HUMAN_SERIES}
          color="#00FF88"
          fillColor="rgba(0,255,136,0.12)"
          compareColor="#7A8499"
          label="overlay"
          baseline={1_000_000}
          width={1200}
          height={320}
          pad={{ t: 16, r: 16, b: 26, l: 56 }}
        />
      </div>
    </Card>
  );
}

function WinnerBars() {
  const metrics = [
    {
      key: "Return (21d)",
      ai: AI_STATS.return,
      human: HUMAN_STATS.return,
      betterIsHigher: true,
      fmt: (v: number) => fmtPct(v, { decimals: 3 }),
    },
    {
      key: "APY (annualised)",
      ai: AI_STATS.apyAnnualized,
      human: HUMAN_STATS.apyAnnualized,
      betterIsHigher: true,
      fmt: (v: number) => v.toFixed(1) + "%",
    },
    {
      key: "Sharpe",
      ai: AI_STATS.sharpe,
      human: HUMAN_STATS.sharpe,
      betterIsHigher: true,
      fmt: (v: number) => v.toFixed(2),
    },
    {
      key: "Max DD",
      ai: AI_STATS.dd,
      human: HUMAN_STATS.dd,
      betterIsHigher: true,
      fmt: (v: number) => fmtPct(v, { decimals: 1 }),
      description: "less negative wins",
    },
    {
      key: "Venue count",
      ai: 4,
      human: 2,
      betterIsHigher: true,
      fmt: (v: number) => String(v),
      description: "whitelisted strategies",
    },
  ];

  const aiWins = metrics.filter((m) => (m.betterIsHigher ? m.ai > m.human : m.ai < m.human)).length;

  return (
    <Card className="p-5 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-4 mb-5">
        <div>
          <div className="text-[10.5px] uppercase tracking-[0.18em] text-dim-500 font-mono mb-1">Scorecard</div>
          <div className="text-white font-semibold text-xl">
            AI wins{" "}
            <span className="text-neon font-mono tabular">
              {aiWins} of {metrics.length}
            </span>{" "}
            metrics
          </div>
        </div>
        <div className="text-[12px] text-dim-400 font-mono">Updated continuously · No survivorship bias</div>
      </div>
      <div className="space-y-4">
        {metrics.map((m) => {
          const max = Math.max(Math.abs(m.ai), Math.abs(m.human)) || 1;
          const aiW = (Math.abs(m.ai) / max) * 100;
          const huW = (Math.abs(m.human) / max) * 100;
          const aiWin = m.betterIsHigher ? m.ai > m.human : m.ai < m.human;
          return (
            <div key={m.key}>
              <div className="flex items-center justify-between gap-3 mb-2 whitespace-nowrap">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-[11px] font-mono uppercase tracking-[0.14em] text-dim-400">{m.key}</span>
                  {m.description && (
                    <span className="hidden md:inline text-[10.5px] text-dim-600 truncate">{m.description}</span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-[11px] font-mono shrink-0">
                  <span className="text-dim-400">
                    Human <span className="text-white tabular">{m.fmt(m.human)}</span>
                  </span>
                  <span className="text-dim-600">|</span>
                  <span className="text-neon">
                    AI <span className="tabular">{m.fmt(m.ai)}</span>
                  </span>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-1">
                <div className="h-2.5 bg-ink-700 rounded-sm overflow-hidden relative">
                  <div className="h-full bg-neon transition-all duration-500" style={{ width: aiW + "%" }} />
                  {aiWin && (
                    <span className="absolute right-1 top-1/2 -translate-y-1/2 text-[9px] font-mono text-black bg-neon px-1 rounded-sm">
                      WIN
                    </span>
                  )}
                </div>
                <div className="h-2.5 bg-ink-700 rounded-sm overflow-hidden relative">
                  <div
                    className="h-full bg-dim-400/70 transition-all duration-500 ml-auto"
                    style={{ width: huW + "%" }}
                  />
                  {!aiWin && (
                    <span className="absolute left-1 top-1/2 -translate-y-1/2 text-[9px] font-mono text-black bg-dim-300 px-1 rounded-sm">
                      WIN
                    </span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function HowItWorks() {
  const steps = [
    {
      n: "01",
      title: "Identical starting conditions",
      body: "Both vaults start with identical $1,000,000 USDC on Mantle Mainnet. Same deposit block, same gas budget.",
    },
    {
      n: "02",
      title: "Different venue access",
      body: "Human PM is restricted to on-chain venues only (Aave V3) — modelling a retail user without CEX accounts. The agent has multi-venue access including Bybit Earn via the 2-of-3 attestor.",
    },
    {
      n: "03",
      title: "Human follows a fixed rule",
      body: "60% Aave V3 USDC / 30% Aave V3 WETH / 10% cash. Weekly rebalance. No discretion, no peeking at the agent's moves. This is the honest baseline.",
    },
    {
      n: "04",
      title: "Agent decides event-driven",
      body: "Vault8004 ingests Allora signals, funding rates, attestor health, and pool depths. It commits a thesis to IPFS, then executes on-chain. 4h cron is the fallback, not the cadence.",
    },
  ];

  return (
    <section>
      <SectionHead
        eyebrow="Methodology"
        title="How this race works"
        subtitle="Production-grade comparison, not a backtest. The asymmetry is the point: AI wins because it has venue access the Human PM cannot get, not because the Human PM is unsophisticated."
      />
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {steps.map((s, i) => (
          <div key={s.n} className="bg-ink-900 border border-ink-600/70 rounded-md p-5 sm:p-6 relative">
            <div className="flex items-baseline gap-3 mb-3">
              <div className="font-mono text-3xl text-neon tabular">{s.n}</div>
              <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-dim-500">
                step {i + 1} / {steps.length}
              </div>
            </div>
            <div className="text-white font-semibold text-[15px] mb-2">{s.title}</div>
            <div className="text-[13.5px] text-dim-300 leading-relaxed">{s.body}</div>
          </div>
        ))}
      </div>

      <div className="mt-5 grid grid-cols-1 md:grid-cols-3 gap-px bg-ink-600/40 border border-ink-600/70 rounded-md overflow-hidden">
        <AuditCell label="vUSDC Token" hash="0x000000000000000000000000000000000000C0DE" />
        <AuditCell label="Human PM Baseline" hash="0x00000000000000000000000000000000B45e1ABe" />
        <AuditCell label="Capital Manager" hash="0x000000000000000000000000000000000000CA91" />
      </div>
    </section>
  );
}

function AuditCell({ label, hash }: { label: string; hash: string }) {
  return (
    <div className="bg-ink-900 px-4 py-3 flex items-center justify-between">
      <span className="text-[10px] font-mono uppercase tracking-[0.16em] text-dim-500">{label}</span>
      <HashChip hash={hash} />
    </div>
  );
}

function CianComparison() {
  return (
    <section>
      <SectionHead
        eyebrow="Adjacent Products"
        title={
          <span>
            How Vault8004 differs from <span className="text-elec">Cian Mantle Vault</span>
          </span>
        }
        subtitle="Cian launched its Mantle vault in Dec 2025 with Bybit + Mantle as deployment partners. It's a fixed-loop curator product — structurally close to us in venues, structurally different in trust model and decision surface."
      />
      <Card className="overflow-hidden">
        <div className="grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 border-b border-ink-600/70 px-4 py-3 bg-ink-850">
          <div className="col-span-4">Dimension</div>
          <div className="col-span-4">Cian Mantle Vault</div>
          <div className="col-span-4 text-neon">
            Vault8004 <span className="text-dim-500 normal-case tracking-normal">— ours</span>
          </div>
        </div>
        {CIAN_COMPARE.map((row, i) => (
          <div
            key={row.dim}
            className={`grid grid-cols-12 items-center px-4 py-3 text-[13px] ${
              i !== CIAN_COMPARE.length - 1 ? "border-b border-ink-600/40" : ""
            }`}
          >
            <div className="col-span-4 text-dim-300 font-mono text-[12px] uppercase tracking-[0.10em]">
              {row.dim}
            </div>
            <div className="col-span-4 text-dim-400">{row.cian}</div>
            <div className="col-span-4 text-white">{row.vault}</div>
          </div>
        ))}
      </Card>
      <div className="mt-3 text-[11.5px] text-dim-500 font-mono">
        Both products use the Bybit Earn surface — the distinguishing claim is verifiable agent reputation
        (ERC-8004) and a transparent decision log, not a different yield universe.
      </div>
    </section>
  );
}
