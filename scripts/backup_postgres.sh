#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${POSTGRES_DB:=financial_rag}"
: "${POSTGRES_USER:=financial_user}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env or the environment}"

mkdir -p backups/postgres

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="backups/postgres/${POSTGRES_DB}_${timestamp}.dump"

docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$backup_path"

echo "Created $backup_path"
