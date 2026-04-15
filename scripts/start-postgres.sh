#!/usr/bin/env bash
set -euo pipefail

# Local Postgres for dev.
# Uses a named volume so data survives container restarts.

NAME="ctf-agent-postgres"
VOL="ctf-agent-postgres-data"

POSTGRES_DB="${POSTGRES_DB:-ctf_agent}"
POSTGRES_USER="${POSTGRES_USER:-ctf_agent}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-ctf_agent}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker start "$NAME" >/dev/null
  echo "Postgres container already exists; started: $NAME"
  exit 0
fi

docker volume create "$VOL" >/dev/null

docker run -d \
  --name "$NAME" \
  -e POSTGRES_DB="$POSTGRES_DB" \
  -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  -p "$POSTGRES_PORT":5432 \
  -v "$VOL":/var/lib/postgresql/data \
  postgres:16

echo "Started Postgres: $NAME (port $POSTGRES_PORT)"
