#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

echo "check Docker __Check Docker__ check Docker"

if [ "${PRODUCERSCENTER_BACKEND_USE_ENV_DATABASE:-false}" != "true" ]; then
  mkdir -p storage
  export PRODUCERSCENTER_BACKEND_DATABASE_URL="sqlite:///./storage/backend.db"
  unset POSTGRES_DB_URL
  unset DATABASE_URL
  echo "Using local SQLite backend DB. Set PRODUCERSCENTER_BACKEND_USE_ENV_DATABASE=true to use .env PostgreSQL."
fi

.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8010}" --reload
