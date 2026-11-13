#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: scripts/restore_postgres.sh backups/postgres/<file>.dump" >&2
  exit 2
fi

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

backup_path="$1"
if [[ ! -f "$backup_path" ]]; then
  echo "Backup file not found: $backup_path" >&2
  exit 1
fi

echo "Restoring $backup_path into database $POSTGRES_DB"
docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$backup_path"
