#!/usr/bin/env bash
set -euo pipefail

# Render Native Runtime start command: bash render-start.sh
export DENO_INSTALL="${DENO_INSTALL:-$PWD/.deno}"
export PATH="$DENO_INSTALL/bin:$PATH"
export DENO_NO_UPDATE_CHECK=1
export DENO_NO_PROMPT=1

command -v deno >/dev/null
deno --version

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8010}"
