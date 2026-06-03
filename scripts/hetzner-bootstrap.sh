#!/usr/bin/env bash
#
# Hetzner box bootstrap for the Vault8004 agent compose stack
# (`mainnet-operations.11`). Idempotent — safe to re-run.
#
# Run on a fresh Ubuntu 24.04 LTS box, as root, after you have:
#   1. Pointed AGENT_API_DOMAIN's A-record at this box's public IP.
#   2. scp'd .env to /root/vault8004/.env
#   3. scp'd the Bybit RSA private key to the path referenced by
#      BYBIT_RSA_HOST_PATH (default /etc/vault8004/bybit-rsa.pem).
#
# What it does:
#   - Installs docker engine + compose plugin from docker.com if missing.
#   - Clones (or pulls) the repo into /opt/vault8004.
#   - Verifies .env exists and the RSA PEM is readable.
#   - Symlinks /opt/vault8004/.env to the .env you placed.
#   - Runs `docker compose up -d --build`.
#   - Tails healthchecks until postgres + api report healthy.

set -euo pipefail

# Two modes for delivering source to the box:
#   1. git clone (set REPO_URL to a private https URL with a deploy token,
#      e.g. https://x-access-token:<token>@github.com/<org>/open-vault.git)
#   2. rsync from the dev machine (skip REPO_URL — just rsync the working
#      tree to $REPO_DIR before running this script; the git step is a no-op
#      if $REPO_DIR/compose.yml already exists and no .git is present).
REPO_URL="${REPO_URL:-}"
REPO_DIR="${REPO_DIR:-/opt/vault8004}"
ENV_SRC="${ENV_SRC:-/root/vault8004/.env}"
LOG_PREFIX="[hetzner-bootstrap]"

log() { printf '%s %s\n' "$LOG_PREFIX" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "run as root (sudo -i then re-run)"
}

install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log "docker + compose plugin already present, skipping install"
        return
    fi
    log "installing docker engine + compose plugin"
    apt-get update
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release git
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
    fi
    local codename
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    cat >/etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $codename stable
EOF
    apt-get update
    apt-get install -y \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
}

clone_or_pull_repo() {
    if [[ -d $REPO_DIR/.git ]]; then
        log "repo already at $REPO_DIR, pulling main"
        git -C "$REPO_DIR" fetch --quiet origin main
        git -C "$REPO_DIR" reset --hard origin/main
        return
    fi
    if [[ -f $REPO_DIR/compose.yml ]]; then
        log "no .git but $REPO_DIR/compose.yml exists — assuming rsync mode, skipping git"
        return
    fi
    [[ -n $REPO_URL ]] || die "REPO_DIR=$REPO_DIR is empty and REPO_URL is not set; rsync the tree first or set REPO_URL"
    log "cloning $REPO_URL into $REPO_DIR"
    git clone --depth=1 "$REPO_URL" "$REPO_DIR"
}

link_env() {
    [[ -f $ENV_SRC ]] || die ".env not found at $ENV_SRC — scp it first"
    if [[ -L $REPO_DIR/.env || -f $REPO_DIR/.env ]]; then
        local existing
        existing="$(readlink -f "$REPO_DIR/.env" 2>/dev/null || echo "")"
        if [[ $existing == "$(readlink -f "$ENV_SRC")" ]]; then
            log ".env already symlinked to $ENV_SRC"
            return
        fi
        log "replacing existing .env (was → $existing)"
        rm -f "$REPO_DIR/.env"
    fi
    ln -s "$ENV_SRC" "$REPO_DIR/.env"
    log "linked $REPO_DIR/.env → $ENV_SRC"
}

check_rsa_pem() {
    local pem
    pem="$(grep -E '^BYBIT_RSA_HOST_PATH=' "$ENV_SRC" | head -n1 | cut -d= -f2- || true)"
    pem="${pem:-/etc/vault8004/bybit-rsa.pem}"
    [[ -f $pem ]] || die "Bybit RSA PEM not found at $pem (set BYBIT_RSA_HOST_PATH or place file there)"
    local mode
    mode="$(stat -c %a "$pem")"
    if [[ $mode != "600" && $mode != "400" ]]; then
        log "WARN: $pem mode=$mode, tightening to 600"
        chmod 600 "$pem"
    fi
    log "RSA PEM ok ($pem, mode $(stat -c %a "$pem"))"
}

compose_up() {
    log "docker compose up -d --build"
    cd "$REPO_DIR"
    docker compose up -d --build
}

wait_healthy() {
    log "waiting for postgres + api healthchecks (up to 120s)"
    local deadline=$(( $(date +%s) + 120 ))
    while (( $(date +%s) < deadline )); do
        local pg api
        pg="$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose -f $REPO_DIR/compose.yml ps -q postgres)" 2>/dev/null || echo missing)"
        api="$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose -f $REPO_DIR/compose.yml ps -q api)" 2>/dev/null || echo missing)"
        log "postgres=$pg api=$api"
        if [[ $pg == healthy && $api == healthy ]]; then
            log "all healthy"
            return
        fi
        sleep 5
    done
    die "healthchecks did not pass in 120s — run 'docker compose logs' to inspect"
}

main() {
    require_root
    install_docker
    clone_or_pull_repo
    link_env
    check_rsa_pem
    compose_up
    wait_healthy
    log "done. Tail logs: cd $REPO_DIR && docker compose logs -f agent"
}

main "$@"
