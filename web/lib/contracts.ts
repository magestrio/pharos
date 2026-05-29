import { ADDRESSES, VUSDCABI } from "@vault8004/abi";
import { mantle } from "wagmi/chains";

const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000" as const;

function pickVUsdcAddress(): `0x${string}` {
  const override = process.env.NEXT_PUBLIC_VUSDC_ADDRESS as `0x${string}` | undefined;
  if (override && override !== ZERO_ADDRESS) return override;
  return ADDRESSES.mantleMainnet.vault;
}

export const VUSDC_ADDRESS = pickVUsdcAddress();
export const VUSDC_CHAIN_ID = mantle.id;
export const VUSDC_ABI = VUSDCABI;

export const isVUsdcConfigured = VUSDC_ADDRESS !== ZERO_ADDRESS;

export const vUsdcContract = {
  address: VUSDC_ADDRESS,
  abi: VUSDC_ABI,
  chainId: VUSDC_CHAIN_ID,
} as const;
