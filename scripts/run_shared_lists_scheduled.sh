#!/usr/bin/env bash
# Refreshes the BUYLIST and DEPOTS tabs. Unlike the EQUITY/ETF jobs this never
# fetches an ISIN those lists already keep current: such rows are copied from
# the EQUITY/ETF CSVs, and only ISINs found nowhere else are fetched (once, even
# if they sit in several BUYLIST/DEPOTS lists) — see refresh_shared_lists.py.
#
# Usage: run_shared_lists_scheduled.sh bid-ask   # every 6 min, trading hours
#        run_shared_lists_scheduled.sh full      # twice a day, full enrichment
set -uo pipefail

MODE="${1:-bid-ask}"
case "$MODE" in
  bid-ask|full) ;;
  *) echo "usage: $0 [bid-ask|full]" >&2; exit 2 ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHON_BIN="${PYTHON_BIN:-python3}"
export REQUEST_SLEEP_SECONDS="${REQUEST_SLEEP_SECONDS:-0.6}"

# Union of the EQUITY (08:40-23:00) and ETF (09:30-19:00) bid/ask windows. The
# full run isn't windowed: cron decides when it fires.
if [[ "$MODE" == "bid-ask" ]]; then
  now_hm=$((10#$(date +%H%M)))
  if [[ "$now_hm" -lt 840 || "$now_hm" -gt 2300 ]]; then
    exit 0
  fi
fi

# One lock for both modes: they rewrite the same files. A skipped bid/ask tick
# is harmless; the full run waits its turn.
exec 200>/tmp/run_shared_lists.lock
if [[ "$MODE" == "bid-ask" ]]; then
  flock -n 200 || exit 0
  sleep "$((RANDOM % 20))"
else
  flock -w 3000 200 || { echo "$(date): could not get the lock, giving up." >&2; exit 1; }
fi

LOG_DIR="$ROOT_DIR/csv_update/_shared_lists_cron_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +'%Y%m%d_%H%M%S')_$MODE.log"

{
  echo "=== Starting shared-lists $MODE refresh: $(date) ==="
  (cd "$ROOT_DIR" && "$PYTHON_BIN" refresh_shared_lists.py "$MODE")
  echo "=== Finished (exit $?): $(date) ==="
} >>"$LOG_FILE" 2>&1

find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +7 -delete

# how to edit crontab (bid/ask 2 min ahead of the EQUITY/ETF ticks so it copies
# their latest quotes; the full run lands after the daily EQUITY/ETF full runs):
# (crontab -l 2>/dev/null; echo "2-59/6 * * * 1-5 /path/to/financial_rag/scripts/run_shared_lists_scheduled.sh bid-ask"; echo "30 10 * * 1-5 /path/to/financial_rag/scripts/run_shared_lists_scheduled.sh full"; echo "10 17 * * 1-5 /path/to/financial_rag/scripts/run_shared_lists_scheduled.sh full") | crontab -
