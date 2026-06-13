import {
  ADDRESSES,
  AaveV3UsdcAdapterABI,
  AaveV3WethAdapterABI,
  BybitAttestorABI,
  CapitalManagerABI,
  DecisionLogABI,
  ReputationOracleABI,
  VUSDCABI,
} from "@vault8004/abi";
import { ACTIVE_CHAIN } from "@/lib/chains";

const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000" as const;

type EnvKey =
  | "NEXT_PUBLIC_VUSDC_ADDRESS"
  | "NEXT_PUBLIC_CAPITAL_MANAGER_ADDRESS"
  | "NEXT_PUBLIC_AAVE_USDC_ADAPTER_ADDRESS"
  | "NEXT_PUBLIC_AAVE_WETH_ADAPTER_ADDRESS"
  | "NEXT_PUBLIC_BYBIT_ATTESTOR_ADDRESS"
  | "NEXT_PUBLIC_USDC_ADDRESS"
  | "NEXT_PUBLIC_DECISION_LOG_ADDRESS"
  | "NEXT_PUBLIC_REPUTATION_ORACLE_ADDRESS";

// Static `process.env.NEXT_PUBLIC_*` access so Next.js inlines each value
// into the client bundle. A dynamic `process.env[envKey]` is NOT inlined
// client-side, so the browser would read `undefined` and fall back to the
// mainnet address while the server reads the real override - a hydration
// mismatch when the two differ (e.g. local `.env.local` anvil overrides).
const ENV_OVERRIDES: Record<EnvKey, string | undefined> = {
  NEXT_PUBLIC_VUSDC_ADDRESS: process.env.NEXT_PUBLIC_VUSDC_ADDRESS,
  NEXT_PUBLIC_CAPITAL_MANAGER_ADDRESS: process.env.NEXT_PUBLIC_CAPITAL_MANAGER_ADDRESS,
  NEXT_PUBLIC_AAVE_USDC_ADAPTER_ADDRESS: process.env.NEXT_PUBLIC_AAVE_USDC_ADAPTER_ADDRESS,
  NEXT_PUBLIC_AAVE_WETH_ADAPTER_ADDRESS: process.env.NEXT_PUBLIC_AAVE_WETH_ADAPTER_ADDRESS,
  NEXT_PUBLIC_BYBIT_ATTESTOR_ADDRESS: process.env.NEXT_PUBLIC_BYBIT_ATTESTOR_ADDRESS,
  NEXT_PUBLIC_USDC_ADDRESS: process.env.NEXT_PUBLIC_USDC_ADDRESS,
  NEXT_PUBLIC_DECISION_LOG_ADDRESS: process.env.NEXT_PUBLIC_DECISION_LOG_ADDRESS,
  NEXT_PUBLIC_REPUTATION_ORACLE_ADDRESS: process.env.NEXT_PUBLIC_REPUTATION_ORACLE_ADDRESS,
};

function pickAddress(envKey: EnvKey, fallback: `0x${string}`): `0x${string}` {
  const override = ENV_OVERRIDES[envKey] as `0x${string}` | undefined;
  if (override && override !== ZERO_ADDRESS) return override;
  return fallback;
}

const M = ADDRESSES.mantleMainnet;

export const VUSDC_ADDRESS = pickAddress("NEXT_PUBLIC_VUSDC_ADDRESS", M.vault);
export const CAPITAL_MANAGER_ADDRESS = pickAddress(
  "NEXT_PUBLIC_CAPITAL_MANAGER_ADDRESS",
  M.capitalManager,
);
export const AAVE_USDC_ADAPTER_ADDRESS = pickAddress(
  "NEXT_PUBLIC_AAVE_USDC_ADAPTER_ADDRESS",
  M.aaveUsdcAdapter,
);
export const AAVE_WETH_ADAPTER_ADDRESS = pickAddress(
  "NEXT_PUBLIC_AAVE_WETH_ADAPTER_ADDRESS",
  M.aaveWethAdapter,
);
export const BYBIT_ATTESTOR_ADDRESS = pickAddress(
  "NEXT_PUBLIC_BYBIT_ATTESTOR_ADDRESS",
  M.bybitAttestor,
);
export const USDC_ADDRESS = pickAddress("NEXT_PUBLIC_USDC_ADDRESS", M.usdc);
export const DECISION_LOG_ADDRESS = pickAddress(
  "NEXT_PUBLIC_DECISION_LOG_ADDRESS",
  M.decisionLog,
);
export const REPUTATION_ORACLE_ADDRESS = pickAddress(
  "NEXT_PUBLIC_REPUTATION_ORACLE_ADDRESS",
  M.reputationOracle,
);
// ERC-8004 registries - already deployed on Mantle Mainnet, no env
// override needed (immutable canonical addresses per CLAUDE.md).
export const IDENTITY_REGISTRY_ADDRESS = M.erc8004IdentityRegistry;
export const REPUTATION_REGISTRY_ADDRESS = M.erc8004ReputationRegistry;

export const VAULT_AGENT_ID: bigint = (() => {
  // Canonical NEXT_PUBLIC_AGENT_ID; fall back to the legacy
  // NEXT_PUBLIC_VAULT_AGENT_ID so a not-yet-updated Vercel env still works.
  const raw =
    process.env.NEXT_PUBLIC_AGENT_ID ?? process.env.NEXT_PUBLIC_VAULT_AGENT_ID;
  if (!raw) return 99n;
  try {
    return BigInt(raw);
  } catch {
    return 99n;
  }
})();

export const VUSDC_CHAIN_ID = ACTIVE_CHAIN.id;
export const VUSDC_ABI = VUSDCABI;

export const isVUsdcConfigured = VUSDC_ADDRESS !== ZERO_ADDRESS;
export const isAllocationConfigured =
  CAPITAL_MANAGER_ADDRESS !== ZERO_ADDRESS &&
  AAVE_USDC_ADAPTER_ADDRESS !== ZERO_ADDRESS &&
  AAVE_WETH_ADAPTER_ADDRESS !== ZERO_ADDRESS &&
  BYBIT_ATTESTOR_ADDRESS !== ZERO_ADDRESS &&
  USDC_ADDRESS !== ZERO_ADDRESS;
export const isDecisionLogConfigured = DECISION_LOG_ADDRESS !== ZERO_ADDRESS;
export const isReputationOracleConfigured = REPUTATION_ORACLE_ADDRESS !== ZERO_ADDRESS;

// Mantle-mainnet DecisionLog (0xB55d…) creation block, verified on-chain via a
// getCode binary search. The event scan starts here so it never walks from
// genesis (96.5M blocks ÷ 10k window ≈ 10_700 requests). Override per
// environment (anvil fork) with NEXT_PUBLIC_DECISION_LOG_DEPLOY_BLOCK.
const DECISION_LOG_DEPLOY_BLOCK_DEFAULT = 96_314_874n;

export const DECISION_LOG_DEPLOY_BLOCK: bigint = (() => {
  const raw = process.env.NEXT_PUBLIC_DECISION_LOG_DEPLOY_BLOCK;
  if (!raw) return DECISION_LOG_DEPLOY_BLOCK_DEFAULT;
  try {
    return BigInt(raw);
  } catch {
    return DECISION_LOG_DEPLOY_BLOCK_DEFAULT;
  }
})();

export const vUsdcContract = {
  address: VUSDC_ADDRESS,
  abi: VUSDC_ABI,
  chainId: VUSDC_CHAIN_ID,
} as const;

export const capitalManagerContract = {
  address: CAPITAL_MANAGER_ADDRESS,
  abi: CapitalManagerABI,
  chainId: VUSDC_CHAIN_ID,
} as const;

export const aaveUsdcAdapterContract = {
  address: AAVE_USDC_ADAPTER_ADDRESS,
  abi: AaveV3UsdcAdapterABI,
  chainId: VUSDC_CHAIN_ID,
} as const;

export const aaveWethAdapterContract = {
  address: AAVE_WETH_ADAPTER_ADDRESS,
  abi: AaveV3WethAdapterABI,
  chainId: VUSDC_CHAIN_ID,
} as const;

export const bybitAttestorContract = {
  address: BYBIT_ATTESTOR_ADDRESS,
  abi: BybitAttestorABI,
  chainId: VUSDC_CHAIN_ID,
} as const;

export const decisionLogContract = {
  address: DECISION_LOG_ADDRESS,
  abi: DecisionLogABI,
  chainId: VUSDC_CHAIN_ID,
} as const;

export const reputationOracleContract = {
  address: REPUTATION_ORACLE_ADDRESS,
  abi: ReputationOracleABI,
  chainId: VUSDC_CHAIN_ID,
} as const;

// ERC-20 fragment - covers everything the mint/redeem flow + cash
// venue allocation need: balanceOf (cash + user), allowance + approve
// (mint approval path), decimals (display formatting).
export const ERC20_ABI = [
  {
    type: "function",
    name: "balanceOf",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ name: "", type: "uint256" }],
    stateMutability: "view",
  },
  {
    type: "function",
    name: "allowance",
    inputs: [
      { name: "owner", type: "address" },
      { name: "spender", type: "address" },
    ],
    outputs: [{ name: "", type: "uint256" }],
    stateMutability: "view",
  },
  {
    type: "function",
    name: "approve",
    inputs: [
      { name: "spender", type: "address" },
      { name: "value", type: "uint256" },
    ],
    outputs: [{ name: "", type: "bool" }],
    stateMutability: "nonpayable",
  },
  {
    type: "function",
    name: "decimals",
    inputs: [],
    outputs: [{ name: "", type: "uint8" }],
    stateMutability: "view",
  },
] as const;

// Kept as alias for back-compat with the allocation hook that only
// needs balanceOf. New call sites should prefer `ERC20_ABI`.
export const ERC20_BALANCE_ABI = ERC20_ABI;

export const usdcContract = {
  address: USDC_ADDRESS,
  abi: ERC20_ABI,
  chainId: VUSDC_CHAIN_ID,
} as const;
