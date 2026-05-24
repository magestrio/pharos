#!/usr/bin/env node
// Wrap setAgentURI(uint256,string) on the canonical ERC-8004 IdentityRegistry
// (Mantle Mainnet) under our Gnosis Safe (2/3). No Safe UI required.
//
// Usage: pnpm safe:set-uri --agent-id 99 --uri 'https://vault8004-web.vercel.app/agent.json'
import { parseArgs } from "node:util";
import { encodeFunctionData, createPublicClient, http } from "viem";
import { executeSafeTx } from "../lib/safe.js";
import { env } from "../lib/env.js";

const IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432" as const;

const ABI = [
  {
    name: "setAgentURI",
    type: "function",
    stateMutability: "nonpayable",
    inputs: [
      { name: "agentId", type: "uint256" },
      { name: "newURI", type: "string" },
    ],
    outputs: [],
  },
  {
    name: "tokenURI",
    type: "function",
    stateMutability: "view",
    inputs: [{ name: "tokenId", type: "uint256" }],
    outputs: [{ type: "string" }],
  },
] as const;

const { values } = parseArgs({
  options: {
    "agent-id": { type: "string" },
    uri: { type: "string" },
    "dry-run": { type: "boolean", default: false },
    "no-verify": { type: "boolean", default: false },
    help: { type: "boolean", short: "h", default: false },
  },
  strict: true,
});

if (values.help || !values["agent-id"] || !values.uri) {
  console.error(
    "usage: pnpm safe:set-uri --agent-id <n> --uri '<ipfs://...|https://...>' [--dry-run] [--no-verify]",
  );
  process.exit(values.help ? 0 : 2);
}

const agentId = BigInt(values["agent-id"]);
const newURI = values.uri;

const data = encodeFunctionData({
  abi: ABI,
  functionName: "setAgentURI",
  args: [agentId, newURI],
});

console.log("============================================================");
console.log("SETAGENTURI");
console.log("============================================================");
console.log(`Target:   ${IDENTITY_REGISTRY}`);
console.log(`agentId:  ${agentId}`);
console.log(`newURI:   ${newURI}`);
console.log(`Selector: ${data.slice(0, 10)} (expected 0x0af28bd3)`);
console.log("============================================================");

if (data.slice(0, 10) !== "0x0af28bd3") {
  console.error("ABORT: selector mismatch");
  process.exit(1);
}

if (values["dry-run"]) {
  console.log(`\nCalldata: ${data}\n--dry-run: no broadcast.`);
  process.exit(0);
}

const { txHash, safeTxHash, signers } = await executeSafeTx({
  to: IDENTITY_REGISTRY,
  value: "0",
  data,
});

console.log("\n============================================================");
console.log("EXECUTED");
console.log("============================================================");
console.log(`safeTxHash: ${safeTxHash}`);
console.log(`txHash:     ${txHash}`);
console.log(`Signers:    ${signers.join(", ")}`);
console.log(`Explorer:   https://mantlescan.xyz/tx/${txHash}`);

if (values["no-verify"]) process.exit(0);

const client = createPublicClient({ transport: http(env.rpcUrl) });
const onChain = await client.readContract({
  address: IDENTITY_REGISTRY,
  abi: ABI,
  functionName: "tokenURI",
  args: [agentId],
});

console.log("\n=== ON-CHAIN VERIFY ===");
console.log(`tokenURI(${agentId}): ${onChain}`);
if (onChain === newURI) {
  console.log("OK — matches submitted URI.");
} else {
  console.error(`MISMATCH — expected: ${newURI}`);
  process.exit(1);
}
