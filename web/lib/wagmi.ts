import { getDefaultConfig } from "@rainbow-me/rainbowkit";

import { ACTIVE_CHAIN, SUPPORTED_CHAINS } from "@/lib/chains";

const projectId = process.env.NEXT_PUBLIC_WC_PROJECT_ID ?? "vault8004-dev-placeholder";

// `SUPPORTED_CHAINS` puts the active chain first so wagmi defaults to
// it. In local dev that's anvil (31337); in prod it's Mantle (5000).
// Cast: getDefaultConfig wants a mutable `chains` tuple.
export const wagmiConfig = getDefaultConfig({
  appName: "Vault8004",
  projectId,
  chains: [...SUPPORTED_CHAINS] as unknown as Parameters<typeof getDefaultConfig>[0]["chains"],
  ssr: true,
});

export { ACTIVE_CHAIN };
