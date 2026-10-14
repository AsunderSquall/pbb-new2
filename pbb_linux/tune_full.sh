#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT/environment.sh"
exec "$ROOT/runtime/miniconda/bin/python" "$ROOT/source/autotune.py" --mode full "$@"
