#!/usr/bin/env bash
# Imports every csv_update/**/*.csv (EQUITY, ETF, TOP_LIST) into PostgreSQL —
# the same tables server_app/api.py and the dashboard read from.
# Run manually or via cron: crontab -e → 0 8 * * * /path/to/update_watchlists_to_pg.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/csv_update/_cron_logs"
LOG_FILE="$LOG_DIR/pg_import_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting csv_update → PostgreSQL import"

source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda activate rag

# DB connection comes from the project's .env (same file docker-compose and
# every other server_app entrypoint use) via server_app/settings.py's
# load_dotenv — no credentials are hardcoded here.
cd "$PROJECT_DIR"
python -m server_app.import_watchlists --root csv_update

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Import complete"
