#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FOLDER="$ROOT_DIR/csv_update/EQUITY"
# Kept deliberately conservative (low concurrency, slower per-request pacing)
# since this fires every 6 min during trading hours, on top of the twice-
# daily full-enrichment runs — onvista.de has no documented rate limit and
# csv_analyse_watchlist.py has no 429/403 backoff for it, so avoiding a block
# in the first place matters more here than raw refresh speed.
MAX_JOBS="3"
export REQUEST_SLEEP_SECONDS="0.6"

# Trading-hour windows this script is allowed to actually run in (24h HHMM,
# local time). Crontab can't cleanly express "only within these two specific
# partial-hour windows", so cron just fires this script every 6 minutes on
# weekdays, aligned to :04/:10/:16... (see crontab line below), and this script decides
# whether "now" falls inside a window before doing any work.
WINDOW1_START="0840"
WINDOW1_END="1530"
WINDOW2_START="1610"
WINDOW2_END="2300"

now_hm=$((10#$(date +%H%M)))
w1_start=$((10#$WINDOW1_START))
w1_end=$((10#$WINDOW1_END))
w2_start=$((10#$WINDOW2_START))
w2_end=$((10#$WINDOW2_END))

if ! { [[ "$now_hm" -ge "$w1_start" && "$now_hm" -le "$w1_end" ]] || [[ "$now_hm" -ge "$w2_start" && "$now_hm" -le "$w2_end" ]]; }; then
  exit 0
fi

# Prevent overlapping runs: if a previous invocation is still running when the
# next 6-minute trigger fires, skip this one instead of piling up concurrent
# runs against the same files.
LOCK_FILE="/tmp/run_bid_ask_equity_scheduled.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "$(date): previous run still in progress, skipping." >> "$FOLDER/_bid_ask_cron_logs/skipped.log" 2>/dev/null || true
  exit 0
fi

# Small random delay so this doesn't fire at the exact same wall-clock second
# every 6 minutes — a perfectly periodic request pattern is itself a signal
# some anti-bot heuristics key off.
sleep "$((RANDOM % 20))"

# cron runs with a minimal environment (no conda activation), so pin the
# interpreter explicitly instead of relying on run_watchlist_bid_ask_parallel.sh's
# `command -v python` auto-detection to happen to fall back correctly.
export PYTHON_BIN="python3"

LOG_DIR="$FOLDER/_bid_ask_cron_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +'%Y%m%d_%H%M%S').log"

{
  echo "=== Starting scheduled EQUITY bid/ask refresh: $(date) ==="
  "$ROOT_DIR/scripts/run_watchlist_bid_ask_parallel.sh" "$FOLDER" "$MAX_JOBS"
  echo "=== Finished: $(date) ==="
} >>"$LOG_FILE" 2>&1

# Prune logs older than 3 days -- this runs far more often than the daily
# full-enrichment job, so logs accumulate much faster.
find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +3 -delete

# how to edit crontab (every 6 minutes, weekdays only; this script itself
# narrows that down to the 08:40-15:30 / 16:10-19:00 windows):
# (crontab -l 2>/dev/null; echo "4-59/6 * * * 1-5 /path/to/financial_rag/scripts/run_bid_ask_equity_scheduled.sh") | crontab -
