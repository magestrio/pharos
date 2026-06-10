"use client";

import Link from "next/link";

import {
  AAVE_USDC_ADAPTER_ADDRESS,
  AAVE_WETH_ADAPTER_ADDRESS,
  BYBIT_ATTESTOR_ADDRESS,
  CAPITAL_MANAGER_ADDRESS,
  DECISION_LOG_ADDRESS,
  IDENTITY_REGISTRY_ADDRESS,
  REPUTATION_ORACLE_ADDRESS,
  REPUTATION_REGISTRY_ADDRESS,
  USDC_ADDRESS,
  VAULT_AGENT_ID,
  VUSDC_ADDRESS,
} from "@/lib/contracts";
import { ErrorPanel, SkeletonBox, SkeletonRow } from "@/components/ui";
import { ipfsGateway, mantleExplorerAddress, mantleExplorerTx } from "@/lib/explorer";
import { formatDateTime } from "@/lib/datetime";
import { useAttestorHealth } from "@/lib/hooks/use-attestor-health";
import { useDecisionEvents } from "@/lib/hooks/use-decision-events";
import { formatBpsAsPct, useReputation } from "@/lib/hooks/use-reputation";
import { useReputationHistory } from "@/lib/hooks/use-reputation-history";

const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000" as const;
const DUNE_PLACEHOLDER_URL = "https://dune.com"; // wire real dashboard slug once published

interface ContractRow {
  label: string;
  address: `0x${string}`;
  status: "live" | "pending";
  note: string;
}

function rows(): ContractRow[] {
  const live = (addr: `0x${string}`): "live" | "pending" =>
    addr === ZERO_ADDRESS ? "pending" : "live";
  return [
    {
      label: "vUSDC",
      address: VUSDC_ADDRESS,
      status: live(VUSDC_ADDRESS),
      note: "ERC-20 yield-bearing wrapper. mint / redeem at the current exchange rate.",
    },
    {
      label: "CapitalManager",
      address: CAPITAL_MANAGER_ADDRESS,
      status: live(CAPITAL_MANAGER_ADDRESS),
      note: "Custody + allocation router. Holds USDC cash buffer; delegates to whitelisted adapters.",
    },
    {
      label: "DecisionLog",
      address: DECISION_LOG_ADDRESS,
      status: live(DECISION_LOG_ADDRESS),
      note: "Emits DecisionRecorded per agent cycle. IPFS rationale CID + actionHash on-chain.",
    },
    {
      label: "ReputationOracle",
      address: REPUTATION_ORACLE_ADDRESS,
      status: live(REPUTATION_ORACLE_ADDRESS),
      note: "Writes annualized APR (bps) to the ERC-8004 Reputation Registry. 1h throttle.",
    },
    {
      label: "AaveV3UsdcAdapter",
      address: AAVE_USDC_ADAPTER_ADDRESS,
      status: live(AAVE_USDC_ADAPTER_ADDRESS),
      note: "Supplies USDC to Aave V3 on Mantle. valueInUsdc() = aUSDC balance.",
    },
    {
      label: "AaveV3WethAdapter",
      address: AAVE_WETH_ADAPTER_ADDRESS,
      status: live(AAVE_WETH_ADAPTER_ADDRESS),
      note: "Supplies WETH to Aave V3. valueInUsdc() converts via the Aave oracle.",
    },
    {
      label: "BybitAttestor",
      address: BYBIT_ATTESTOR_ADDRESS,
      status: live(BYBIT_ATTESTOR_ADDRESS),
      note: "2-of-3 Safe pushes attested Bybit Earn balance. valueInUsdc() = last attestation.",
    },
    {
      label: "USDC (Mantle)",
      address: USDC_ADDRESS,
      status: live(USDC_ADDRESS),
      note: "Native USDC on Mantle. Underlying asset of vUSDC.",
    },
    {
      label: "ERC-8004 Identity Registry",
      address: IDENTITY_REGISTRY_ADDRESS,
      status: "live",
      note: `Canonical agent NFT registry. Pharos token id = ${VAULT_AGENT_ID.toString()}.`,
    },
    {
      label: "ERC-8004 Reputation Registry",
      address: REPUTATION_REGISTRY_ADDRESS,
      status: "live",
      note: "Canonical score store. Receives writes from ReputationOracle.updateReputation().",
    },
  ];
}

export function VerifyPage() {
  return (
    <div className="space-y-10">
      <Header />
      <ContractsSection />
      <DecisionsSection />
      <ReputationSection />
      <AttestorSafeSection />
      <ExternalDashboardSection />
    </div>
  );
}

function Header() {
  return (
    <div>
      <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-2">
        On-chain verification
      </div>
      <h1 className="text-3xl sm:text-4xl font-semibold text-white">
        Every claim made elsewhere has an on-chain proof here.
      </h1>
      <p className="mt-3 text-[14px] text-dim-300 max-w-2xl leading-relaxed">
        This is the bench: every contract, every decision, every reputation update, every Safe
        signature in one page. No screenshots, no off-chain assertions - only links to Mantle
        Explorer + the IPFS gateway.
      </p>
    </div>
  );
}

function ContractsSection() {
  const data = rows();
  return (
    <SectionCard
      title="Contract addresses"
      subtitle="Mantle Mainnet. Each row links to the verified contract page on Mantle Explorer."
    >
      <div className="overflow-hidden border border-ink-600/40 rounded-sm">
        <div className="hidden md:grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 bg-ink-850 px-3 py-2">
          <div className="col-span-3">Contract</div>
          <div className="col-span-3">Address</div>
          <div className="col-span-1">Status</div>
          <div className="col-span-5">Role</div>
        </div>
        {data.map((row) => {
          const live = row.status === "live";
          return (
            <a
              key={row.label}
              href={live ? mantleExplorerAddress(row.address) : undefined}
              target={live ? "_blank" : undefined}
              rel={live ? "noopener noreferrer" : undefined}
              className={`block border-t border-ink-600/30 ${
                live ? "hover:bg-ink-850/60 transition-colors" : "cursor-default"
              }`}
            >
              <div className="md:grid md:grid-cols-12 md:items-center px-3 py-3 gap-2 space-y-2 md:space-y-0">
                <div className="md:col-span-3 text-[12.5px] text-white font-medium">{row.label}</div>
                <div className="md:col-span-3 font-mono text-[11px] text-dim-300 tabular">
                  {short(row.address)}
                </div>
                <div className="md:col-span-1">
                  <StatusPill live={live} />
                </div>
                <div className="md:col-span-5 text-[11.5px] text-dim-400 leading-snug">
                  {row.note}
                </div>
              </div>
            </a>
          );
        })}
      </div>
    </SectionCard>
  );
}

function DecisionsSection() {
  const { events, isLive, isLoading, isError } = useDecisionEvents();
  const top = events.slice(0, 10);
  return (
    <SectionCard
      title="Last 10 DecisionLog events"
      subtitle="Each entry links the on-chain transaction to the IPFS-pinned rationale CID."
    >
      {!isLive && <DeployNotice what="DecisionLog" />}
      {isLive && isError && events.length === 0 && (
        <ErrorPanel label="Couldn't fetch DecisionRecorded events from Mantle RPC." />
      )}
      {isLive && isLoading && events.length === 0 && !isError && (
        <div className="space-y-2">
          <SkeletonRow width="80%" />
          <SkeletonRow width="65%" />
          <SkeletonRow width="72%" />
        </div>
      )}
      {isLive && !isLoading && !isError && top.length === 0 && (
        <div className="text-[12px] text-dim-300 font-mono">
          No DecisionRecorded events emitted yet. First agent cycle seeds this list.
        </div>
      )}
      {isLive && top.length > 0 && (
        <div className="overflow-hidden border border-ink-600/40 rounded-sm">
          <div className="grid grid-cols-12 text-[10px] uppercase tracking-[0.16em] font-mono text-dim-500 bg-ink-850 px-3 py-2">
            <div className="col-span-1 text-right">#</div>
            <div className="col-span-3">Timestamp</div>
            <div className="col-span-4">Decision ID</div>
            <div className="col-span-2">IPFS</div>
            <div className="col-span-2 text-right">Tx</div>
          </div>
          {top.map((ev, i) => (
            <div
              key={`${ev.txHash}-${ev.logIndex}`}
              className="grid grid-cols-12 items-center px-3 py-2 border-t border-ink-600/30 text-[12px] font-mono"
            >
              <div className="col-span-1 text-right text-dim-400 tabular">{i + 1}</div>
              <div className="col-span-3 text-dim-300 tabular">
                {formatDateTime(ev.timestamp * 1000)}
              </div>
              <div className="col-span-4 text-white tabular">{short(ev.decisionId, 10, 6)}</div>
              <div className="col-span-2">
                {ev.ipfsCid ? (
                  <a
                    href={ipfsGateway(ev.ipfsCid)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-elec hover:text-elec-soft"
                  >
                    {ev.ipfsCid.slice(0, 8)}…
                  </a>
                ) : (
                  <span className="text-dim-500">-</span>
                )}
              </div>
              <div className="col-span-2 text-right">
                <a
                  href={mantleExplorerTx(ev.txHash)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-elec hover:text-elec-soft"
                >
                  {short(ev.txHash, 6, 4)}
                </a>
              </div>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
}

function ReputationSection() {
  const rep = useReputation();
  const history = useReputationHistory();
  const { points } = history;
  const latest = points.length > 0 ? points[points.length - 1] : null;

  return (
    <SectionCard
      title="Latest reputation update"
      subtitle={`ERC-8004 token #${VAULT_AGENT_ID.toString()}. The last ReputationOracle write to the canonical Reputation Registry.`}
      action={
        <Link
          href={`/reputation/${VAULT_AGENT_ID.toString()}`}
          className="text-[12px] font-mono text-elec hover:text-elec-soft inline-flex items-center gap-1.5"
        >
          Full history →
        </Link>
      }
    >
      {!rep.isLive && <DeployNotice what="ReputationOracle" />}
      {rep.isLive && history.isError && !latest && (
        <ErrorPanel label="Couldn't fetch ReputationUpdated history from Mantle RPC." />
      )}
      {rep.isLive && history.isLoading && !latest && !history.isError && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <SkeletonBox className="h-14" />
          <SkeletonBox className="h-14" />
          <SkeletonBox className="h-14" />
          <SkeletonBox className="h-14" />
        </div>
      )}
      {rep.isLive && !history.isLoading && !history.isError && !latest && (
        <div className="text-[12px] text-dim-300 font-mono">
          No ReputationUpdated events yet - current value:{" "}
          <span className="text-white">{formatBpsAsPct(rep.lastScoreBps)}</span>.
        </div>
      )}
      {rep.isLive && latest && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-[12px] font-mono">
          <Stat label="Score" value={formatBpsAsPct(latest.scoreBps)} tone="neon" />
          <Stat label="Update #" value={String(latest.updateIndex)} />
          <Stat
            label="Vault assets"
            value={`$${(Number(latest.currentAssets) / 1_000_000).toLocaleString("en-US", {
              maximumFractionDigits: 0,
            })}`}
          />
          <Stat
            label="Tx"
            value={
              <a
                href={mantleExplorerTx(latest.txHash)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-elec hover:text-elec-soft"
              >
                {short(latest.txHash, 6, 4)}
              </a>
            }
          />
        </div>
      )}
    </SectionCard>
  );
}

function AttestorSafeSection() {
  const health = useAttestorHealth();
  const safe = health.attestorAddress;

  return (
    <SectionCard
      title="Bybit attestor - Safe verification"
      subtitle="2-of-3 Gnosis Safe pushing attested off-chain Bybit Earn balance. Owners are verifiable on Mantle Explorer + the Safe app."
    >
      {!health.isLive && <DeployNotice what="BybitAttestor" />}
      {health.isLive && safe && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <RowCard
            label="Safe address"
            value={
              <a
                href={mantleExplorerAddress(safe)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-elec hover:text-elec-soft font-mono text-[12px]"
              >
                {short(safe, 8, 6)}
              </a>
            }
          />
          <RowCard
            label="Control"
            value={<span className="text-white text-[12.5px]">2-of-3 multisig</span>}
            sub="Local A + B + cold C. Owners enumerable via the Safe contract."
          />
          <RowCard
            label="Status"
            value={
              <span
                className={
                  health.status === "HEALTHY"
                    ? "text-neon"
                    : health.status === "DEGRADED"
                      ? "text-warn"
                      : health.status === "CRITICAL"
                        ? "text-danger"
                        : "text-dim-300"
                }
              >
                {health.status}
              </span>
            }
            sub={
              health.lastAttestationSec !== null
                ? `Last push ${Math.max(0, Math.floor(health.lagSec / 60))} min ago.`
                : "Awaiting first attestation."
            }
          />
        </div>
      )}
    </SectionCard>
  );
}

function ExternalDashboardSection() {
  return (
    <SectionCard
      title="External dashboards"
      subtitle="Mantle supports Dune. Once a public dashboard is published it lands here - TVL, decision rate, reputation curve, hedge funding earned."
    >
      <div className="flex flex-wrap gap-3 text-[12px] font-mono">
        <a
          href={DUNE_PLACEHOLDER_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="px-3 h-9 inline-flex items-center gap-2 border border-ink-600/70 rounded-sm bg-ink-900 text-dim-300 hover:text-white hover:bg-ink-800"
        >
          Dune dashboard
          <span className="text-dim-500 text-[10.5px] uppercase tracking-[0.14em]">pending</span>
        </a>
        <Link
          href="/history"
          className="px-3 h-9 inline-flex items-center gap-2 border border-ink-600/70 rounded-sm bg-ink-900 text-elec hover:text-elec-soft hover:bg-ink-800"
        >
          Cycle history →
        </Link>
        <Link
          href={`/reputation/${VAULT_AGENT_ID.toString()}`}
          className="px-3 h-9 inline-flex items-center gap-2 border border-ink-600/70 rounded-sm bg-ink-900 text-elec hover:text-elec-soft hover:bg-ink-800"
        >
          Reputation page →
        </Link>
      </div>
    </SectionCard>
  );
}

// ──────────────────────────── building blocks ────────────────────────

function SectionCard({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="bg-ink-900 border border-ink-600/70 rounded-md">
      <div className="px-5 py-4 border-b border-ink-600/40 flex items-start justify-between gap-4">
        <div>
          <div className="text-[10.5px] font-mono uppercase tracking-[0.18em] text-dim-500">
            {title}
          </div>
          {subtitle && (
            <div className="mt-1.5 text-[12px] text-dim-400 leading-relaxed max-w-2xl">
              {subtitle}
            </div>
          )}
        </div>
        {action}
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

function StatusPill({ live }: { live: boolean }) {
  return live ? (
    <span className="inline-flex items-center gap-1 text-[10px] font-mono uppercase tracking-[0.14em] text-neon">
      <span className="w-1.5 h-1.5 rounded-sm bg-neon" /> live
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-[10px] font-mono uppercase tracking-[0.14em] text-dim-500">
      <span className="w-1.5 h-1.5 rounded-sm bg-dim-500" /> pending
    </span>
  );
}

function Stat({
  label,
  value,
  tone = "white",
}: {
  label: string;
  value: React.ReactNode;
  tone?: "white" | "neon";
}) {
  return (
    <div className="bg-ink-850/60 border border-ink-600/40 rounded-sm px-3 py-2">
      <div className="text-[9.5px] uppercase tracking-[0.14em] text-dim-500">{label}</div>
      <div className={`tabular mt-1 ${tone === "neon" ? "text-neon" : "text-white"}`}>{value}</div>
    </div>
  );
}

function RowCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
}) {
  return (
    <div className="bg-ink-850/60 border border-ink-600/40 rounded-sm px-3 py-3">
      <div className="text-[9.5px] uppercase tracking-[0.14em] text-dim-500">{label}</div>
      <div className="mt-1">{value}</div>
      {sub && <div className="mt-1.5 text-[11px] text-dim-400 leading-snug">{sub}</div>}
    </div>
  );
}

function DeployNotice({ what }: { what: string }) {
  return (
    <div className="bg-ink-850/60 border border-ink-600/40 rounded-sm px-3 py-3 text-[12px] font-mono text-warn">
      {what} not deployed yet - this section populates after mainnet-deploy.
    </div>
  );
}

function short(hex: string, head = 6, tail = 4): string {
  if (hex.length <= head + tail + 2) return hex;
  return `${hex.slice(0, head)}…${hex.slice(-tail)}`;
}
