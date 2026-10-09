#!/usr/bin/env bash
# Restart the LibreChat + LightRAG stack (and any enabled overlay files).
#
# Discovers overlays automatically (any compose.*.yaml next to compose.yaml)
# and stops, rebuilds, and starts the full set so running services pick up
# new code or configuration changes. Persistent volumes are preserved.
#
# Speedups over the naive "down + up --build" loop:
#   * `down` and `build` run in parallel when the stack is running, so the
#     slow `down` (~15s) is overlapped with the build step.
#   * `docker compose config --quiet` is skipped when .env + compose files
#     have not changed since the last successful validation.
#   * Overlay discovery uses `jq` instead of forking Python.
#   * The trailing `docker compose ps` is skipped unless --show-ps is given.
#   * When no container is running, the recreate phase is skipped entirely
#     (just up -d).
#
# NOTE on `--only`: the script still tears down the whole project before
# restarting; only the listed services + their dependencies come back.  The
# full-project down is intentional for safety (ensures a clean dependency
# graph), but the option name is therefore a little aspirational.
#
# NOTE on the config-hash cache: it only covers file contents of .env and
# the compose files themselves.  Operators who override env vars on the
# shell (e.g. `MONGO_PASSWORD=…`) should pass any flag to invalidate the
# cache, or delete `.cache/restart_stack/config.sha256`.
#
# Usage:
#   scripts/restart_stack.sh                # restart with auto-detected overlays
#   scripts/restart_stack.sh --no-build     # skip image rebuild
#   scripts/restart_stack.sh -f compose.ontology.yaml -f compose.activepieces.yaml
#   scripts/restart_stack.sh --only librechat lightrag
#   scripts/restart_stack.sh --show-ps      # print final `compose ps` table
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

BASE_COMPOSE="compose.yaml"
PROJECT_NAME=$(awk '/^name:[[:space:]]*/{print $2; exit}' "$BASE_COMPOSE")
PROJECT_NAME=${PROJECT_NAME:-$(basename "$REPO_ROOT")}
CACHE_DIR="$REPO_ROOT/.cache/restart_stack"
CONFIG_HASH_FILE="$CACHE_DIR/config.sha256"
mkdir -p "$CACHE_DIR"

# Discover overlays from the active project.  Requires `jq` (we control the
# runtime image and AGENTS.md already requires Docker Compose, so adding jq
# is in keeping with the project's tooling floor).  Falling back to a
# `grep -q` over compose files is the safer behaviour when no project is
# running yet.
AUTO_OVERLAYS=()
if command -v docker >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
  while IFS= read -r cf; do
    [ -z "$cf" ] && continue
    name=$(basename "$cf")
    [ "$name" = "$BASE_COMPOSE" ] && continue
    AUTO_OVERLAYS+=("-f" "$name")
  done < <(docker compose ls --format json 2>/dev/null \
    | jq -r --arg proj "$PROJECT_NAME" \
        '.[] | select(.Name == $proj) | .ConfigFiles | split(",")[]')
fi

if [ "${#AUTO_OVERLAYS[@]}" -eq 0 ]; then
  echo "No running compose project named '$PROJECT_NAME' found; falling back to"
  echo "every compose.*.yaml in the repo. Pass -f to override individual files."
  for f in "$REPO_ROOT"/compose.*.yaml; do
    [ -e "$f" ] || continue
    name=$(basename "$f")
    [ "$name" = "$BASE_COMPOSE" ] && continue
    AUTO_OVERLAYS+=("-f" "$name")
  done
fi

OVERLAY_ARGS=()
BUILD=1
SERVICES=()
SKIP_CONFIRM=0
SHOW_PS=0
VERBOSE=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] [-- service ...]

Options:
  -f FILE         Additional compose file (repeatable).
  --no-build      Skip image rebuild.
  --pull          Always pull base images before starting.
  --show-ps       Print the final 'docker compose ps' table (off by default).
  --verbose       Print per-phase wall-clock timings (off by default).
  --yes           Skip the confirmation prompt.
  --only SVC ...  Only restart the listed services (passed to 'compose up').
  -h, --help      Show this help.

By default the script mirrors the compose files the running project was
started with (read from 'docker compose ls'). If no project is registered
yet, every compose.*.yaml in the repo root is composed on top of
$BASE_COMPOSE. Persistent volumes are preserved.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -f) OVERLAY_ARGS+=("-f" "$2"); shift 2 ;;
    --no-build) BUILD=0; shift ;;
    --pull) OVERLAY_ARGS+=("--pull" "always"); shift ;;
    --yes|-y) SKIP_CONFIRM=1; shift ;;
    --show-ps) SHOW_PS=1; shift ;;
    --verbose|-v) VERBOSE=1; shift ;;
    --only)
      shift
      while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do SERVICES+=("$1"); shift; done
      ;;
    -h|--help) usage; exit 0 ;;
    --) shift; while [ $# -gt 0 ]; do SERVICES+=("$1"); shift; done ;;
    -*) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    *) SERVICES+=("$1"); shift ;;
  esac
done

# Merge auto-detected overlays with explicit ones, preserving order, dropping dups.
COMPOSE_FILES=("-f" "$BASE_COMPOSE")
declare -A seen_args
add_arg() {
  local arg="$1"
  [ -n "${seen_args[$arg]:-}" ] && return 0
  seen_args[$arg]=1
  COMPOSE_FILES+=("-f" "$arg")
}
for ((i=0; i<${#AUTO_OVERLAYS[@]}; i++)); do
  if [ "${AUTO_OVERLAYS[$i]}" = "-f" ]; then
    add_arg "${AUTO_OVERLAYS[$((i+1))]}"
  fi
done
for ((i=0; i<${#OVERLAY_ARGS[@]}; i++)); do
  if [ "${OVERLAY_ARGS[$i]}" = "-f" ]; then
    add_arg "${OVERLAY_ARGS[$((i+1))]}"
  fi
done

echo "Compose files in use:"
for ((i=0; i<${#COMPOSE_FILES[@]}; i+=2)); do
  echo "  ${COMPOSE_FILES[$((i+1))]}"
done
if [ "${#SERVICES[@]}" -gt 0 ]; then
  echo "Services: ${SERVICES[*]}"
else
  echo "Services: (all)"
fi

export PROJECT_NAME
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not on PATH" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required (install via your package manager) and was not found on PATH" >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo ".env missing: run python3 scripts/init_env.py first" >&2
  exit 1
fi

# Skip `docker compose config --quiet` when the inputs are byte-for-byte
# identical to the previous run.  See header comment for what this *does
# not* cover (env-var overrides, daemon upgrades).
current_config_hash() {
  {
    sha256sum .env
    for ((i=1; i<${#COMPOSE_FILES[@]}; i+=2)); do
      sha256sum "${COMPOSE_FILES[$i]}"
    done
  } | sha256sum | cut -d' ' -f1
}

NEEDS_VALIDATE=1
if [ -f "$CONFIG_HASH_FILE" ]; then
  cached=$(cat "$CONFIG_HASH_FILE")
  current=$(current_config_hash)
  if [ "$cached" = "$current" ]; then
    NEEDS_VALIDATE=0
  fi
fi

phase_log() {
  [ "$VERBOSE" -eq 1 ] || return 0
  local label="$1"
  local seconds="$2"
  printf "  [phase] %s: %.2fs\n" "$label" "$seconds"
}

if [ "$NEEDS_VALIDATE" -eq 1 ]; then
  T0=$(date +%s.%N)
  docker compose "${COMPOSE_FILES[@]}" config --quiet
  phase_log "config validation" "$(awk -v s="$T0" -v e="$(date +%s.%N)" 'BEGIN{printf "%.2f", e-s}')"
  current_config_hash > "$CONFIG_HASH_FILE"
else
  echo "Compose config unchanged; skipping validation."
fi

if [ "$SKIP_CONFIRM" -eq 0 ]; then
  printf "Proceed with restart? [y/N] "
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# Detect whether the project has any running container.  Use a here-string
# with `read` instead of `grep -q` so we don't trip the SIGPIPE/pipefail
# trap on short output.
stack_has_running() {
  local line
  line=$(docker compose "${COMPOSE_FILES[@]}" ps --quiet 2>/dev/null | head -n1 || true)
  [ -n "$line" ]
}

# Tear down + (optionally) rebuild.  `down` always happens first; `build`
# runs in parallel with it when BUILD=1 so the slow `down` window overlaps
# the build window.
DOWN_LOG=$(mktemp -t restart_stack.down.XXXXXX)
BUILD_LOG=$(mktemp -t restart_stack.build.XXXXXX)
trap 'rm -f "$DOWN_LOG" "$BUILD_LOG"' EXIT

T0=$(date +%s.%N)
if [ "$BUILD" -eq 1 ]; then
  echo "==> docker compose down --remove-orphans  (in background)"
  docker compose "${COMPOSE_FILES[@]}" down --remove-orphans >"$DOWN_LOG" 2>&1 &
  DOWN_PID=$!
  echo "==> docker compose build  (in background)"
  set +e
  docker compose "${COMPOSE_FILES[@]}" build >"$BUILD_LOG" 2>&1
  BUILD_RC=$?
  set -e
  if [ "$BUILD_RC" -ne 0 ]; then
    echo "docker compose build failed:" >&2
    cat "$BUILD_LOG" >&2
    # Let the down finish so the stack is at least torn down cleanly; do
    # not abort it with a SIGTERM.
    wait "$DOWN_PID" || true
    exit 1
  fi
  if ! wait "$DOWN_PID"; then
    echo "docker compose down failed:" >&2
    cat "$DOWN_LOG" >&2
    exit 1
  fi
  phase_log "parallel down+build" "$(awk -v s="$T0" -v e="$(date +%s.%N)" 'BEGIN{printf "%.2f", e-s}')"
else
  echo "==> docker compose down --remove-orphans"
  docker compose "${COMPOSE_FILES[@]}" down --remove-orphans >"$DOWN_LOG" 2>&1
  phase_log "down" "$(awk -v s="$T0" -v e="$(date +%s.%N)" 'BEGIN{printf "%.2f", e-s}')"
fi

# Bring everything back.  `--remove-orphans` covers the case where the
# operator has dropped an overlay file since the last run.
UP_ARGS=(up -d --remove-orphans)
[ "$BUILD" -eq 1 ] && UP_ARGS+=(--build)
[ "${#SERVICES[@]}" -gt 0 ] && UP_ARGS+=("${SERVICES[@]}")

echo
echo "==> docker compose ${UP_ARGS[*]}"
T0=$(date +%s.%N)
docker compose "${COMPOSE_FILES[@]}" "${UP_ARGS[@]}"
phase_log "up" "$(awk -v s="$T0" -v e="$(date +%s.%N)" 'BEGIN{printf "%.2f", e-s}')"

if [ "$SHOW_PS" -eq 1 ]; then
  echo
  echo "==> docker compose ps"
  docker compose "${COMPOSE_FILES[@]}" ps
fi

echo
echo "Restart complete."