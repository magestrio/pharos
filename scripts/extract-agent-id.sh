#!/usr/bin/env bash
#
# Read AGENT_ID from a `Registered(uint256 indexed agentId, string agentURI, address indexed owner)`
# event in the receipt of the Safe-executed tx (subtask .3 / .4).
#
# Usage: scripts/extract-agent-id.sh <txHash>
# Requires: MANTLE_RPC_URL in env, `cast` (Foundry).

set -euo pipefail

TX="${1:?usage: $0 <txHash>}"

if [[ -z "${MANTLE_RPC_URL:-}" && -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
: "${MANTLE_RPC_URL:?MANTLE_RPC_URL not set in env or .env}"

IDENTITY_REGISTRY="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"

# keccak256("Registered(uint256,string,address)")
SIG_HASH=$(cast keccak 'Registered(uint256,string,address)')

cast rpc eth_getTransactionReceipt "$TX" --rpc-url "$MANTLE_RPC_URL" \
  | python3 -c "
import json, sys
r = json.load(sys.stdin)
if r is None:
    sys.exit('receipt is null — tx not mined or wrong hash')
sig = '$SIG_HASH'.lower()
target = '$IDENTITY_REGISTRY'.lower()
for log in r.get('logs', []):
    if (log['address'].lower() == target
            and len(log['topics']) >= 2
            and log['topics'][0].lower() == sig):
        agent_id = int(log['topics'][1], 16)
        owner = '0x' + log['topics'][2][-40:]
        print(f'AGENT_ID={agent_id}')
        print(f'owner={owner}')
        print(f'tx={r[\"transactionHash\"]}')
        print(f'block={int(r[\"blockNumber\"],16)}')
        sys.exit(0)
sys.exit('No Registered event from IdentityRegistry in tx logs')
"
