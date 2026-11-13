#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/app}"
PYTHON_BIN="${PYTHON_BIN:-python}"
OUTPUT_DIR="${OUTPUT_DIR:-$APP_DIR/csv_update/TOP_LIST}"
WRITE_CSV="${WRITE_CSV:-1}"

mkdir -p "$OUTPUT_DIR" "$OUTPUT_DIR/logs"

timestamp="$(date +%Y%m%d_%H%M)"
output_path="$OUTPUT_DIR/top_liste_$timestamp.xlsx"
log_path="$OUTPUT_DIR/logs/top_liste_$timestamp.log"
latest_xlsx="$OUTPUT_DIR/top_liste_latest.xlsx"
latest_csv="$OUTPUT_DIR/top_liste_latest.csv"

args=("$APP_DIR/fetch_finanzen_topflop.py" "--output" "$output_path")
if [[ "$WRITE_CSV" != "0" ]]; then
    args+=("--csv")
fi
# LIVE_PUBLISH=1 writes top_liste_latest.csv after every finished index, and
# LIVE_PG=1 imports it into PostgreSQL each time, so the dashboard fills up
# index by index instead of all at once when the run ends. Both need the CSV.
if [[ "${LIVE_PUBLISH:-0}" == "1" && "$WRITE_CSV" != "0" ]]; then
    args+=("--live-csv" "$latest_csv")
    if [[ "${LIVE_PG:-0}" == "1" ]]; then
        args+=("--live-pg")
    fi
fi
if [[ "$#" -gt 0 ]]; then
    args+=("--indices" "$@")
fi

cd "$APP_DIR"

echo "Financial RAG : $APP_DIR"
echo "Python        : $PYTHON_BIN"
echo "Output XLSX   : $output_path"
echo "Log           : $log_path"

set +e
"$PYTHON_BIN" "${args[@]}" 2>&1 | tee "$log_path"
exit_code="${PIPESTATUS[0]}"
set -e

if [[ "$exit_code" -ne 0 ]]; then
    echo "fetch_finanzen_topflop.py failed with exit code $exit_code. See log: $log_path" >&2
    exit "$exit_code"
fi

if [[ -f "$output_path" ]]; then
    cp -f "$output_path" "$latest_xlsx"
    echo "Updated latest XLSX: $latest_xlsx"
fi

csv_path="${output_path%.xlsx}.csv"
if [[ "$WRITE_CSV" != "0" && -f "$csv_path" ]]; then
    # Copy next to it and rename: a plain cp truncates the file first, and the
    # importer may read it at that moment.
    cp -f "$csv_path" "$latest_csv.tmp"
    mv -f "$latest_csv.tmp" "$latest_csv"
    echo "Updated latest CSV : $latest_csv"
fi

# DAX and TecDAX are refreshed hourly by scripts/run_index_update.sh together
# with the other INDEX lists. Set UPDATE_DAX_TECDAX=1 (the docker-compose
# topflop job does) to refresh them here as well, after every Top list run.
if [[ "${UPDATE_DAX_TECDAX:-0}" == "1" ]]; then
    index_output="$APP_DIR/csv_update/INDEX/DAX40.csv"
    tecdax_output="$APP_DIR/csv_update/INDEX/TecDAX.csv"
    mkdir -p "$(dirname "$index_output")"
    "$PYTHON_BIN" "$APP_DIR/fetch_dax40.py" \
        --output "$index_output" \
        --tecdax-output "$tecdax_output" 2>&1 | tee -a "$log_path"
fi

echo "Done."
