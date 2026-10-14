#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT/environment.sh"
PYTHON="$ROOT/runtime/miniconda/bin/python"
exec "$PYTHON" "$ROOT/source/main.py" -c "$ROOT/config/config.yaml" "$@"
