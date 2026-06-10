"use client";

import Link from "next/link";
import { useState } from "react";
import { useConnectModal } from "@rainbow-me/rainbowkit";
import { useAccount, useDisconnect } from "wagmi";

import { Button, Eyebrow, HashChip, Icon, LiveDot } from "@/components/ui";
import { BRAND } from "@/lib/brand";
import { LiveTicker } from "@/components/live-ticker";
import { DecisionLog } from "@/components/screens/decision-log";
import { VaultCard } from "@/components/screens/vault-card";
import { StoreProvider } from "@/lib/agent-store-context";
import type { CycleSummary, Portfolio } from "@/lib/agent-api";
import {
  BYBIT_ATTESTOR_ADDRESS,
  CAPITAL_MANAGER_ADDRESS,
  DECISION_LOG_ADDRESS,
  IDENTITY_REGISTRY_ADDRESS,
  REPUTATION_ORACLE_ADDRESS,
  REPUTATION_REGISTRY_ADDRESS,
  VUSDC_ADDRESS,
  isDecisionLogConfigured,
  isReputationOracleConfigured,
  isVUsdcConfigured,
} from "@/lib/contracts";
import { mantleExplorerAddress } from "@/lib/explorer";

// Gnosis Safe (2-of-3) that owns the agent NFT + signs attestations.
// Canonical address from CLAUDE.md - not an env-driven deploy artifact.
const SAFE_OWNER_ADDRESS = "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037";

type ContractEntry = {
  label: string;
  hash: string;
  pending?: boolean;
  sub?: string;
};

// Deployed-contract footer rows, sourced from @/lib/contracts (real
// addresses or 0x0 → "[mainnet pending]"). No mock placeholders.
const FOOTER_CONTRACTS: ContractEntry[] = [
  { label: "vUSDC TOKEN", hash: VUSDC_ADDRESS, pending: !isVUsdcConfigured },
  { label: "CAPITAL MANAGER", hash: CAPITAL_MANAGER_ADDRESS },
  { label: "DECISION LOG", hash: DECISION_LOG_ADDRESS, pending: !isDecisionLogConfigured },
  {
    label: "REPUTATION ORACLE",
    hash: REPUTATION_ORACLE_ADDRESS,
    pending: !isReputationOracleConfigured,
  },
  { label: "BYBIT ATTESTOR", hash: BYBIT_ATTESTOR_ADDRESS },
  { label: "ERC-8004 REPUTATION", hash: REPUTATION_REGISTRY_ADDRESS },
  { label: "ERC-8004 IDENTITY", hash: IDENTITY_REGISTRY_ADDRESS },
  { label: "GNOSIS SAFE OWNER", hash: SAFE_OWNER_ADDRESS, sub: "2-of-3" },
];

// "Human vs AI" tab removed pending mainnet-operations.3 (Human PM
// baseline) - no real comparison data yet, mocking it is dishonest.
const TABS = [
  { id: "vault", label: "Agent Dashboard", short: "Dashboard" },
  { id: "decisions", label: "Decision Log", short: "Decisions" },
] as const;

type TabId = (typeof TABS)[number]["id"];

type ShellProps = {
  /** Server-fetched cycle list (`frontend-complete.5`). When the API
   * was unreachable at render time the parent passes `[]` - child
   * tabs handle the empty state. */
  initialCycles?: CycleSummary[];
  initialPortfolio?: Portfolio | null;
};

export function Shell({
  initialCycles = [],
  initialPortfolio = null,
}: ShellProps = {}) {
  const [tab, setTab] = useState<TabId>("vault");

  return (
    <StoreProvider
      initialCycles={initialCycles}
      initialPortfolio={initialPortfolio}
    >
      <ShellBody tab={tab} setTab={setTab} />
    </StoreProvider>
  );
}

function ShellBody({
  tab,
  setTab,
}: {
  tab: TabId;
  setTab: (t: TabId) => void;
}) {
  return (
    <div className="min-h-screen flex flex-col bg-ink-950 relative">
      <div
        className="fixed inset-0 bg-grid pointer-events-none"
        style={{
          maskImage: "linear-gradient(to bottom, black 0%, transparent 70%)",
          WebkitMaskImage: "linear-gradient(to bottom, black 0%, transparent 70%)",
        }}
      />
      <div
        className="fixed inset-0 bg-noise pointer-events-none opacity-60"
        aria-hidden
      />
      <div
        className="fixed top-0 inset-x-0 h-[40vh] pointer-events-none"
        aria-hidden
        style={{
          background:
            "radial-gradient(80% 100% at 50% 0%, rgba(246,169,75,0.05), transparent 70%)",
        }}
      />

      <header className="relative z-20 border-b border-ink-600/60 bg-ink-950/85 backdrop-blur sticky top-0">
        <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Logo />
            <div className="hidden md:flex items-center gap-2 pl-3 border-l border-ink-600 text-[11px] font-mono">
              <span className="text-dim-500 uppercase tracking-[0.16em]">v1.0.4</span>
              <span className="text-dim-600">·</span>
              <span className="text-dim-400 uppercase tracking-[0.14em]">Mantle Mainnet</span>
              <span className="inline-flex items-center gap-1.5 text-accent ml-1">
                <LiveDot size={6} />
              </span>
            </div>
          </div>

          <nav className="hidden md:flex items-center gap-1 bg-ink-900/60 border border-ink-600/70 rounded-[3px] p-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`relative px-4 h-8 rounded-[2px] text-[12px] font-mono uppercase tracking-[0.12em] transition-colors
                  ${
                    tab === t.id
                      ? "bg-accent/[0.10] text-accent"
                      : "text-dim-300 hover:text-white hover:bg-ink-800"
                  }`}
              >
                {t.label}
                {tab === t.id && (
                  <span className="absolute bottom-[-5px] left-3 right-3 h-[2px] bg-accent rounded-full shadow-[0_0_8px_rgba(246,169,75,0.6)]" />
                )}
              </button>
            ))}
          </nav>

          <div className="flex items-center gap-2">
            <Link
              className="hidden lg:inline-flex items-center gap-1.5 px-3 h-8 border border-ink-600/70 rounded-[3px] bg-ink-900 text-[11px] font-mono uppercase tracking-[0.14em] text-dim-300 hover:text-accent hover:border-accent/40 transition-colors"
              href="/history"
            >
              History <Icon.Ext />
            </Link>
            <ConnectWalletButton />
          </div>
        </div>

        <div className="md:hidden border-t border-ink-600/60 flex">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex-1 h-10 text-[12px] font-mono uppercase tracking-[0.12em] transition-colors border-r last:border-r-0 border-ink-600/60
                ${tab === t.id ? "text-accent bg-accent/[0.08]" : "text-dim-300 bg-ink-950"}`}
            >
              {t.short}
              {tab === t.id && (
                <div className="h-[2px] bg-accent mt-1 mx-3 rounded-full" />
              )}
            </button>
          ))}
        </div>
      </header>

      <LiveTicker />

      <main className="relative flex-1 max-w-[1440px] mx-auto w-full px-4 sm:px-6 lg:px-8 py-8 sm:py-10">
        {tab === "vault" && <VaultCard />}
        {tab === "decisions" && <DecisionLog />}
      </main>

      <Footer />
    </div>
  );
}

function Logo() {
  return (
    <a href="#" className="flex items-center gap-2.5 group">
      <div className="relative w-8 h-8 grid place-items-center bg-gradient-to-b from-ink-850 to-ink-900 border border-accent/30 rounded-[3px] shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="#F6A94B" strokeWidth="1.8" strokeLinejoin="round">
          <path d="M12 3 21 7.5v9L12 21 3 16.5v-9z" />
          <circle cx="12" cy="12" r="2.5" fill="#F6A94B" stroke="none" />
        </svg>
        <span
          className="absolute -inset-px rounded-[3px] pointer-events-none"
          style={{ boxShadow: "inset 0 0 14px rgba(246,169,75,0.25)" }}
        />
      </div>
      <div className="leading-none">
        <div className="font-serif italic text-[10px] text-accent tracking-tight mb-0.5">
          vUSDC
        </div>
        <div className="font-mono text-[12.5px] text-white font-semibold tracking-tight">
          {BRAND.wordmark}
        </div>
        <div className="font-mono text-[9px] text-dim-500 tracking-[0.18em] uppercase mt-0.5">
          ERC-8004 · #001
        </div>
      </div>
    </a>
  );
}

function ConnectWalletButton() {
  const [hover, setHover] = useState(false);
  const { address, isConnected } = useAccount();
  const { openConnectModal } = useConnectModal();
  const { disconnect } = useDisconnect();

  if (isConnected && address) {
    const short = `${address.slice(0, 6)}…${address.slice(-4)}`;
    return (
      <button
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        onClick={() => disconnect()}
        className="inline-flex items-center gap-2 px-3 h-9 bg-ink-900 border border-accent/40 rounded-[3px] text-[11px] font-mono uppercase tracking-[0.12em] text-white hover:bg-ink-800"
      >
        <span className="w-2 h-2 rounded-full bg-accent live-dot" />
        <span className="tabular">{hover ? "Disconnect" : short}</span>
      </button>
    );
  }
  return (
    <button
      onClick={() => openConnectModal?.()}
      disabled={!openConnectModal}
      className="inline-flex items-center gap-2 px-4 h-9 bg-accent text-[#1B1300] rounded-[3px] text-[11px] font-mono font-bold uppercase tracking-[0.14em] shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_0_0_1px_rgba(246,169,75,0.5),0_6px_18px_-8px_rgba(246,169,75,0.45)] hover:bg-accent-soft active:translate-y-px transition-all disabled:opacity-50"
    >
      <Icon.Wallet />
      Connect Wallet
    </button>
  );
}

function Footer() {
  return (
    <footer className="relative border-t border-ink-600/60 mt-16">
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-10">
        <div className="mb-6">
          <div className="mb-4 flex items-baseline gap-3">
            <Eyebrow tone="accent">Deployed contracts</Eyebrow>
            <span className="font-serif italic text-[14px] text-dim-300">Mantle Mainnet</span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-px bg-ink-600/40 border border-ink-600/70 rounded-md overflow-hidden">
            {FOOTER_CONTRACTS.map((c) => (
              <div
                key={c.label}
                className="bg-ink-900 px-4 py-3 flex items-center justify-between gap-3"
              >
                <div className="min-w-0">
                  <div className="text-[9.5px] font-mono uppercase tracking-[0.16em] text-dim-500 flex items-center gap-1.5">
                    {c.label}
                    {c.pending && (
                      <span className="text-warn/70 normal-case tracking-normal text-[9px]">
                        [mainnet pending]
                      </span>
                    )}
                    {c.sub && (
                      <span className="text-dim-600 normal-case tracking-normal text-[9px]">
                        {c.sub}
                      </span>
                    )}
                  </div>
                </div>
                {c.pending ? (
                  <span className="text-[10px] font-mono text-dim-600">not deployed</span>
                ) : (
                  <a
                    href={mantleExplorerAddress(c.hash)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="hover:text-white"
                  >
                    <HashChip hash={c.hash} head={6} tail={4} />
                  </a>
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 text-[11px] font-mono text-dim-500 pt-5 border-t border-ink-600/40">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span className="text-white font-semibold tracking-[0.05em]">{BRAND.name} · {BRAND.token}</span>
            <span className="text-dim-600">·</span>
            <span>ERC-8004 · Mantle Mainnet</span>
            <span className="text-dim-600">·</span>
            <span className="font-serif italic text-dim-400 normal-case tracking-normal">
              Built for Mantle Turing Test Hackathon 2026
            </span>
          </div>
          <div className="flex items-center gap-4">
            <span className="inline-flex items-center gap-1.5 text-accent">
              <LiveDot size={6} /> Mantle Mainnet
            </span>
          </div>
        </div>
      </div>
    </footer>
  );
}
