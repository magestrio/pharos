#!/usr/bin/env node
// Wrap register(string) on the canonical ERC-8004 IdentityRegistry under our
// Gnosis Safe (2/3). Mints the agent NFT with msg.sender = Safe → Safe is owner.
// Already executed once (AGENT_ID=99). This is for repeatability / future use.
//
// Usage: pnpm safe:register --uri 'ipfs://<cid>' [--dry-run]
import { parseArgs } from "node:util";
import { encodeFunctionData, decodeEventLog, createPublicClient, http } from "viem";
import { executeSafeTx } from "../lib/safe.js";
import { env } from "../lib/env.js";

const IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432" as const;

const ABI = [
  {
    name: "register",
    type: "function",
    stateMutability: "nonpayable",
    inputs: [{ name: "agentURI", type: "string" }],
    outputs: [{ name: "agentId", type: "uint256" }],
  },
  {
    name: "Registered",
    type: "event",
    inputs: [
      { name: "agentId", type: "uint256", indexed: true },
      { name: "agentURI", type: "string", indexed: false },
      { name: "owner", type: "address", indexed: true },
    ],
  },
] as const;

const { values } = parseArgs({
  options: {
    uri: { type: "string" },
    "dry-run": { type: "boolean", default: false },
    help: { type: "boolean", short: "h", default: false },
  },
  strict: true,
});

if (values.help || !values.uri) {
  console.error("usage: pnpm safe:register --uri '<ipfs://...|https://...>' [--dry-run]");
  process.exit(values.help ? 0 : 2);
}

const newURI = values.uri;

const data = encodeFunctionData({
  abi: ABI,
  functionName: "register",
  args: [newURI],
});

console.log("============================================================");
console.log("REGISTER AGENT");
console.log("============================================================");
console.log(`Target:   ${IDENTITY_REGISTRY}`);
console.log(`agentURI: ${newURI}`);
console.log(`Selector: ${data.slice(0, 10)} (expected 0xf2c298be)`);
console.log("============================================================");

if (data.slice(0, 10) !== "0xf2c298be") {
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

const client = createPublicClient({ transport: http(env.rpcUrl) });
const receipt = await client.getTransactionReceipt({ hash: txHash });

let agentId: bigint | undefined;
let owner: `0x${string}` | undefined;
for (const log of receipt.logs) {
  if (log.address.toLowerCase() !== IDENTITY_REGISTRY.toLowerCase()) continue;
  try {
    const decoded = decodeEventLog({ abi: ABI, data: log.data, topics: log.topics });
    if (decoded.eventName === "Registered") {
      agentId = decoded.args.agentId;
      owner = decoded.args.owner;
      break;
    }
  } catch {
    /* not a Registered event */
  }
}

if (agentId === undefined || owner === undefined) {
  console.error("ABORT: no Registered event in receipt");
  process.exit(1);
}

console.log("\n=== AGENT REGISTERED ===");
console.log(`AGENT_ID: ${agentId}`);
console.log(`Owner:    ${owner}`);
console.log(`Explorer: https://mantlescan.xyz/tx/${txHash}`);
console.log(`NFT view: https://mantlescan.xyz/token/${IDENTITY_REGISTRY}?a=${agentId}`);

if (owner.toLowerCase() !== env.safeAddress.toLowerCase()) {
  console.error(
    `\nWARNING: owner (${owner}) != SAFE_ADDRESS (${env.safeAddress}). Manual review required.`,
  );
  process.exit(1);
}

console.log("\nWrite to .env:");
console.log(`AGENT_ID=${agentId}`);
