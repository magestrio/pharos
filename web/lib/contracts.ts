import {
  ADDRESSES,
  AaveV3UsdcAdapterABI,
  AaveV3WethAdapterABI,
  BybitAttestorABI,
  CapitalManagerABI,
  VUSDCABI,
} from "@vault8004/abi";
import { mantle } from "wagmi/chains";

const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000" as const;

type EnvKey =
  | "NEXT_PUBLIC_VUSDC_ADDRESS"
  | "NEXT_PUBLIC_CAPITAL_MANAGER_ADDRESS"
  | "NEXT_PUBLIC_AAVE_USDC_ADAPTER_ADDRESS"
  | "NEXT_PUBLIC_AAVE_WETH_ADAPTER_ADDRESS"
  | "NEXT_PUBLIC_BYBIT_ATTESTOR_ADDRESS"
  | "NEXT_PUBLIC_USDC_ADDRESS";

function pickAddress(envKey: EnvKey, fallback: `0x${string}`): `0x${string}` {
  const override = process.env[envKey] as `0x${string}` | undefined;
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

export const VUSDC_CHAIN_ID = mantle.id;
export const VUSDC_ABI = VUSDCABI;

export const isVUsdcConfigured = VUSDC_ADDRESS !== ZERO_ADDRESS;
export const isAllocationConfigured =
  CAPITAL_MANAGER_ADDRESS !== ZERO_ADDRESS &&
  AAVE_USDC_ADAPTER_ADDRESS !== ZERO_ADDRESS &&
  AAVE_WETH_ADAPTER_ADDRESS !== ZERO_ADDRESS &&
  BYBIT_ATTESTOR_ADDRESS !== ZERO_ADDRESS &&
  USDC_ADDRESS !== ZERO_ADDRESS;

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

// Minimal ERC-20 fragment — read-only balanceOf for the cash venue.
export const ERC20_BALANCE_ABI = [
  {
    type: "function",
    name: "balanceOf",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ name: "", type: "uint256" }],
    stateMutability: "view",
  },
] as const;

export const usdcContract = {
  address: USDC_ADDRESS,
  abi: ERC20_BALANCE_ABI,
  chainId: VUSDC_CHAIN_ID,
} as const;
