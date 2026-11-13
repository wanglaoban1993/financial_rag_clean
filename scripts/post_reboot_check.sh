#!/usr/bin/env bash
# Verifies that everything the RAG stack + dashboard depends on came back up
# after a server reboot: docker containers, the local Ollama model service,
# the ingestion scheduler (financial-rag-main.service), and cron.
#
# Exit code is non-zero if anything is down, so this can also be wired into
# a monitoring check, not just run by hand.
#
# Usage:
#   scripts/post_reboot_check.sh          # check only
#   scripts/post_reboot_check.sh --fix    # also try to start whatever is down
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX="${1:-}"

PASS="\033[32mOK\033[0m"
FAIL="\033[31mFAIL\033[0m"
failures=0

check() {
    local label="$1" ok="$2"
    if [[ "$ok" == "0" ]]; then
        printf "  [%b] %s\n" "$PASS" "$label"
    else
        printf "  [%b] %s\n" "$FAIL" "$label"
        failures=$((failures + 1))
    fi
}

echo "== Docker engine =="
systemctl is-active --quiet docker.service
docker_ok=$?
check "docker.service active" "$docker_ok"
if [[ "$docker_ok" != "0" && "$FIX" == "--fix" ]]; then
    echo "    -> starting docker.service"
    sudo systemctl start docker.service
fi

echo
echo "== Docker compose containers =="
cd "$ROOT_DIR"
for svc in postgres api agent-api dashboard cloudflared; do
    state="$(docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -x "$svc")"
    if [[ "$svc" == "$state" ]]; then
        check "$svc" 0
    else
        check "$svc" 1
        if [[ "$FIX" == "--fix" ]]; then
            echo "    -> starting $svc"
            if [[ "$svc" == "cloudflared" ]]; then
                docker compose --profile tunnel up -d cloudflared
            else
                docker compose up -d "$svc"
            fi
        fi
    fi
done

echo
echo "== Ollama (local LLM, used by RAG routing/answering) =="
snap services ollama 2>/dev/null | awk 'NR==2{print $3}' | grep -qx active
ollama_svc_ok=$?
check "ollama.listener snap service active" "$ollama_svc_ok"
if [[ "$ollama_svc_ok" != "0" && "$FIX" == "--fix" ]]; then
    echo "    -> starting ollama snap service"
    sudo snap start ollama
fi

curl -s -m 3 http://localhost:11434/api/version >/dev/null 2>&1
check "ollama API responding on :11434" "$?"

echo
echo "== Ingestion scheduler (main.py) =="
systemctl is-active --quiet financial-rag-main.service
main_ok=$?
check "financial-rag-main.service active" "$main_ok"
if [[ "$main_ok" != "0" && "$FIX" == "--fix" ]]; then
    echo "    -> starting financial-rag-main.service"
    sudo systemctl start financial-rag-main.service
fi

echo
echo "== Cron (scheduled watchlist/topflop/deploy jobs) =="
systemctl is-active --quiet cron.service
cron_ok=$?
check "cron.service active" "$cron_ok"
if [[ "$cron_ok" != "0" && "$FIX" == "--fix" ]]; then
    echo "    -> starting cron.service"
    sudo systemctl start cron.service
fi

echo
echo "== Local endpoints =="
curl -sf -m 3 -o /dev/null http://127.0.0.1:8501
check "dashboard responding on :8501" "$?"
curl -sf -m 3 -o /dev/null http://127.0.0.1:8000/health
check "api responding on :8000/health" "$?"

echo
if [[ "$failures" -eq 0 ]]; then
    echo "All checks passed."
else
    echo "$failures check(s) failed."
    if [[ "$FIX" != "--fix" ]]; then
        echo "Re-run with --fix to attempt starting the failed services."
    fi
fi
exit "$failures"
