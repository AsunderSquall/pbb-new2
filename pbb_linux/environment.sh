#!/usr/bin/env bash
set -euo pipefail
PBB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PBB_ROOT
export PATH="$PBB_ROOT/runtime/miniconda/bin:$PATH"
export LD_LIBRARY_PATH="$PBB_ROOT/runtime/miniconda/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
