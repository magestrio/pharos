# shellcheck shell=bash
# anvil.sh — start/stop a forked Mantle node on :8545.

[[ -n "${_VAULT_ANVIL_LOADED:-}" ]] && return 0
_VAULT_ANVIL_LOADED=1

source "$VAULT_ROOT/scripts/lib/common.sh"
source "$VAULT_ROOT/scripts/lib/proc.sh"

ANVIL_PORT="${ANVIL_PORT:-8545}"
ANVIL_CHAIN_ID="${ANVIL_CHAIN_ID:-31337}"

# Anvil dev account #0 — public, well-known, only used for local fork.
# Do NOT use these keys anywhere else.
ANVIL_DEPLOYER_ADDR="0xf39Fd6e51aad88F6F4ce6aB8827279cfFFb92266"
ANVIL_DEPLOYER_PK="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_AGENT_ADDR="0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ANVIL_SAFE_ADDR="0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"

start_anvil() {
  require_var MANTLE_RPC_URL

  local existing
  existing=$(port_owner_pid "$ANVIL_PORT" || true)
  if [[ -n $existing ]]; then
    if is_alive "$(_pidf anvil)" && [[ "$(cat "$(_pidf anvil)")" == "$existing" ]]; then
      log_info "anvil already running on :$ANVIL_PORT (pid=$existing)"
      return 0
    fi
    die "port $ANVIL_PORT in use by pid $existing (not ours). Run './vault.sh local stop' or free the port."
  fi

  log_info "starting anvil fork of \$MANTLE_RPC_URL on :$ANVIL_PORT"
  start_bg anvil -- anvil \
    --fork-url "$MANTLE_RPC_URL" \
    --chain-id "$ANVIL_CHAIN_ID" \
    --port "$ANVIL_PORT" \
    --host 127.0.0.1 \
    --block-time 2

  # Wait for RPC to respond.
  local i
  for i in {1..30}; do
    if cast block-number --rpc-url "http://127.0.0.1:$ANVIL_PORT" >/dev/null 2>&1; then
      log_info "anvil healthy (block=$(cast block-number --rpc-url "http://127.0.0.1:$ANVIL_PORT"))"
      return 0
    fi
    sleep 0.5
  done
  log_err "anvil did not become healthy in 15s. Last 30 lines:"
  tail -n 30 "$(_logf anvil)" >&2 || true
  return 1
}

stop_anvil() {
  stop_pidfile anvil
}

anvil_status() {
  if is_alive "$(_pidf anvil)"; then
    local block
    block=$(cast block-number --rpc-url "http://127.0.0.1:$ANVIL_PORT" 2>/dev/null || echo "?")
    printf 'pid=%s :%s block=%s' "$(cat "$(_pidf anvil)")" "$ANVIL_PORT" "$block"
  else
    printf 'down'
  fi
}
