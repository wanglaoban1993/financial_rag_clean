#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECONDS=0

# Change these defaults when you want to run a different folder or concurrency.
DEFAULT_WATCHLIST_FOLDER="$ROOT_DIR/csv_update/ETF"
DEFAULT_MAX_JOBS="8"
DEFAULT_PYTHON_BIN_FALLBACK="python3"

FOLDER="${1:-${WATCHLIST_FOLDER:-$DEFAULT_WATCHLIST_FOLDER}}"
MAX_JOBS="${2:-${WATCHLIST_MAX_JOBS:-$DEFAULT_MAX_JOBS}}"
CLASSIFICATION_PROVIDER="${CLASSIFICATION_PROVIDER:-minimax}"
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"
REQUEST_SLEEP_SECONDS="${REQUEST_SLEEP_SECONDS:-0.2}"
YAHOO_BATCH_SIZE="${YAHOO_BATCH_SIZE:-100}"
YAHOO_BATCH_COOLDOWN="${YAHOO_BATCH_COOLDOWN:-5}"
SKIP_SUBCATEGORIES="${SKIP_SUBCATEGORIES:-1}"
UPDATE_TYPES="${UPDATE_TYPES:-etf,fund,other}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  : # explicit override from caller, use as-is
else
  resolved_python="$(command -v python 2>/dev/null || true)"
  PYTHON_BIN="${resolved_python:-$DEFAULT_PYTHON_BIN_FALLBACK}"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found or not executable: $PYTHON_BIN" >&2
  echo "Set PYTHON_BIN=/path/to/python and run again." >&2
  exit 1
fi

if [[ ! -d "$FOLDER" ]]; then
  echo "Folder not found: $FOLDER" >&2
  exit 1
fi

if ! [[ "$MAX_JOBS" =~ ^[0-9]+$ ]] || [[ "$MAX_JOBS" -lt 1 ]]; then
  echo "MAX_JOBS must be a positive integer, got: $MAX_JOBS" >&2
  exit 1
fi

FOLDER="$(cd "$FOLDER" && pwd)"
LOG_DIR="$FOLDER/_watchlist_logs"
TEMP_INPUT_DIR="$LOG_DIR/_inputs"
mkdir -p "$LOG_DIR" "$TEMP_INPUT_DIR"

running_jobs() {
  jobs -rp | wc -l | tr -d ' '
}

echo "Root        : $ROOT_DIR"
echo "Folder      : $FOLDER"
echo "Python      : $PYTHON_BIN"
echo "Max jobs    : $MAX_JOBS"
echo "Provider    : $CLASSIFICATION_PROVIDER"
echo "Req sleep   : $REQUEST_SLEEP_SECONDS"
echo "Yahoo batch : $YAHOO_BATCH_SIZE rows / ${YAHOO_BATCH_COOLDOWN}s cooldown"
echo "Skip subcat : $SKIP_SUBCATEGORIES"
echo "Update types: ${UPDATE_TYPES:-all}"
echo "Log folder  : $LOG_DIR"
echo

extra_args=()
if [[ "$SKIP_SUBCATEGORIES" == "1" ]]; then
  extra_args+=(--skip-subcategories)
fi

update_type_args=()
if [[ -n "$UPDATE_TYPES" ]]; then
  update_type_args+=(--update-types)
  IFS=',' read -ra _update_types <<< "$UPDATE_TYPES"
  for raw_type in "${_update_types[@]}"; do
    trimmed_type="${raw_type#"${raw_type%%[![:space:]]*}"}"
    trimmed_type="${trimmed_type%"${trimmed_type##*[![:space:]]}"}"
    [[ -n "$trimmed_type" ]] && update_type_args+=("$trimmed_type")
  done
fi

shopt -s nullglob
unsorted_csv_files=("$FOLDER"/*.csv)
shopt -u nullglob

if [[ "${#unsorted_csv_files[@]}" -eq 0 ]]; then
  echo "No CSV files found in: $FOLDER"
  exit 1
fi

mapfile -t csv_files < <(printf '%s\n' "${unsorted_csv_files[@]}" | sort)

started=0
job_pids=()
job_files=()
job_stdout=()
job_stderr=()

for csv_file in "${csv_files[@]}"; do
  base_name="$(basename "$csv_file")"
  case "$base_name" in
    *_top.csv)
      echo "Skipping generated top CSV: $csv_file"
      continue
      ;;
  esac

  while [[ "$(running_jobs)" -ge "$MAX_JOBS" ]]; do
    sleep "$SLEEP_SECONDS"
  done

  started=$((started + 1))
  name_no_ext="${base_name%.csv}"
  safe_name="$(printf '%s' "$name_no_ext" | sed 's/[^A-Za-z0-9._-]/_/g')"
  stdout_log="$LOG_DIR/$safe_name.out.log"
  stderr_log="$LOG_DIR/$safe_name.err.log"
  temp_input="$TEMP_INPUT_DIR/$safe_name.csv"
  output_xlsx="$FOLDER/$name_no_ext.xlsx"
  cp -f "$csv_file" "$temp_input"

  echo "[$started] Starting ETF update: $csv_file"
  (
    cd "$ROOT_DIR"
    "$PYTHON_BIN" csv_analyse_watchlist.py "$temp_input" "$output_xlsx" \
      --skip-isin-links \
      --classification-provider "$CLASSIFICATION_PROVIDER" \
      --sleep "$REQUEST_SLEEP_SECONDS" \
      --yahoo-batch-size "$YAHOO_BATCH_SIZE" \
      --yahoo-batch-cooldown "$YAHOO_BATCH_COOLDOWN" \
      "${extra_args[@]}" \
      "${update_type_args[@]}"
  ) >"$stdout_log" 2>"$stderr_log" &

  job_pids+=("$!")
  job_files+=("$csv_file")
  job_stdout+=("$stdout_log")
  job_stderr+=("$stderr_log")
done

echo
echo "Started $started ETF job(s). Waiting for completion..."

failed=0
for i in "${!job_pids[@]}"; do
  if wait "${job_pids[$i]}"; then
    :
  else
    failed=$((failed + 1))
    echo "FAILED: ${job_files[$i]}"
    echo "  stdout: ${job_stdout[$i]}"
    echo "  stderr: ${job_stderr[$i]}"
  fi
done

elapsed="$SECONDS"
printf 'Total runtime: %02d:%02d:%02d\n' $((elapsed / 3600)) $(((elapsed % 3600) / 60)) $((elapsed % 60))

if [[ "$failed" -gt 0 ]]; then
  echo "$failed ETF job(s) failed." >&2
  exit 1
fi

echo "All ETF jobs finished."