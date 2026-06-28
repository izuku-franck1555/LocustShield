#!/usr/bin/env bash
# Launch the R0 suitability platform (FastAPI + static frontend).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv (.venv) ..."
  python3.12 -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip wheel
  ./.venv/bin/python -m pip install -r requirements.txt
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "Serving on http://${HOST}:${PORT}  (API under /api, frontend at /)"
exec ./.venv/bin/uvicorn backend.api:app --host "$HOST" --port "$PORT" "$@"
