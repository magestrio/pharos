# shellcheck shell=bash
# remote.sh — SSH helpers for the Hetzner host. Reads HETZNER_HOST,
# HETZNER_USER (default root), HETZNER_REPO_DIR (default /opt/vault8004)
# from the loaded environment (.env).

[[ -n "${_VAULT_REMOTE_LOADED:-}" ]] && return 0
_VAULT_REMOTE_LOADED=1

source "$VAULT_ROOT/scripts/lib/common.sh"

remote_ssh() {
  require_var HETZNER_HOST
  local user="${HETZNER_USER:-root}"
  local cmd=$1
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$user@$HETZNER_HOST" "$cmd"
}

remote_compose() {
  local repo_dir="${HETZNER_REPO_DIR:-/opt/vault8004}"
  remote_ssh "cd $repo_dir && docker compose $*"
}

remote_check() {
  require_var HETZNER_HOST
  local user="${HETZNER_USER:-root}"
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$user@$HETZNER_HOST" true \
    || die "cannot reach $user@$HETZNER_HOST via SSH (check key, host, network)"
}
