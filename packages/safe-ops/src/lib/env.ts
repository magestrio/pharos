import { config } from "dotenv";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "../../../..");

config({ path: resolve(repoRoot, ".env") });

function need(name: string): string {
  const v = process.env[name];
  if (!v || !v.trim()) {
    throw new Error(`Missing env: ${name} (looked in ${repoRoot}/.env and process env)`);
  }
  return v.trim();
}

function asKey(name: string, key: string): `0x${string}` {
  const stripped = key.startsWith("0x") ? key.slice(2) : key;
  if (!/^[0-9a-fA-F]{64}$/.test(stripped)) {
    throw new Error(`${name} must be a 32-byte hex private key (got length ${stripped.length})`);
  }
  return `0x${stripped}` as `0x${string}`;
}

function asAddr(name: string, addr: string): `0x${string}` {
  if (!/^0x[0-9a-fA-F]{40}$/.test(addr)) {
    throw new Error(`${name} must be a 0x + 40-hex address (got: ${addr})`);
  }
  return addr as `0x${string}`;
}

// Lazy getters — each field validates only when accessed, so --dry-run paths
// that don't need signer keys won't fail on missing SAFE_SIGNER_*_KEY.
export const env = {
  get rpcUrl(): string {
    return need("MANTLE_RPC_URL");
  },
  get safeAddress(): `0x${string}` {
    return asAddr("SAFE_ADDRESS", need("SAFE_ADDRESS"));
  },
  get signerAKey(): `0x${string}` {
    return asKey("SAFE_SIGNER_A_KEY", need("SAFE_SIGNER_A_KEY"));
  },
  get signerBKey(): `0x${string}` {
    return asKey("SAFE_SIGNER_B_KEY", need("SAFE_SIGNER_B_KEY"));
  },
};
