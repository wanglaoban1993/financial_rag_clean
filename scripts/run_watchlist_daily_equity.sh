#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FOLDER="$ROOT_DIR/csv_update/EQUITY"
MAX_JOBS="10"

# cron runs with a minimal environment (no conda activation), so pin the
# interpreter explicitly instead of relying on run_watchlist_equity_parallel.sh's
# `command -v python` auto-detection to happen to fall back correctly.
export PYTHON_BIN="python3"

# Slower per-request pacing to avoid Yahoo Finance rate-limiting on large files
# (some watchlists have 100+ rows, each needing its own Yahoo lookup).
export REQUEST_SLEEP_SECONDS="1.2"

LOG_DIR="$FOLDER/_cron_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +'%Y%m%d_%H%M%S').log"

{
  echo "=== Starting scheduled EQUITY watchlist run: $(date) ==="
  "$ROOT_DIR/scripts/run_watchlist_equity_parallel.sh" "$FOLDER" "$MAX_JOBS"
  echo "=== Finished: $(date) ==="
} >>"$LOG_FILE" 2>&1

# Prune logs older than 30 days so this doesn't grow forever.
find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +30 -delete

# how to edit crontab: 
# (crontab -l 2>/dev/null; echo "10 8 * * 1-5 /path/to/financial_rag/scripts/run_watchlist_daily_equity.sh"; echo "40 15 * * 1-5 /path/to/financial_rag/scripts/run_watchlist_daily_equity.sh") | crontab -
