import { getDefaultConfig } from "@rainbow-me/rainbowkit";
import { mantle, mantleSepoliaTestnet } from "wagmi/chains";

const projectId = process.env.NEXT_PUBLIC_WC_PROJECT_ID ?? "vault8004-dev-placeholder";

export const wagmiConfig = getDefaultConfig({
  appName: "Vault8004",
  projectId,
  chains: [mantle, mantleSepoliaTestnet],
  ssr: true,
});
