# Vault8004

AI yield vault on Mantle. See [CONTEXT.md](./CONTEXT.md) for full project context.

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
