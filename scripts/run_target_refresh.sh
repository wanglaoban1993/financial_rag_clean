#!/usr/bin/env bash
set -uo pipefail

# Refreshes the shared analyst-target (TMP) cache read by the EQUITY watchlists,
# the Top list and the INDEX lists. Scheduled for 10:00 and 16:30, Mon-Fri:
#   0 10 * * 1-5 /path/to/financial_rag/scripts/run_target_refresh.sh
#   30 16 * * 1-5 /path/to/financial_rag/scripts/run_target_refresh.sh
# It only writes csv_update/_target_cache.json (atomically), never the list
# CSVs, so it can overlap with the bid/ask, Top/INDEX and full-enrichment jobs.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/csv_update/_target_cache_logs"
mkdir -p "$LOG_DIR"

# One refresh at a time: a manual run during the scheduled one would only
# double the Yahoo traffic.
exec 200>"/tmp/run_target_refresh.lock"
if ! flock -n 200; then
  echo "$(date): previous refresh still in progress, skipping." >> "$LOG_DIR/skipped.log"
  exit 0
fi

# cron runs with a minimal environment (no conda activation).
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="$LOG_DIR/$(date +'%Y%m%d_%H%M%S').log"

{
  echo "=== Starting target price refresh: $(date) ==="
  cd "$ROOT_DIR" && "$PYTHON_BIN" refresh_targets.py --workers "${TARGET_WORKERS:-2}" --sleep "${TARGET_SLEEP_SECONDS:-1.0}"
  echo "=== Finished: $(date) (exit $?) ==="
} >>"$LOG_FILE" 2>&1

# Prune logs older than 30 days so this doesn't grow forever.
find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +30 -delete
