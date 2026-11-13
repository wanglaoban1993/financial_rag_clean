#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FOLDER="$ROOT_DIR/csv_update/TOP_LIST"
SOURCE_CSV="$FOLDER/top_liste_latest.csv"

# cron runs with a minimal environment (no conda activation), so pin the
# interpreter explicitly instead of relying on `command -v python`'s
# auto-detection to happen to fall back correctly.
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Slower per-request pacing to avoid Yahoo Finance rate-limiting.
REQUEST_SLEEP_SECONDS="${REQUEST_SLEEP_SECONDS:-1.2}"
CLASSIFICATION_PROVIDER="${CLASSIFICATION_PROVIDER:-minimax}"
YAHOO_BATCH_SIZE="${YAHOO_BATCH_SIZE:-100}"
YAHOO_BATCH_COOLDOWN="${YAHOO_BATCH_COOLDOWN:-5}"

# server_app.import_watchlists only ever reads top_liste_latest.csv for
# TOP_LIST (the timestamped snapshot files are historical archive, not fed to
# Postgres) so that's the only file worth spending API calls resolving.
#
# It also isn't an ActiveTrader export like the EQUITY/ETF folders' CSVs, so
# it can't be handed to csv_analyse_watchlist.py directly: that parser hard
# -requires ISIN/Name/WKN columns (_read_watchlist_csv in
# csv_analyse_watchlist.py), and fetch_finanzen_topflop.py's output has no
# WKN column at all (FetchedAt,Index,Rank,Name,ISIN,...). Convert first.

LOG_DIR="$FOLDER/_cron_logs"
CONVERTED_DIR="$FOLDER/_watchlist_logs/_converted"
mkdir -p "$LOG_DIR" "$CONVERTED_DIR"
LOG_FILE="$LOG_DIR/$(date +'%Y%m%d_%H%M%S').log"
CONVERTED_CSV="$CONVERTED_DIR/top_liste_latest.csv"
OUTPUT_XLSX="$CONVERTED_DIR/top_liste_latest_enriched.xlsx"

{
  echo "=== Starting scheduled TOP_LIST watchlist enrichment run: $(date) ==="

  if [[ ! -f "$SOURCE_CSV" ]]; then
    echo "Source CSV not found: $SOURCE_CSV"
    exit 1
  fi

  "$PYTHON_BIN" "$ROOT_DIR/scripts/convert_topflop_watchlist.py" "$SOURCE_CSV" "$CONVERTED_CSV"

  cd "$ROOT_DIR"
  "$PYTHON_BIN" csv_analyse_watchlist.py "$CONVERTED_CSV" "$OUTPUT_XLSX" \
    --skip-isin-links \
    --skip-subcategories \
    --classification-provider "$CLASSIFICATION_PROVIDER" \
    --sleep "$REQUEST_SLEEP_SECONDS" \
    --yahoo-batch-size "$YAHOO_BATCH_SIZE" \
    --yahoo-batch-cooldown "$YAHOO_BATCH_COOLDOWN" \
    --update-types all

  echo "=== Finished: $(date) ==="
} >>"$LOG_FILE" 2>&1

# Prune logs older than 30 days so this doesn't grow forever.
find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +30 -delete

# how to edit crontab (scheduled 5min after the 11:00/14:00 topflop fetch
# runs, once the fresh CSV has landed; deliberately skips the 8:15/15:45
# fetch slots since those sit too close to the 8:10 equity and 15:40 equity /
# 16:30 ETF runs, which would race writes to the shared
# isin_symbol_candidates.yaml / llm_tag_cache.json caches):
# (crontab -l 2>/dev/null; echo "5 11 * * 1-5 /path/to/financial_rag/scripts/run_watchlist_daily_topflop.sh"; echo "5 14 * * 1-5 /path/to/financial_rag/scripts/run_watchlist_daily_topflop.sh") | crontab -
