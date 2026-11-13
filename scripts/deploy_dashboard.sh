#!/usr/bin/env bash
# Polls origin for new commits touching the dashboard/api/agent-api image
# sources and, if found, rebuilds + restarts just the affected
# docker-compose services (each group is checked and deployed independently,
# so an agent-api-only change doesn't trigger a dashboard/api rebuild).
#
# Does NOT touch csv_update/ or run `git pull`/`git merge` — those CSVs are
# tracked in git and get rewritten locally by watchlist_scheduler.py, so a
# plain pull would conflict with in-progress local writes. Instead this only
# checks out the specific paths each image actually needs.
#
# Run manually or via cron:
#   */5 * * * * /path/to/financial_rag/scripts/deploy_dashboard.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BRANCH="${DEPLOY_BRANCH:-dev}"
STATE_FILE="$PROJECT_DIR/ops/.last_deployed_sha"
LOG_FILE="$PROJECT_DIR/ops/deploy.log"

DASHBOARD_PATHS=(server_app .streamlit requirements.server.txt Dockerfile.server)
AGENT_PATHS=(server_app rag ingestion processing config_loader.py requirements.txt requirements.agent.txt Dockerfile.agent)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

cd "$PROJECT_DIR"
mkdir -p "$(dirname "$STATE_FILE")"

git fetch origin "$BRANCH" --quiet
REMOTE_SHA="$(git rev-parse "origin/$BRANCH")"
LAST_SHA="$(cat "$STATE_FILE" 2>/dev/null || true)"

if [[ "$REMOTE_SHA" == "$LAST_SHA" ]]; then
    exit 0
fi

deploy_needed=0
dashboard_changed=1
agent_changed=1
if [[ -n "$LAST_SHA" ]]; then
    git diff --quiet "$LAST_SHA" "$REMOTE_SHA" -- "${DASHBOARD_PATHS[@]}" 2>/dev/null && dashboard_changed=0
    git diff --quiet "$LAST_SHA" "$REMOTE_SHA" -- "${AGENT_PATHS[@]}" 2>/dev/null && agent_changed=0
fi

# Never overwrite uncommitted work. The `git checkout origin/<branch> -- <paths>` below replaces
# modified files in those paths without asking, so if anything there is uncommitted (or an
# untracked file sits where origin has one), skip this run WITHOUT recording the new sha: the next
# run retries once the work is committed or stashed. Pushes that touch none of the deployed paths
# are not affected, and neither are files outside them (csv_update/ is rewritten all day).
# DEPLOY_FORCE=1 bypasses the check.
guarded_paths=()
if [[ "$dashboard_changed" == "1" ]]; then guarded_paths+=("${DASHBOARD_PATHS[@]}"); fi
if [[ "$agent_changed" == "1" ]]; then guarded_paths+=("${AGENT_PATHS[@]}"); fi
if [[ ${#guarded_paths[@]} -gt 0 && "${DEPLOY_FORCE:-0}" != "1" ]]; then
    at_risk="$(
        {
            git status --porcelain --untracked-files=no -- "${guarded_paths[@]}" | cut -c4-
            comm -12 <(git ls-files --others --exclude-standard -- "${guarded_paths[@]}" | sort) \
                     <(git ls-tree -r --name-only "origin/$BRANCH" -- "${guarded_paths[@]}" | sort)
        } | sort -u
    )"
    if [[ -n "$at_risk" ]]; then
        log "SKIPPED deploy of $REMOTE_SHA: uncommitted changes in deployed paths would be overwritten. Commit or stash them; the next run retries. Files: $(echo "$at_risk" | head -20 | tr '\n' ' ')"
        exit 0
    fi
fi

if [[ "$dashboard_changed" == "1" ]]; then
    log "Deploying dashboard/api: $LAST_SHA -> $REMOTE_SHA"
    git checkout "origin/$BRANCH" -- "${DASHBOARD_PATHS[@]}"
    docker compose build dashboard api >> "$LOG_FILE" 2>&1
    docker compose up -d dashboard api >> "$LOG_FILE" 2>&1
    deploy_needed=1
fi

if [[ "$agent_changed" == "1" ]]; then
    log "Deploying agent-api: $LAST_SHA -> $REMOTE_SHA"
    git checkout "origin/$BRANCH" -- "${AGENT_PATHS[@]}"
    docker compose build agent-api >> "$LOG_FILE" 2>&1
    docker compose up -d agent-api >> "$LOG_FILE" 2>&1
    deploy_needed=1
fi

if [[ "$deploy_needed" == "0" ]]; then
    log "New commits on $BRANCH ($LAST_SHA -> $REMOTE_SHA) but none touch deployed image sources; skipping rebuild"
fi

echo "$REMOTE_SHA" > "$STATE_FILE"
log "Deploy check finished ($REMOTE_SHA)"
