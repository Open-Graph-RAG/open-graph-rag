#!/bin/sh
set -eu
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 DESTINATION_DIRECTORY" >&2
  exit 2
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"
exec python3 "$SCRIPT_DIR/backup_engine.py" snapshot-volumes --destination "$1"
