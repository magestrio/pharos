// Mirrors the IPFS-pinned agent manifest at
//   ipfs://bafkreibb4udpnjuyg5h5sijkw67rimvs3bwybnp5e6tabdynkah7ui3gj4
// Keep this in sync with /assets/agent/agent.json (image CID inlined).
// Once vault8004.xyz is live, Safe can `setAgentURI(99, "https://vault8004.xyz/agent.json")`.

import { NextResponse } from "next/server";

export const dynamic = "force-static";

const AGENT_MANIFEST = {
  name: "Vault8004 Agent",
  description:
    "Autonomous AI portfolio manager for vUSDC - AI-managed yield-bearing USDC wrapper on Mantle. Allocates capital across Bybit Earn (200+ products via attested oracle) with event-driven rebalancing and 4h cron fallback. Every decision logged on-chain in DecisionLog with rationale pinned to IPFS. Reputation pushed to canonical ERC-8004 ReputationRegistry as cumulative annualized APR in basis points, computed from vUSDC.exchangeRate() growth.",
  image: "ipfs://bafkreidcnwysk4xipcl7mnlkgirrxd6euoe7qq2uhk2opv5lgn7vbp33xa",
  endpoint: "https://vault8004.xyz/agent.json",
  attributes: [
    { trait_type: "Model", value: "Claude Opus 4.7" },
    { trait_type: "Product", value: "vUSDC - yield-bearing USDC wrapper" },
    { trait_type: "Strategy", value: "Bybit Earn (200+ products) + delta-neutral hedging" },
    { trait_type: "Risk Profile", value: "AI-curated, hedged volatile + stable yield" },
    { trait_type: "Cycle Interval", value: "Event-driven + 4h fallback" },
    { trait_type: "Mainnet", value: "Mantle" },
    { trait_type: "Reputation", value: "ERC-8004 ReputationRegistry" },
    { trait_type: "Owner", value: "Gnosis Safe (2-of-3)" },
    { trait_type: "Bybit Bridge", value: "Attested oracle (Ondo USDY pattern)" },
  ],
};

export function GET() {
  return NextResponse.json(AGENT_MANIFEST, {
    headers: {
      "Cache-Control": "public, s-maxage=3600, stale-while-revalidate=86400",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
