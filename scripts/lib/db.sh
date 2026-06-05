# shellcheck shell=bash
# db.sh — postgres container (reuses compose.yml's `postgres` service),
# DROP+CREATE the vault8004 database, then apply migrations via the same
# code path the agent uses at runtime (agent.sandbox.store.schema).

[[ -n "${_VAULT_DB_LOADED:-}" ]] && return 0
_VAULT_DB_LOADED=1

source "$VAULT_ROOT/scripts/lib/common.sh"
source "$VAULT_ROOT/scripts/lib/env.sh"

PG_HOST="${PG_HOST:-127.0.0.1}"
PG_USER="${PG_USER:-vault8004}"
PG_DB="${PG_DB:-vault8004}"
# PG_PORT is read at call time (not source time), because .env hasn't
# been loaded when db.sh is first sourced.
pg_port() { echo "${POSTGRES_HOST_PORT:-5432}"; }

local_database_url() {
  printf 'postgres://%s:%s@%s:%s/%s' \
    "$PG_USER" "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required in .env}" \
    "$PG_HOST" "$(pg_port)" "$PG_DB"
}

start_pg() {
  require_var POSTGRES_PASSWORD
  # `docker compose config` parses the whole file and fails on missing
  # required vars even for services we don't bring up. Caddy needs
  # AGENT_API_DOMAIN + CADDY_EMAIL — supply placeholder locals so the
  # file parses; caddy itself is never started here.
  : "${AGENT_API_DOMAIN:=placeholder.local}"
  : "${CADDY_EMAIL:=local@example.com}"
  export AGENT_API_DOMAIN CADDY_EMAIL

  # If a previous session brought up the full compose stack, the api
  # container is still holding :8000 and the agent container is racing
  # our local uvicorn / loop. Stop them so local processes own those
  # roles (compose's own postgres is the only thing we want).
  ( cd "$VAULT_ROOT" && docker compose stop api agent caddy ) >/dev/null 2>&1 || true

  ( cd "$VAULT_ROOT" && docker compose up -d postgres )

  log_info "waiting for postgres to be ready..."
  local i
  for i in {1..30}; do
    if docker compose -f "$VAULT_ROOT/compose.yml" exec -T postgres \
        pg_isready -U "$PG_USER" -d postgres >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if (( i == 30 )); then
    die "postgres did not become healthy in 30s"
  fi
  ensure_pg_role_or_reinit
  log_info "postgres healthy on $PG_HOST:$(pg_port) (role=$PG_USER)"
}

# Verify the agent will actually be able to auth. We test the SAME path
# asyncpg uses (TCP from host with password), not in-container psql —
# psql over the unix socket uses `trust` auth in the postgres image and
# would falsely pass even when the role doesn't exist.
_host_auth_ok() {
  DATABASE_URL="postgres://$PG_USER:$POSTGRES_PASSWORD@$PG_HOST:$(pg_port)/postgres" \
    uv run --directory "$VAULT_ROOT/agent" python -c '
import asyncio, asyncpg, os, sys
async def main():
    try:
        c = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=5)
        await c.close()
    except Exception as e:
        print(type(e).__name__, e, file=sys.stderr)
        sys.exit(1)
asyncio.run(main())
' >/dev/null 2>&1
}

# Postgres image creates the POSTGRES_USER role only on FIRST init (empty
# pgdata). If the volume was initialized with a different config, the
# role won't exist or the password won't match. Fix: detect via real
# TCP auth, wipe the pgdata volume, let the image re-init cleanly.
# Idempotent: no-op when auth already works.
ensure_pg_role_or_reinit() {
  if _host_auth_ok; then
    return 0
  fi

  log_warn "pg host auth failed (role/password mismatch) — wiping pgdata volume"
  ( cd "$VAULT_ROOT" && docker compose stop postgres ) >/dev/null 2>&1 || true
  ( cd "$VAULT_ROOT" && docker compose rm -f postgres ) >/dev/null 2>&1 || true

  local vol
  vol=$(docker volume ls --format '{{.Name}}' | grep -E '_pgdata$' | head -1 || true)
  if [[ -n $vol ]]; then
    docker volume rm "$vol" >/dev/null 2>&1 || true
    log_dim "removed volume: $vol"
  fi

  ( cd "$VAULT_ROOT" && docker compose up -d postgres )
  log_info "waiting for postgres re-init..."
  local j
  for j in {1..60}; do
    if _host_auth_ok; then
      return 0
    fi
    sleep 1
  done
  die "postgres failed to re-initialize. Check: docker compose logs postgres"
}

stop_pg() {
  ( cd "$VAULT_ROOT" && docker compose stop postgres api agent caddy ) >/dev/null 2>&1 || true
  log_info "postgres + compose sidecars stopped"
}

reset_db() {
  log_info "dropping and recreating database '$PG_DB'"
  PGPASSWORD="$POSTGRES_PASSWORD" docker compose -f "$VAULT_ROOT/compose.yml" \
    exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
    psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname='$PG_DB' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $PG_DB;
CREATE DATABASE $PG_DB OWNER $PG_USER;
SQL

  log_info "applying migrations via agent.sandbox.store.schema"
  DATABASE_URL="$(local_database_url)" \
    uv run --directory "$VAULT_ROOT/agent" python -c "
import asyncio
from agent.sandbox.store.pool import open_pool
from agent.sandbox.store.schema import apply_migrations

async def main():
    async with open_pool() as pool:
        applied = await apply_migrations(pool)
        print('applied:', applied or '(none — db already up-to-date)')

asyncio.run(main())
"
}

db_status() {
  if ! is_alive_container postgres; then
    printf 'down'
    return
  fi
  local cycles last
  cycles=$(PGPASSWORD="$POSTGRES_PASSWORD" docker compose -f "$VAULT_ROOT/compose.yml" \
    exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
    psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT count(*) FROM cycles" 2>/dev/null || echo "?")
  last=$(PGPASSWORD="$POSTGRES_PASSWORD" docker compose -f "$VAULT_ROOT/compose.yml" \
    exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
    psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT max(ts) FROM cycles" 2>/dev/null || echo "?")
  printf ':%s cycles=%s last=%s' "$(pg_port)" "${cycles:-?}" "${last:-?}"
}

is_alive_container() {
  docker compose -f "$VAULT_ROOT/compose.yml" ps --status running --services 2>/dev/null \
    | grep -q "^$1$"
}
