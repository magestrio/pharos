# shellcheck shell=bash
# proc.sh — minimal process supervision via PID files.
#
# `start_bg <name> -- <cmd...>` runs cmd in a new session, writes PID
# to .vault8004/run/<name>.pid and logs to .vault8004/run/<name>.log.
# `stop_pidfile <name>` sends SIGTERM to the process group, waits up to
# 5s, then SIGKILL.

[[ -n "${_VAULT_PROC_LOADED:-}" ]] && return 0
_VAULT_PROC_LOADED=1

source "$VAULT_ROOT/scripts/lib/common.sh"

_pidf() { printf '%s/%s.pid' "$VAULT_RUN_DIR" "$1"; }
_logf() { printf '%s/%s.log' "$VAULT_RUN_DIR" "$1"; }

is_alive() {
  local pidf=$1
  [[ -f $pidf ]] || return 1
  local pid
  pid=$(cat "$pidf" 2>/dev/null) || return 1
  [[ -n $pid ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

start_bg() {
  # usage: start_bg <name> -- <cmd...>
  local name=$1; shift
  [[ "${1:-}" == "--" ]] || die "start_bg: expected '--' before command"
  shift
  local pidf log
  pidf=$(_pidf "$name")
  log=$(_logf "$name")

  if is_alive "$pidf"; then
    log_info "$name already running (pid=$(cat "$pidf"))"
    return 0
  fi
  rm -f "$pidf"

  # Plain nohup — setsid is not available on macOS by default. We kill
  # children explicitly via pgrep -P (see _kill_tree below) instead of
  # relying on process-group semantics.
  nohup "$@" >"$log" 2>&1 &
  local pid=$!
  echo "$pid" >"$pidf"

  # Sanity wait — many tools exit immediately on misconfig.
  sleep 0.4
  if ! is_alive "$pidf"; then
    log_err "$name failed to start. Last 30 lines of $log:"
    tail -n 30 "$log" >&2 || true
    rm -f "$pidf"
    return 1
  fi
  log_info "$name started (pid=$pid, log=$log)"
}

# Collect a process and all its descendants (BFS via pgrep -P). Works on
# both macOS and Linux.
_descendant_pids() {
  local root=$1
  local pids=("$root")
  local i=0
  while (( i < ${#pids[@]} )); do
    local children
    children=$(pgrep -P "${pids[$i]}" 2>/dev/null || true)
    if [[ -n $children ]]; then
      while read -r child; do
        [[ -n $child ]] && pids+=("$child")
      done <<<"$children"
    fi
    ((i++))
  done
  printf '%s\n' "${pids[@]}"
}

_kill_tree() {
  local root=$1
  local signal=${2:-TERM}
  # Kill children first, then the root — avoids zombies inheriting fresh
  # children mid-kill.
  local pids
  mapfile -t pids < <(_descendant_pids "$root")
  local n=${#pids[@]}
  local i
  for (( i=n-1; i>=0; i-- )); do
    kill "-$signal" "${pids[$i]}" 2>/dev/null || true
  done
}

stop_pidfile() {
  local name=$1
  local pidf
  pidf=$(_pidf "$name")
  [[ -f $pidf ]] || return 0
  local pid
  pid=$(cat "$pidf" 2>/dev/null) || { rm -f "$pidf"; return 0; }

  if ! kill -0 "$pid" 2>/dev/null; then
    log_dim "$name: stale pidfile (pid $pid not running), cleaning up"
    rm -f "$pidf"
    return 0
  fi

  _kill_tree "$pid" TERM
  for _ in {1..10}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    log_warn "$name (pid $pid) ignored SIGTERM, sending SIGKILL"
    _kill_tree "$pid" KILL
  fi
  rm -f "$pidf"
  log_info "$name stopped"
}

proc_status() {
  local name=$1
  local pidf
  pidf=$(_pidf "$name")
  if is_alive "$pidf"; then
    printf 'pid=%s' "$(cat "$pidf")"
  else
    printf 'down'
  fi
}
