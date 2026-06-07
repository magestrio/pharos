# shellcheck shell=bash
# remote.sh — SSH helpers for the Hetzner host. Reads HETZNER_HOST,
# HETZNER_USER (default root), HETZNER_REPO_DIR (default /opt/vault8004)
# from the loaded environment (.env).

[[ -n "${_VAULT_REMOTE_LOADED:-}" ]] && return 0
_VAULT_REMOTE_LOADED=1

source "$VAULT_ROOT/scripts/lib/common.sh"

# Shared SSH opts. Adds `-i $HETZNER_SSH_KEY` (default ~/.ssh/hetzner_ed25519)
# when that key exists — the Hetzner box authorizes a dedicated deploy key
# that is NOT one of ssh's default identities, so without this an empty
# ssh-agent / fresh shell gets "Permission denied (publickey)".
_hetzner_ssh_opts() {
  local key="${HETZNER_SSH_KEY:-$HOME/.ssh/hetzner_ed25519}"
  local opts="-o BatchMode=yes -o ConnectTimeout=10"
  [[ -f "$key" ]] && opts="$opts -i $key"
  printf '%s' "$opts"
}

remote_ssh() {
  require_var HETZNER_HOST
  local user="${HETZNER_USER:-root}"
  local cmd=$1
  # shellcheck disable=SC2086
  ssh $(_hetzner_ssh_opts) "$user@$HETZNER_HOST" "$cmd"
}

remote_compose() {
  local repo_dir="${HETZNER_REPO_DIR:-/opt/vault8004}"
  remote_ssh "cd $repo_dir && docker compose $*"
}

# Push the local working tree to $HETZNER_REPO_DIR via rsync. No git
# remote is configured, so this IS the code-delivery step before a
# `docker compose up -d --build`. NEVER syncs .env/.env.local (the host
# keeps its own prod secrets) or build/runtime/VCS junk. `--delete`
# mirrors the tree so removed files don't linger in the build context.
remote_sync() {
  require_var HETZNER_HOST
  require_cmd rsync
  local user="${HETZNER_USER:-root}"
  local repo_dir="${HETZNER_REPO_DIR:-/opt/vault8004}"
  local excludes=(
    --exclude='.git/'
    --exclude='.env'           # host keeps its own prod .env (symlinked)
    --exclude='.env.*'         # .env.local / .env.example etc.
    --exclude='*.pem'          # never ship private keys
    --exclude='node_modules/'
    --exclude='.venv/'
    --exclude='__pycache__/'
    --exclude='*.pyc'
    --exclude='.pytest_cache/'
    --exclude='.ruff_cache/'
    --exclude='.mypy_cache/'
    --exclude='.vault8004/'    # local run dir (pids/logs)
    --exclude='.next/'
    --exclude='contracts/out/'
    --exclude='contracts/cache/'
    --exclude='contracts/broadcast/'
    --exclude='agent/sandbox/snapshots/'   # docker volumes on host
    --exclude='agent/sandbox/decisions/'
    --exclude='agent/sandbox/executions/'
    --exclude='agent/sandbox/captures/'
    --exclude='agent/sandbox/state/'
    --exclude='agent/sandbox/events/'
    --exclude='.DS_Store'
  )
  log_info "rsync → $user@$HETZNER_HOST:$repo_dir (excl: .env*, *.pem, .git, node_modules, .venv, build/runtime)"
  rsync -az --delete "${excludes[@]}" \
    -e "ssh $(_hetzner_ssh_opts)" \
    "$VAULT_ROOT/" "$user@$HETZNER_HOST:$repo_dir/"
  log_info "sync done"
}

remote_check() {
  require_var HETZNER_HOST
  local user="${HETZNER_USER:-root}"
  # shellcheck disable=SC2086
  ssh $(_hetzner_ssh_opts) "$user@$HETZNER_HOST" true \
    || die "cannot reach $user@$HETZNER_HOST via SSH (check key, host, network)"
}
