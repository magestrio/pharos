// Single source of truth for product branding.
//
// The product is "Pharos" / "Pharos Vault". `vUSDC` is the on-chain token
// symbol (deployed on Mantle) and must NOT be rebranded here - the UI would
// then disagree with the wallet / explorer. "ERC-8004" is the on-chain
// protocol, also not a brand string. Keep those literal at their callsites.

export const BRAND = {
  name: "Pharos",
  full: "Pharos Vault",
  token: "vUSDC",
  wordmark: "PHAROS",
} as const;
