#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8790}"
PY="${ELISA_PY:-.venv/bin/python}"
BIND="${ELISA_BIND:-127.0.0.1}"

if [ ! -x "$PY" ]; then
  echo "Create a venv first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

export ELISA_BIND="$BIND"
echo "Monitoring · SisuNymous on http://${BIND}:$PORT"
if command -v open >/dev/null 2>&1 && [ "$BIND" = "127.0.0.1" ]; then
  (sleep 1 && open "http://${BIND}:$PORT") &
fi
exec "$PY" -u serve.py "$PORT"
