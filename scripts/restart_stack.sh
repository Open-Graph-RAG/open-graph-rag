#!/usr/bin/env bash
# Restart the LibreChat + LightRAG stack (and any enabled overlay files).
#
# Discovers overlays automatically (any compose.*.yaml next to compose.yaml)
# and stops, rebuilds, and starts the full set so running services pick up
# new code or configuration changes. Persistent volumes are preserved.
#
# Usage:
#   scripts/restart_stack.sh                # restart with auto-detected overlays
#   scripts/restart_stack.sh --no-build     # skip image rebuild
#   scripts/restart_stack.sh -f compose.ontology.yaml -f compose.activepieces.yaml
#   scripts/restart_stack.sh --only librechat lightrag   # restart specific services
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

BASE_COMPOSE="compose.yaml"
PROJECT_NAME=$(awk '/^name:[[:space:]]*/{print $2; exit}' "$BASE_COMPOSE")
PROJECT_NAME=${PROJECT_NAME:-$(basename "$REPO_ROOT")}
AUTO_OVERLAYS=()

# Prefer the compose files the running project was started with. Falling
# back to "every overlay in the repo" picks up files like
# compose.decision.yaml that the operator has not enabled and breaks
# restarts on hosts that lack the required device drivers.
if command -v docker >/dev/null 2>&1; then
  while IFS= read -r cf; do
    [ -z "$cf" ] && continue
    name=$(basename "$cf")
    [ "$name" = "$BASE_COMPOSE" ] && continue
    AUTO_OVERLAYS+=("-f" "$name")
  done < <(docker compose ls --format json 2>/dev/null \
    | PROJECT_NAME="$PROJECT_NAME" python3 -c "
import json, sys, os
target = os.environ.get('PROJECT_NAME','')
for entry in json.load(sys.stdin):
    if entry.get('Name') != target:
        continue
    cf = entry.get('ConfigFiles')
    if isinstance(cf, str):
        for p in cf.split(','):
            p = p.strip()
            if p:
                print(p)
    elif isinstance(cf, list):
        for p in cf:
            print(p)
")
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

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] [-- service ...]

Options:
  -f FILE         Additional compose file (repeatable).
  --no-build      Pull/build only when images are missing.
  --pull          Always pull base images before starting.
  --yes           Skip the confirmation prompt.
  --only SVC ...  Only restart the listed services (passed to 'compose up').
  -h, --help      Show this help.

By default, the script mirrors the compose files the running project was
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

if [ ! -f .env ]; then
  echo ".env missing: run python3 scripts/init_env.py first" >&2
  exit 1
fi

# Validate the merged config before tearing anything down.
docker compose "${COMPOSE_FILES[@]}" config --quiet

if [ "$SKIP_CONFIRM" -eq 0 ]; then
  printf "Proceed with restart? [y/N] "
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

echo
echo "==> docker compose down"
docker compose "${COMPOSE_FILES[@]}" down --remove-orphans

UP_ARGS=(up -d)
if [ "$BUILD" -eq 1 ]; then UP_ARGS+=(--build); fi
if [ "${#SERVICES[@]}" -gt 0 ]; then UP_ARGS+=("${SERVICES[@]}"); fi

echo
echo "==> docker compose ${UP_ARGS[*]}"
docker compose "${COMPOSE_FILES[@]}" "${UP_ARGS[@]}"

echo
echo "==> docker compose ps"
docker compose "${COMPOSE_FILES[@]}" ps

echo
echo "Restart complete."
