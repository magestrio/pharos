export const ADDRESSES = {
  mantleMainnet: {
    chainId: 5000,
    vault: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    capitalManager: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    decisionLog: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    reputationOracle: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    // Canonical ERC-8004 Identity Registry on Mantle — holds the agent
    // NFT (AGENT_ID = 99) + tokenURI pointer to the pinned agent.json.
    erc8004IdentityRegistry: "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432" as `0x${string}`,
    // Canonical ERC-8004 Reputation Registry on Mantle — receives the
    // per-update score writes from ReputationOracle.
    erc8004ReputationRegistry: "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63" as `0x${string}`,
    aaveUsdcAdapter: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    aaveWethAdapter: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    bybitAttestor: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    usdc: "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9" as `0x${string}`,
  },
  mantleSepolia: {
    chainId: 5003,
    vault: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    capitalManager: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    decisionLog: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    reputationOracle: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    erc8004IdentityRegistry: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    erc8004ReputationRegistry: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    aaveUsdcAdapter: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    aaveWethAdapter: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    bybitAttestor: "0x0000000000000000000000000000000000000000" as `0x${string}`,
    usdc: "0x0000000000000000000000000000000000000000" as `0x${string}`,
  },
} as const;

// Canonical Vault8004 agent ID in the ERC-8004 Identity Registry
// (registered 2026-05-24, owner = SAFE per CLAUDE.md). Override via
// env if testing against a different registration.
export const VAULT_AGENT_ID = 99n;
