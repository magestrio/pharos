#!/usr/bin/env bash
# vault.sh — unified orchestrator for vault8004 (local + remote).
#
#   ./vault.sh local  start|stop|restart|status
#   ./vault.sh local  agent:run | agent:loop
#   ./vault.sh local  db:reset | contracts:deploy | logs <svc>
#   ./vault.sh remote start|stop|restart|status
#   ./vault.sh remote agent:run | agent:loop
#
# `local` mode: anvil-fork of Mantle + postgres (docker) + Deploy.s.sol
# Phase A + Next.js dev + FastAPI read API. All background services have
# PID files in .vault8004/run/<name>.pid and logs at .vault8004/run/<name>.log.
#
# `remote` mode: shells into HETZNER_HOST and runs docker compose there.
# Assumes hetzner-bootstrap.sh has already provisioned the host.

set -euo pipefail

VAULT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
export VAULT_ROOT

source "$VAULT_ROOT/scripts/lib/common.sh"
source "$VAULT_ROOT/scripts/lib/env.sh"
source "$VAULT_ROOT/scripts/lib/proc.sh"

print_help() {
  cat <<'HELP'
vault8004 orchestrator.

  pnpm <command> [--local|--remote]      (default: --local)

Commands:
  start             Bring up anvil-fork, postgres, deploy contracts,
                    start API + web + agent loop (local).
                    Remote: docker compose up -d --build on Hetzner.
  stop              Stop services.
  restart           stop + start.
  status            What's running, contract addresses, db state.
  logs <service>    Tail .vault8004/run/<service>.log (local only).
  agent:run         One agent cycle.
  agent:loop        Continuous agent loop.
  db:reset          Drop + recreate DB + apply migrations (local).
  contracts:deploy  Deploy contracts to running anvil (local).

Examples:
  pnpm start                  # local: everything up
  pnpm start --remote         # ssh into Hetzner, docker compose up -d
  pnpm status --remote
  pnpm agent:run --remote
  pnpm logs agent-loop        # tail local agent log

Direct (no pnpm):
  bash scripts/vault.sh <command> [--local|--remote] [args]
HELP
}

# Dispatch ----------------------------------------------------------------
#
# Argument shape: <command> [--local|--remote] [extra-args...]
# Flag order doesn't matter — --local/--remote can come before or after
# the command. Default is --local.

env_arg=local
cmd=""
extra_args=()

while (( $# > 0 )); do
  case "$1" in
    --local)         env_arg=local;  shift ;;
    --remote)        env_arg=remote; shift ;;
    help|-h|--help)  print_help; exit 0 ;;
    *)
      if [[ -z "$cmd" ]]; then
        cmd=$1
      else
        extra_args+=("$1")
      fi
      shift
      ;;
  esac
done

if [[ -z "$cmd" ]]; then
  print_help
  exit 0
fi

case "$env_arg" in
  local)
    source "$VAULT_ROOT/scripts/lib/anvil.sh"
    source "$VAULT_ROOT/scripts/lib/db.sh"
    source "$VAULT_ROOT/scripts/lib/deploy.sh"
    ;;
  remote)
    source "$VAULT_ROOT/scripts/lib/remote.sh"
    ;;
esac

set -- "${extra_args[@]+"${extra_args[@]}"}"

# Local commands ----------------------------------------------------------

preflight_local() {
  require_cmd anvil forge cast jq docker pnpm uv
  docker info >/dev/null 2>&1 || die "docker daemon not running"
}

start_api_local() {
  start_bg api -- bash -c "
    set -a
    [[ -f '$VAULT_ROOT/.env' ]] && source '$VAULT_ROOT/.env'
    [[ -f '$VAULT_ROOT/.env.local' ]] && source '$VAULT_ROOT/.env.local'
    set +a
    cd '$VAULT_ROOT/agent' && exec uv run uvicorn agent.api.server:app --host 127.0.0.1 --port 8000
  "
}

start_web_local() {
  # Next.js loads NEXT_PUBLIC_* from .env.local in its OWN cwd (web/),
  # not from the monorepo root. Process.env inheritance is unreliable
  # here — `next dev` re-reads files for HMR. Symlink the root files
  # into web/ so Next sees them as if they were native. Both files are
  # gitignored at every depth.
  ln -sf "$VAULT_ROOT/.env.local" "$VAULT_ROOT/web/.env.local"
  [[ -f "$VAULT_ROOT/.env" ]] && ln -sf "$VAULT_ROOT/.env" "$VAULT_ROOT/web/.env"
  start_bg web -- bash -c "
    cd '$VAULT_ROOT' && exec pnpm --filter web dev
  "
}

agent_once_local() {
  load_env
  load_env_local
  log_info "running one agent cycle against anvil-fork (--live --yes --once)"
  ( cd "$VAULT_ROOT/agent" && \
    uv run python -m agent.sandbox.loop --once --enable-store --live --yes )
}

start_agent_loop_local() {
  start_bg agent-loop -- bash -c "
    set -a
    [[ -f '$VAULT_ROOT/.env' ]] && source '$VAULT_ROOT/.env'
    [[ -f '$VAULT_ROOT/.env.local' ]] && source '$VAULT_ROOT/.env.local'
    set +a
    cd '$VAULT_ROOT/agent' && exec uv run python -m agent.sandbox.loop --enable-store --enable-watcher --live --yes
  "
}

show_status_local() {
  load_env 2>/dev/null || true
  load_env_local 2>/dev/null || true

  echo "ENV: local"
  echo "[anvil]    $(anvil_status)"
  if is_alive_container postgres; then
    echo "[postgres] running  $(db_status)"
  else
    echo "[postgres] down"
  fi
  echo "[api]      $(proc_status api)"
  echo "[web]      $(proc_status web)"
  echo "[agent-loop] $(proc_status agent-loop)"
  if [[ -f $VAULT_ROOT/.env.local ]]; then
    echo "[contracts]"
    grep -E '^(CAPITAL_MANAGER|VUSDC|DECISION_LOG|REPUTATION_ORACLE|AAVE_V3_USDC|AAVE_V3_WETH|BYBIT_ATTESTOR)_(ADDRESS|ADAPTER)=' \
      "$VAULT_ROOT/.env.local" | sed 's/^/  /'
  else
    echo "[contracts] .env.local missing — run 'pnpm start' or 'pnpm contracts:deploy'"
  fi
}

start_local() {
  preflight_local
  load_env
  start_anvil
  start_pg
  reset_db
  deploy_local
  load_env_local
  start_api_local
  start_web_local
  start_agent_loop_local
  echo
  show_status_local
  echo
  log_info "everything up. Tail logs: pnpm logs agent-loop"
}

stop_local() {
  set +e
  stop_pidfile agent-loop
  stop_pidfile web
  stop_pidfile api
  stop_anvil
  stop_pg
  set -e
}

# Remote commands ---------------------------------------------------------

remote_start()   { load_env; remote_check; remote_compose up -d --build; }
remote_stop()    { load_env; remote_check; remote_compose down; }
remote_restart() { load_env; remote_check; remote_compose restart; }
remote_status()  { load_env; remote_check; remote_compose ps; }
remote_agent_run() {
  load_env; remote_check
  local repo_dir="${HETZNER_REPO_DIR:-/opt/vault8004}"
  remote_ssh "cd $repo_dir && docker compose exec -T agent python -m agent.sandbox.loop --once --enable-store --live --yes"
}
remote_agent_loop() {
  load_env; remote_check
  local repo_dir="${HETZNER_REPO_DIR:-/opt/vault8004}"
  remote_ssh "cd $repo_dir && docker compose logs -f --tail=50 agent"
}

# Command table -----------------------------------------------------------

run_local() {
  case "$cmd" in
    start)            start_local ;;
    stop)             stop_local ;;
    restart)          stop_local; start_local ;;
    status)           show_status_local ;;
    agent:run)        agent_once_local ;;
    agent:loop)       load_env; load_env_local; start_agent_loop_local ;;
    db:reset)         load_env; start_pg; reset_db ;;
    contracts:deploy) load_env; start_anvil; deploy_local ;;
    logs)
      local svc=${1:-}
      [[ -n $svc ]] || die "usage: pnpm logs <service>  (anvil|api|web|agent-loop)"
      local f="$VAULT_RUN_DIR/$svc.log"
      [[ -f $f ]] || die "no log file at $f"
      tail -f "$f"
      ;;
    *) log_err "unknown command: '$cmd'"; print_help; exit 1 ;;
  esac
}

run_remote() {
  case "$cmd" in
    start)       remote_start ;;
    stop)        remote_stop ;;
    restart)     remote_restart ;;
    status)      remote_status ;;
    agent:run)   remote_agent_run ;;
    agent:loop)  remote_agent_loop ;;
    *) log_err "unknown remote command: '$cmd'"; print_help; exit 1 ;;
  esac
}

if [[ $env_arg == local ]]; then
  run_local "$@"
else
  run_remote "$@"
fi
