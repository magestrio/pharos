# Vault8004

AI-managed, yield-bearing USDC wrapper (vUSDC) on Mantle. A Claude Opus agent
allocates capital across Bybit Earn (200+ products via an attested oracle),
rebalancing on market events with a 4h fallback heartbeat.
Every decision is recorded on-chain in a DecisionLog with an IPFS rationale, and
the agent's track record lives in the canonical ERC-8004 Reputation Registry.

**Live demo:** [vault8004-web.vercel.app](https://vault8004-web.vercel.app/)

Built for the Mantle Turing Test 2026 hackathon.

## How it works

1. **Snapshot** — the agent pulls live market data: Bybit Earn APRs across 200+
   products, the USDC peg, funding rates, and an Allora 8h price forecast.
2. **Decide** — Claude Opus proposes a target allocation: weights per venue, with
   product picks and paired perp hedges for any non-stable position.
3. **Validate** — a deterministic validator enforces hard caps independently of
   the LLM: per-venue and per-product limits, liquidity and executability checks,
   peg-stress and hedge rules. The model is never trusted blindly.
4. **Execute** — approved allocations are placed on Bybit and reconciled; capital
   movements are simulated for safety before execution.
5. **Anchor** — the decision plus its IPFS rationale CID is written to the on-chain
   DecisionLog, and reputation (time-weighted return) is attested to ERC-8004.

Rebalancing is event-driven — a watcher reacts to peg moves, funding shifts and
other signals — with a 4h fallback so the vault never goes stale.

## Architecture

- `contracts/` — Solidity 0.8.24 (Foundry): DecisionLog, CapitalManager, and
  Bybit adapters. No upgradeable proxy, no delegatecall.
- `agent/` — Python 3.11: the Claude Opus reasoning loop, deterministic validator,
  Bybit execution, on-chain writer, and a Postgres cycle store with a read API.
- `web/` — Next.js 14 + wagmi v2 dashboard, deployed on Vercel.
- `packages/abi` — shared ABI package generated from `contracts/out/`.
- `packages/safe-ops` — TypeScript CLI for Gnosis Safe operations.

## On-chain (Mantle mainnet)

ERC-8004 identity and reputation:

- Identity Registry — `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`
- Reputation Registry — `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`
- Agent ID — `99`

vUSDC stack:

- DecisionLog — `0xB55dc4C5B671d49bcEcded622167f850D41f0176`
- CapitalManager — `0x7a3b755179DD7Db5d6D1852A977eAaa700Fb874F`
- Bybit attestor adapter — `0x84FAE3ded0d51442206a6678D3c5bE3DDc53317f`
- Safe (2/3, owner) — `0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037`

## Quickstart

```bash
# Install JS dependencies
pnpm install

# Install Foundry contracts dependencies
cd contracts && forge install

# Install Python agent dependencies
cd agent && uv sync
```

## Dev

```bash
pnpm dev:web          # Next.js frontend
pnpm dev:agent        # Python agent
pnpm build:contracts  # Compile contracts + export ABI
```
