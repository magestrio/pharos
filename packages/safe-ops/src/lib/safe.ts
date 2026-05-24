// protocol-kit is CJS; tsx's ESM loader does NOT apply esModuleInterop at
// runtime, so `import Safe from "..."` yields the namespace object instead of
// the default export. Unwrap defensively at the call site.
import SafeMod from "@safe-global/protocol-kit";
import { env } from "./env.js";

const Safe = ((SafeMod as unknown) as { default?: typeof SafeMod }).default ?? SafeMod;

export interface SafeTxInput {
  to: `0x${string}`;
  value: bigint | string;
  data: `0x${string}`;
}

export interface SafeTxResult {
  txHash: `0x${string}`;
  safeTxHash: `0x${string}`;
  signers: `0x${string}`[];
}

// Gathers 2/3 signatures locally (A then B) and broadcasts the executed tx.
// No Safe Transaction Service round-trip — pure on-chain.
export async function executeSafeTx(tx: SafeTxInput): Promise<SafeTxResult> {
  const safeA = await Safe.init({
    provider: env.rpcUrl,
    signer: env.signerAKey,
    safeAddress: env.safeAddress,
  });

  const safeTx = await safeA.createTransaction({
    transactions: [
      {
        to: tx.to,
        value: tx.value.toString(),
        data: tx.data,
      },
    ],
  });

  const safeTxHash = (await safeA.getTransactionHash(safeTx)) as `0x${string}`;

  const signedA = await safeA.signTransaction(safeTx);

  const safeB = await Safe.init({
    provider: env.rpcUrl,
    signer: env.signerBKey,
    safeAddress: env.safeAddress,
  });
  const signedAB = await safeB.signTransaction(signedA);

  // Sanity: enough signatures to satisfy threshold
  const threshold = await safeA.getThreshold();
  if (signedAB.signatures.size < threshold) {
    throw new Error(`got ${signedAB.signatures.size} sigs, need ${threshold}`);
  }

  const execResult = (await safeA.executeTransaction(signedAB)) as {
    hash?: string;
    transactionResponse?: { hash?: string; wait?: () => Promise<unknown> };
  };
  // Safe SDK v5: top-level `hash`. Older shapes nest under `transactionResponse`.
  const txHash = (execResult.hash ?? execResult.transactionResponse?.hash) as
    | `0x${string}`
    | undefined;
  if (!txHash) throw new Error("execute returned no tx hash");
  // Best-effort wait for inclusion if the SDK exposed it.
  await execResult.transactionResponse?.wait?.();

  return {
    txHash,
    safeTxHash,
    signers: [...signedAB.signatures.keys()] as `0x${string}`[],
  };
}

export async function describeSafe(): Promise<{
  address: `0x${string}`;
  owners: `0x${string}`[];
  threshold: number;
}> {
  // Read-only init — no signer key required.
  const safe = await Safe.init({
    provider: env.rpcUrl,
    safeAddress: env.safeAddress,
  });
  return {
    address: env.safeAddress,
    owners: (await safe.getOwners()) as `0x${string}`[],
    threshold: await safe.getThreshold(),
  };
}
