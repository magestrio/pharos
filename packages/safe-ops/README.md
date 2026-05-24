# @vault8004/safe-ops

Headless Safe ops via `@safe-global/protocol-kit` — gathers 2/3 signatures locally
(signer A + signer B) and broadcasts the executed tx in one shot. No Safe UI,
no Safe Transaction Service round-trip.

## Setup

Add to repo-root `.env`:

```
MANTLE_RPC_URL=https://rpc.mantle.xyz
SAFE_ADDRESS=0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037
SAFE_SIGNER_A_KEY=0x<32-byte hex private key for signer A>
SAFE_SIGNER_B_KEY=0x<32-byte hex private key for signer B>
```

**Security note:** Keeping both A and B keys on the same machine collapses 2/3 to
effectively 1/1 from a compromise standpoint. Acceptable for hackathon ops
velocity; not for prod treasury. Signer C is cold backup — never put in `.env`.

## Scripts

All accept `--dry-run` (encode + print, no broadcast) and `-h`.

### `pnpm safe:tx` — generic

```bash
pnpm safe:tx --to 0x... --data 0x... [--value 0]
```

### `pnpm safe:set-uri` — wrap `setAgentURI(uint256,string)`

```bash
pnpm safe:set-uri --agent-id 99 --uri 'https://vault8004-web.vercel.app/agent.json'
```

Verifies on-chain `tokenURI(agentId)` matches after execute. Pass `--no-verify`
to skip.

### `pnpm safe:register` — wrap `register(string)` (one-time use)

```bash
pnpm safe:register --uri 'ipfs://<cid>'
```

Parses `Registered` event from receipt, prints `AGENT_ID` + owner check.

## How it works

1. Init `Safe.init` with signer A key → connects to the on-chain Safe contract.
2. `createTransaction({ to, value, data })` → builds Safe-formatted tx data
   (operation = CALL, nonce auto-fetched).
3. `signTransaction(safeTx)` adds signer A's EIP-712 signature.
4. Init second `Safe.init` instance with signer B, call `signTransaction` again
   → appends signer B's signature. Now have 2 sigs ≥ threshold.
5. `executeTransaction(signedAB)` from signer A → broadcasts on-chain. Any
   signer can broadcast; B works too.

## When to fall back to Safe UI

- Need signer C (cold backup). C's key should never be in `.env`.
- Sanity-check tx in Safe simulator before broadcast.
- Co-signers don't trust this machine.
