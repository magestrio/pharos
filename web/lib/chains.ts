import { defineChain } from "viem";
import { mantle, mantleSepoliaTestnet } from "wagmi/chains";

// Local anvil-fork of Mantle mainnet, spun up by `pnpm start` /
// `scripts/vault.sh`. Custom chainId (31337) because anvil defaults
// to that and we want MetaMask to clearly distinguish the local
// devnet from real Mantle (5000) — same RPC behavior, different id.
export const anvilLocal = defineChain({
  id: 31337,
  name: "Anvil (Mantle fork)",
  nativeCurrency: { name: "Mantle", symbol: "MNT", decimals: 18 },
  rpcUrls: {
    default: {
      http: [
        process.env.NEXT_PUBLIC_RPC_URL ?? "http://127.0.0.1:8545",
      ],
    },
  },
  testnet: true,
});

// Resolve which chain the deployed contract addresses correspond to.
// Driven by NEXT_PUBLIC_CHAIN_ID — `scripts/vault.sh` writes "31337"
// into .env.local at local-deploy time, prod env sets "5000".
function resolveActiveChain() {
  const raw = process.env.NEXT_PUBLIC_CHAIN_ID;
  if (!raw) return mantle;
  const id = Number(raw);
  if (id === anvilLocal.id) return anvilLocal;
  if (id === mantleSepoliaTestnet.id) return mantleSepoliaTestnet;
  return mantle;
}

export const ACTIVE_CHAIN = resolveActiveChain();
export const IS_LOCAL_DEV = ACTIVE_CHAIN.id === anvilLocal.id;

// Chain list passed to wagmi. We always include mantle so wagmi's
// `defaultChain` resolution still works in prod previews regardless
// of env; the local anvil prepends only when env says so, otherwise
// MetaMask shouldn't see it as an option.
export const SUPPORTED_CHAINS = IS_LOCAL_DEV
  ? ([anvilLocal, mantle, mantleSepoliaTestnet] as const)
  : ([mantle, mantleSepoliaTestnet] as const);
