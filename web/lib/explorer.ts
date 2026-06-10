import { mantle, mantleSepoliaTestnet } from "wagmi/chains";

import { ACTIVE_CHAIN, anvilLocal } from "@/lib/chains";

const EXPLORER_BASE: Record<number, string> = {
  [mantle.id]: "https://explorer.mantle.xyz",
  [mantleSepoliaTestnet.id]: "https://explorer.sepolia.mantle.xyz",
  // Local anvil - no real explorer. Surface a deterministic placeholder
  // so the UI link is still clickable (opens a tab with "anvil-local")
  // instead of silently 404'ing on Mantle's site.
  [anvilLocal.id]: "https://anvil-local.invalid",
};

const DEFAULT_CHAIN_ID = ACTIVE_CHAIN.id;

function base(chainId: number = DEFAULT_CHAIN_ID): string {
  return EXPLORER_BASE[chainId] ?? EXPLORER_BASE[DEFAULT_CHAIN_ID];
}

export function mantleExplorerTx(hash: string, chainId?: number): string {
  return `${base(chainId)}/tx/${hash}`;
}

export function mantleExplorerAddress(addr: string, chainId?: number): string {
  return `${base(chainId)}/address/${addr}`;
}

export function mantleExplorerBlock(blockNumber: bigint | number, chainId?: number): string {
  return `${base(chainId)}/block/${blockNumber.toString()}`;
}

const IPFS_GATEWAY = "https://gateway.pinata.cloud/ipfs";

export function ipfsGateway(cid: string): string {
  const trimmed = cid.startsWith("ipfs://") ? cid.slice("ipfs://".length) : cid;
  return `${IPFS_GATEWAY}/${trimmed}`;
}
