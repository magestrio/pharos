# shellcheck shell=bash
# common.sh — logging, error helpers, path constants. Sourced by vault.sh
# and every other scripts/lib/*.sh.

# Guard against double-source.
[[ -n "${_VAULT_COMMON_LOADED:-}" ]] && return 0
_VAULT_COMMON_LOADED=1

# Paths — VAULT_ROOT must be exported by the caller (vault.sh sets it).
: "${VAULT_ROOT:?VAULT_ROOT must be set before sourcing common.sh}"
VAULT_RUN_DIR="$VAULT_ROOT/.vault8004/run"
VAULT_STATE_DIR="$VAULT_ROOT/.vault8004"
mkdir -p "$VAULT_RUN_DIR"

# Colors — disabled if stderr not a TTY (CI / piped logs stay readable).
if [[ -t 2 ]]; then
  _C_RED=$'\033[31m'; _C_YEL=$'\033[33m'; _C_GRN=$'\033[32m'
  _C_DIM=$'\033[2m'; _C_RST=$'\033[0m'
else
  _C_RED=""; _C_YEL=""; _C_GRN=""; _C_DIM=""; _C_RST=""
fi

log_info()  { printf '%s[vault]%s %s\n' "$_C_GRN" "$_C_RST" "$*" >&2; }
log_warn()  { printf '%s[vault]%s %s\n' "$_C_YEL" "$_C_RST" "$*" >&2; }
log_err()   { printf '%s[vault]%s %s\n' "$_C_RED" "$_C_RST" "$*" >&2; }
log_dim()   { printf '%s[vault] %s%s\n' "$_C_DIM" "$*" "$_C_RST" >&2; }

die() { log_err "$*"; exit 1; }

require_cmd() {
  local missing=()
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  if (( ${#missing[@]} > 0 )); then
    die "missing required tools: ${missing[*]}. Install them and retry."
  fi
}

require_var() {
  local name=$1
  if [[ -z "${!name:-}" ]]; then
    die "$name is required but not set. Add it to .env or export it."
  fi
}

# Port helpers
port_owner_pid() {  # echoes PID listening on $1 (or empty)
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n1
}
