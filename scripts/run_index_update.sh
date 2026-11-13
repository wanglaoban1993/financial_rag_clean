#!/usr/bin/env bash
set -uo pipefail

# Refreshes all INDEX lists: DAX, TecDAX, MDAX, SDAX, EURO STOXX 50, Hang Seng,
# Dow Jones, Nasdaq100. One lookup per constituent on onvista/Yahoo, about six
# minutes for all eight (the Top list job no longer touches DAX/TecDAX). Scheduled hourly on weekdays (more often hit the Yahoo
# and EODHD limits), offset from the Top/INDEX job (:02/:32) and the
# 6-minute bid/ask jobs:
#   9 9-22 * * 1-5 /path/to/financial_rag/scripts/run_index_update.sh
# update_watchlists_to_pg.sh already imports the whole csv_update tree, so the
# dashboard picks the files up with its next import.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/csv_update/INDEX/_cron_logs"
mkdir -p "$LOG_DIR"

# A run that is still going when the next trigger fires makes that trigger skip.
exec 200>"/tmp/run_index_update.lock"
if ! flock -n 200; then
  echo "$(date): previous run still in progress, skipping." >> "$LOG_DIR/skipped.log"
  exit 0
fi

# cron runs with a minimal environment (no conda activation).
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="$LOG_DIR/$(date +'%Y%m%d_%H%M%S').log"

# Each index CSV is imported into PostgreSQL right after it is written (under
# /tmp/pg_import.lock, like update_watchlists_to_pg.sh), so DAX shows up in the
# dashboard minutes into the run instead of at the next :05/:20/:35/:50 import.
# LIVE_PG=0 turns that off.
live_args=()
if [[ "${LIVE_PG:-1}" == "1" ]]; then
  live_args+=(--live-pg)
fi

{
  echo "=== Starting INDEX update: $(date) ==="
  cd "$ROOT_DIR" && "$PYTHON_BIN" fetch_dax40.py \
    --indices DAX TecDAX MDAX SDAX "EURO STOXX 50" "Hang Seng" "Dow Jones" Nasdaq100 \
    "${live_args[@]}"
  echo "=== Finished: $(date) (exit $?) ==="
} >>"$LOG_FILE" 2>&1

# Prune logs older than 30 days so this doesn't grow forever.
find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +30 -delete
