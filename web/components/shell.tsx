"use client";

import Link from "next/link";
import { useState } from "react";

import { CONTRACTS } from "@/lib/data";
import { HashChip, Icon, LiveDot } from "@/components/ui";
import { LiveTicker } from "@/components/live-ticker";
import { DecisionLog } from "@/components/screens/decision-log";
import { VaultCard } from "@/components/screens/vault-card";
import { StoreProvider } from "@/lib/agent-store-context";
import type { CycleSummary, Portfolio } from "@/lib/agent-api";

// "Human vs AI" tab removed pending mainnet-operations.3 (Human PM
// baseline) — no real comparison data yet, mocking it is dishonest.
const TABS = [
  { id: "vault", label: "Agent Dashboard", short: "Dashboard" },
  { id: "decisions", label: "Decision Log", short: "Decisions" },
] as const;

type TabId = (typeof TABS)[number]["id"];

type ShellProps = {
  /** Server-fetched cycle list (`frontend-complete.5`). When the API
   * was unreachable at render time the parent passes `[]` — child
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

      <header className="relative z-20 border-b border-ink-600/60 bg-ink-950/85 backdrop-blur sticky top-0">
        <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Logo />
            <div className="hidden md:flex items-center gap-2 pl-3 border-l border-ink-600 text-[11px] font-mono">
              <span className="text-dim-500 uppercase tracking-[0.14em]">v1.0.4</span>
              <span className="text-dim-600">·</span>
              <span className="text-dim-400">Mantle Mainnet</span>
              <span className="inline-flex items-center gap-1.5 text-neon ml-1">
                <LiveDot size={6} />
              </span>
            </div>
          </div>

          <nav className="hidden md:flex items-center gap-1 bg-ink-900 border border-ink-600/70 rounded-sm p-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`px-3.5 h-8 rounded-sm text-[12.5px] font-medium transition-colors
                  ${tab === t.id ? "bg-ink-700 text-white" : "text-dim-300 hover:text-white hover:bg-ink-800"}`}
              >
                {t.label}
              </button>
            ))}
          </nav>

          <div className="flex items-center gap-2">
            <Link
              className="hidden lg:inline-flex items-center gap-1.5 px-3 h-8 border border-ink-600/70 rounded-sm bg-ink-900 text-[11.5px] font-mono text-dim-300 hover:text-white"
              href="/history"
            >
              <span className="text-dim-500">HISTORY</span>
              <Icon.Ext />
            </Link>
            <a
              className="hidden lg:inline-flex items-center gap-1.5 px-3 h-8 border border-ink-600/70 rounded-sm bg-ink-900 text-[11.5px] font-mono text-dim-300 hover:text-white"
              href="#"
            >
              <span className="text-dim-500">DOCS</span>
              <Icon.Ext />
            </a>
            <a
              className="hidden lg:inline-flex items-center gap-1.5 px-3 h-8 border border-ink-600/70 rounded-sm bg-ink-900 text-[11.5px] font-mono text-dim-300 hover:text-white"
              href="#"
            >
              <span className="text-dim-500">GH</span>
              <Icon.Ext />
            </a>
            <ConnectWalletButton />
          </div>
        </div>

        <div className="md:hidden border-t border-ink-600/60 flex">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex-1 h-10 text-[12px] font-medium transition-colors border-r last:border-r-0 border-ink-600/60
                ${tab === t.id ? "text-white bg-ink-800" : "text-dim-300 bg-ink-950"}`}
            >
              {t.short}
              {tab === t.id && <div className="h-[2px] bg-neon mt-1 mx-3" />}
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
      <div className="relative w-7 h-7 grid place-items-center bg-ink-900 border border-ink-500 rounded-sm">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#00FF88" strokeWidth="2" strokeLinejoin="round">
          <path d="M12 3 21 7.5v9L12 21 3 16.5v-9z" />
          <circle cx="12" cy="12" r="2.5" fill="#00FF88" stroke="none" />
        </svg>
        <span
          className="absolute -inset-px rounded-sm pointer-events-none"
          style={{ boxShadow: "inset 0 0 12px rgba(0,255,136,0.2)" }}
        />
      </div>
      <div className="leading-none">
        <div className="font-mono text-[13px] text-white font-semibold tracking-tight">VAULT8004</div>
        <div className="font-mono text-[9.5px] text-dim-500 tracking-[0.18em] uppercase mt-0.5">ERC-8004 · #001</div>
      </div>
    </a>
  );
}

function ConnectWalletButton() {
  const [connected, setConnected] = useState(false);
  const [hover, setHover] = useState(false);
  const addr = "0x9F3a...87bC";

  if (connected) {
    return (
      <button
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        onClick={() => setConnected(false)}
        className="inline-flex items-center gap-2 px-3 h-8 bg-ink-900 border border-neon/40 rounded-sm text-[12px] font-mono text-white"
      >
        <span className="w-2 h-2 rounded-full bg-neon live-dot" />
        <span className="tabular">{hover ? "Disconnect" : addr}</span>
      </button>
    );
  }
  return (
    <button
      onClick={() => setConnected(true)}
      className="inline-flex items-center gap-2 px-3.5 h-8 bg-neon text-black rounded-sm text-[12px] font-semibold hover:bg-neon-soft transition-colors"
    >
      <Icon.Wallet />
      Connect Wallet
    </button>
  );
}

function Footer() {
  return (
    <footer className="relative border-t border-ink-600/60 mt-12">
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="mb-6">
          <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-dim-500 mb-3">
            Deployed contracts · Mantle Mainnet
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-px bg-ink-600/40 border border-ink-600/70 rounded-md overflow-hidden">
            {CONTRACTS.map((c) => (
              <div key={c.label} className="bg-ink-900 px-4 py-2.5 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[9.5px] font-mono uppercase tracking-[0.14em] text-dim-500 flex items-center gap-1.5">
                    {c.label}
                    {c.placeholder && (
                      <span className="text-warn/70 normal-case tracking-normal text-[9px]">[mainnet pending]</span>
                    )}
                    {c.sub && (
                      <span className="text-dim-600 normal-case tracking-normal text-[9px]">{c.sub}</span>
                    )}
                  </div>
                </div>
                <HashChip hash={c.hash} head={6} tail={4} />
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 text-[11px] font-mono text-dim-500 pt-4 border-t border-ink-600/40">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span className="text-dim-300">Vault8004 · vUSDC</span>
            <span className="text-dim-600">·</span>
            <span>ERC-8004 · Mantle Mainnet</span>
            <span className="text-dim-600">·</span>
            <span>Built for Mantle Turing Test Hackathon 2026</span>
          </div>
          <div className="flex items-center gap-4">
            <span>
              Block <span className="text-dim-300 tabular">#4,219,847</span>
            </span>
            <span>
              Latency <span className="text-neon tabular">142ms</span>
            </span>
            <span className="inline-flex items-center gap-1.5">
              <LiveDot size={6} /> RPC OK
            </span>
          </div>
        </div>
      </div>
    </footer>
  );
}
