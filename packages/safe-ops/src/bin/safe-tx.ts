#!/usr/bin/env node
// Generic Safe tx runner: signs with A + B locally and broadcasts on-chain.
// Usage: pnpm safe:tx --to 0x... --data 0x... [--value 0] [--dry-run]
import { parseArgs } from "node:util";
import { executeSafeTx, describeSafe } from "../lib/safe.js";

const { values } = parseArgs({
  options: {
    to: { type: "string" },
    value: { type: "string", default: "0" },
    data: { type: "string" },
    "dry-run": { type: "boolean", default: false },
    help: { type: "boolean", short: "h", default: false },
  },
  strict: true,
});

if (values.help || !values.to || !values.data) {
  console.error(
    "usage: pnpm safe:tx --to 0x<addr> --data 0x<hex> [--value 0] [--dry-run]",
  );
  process.exit(values.help ? 0 : 2);
}

if (!/^0x[0-9a-fA-F]{40}$/.test(values.to)) {
  console.error(`invalid --to: ${values.to}`);
  process.exit(2);
}
if (!/^0x[0-9a-fA-F]*$/.test(values.data)) {
  console.error("invalid --data: must be hex starting with 0x");
  process.exit(2);
}

const dataPreview =
  values.data.length > 100 ? `${values.data.slice(0, 80)}... (${values.data.length} chars)` : values.data;

const safeInfo = await describeSafe();
console.log("============================================================");
console.log("SAFE TX");
console.log("============================================================");
console.log(`Safe:      ${safeInfo.address}`);
console.log(`Owners:    ${safeInfo.owners.join(", ")}`);
console.log(`Threshold: ${safeInfo.threshold}`);
console.log("---");
console.log(`To:        ${values.to}`);
console.log(`Value:     ${values.value}`);
console.log(`Data:      ${dataPreview}`);
console.log("============================================================");

if (values["dry-run"]) {
  console.log("\n--dry-run: no broadcast.");
  process.exit(0);
}

const { txHash, safeTxHash, signers } = await executeSafeTx({
  to: values.to as `0x${string}`,
  value: values.value,
  data: values.data as `0x${string}`,
});

console.log("\n============================================================");
console.log("EXECUTED");
console.log("============================================================");
console.log(`safeTxHash: ${safeTxHash}`);
console.log(`txHash:     ${txHash}`);
console.log(`Signers:    ${signers.join(", ")}`);
console.log(`Explorer:   https://mantlescan.xyz/tx/${txHash}`);
console.log("============================================================");
