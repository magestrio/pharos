"use client";

import { ConnectButton } from "@rainbow-me/rainbowkit";
import { useEffect, useState } from "react";
import { formatUnits, parseUnits } from "viem";
import {
  useAccount,
  useReadContracts,
  useWaitForTransactionReceipt,
  useWriteContract,
} from "wagmi";

import {
  VUSDC_ADDRESS,
  VUSDC_CHAIN_ID,
  isVUsdcConfigured,
  usdcContract,
  vUsdcContract,
} from "@/lib/contracts";
import { mantleExplorerTx } from "@/lib/explorer";

// USDC + vUSDC are both 6 decimals.
const TOKEN_DECIMALS = 6;
const REFETCH_INTERVAL_MS = 15_000;
const ZERO = 0n;

type Mode = "mint" | "redeem";

export function MintRedeemPanel() {
  const [mode, setMode] = useState<Mode>("mint");
  return (
    <section id="mint-redeem" className="scroll-mt-24">
      <div className="bg-ink-900 border border-ink-600/70 rounded-md overflow-hidden">
        <div className="flex items-center justify-between px-4 sm:px-5 py-3 border-b border-ink-600/70 bg-ink-850">
          <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em] text-dim-400">
            <span className="w-1.5 h-1.5 rounded-sm bg-neon" />
            User Flow
          </div>
          <ConnectButton
            accountStatus={{ smallScreen: "avatar", largeScreen: "full" }}
            chainStatus="icon"
            showBalance={false}
          />
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-px bg-ink-600/40">
          <div className="bg-ink-900 p-5 sm:p-6">
            <div className="flex items-center gap-1.5 bg-ink-850 border border-ink-600/70 rounded-sm p-1 w-fit mb-5">
              <ModeTab active={mode === "mint"} onClick={() => setMode("mint")}>
                Mint
              </ModeTab>
              <ModeTab active={mode === "redeem"} onClick={() => setMode("redeem")}>
                Redeem
              </ModeTab>
            </div>
            {mode === "mint" ? <MintForm /> : <RedeemForm />}
          </div>
          <div className="bg-ink-850/40 p-5 sm:p-6">
            <FlowExplainer mode={mode} />
          </div>
        </div>
      </div>
    </section>
  );
}

function ModeTab({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-4 h-8 rounded-sm text-[12px] font-mono transition-colors ${
        active ? "bg-ink-700 text-white" : "text-dim-300 hover:text-white hover:bg-ink-800"
      }`}
    >
      {children}
    </button>
  );
}

function FlowExplainer({ mode }: { mode: Mode }) {
  if (mode === "mint") {
    return (
      <div className="space-y-3 text-[12px] text-dim-300 leading-relaxed">
        <Eyebrow>How mint works</Eyebrow>
        <p>
          1. Approve USDC for the vUSDC contract. One-time per amount; reused for future mints.
        </p>
        <p>
          2. Call <code className="font-mono text-white">vUSDC.mint(usdcAmount, you)</code>. You
          receive <span className="text-neon">vUSDC</span> at the current exchange rate.
        </p>
        <p>
          As the agent allocates the underlying USDC across Aave + Bybit Earn, the exchange rate
          grows. Your vUSDC redeems for more USDC than it costs today — yield realised, never
          marked-to-market.
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-3 text-[12px] text-dim-300 leading-relaxed">
      <Eyebrow>How redeem works</Eyebrow>
      <p>
        Call <code className="font-mono text-white">vUSDC.redeem(vusdcAmount, you)</code>. Your vUSDC
        burns; you receive USDC at the current exchange rate.
      </p>
      <p>
        Redemption settles from the on-chain cash buffer. If the buffer is depleted, the agent
        unwinds positions to refill it before your redeem succeeds — usually within one cycle.
      </p>
    </div>
  );
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-dim-500">{children}</div>
  );
}

function MintForm() {
  // Mount-gate: SSR has no wallet / no on-chain reads. Server may also
  // see a different .env snapshot than the cached client bundle. Render
  // the PreDeployNotice placeholder on first paint, then swap to the
  // real form after mount — guarantees SSR/client HTML matches.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const { address, isConnected } = useAccount();
  const [input, setInput] = useState("");
  const parsed = parseAmountSafe(input);

  const reads = useReadContracts({
    allowFailure: true,
    contracts: [
      { ...usdcContract, functionName: "balanceOf", args: [address ?? "0x0000000000000000000000000000000000000000"] },
      { ...usdcContract, functionName: "allowance", args: [address ?? "0x0000000000000000000000000000000000000000", VUSDC_ADDRESS] },
      { ...vUsdcContract, functionName: "previewMint", args: [parsed ?? ZERO] },
    ],
    query: {
      enabled: isVUsdcConfigured && !!address,
      refetchInterval: REFETCH_INTERVAL_MS,
    },
  });

  const [usdcBalanceR, allowanceR, previewR] = reads.data ?? [];
  const usdcBalance = pickBig(usdcBalanceR);
  const allowance = pickBig(allowanceR);
  const previewVusdc = pickBig(previewR);

  const needsApprove = parsed !== null && (allowance ?? ZERO) < parsed;
  const insufficient = parsed !== null && (usdcBalance ?? ZERO) < parsed;

  const approve = useWriteContract();
  const mint = useWriteContract();
  const approveReceipt = useWaitForTransactionReceipt({
    hash: approve.data,
    chainId: VUSDC_CHAIN_ID,
  });
  const mintReceipt = useWaitForTransactionReceipt({
    hash: mint.data,
    chainId: VUSDC_CHAIN_ID,
  });

  if (!mounted || !isVUsdcConfigured) {
    return <PreDeployNotice action="mint" />;
  }

  return (
    <div className="space-y-4">
      <AmountField
        label="USDC to deposit"
        value={input}
        onChange={setInput}
        symbol="USDC"
        balance={formatAmount(usdcBalance)}
        onMax={() =>
          usdcBalance !== undefined && setInput(formatUnits(usdcBalance, TOKEN_DECIMALS))
        }
        disabled={!isConnected}
      />
      <PreviewRow
        label="You receive"
        value={parsed === null ? "—" : `${formatAmount(previewVusdc)} vUSDC`}
        tone="neon"
      />
      {!isConnected ? (
        <ConnectGate />
      ) : insufficient ? (
        <ErrorRow>Insufficient USDC balance for this amount.</ErrorRow>
      ) : needsApprove ? (
        <PrimaryButton
          loading={approve.isPending || approveReceipt.isLoading}
          disabled={parsed === null || parsed === ZERO}
          onClick={() => {
            if (!parsed) return;
            approve.writeContract({
              ...usdcContract,
              functionName: "approve",
              args: [VUSDC_ADDRESS, parsed],
            });
          }}
        >
          {approve.isPending
            ? "Confirm in wallet…"
            : approveReceipt.isLoading
              ? "Approving…"
              : "Approve USDC"}
        </PrimaryButton>
      ) : (
        <PrimaryButton
          loading={mint.isPending || mintReceipt.isLoading}
          disabled={parsed === null || parsed === ZERO || !address}
          onClick={() => {
            if (!parsed || !address) return;
            mint.writeContract({
              ...vUsdcContract,
              functionName: "mint",
              args: [parsed, address],
            });
          }}
        >
          {mint.isPending
            ? "Confirm in wallet…"
            : mintReceipt.isLoading
              ? "Minting…"
              : "Mint vUSDC"}
        </PrimaryButton>
      )}
      <TxStatus
        label="Approve"
        hash={approve.data}
        receipt={approveReceipt}
        error={approve.error}
      />
      <TxStatus label="Mint" hash={mint.data} receipt={mintReceipt} error={mint.error} />
    </div>
  );
}

function RedeemForm() {
  // See MintForm: mount-gate so the first paint matches SSR output.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const { address, isConnected } = useAccount();
  const [input, setInput] = useState("");
  const parsed = parseAmountSafe(input);

  const reads = useReadContracts({
    allowFailure: true,
    contracts: [
      { ...vUsdcContract, functionName: "balanceOf", args: [address ?? "0x0000000000000000000000000000000000000000"] },
      { ...vUsdcContract, functionName: "previewRedeem", args: [parsed ?? ZERO] },
    ],
    query: {
      enabled: isVUsdcConfigured && !!address,
      refetchInterval: REFETCH_INTERVAL_MS,
    },
  });

  const [vUsdcBalanceR, previewR] = reads.data ?? [];
  const vUsdcBalance = pickBig(vUsdcBalanceR);
  const previewUsdc = pickBig(previewR);

  const insufficient = parsed !== null && (vUsdcBalance ?? ZERO) < parsed;

  const redeem = useWriteContract();
  const redeemReceipt = useWaitForTransactionReceipt({
    hash: redeem.data,
    chainId: VUSDC_CHAIN_ID,
  });

  if (!mounted || !isVUsdcConfigured) {
    return <PreDeployNotice action="redeem" />;
  }

  return (
    <div className="space-y-4">
      <AmountField
        label="vUSDC to redeem"
        value={input}
        onChange={setInput}
        symbol="vUSDC"
        balance={formatAmount(vUsdcBalance)}
        onMax={() =>
          vUsdcBalance !== undefined && setInput(formatUnits(vUsdcBalance, TOKEN_DECIMALS))
        }
        disabled={!isConnected}
      />
      <PreviewRow
        label="You receive"
        value={parsed === null ? "—" : `${formatAmount(previewUsdc)} USDC`}
        tone="neon"
      />
      {!isConnected ? (
        <ConnectGate />
      ) : insufficient ? (
        <ErrorRow>Insufficient vUSDC balance for this amount.</ErrorRow>
      ) : (
        <PrimaryButton
          loading={redeem.isPending || redeemReceipt.isLoading}
          disabled={parsed === null || parsed === ZERO || !address}
          onClick={() => {
            if (!parsed || !address) return;
            redeem.writeContract({
              ...vUsdcContract,
              functionName: "redeem",
              args: [parsed, address],
            });
          }}
        >
          {redeem.isPending
            ? "Confirm in wallet…"
            : redeemReceipt.isLoading
              ? "Redeeming…"
              : "Redeem vUSDC"}
        </PrimaryButton>
      )}
      {redeem.error && redeem.error.message.toLowerCase().includes("insufficient") && (
        <ErrorRow>
          On-chain cash buffer depleted — the agent will unwind a position to refill it.
        </ErrorRow>
      )}
      <TxStatus label="Redeem" hash={redeem.data} receipt={redeemReceipt} error={redeem.error} />
    </div>
  );
}

function AmountField({
  label,
  value,
  onChange,
  symbol,
  balance,
  onMax,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  symbol: string;
  balance: string | null;
  onMax: () => void;
  disabled?: boolean;
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[10px] font-mono uppercase tracking-[0.18em] text-dim-500">{label}</span>
        {balance !== null && (
          <button
            type="button"
            onClick={onMax}
            disabled={disabled}
            className="text-[10px] font-mono text-dim-400 hover:text-white disabled:hover:text-dim-400 tabular"
          >
            balance: {balance} <span className="text-elec">MAX</span>
          </button>
        )}
      </div>
      <div className="flex items-center gap-2 bg-ink-850 border border-ink-600/70 rounded-sm px-3 h-12 focus-within:border-neon/50">
        <input
          inputMode="decimal"
          placeholder="0.00"
          value={value}
          onChange={(e) => onChange(sanitizeDecimal(e.target.value))}
          disabled={disabled}
          className="flex-1 bg-transparent outline-none font-mono text-white tabular text-lg disabled:opacity-60"
        />
        <span className="text-[12px] font-mono text-dim-300">{symbol}</span>
      </div>
    </div>
  );
}

function PreviewRow({
  label,
  value,
  tone = "white",
}: {
  label: string;
  value: string;
  tone?: "white" | "neon";
}) {
  return (
    <div className="flex items-center justify-between text-[12px] font-mono">
      <span className="text-dim-500">{label}</span>
      <span className={`tabular ${tone === "neon" ? "text-neon" : "text-white"}`}>{value}</span>
    </div>
  );
}

function PrimaryButton({
  children,
  onClick,
  disabled,
  loading,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  loading?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      className="w-full bg-neon text-black h-11 rounded-sm text-[13px] font-medium hover:bg-neon-soft transition-colors disabled:bg-ink-700 disabled:text-dim-500"
    >
      {children}
    </button>
  );
}

function ConnectGate() {
  return (
    <div className="bg-ink-850/60 border border-ink-600/40 rounded-sm px-3 py-3 text-[12px] font-mono text-dim-300 text-center">
      Connect wallet to continue
    </div>
  );
}

function PreDeployNotice({ action }: { action: "mint" | "redeem" }) {
  return (
    <div className="bg-ink-850/60 border border-ink-600/40 rounded-sm px-3 py-4 text-[12px] font-mono text-dim-300">
      <div className="text-warn mb-1">vUSDC contract not deployed yet.</div>
      The {action} flow goes live once the vUSDC stack is on Mantle Mainnet (mainnet-deploy epic).
    </div>
  );
}

function ErrorRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-danger/5 border border-danger/30 rounded-sm px-3 py-2 text-[12px] font-mono text-danger">
      {children}
    </div>
  );
}

type WriteError = { message?: string } | null | undefined;

function TxStatus({
  label,
  hash,
  receipt,
  error,
}: {
  label: string;
  hash: `0x${string}` | undefined;
  receipt: ReturnType<typeof useWaitForTransactionReceipt>;
  error: WriteError;
}) {
  if (!hash && !error) return null;
  if (error && !hash) {
    const msg = error.message ?? "unknown error";
    return <ErrorRow>{label} failed: {msg.slice(0, 120)}</ErrorRow>;
  }
  if (!hash) return null;
  const status = receipt.isLoading
    ? "pending"
    : receipt.isSuccess
      ? "confirmed"
      : receipt.isError
        ? "failed"
        : "submitted";
  const tone =
    status === "confirmed"
      ? "text-neon"
      : status === "failed"
        ? "text-danger"
        : "text-dim-300";
  return (
    <div className="flex items-center justify-between text-[11px] font-mono">
      <span className={`uppercase tracking-[0.14em] ${tone}`}>
        {label}: {status}
      </span>
      <a
        href={mantleExplorerTx(hash)}
        target="_blank"
        rel="noopener noreferrer"
        className="text-elec hover:text-elec-soft"
      >
        {hash.slice(0, 7)}…{hash.slice(-5)}
      </a>
    </div>
  );
}

function pickBig(
  entry: { status?: "success" | "failure"; result?: unknown } | undefined,
): bigint | undefined {
  if (!entry || entry.status !== "success") return undefined;
  if (typeof entry.result === "bigint") return entry.result;
  return undefined;
}

function parseAmountSafe(raw: string): bigint | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  try {
    return parseUnits(trimmed as `${number}`, TOKEN_DECIMALS);
  } catch {
    return null;
  }
}

function sanitizeDecimal(raw: string): string {
  // Allow only digits + single decimal point; drop everything else.
  const cleaned = raw.replace(/[^0-9.]/g, "");
  const firstDot = cleaned.indexOf(".");
  if (firstDot === -1) return cleaned;
  return cleaned.slice(0, firstDot + 1) + cleaned.slice(firstDot + 1).replace(/\./g, "");
}

function formatAmount(value: bigint | undefined): string | null {
  if (value === undefined) return null;
  const asFloat = Number(formatUnits(value, TOKEN_DECIMALS));
  if (!Number.isFinite(asFloat)) return null;
  return asFloat.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

// Anchor id callers can target so the hero button scrolls here.
export const MINT_REDEEM_ANCHOR = "#mint-redeem";
