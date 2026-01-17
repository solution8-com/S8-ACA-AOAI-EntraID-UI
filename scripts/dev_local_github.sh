#!/usr/bin/env bash
set -euo pipefail

compose_cmd=(docker compose)
if ! "${compose_cmd[@]}" version >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
fi

cleanup() {
  "${compose_cmd[@]}" stop redis >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

"${compose_cmd[@]}" up -d redis

if [ "$#" -gt 0 ]; then
  "$@"
else
  python -m quart --app src.quartapp run --port 50505 --reload
fi
