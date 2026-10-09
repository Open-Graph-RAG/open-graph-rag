#!/usr/bin/env sh
set -eu
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <snapshot-id|latest>" >&2
  exit 2
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/restore_engine.py" --snapshot "$1" --verify-only
