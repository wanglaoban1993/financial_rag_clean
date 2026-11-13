"""Standalone scheduler for periodic csv_update/active_trader refreshes.

Runs independently of main.py (which handles email/PDF/market ingestion).
Daily cycle:
  1. At 08:00, run the full enrichment (all fields) for equity rows, then
     for ETF+Fund rows — sequentially, since both write to the same files
     and must not run concurrently.
  2. Once both finish, refresh Bid/Ask (equity only) every 10 minutes,
     until 17:00.
  3. Wait for the next day's 08:00 and repeat.

In parallel (separate thread — different files, no conflict with the above):
  fetch_finanzen_topflop.py runs every 20 minutes from 10:00 to 17:00.
"""

import subprocess
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
EQUITY_DIR = PROJECT_ROOT / "csv_update" / "EQUITY"
ETF_DIR = PROJECT_ROOT / "csv_update" / "ETF"

FULL_UPDATE_SCRIPT = SCRIPTS_DIR / "run_watchlist_parallel.ps1"
BID_ASK_SCRIPT = SCRIPTS_DIR / "run_watchlist_bid_ask_parallel.ps1"
TOPFLOP_SCRIPT = SCRIPTS_DIR / "run_finanzen_topflop_update.ps1"

DAILY_START_TIME = dtime(8, 0)
BID_ASK_END_TIME = dtime(17, 0)
BID_ASK_INTERVAL_MINUTES = 10

TOPFLOP_START_TIME = dtime(10, 0)
TOPFLOP_END_TIME = dtime(17, 0)
TOPFLOP_INTERVAL_MINUTES = 20

FULL_UPDATE_MAX_JOBS = 8
BID_ASK_MAX_JOBS = 9


def _log(message: str):
    print(f"[watchlist_scheduler] {datetime.now():%Y-%m-%d %H:%M:%S} - {message}")


def sync_dashboard_db():
    """Re-import csv_update into the Docker Postgres/API stack (server_app
    dashboard) so it reflects whatever this script just wrote. Best-effort —
    if Docker isn't running, this logs a warning instead of crashing the
    whole scheduler, since the CSV refresh itself already succeeded."""
    cmd = ["docker", "compose", "run", "--rm", "api", "python", "-m", "server_app.import_watchlists"]
    _log("syncing dashboard DB (docker compose run --rm api python -m server_app.import_watchlists)")
    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"WARNING: dashboard DB sync failed to run ({exc}) — is Docker running?")
        return False
    if result.returncode != 0:
        _log(f"WARNING: dashboard DB sync exited with code {result.returncode}: {result.stderr.strip()[:500]}")
        return False
    last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    _log(f"dashboard DB sync done ({last_line})")
    return True


def _run_powershell(script: Path, folder: Path, max_jobs: int, update_types: list[str]) -> bool:
    # -UpdateTypes is a single comma-joined string, not separate argv tokens —
    # PowerShell's -File argument binding only grabs one bare token per named
    # array-ish parameter from an externally-invoked process; anything past
    # the first value silently spills over into the next unbound parameter.
    #
    # -PythonBin is pinned to sys.executable (the interpreter running this
    # script right now) rather than left to the .ps1's own "python" lookup —
    # a freshly spawned powershell.exe child doesn't reliably see whatever
    # made "python" resolve to this conda env in the calling session, and
    # silently falls back to a different install missing packages like
    # yfinance.
    cmd = [
        "powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Folder", str(folder),
        "-MaxJobs", str(max_jobs),
        "-PythonBin", sys.executable,
        "-UpdateTypes", ",".join(update_types),
    ]
    _log(f"running {script.name} ({', '.join(update_types)}, -MaxJobs {max_jobs})")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        _log(f"WARNING: {script.name} exited with code {result.returncode}")
        return False
    return True


def run_daily_full_updates():
    """Full enrichment (all fields), equity then ETF+Fund — sequential, different folders."""
    _run_powershell(FULL_UPDATE_SCRIPT, EQUITY_DIR, FULL_UPDATE_MAX_JOBS, ["equity"])
    _run_powershell(FULL_UPDATE_SCRIPT, ETF_DIR, FULL_UPDATE_MAX_JOBS, ["etf", "fund"])
    _log("daily full updates done")
    sync_dashboard_db()


def run_bid_ask_loop():
    """Bid/Ask-only refresh (equity), every 10 min, until BID_ASK_END_TIME."""
    _log(f"starting bid/ask loop (until {BID_ASK_END_TIME:%H:%M})")
    while datetime.now().time() < BID_ASK_END_TIME:
        _run_powershell(BID_ASK_SCRIPT, EQUITY_DIR, BID_ASK_MAX_JOBS, ["equity"])
        sync_dashboard_db()
        next_run = datetime.now() + timedelta(minutes=BID_ASK_INTERVAL_MINUTES)
        while datetime.now() < next_run and datetime.now().time() < BID_ASK_END_TIME:
            time.sleep(30)
    _log("bid/ask loop finished for today")


def run_topflop_update() -> bool:
    """fetch_finanzen_topflop.py via its PowerShell wrapper — writes into
    csv_update/TOP_LIST, a different folder than active_trader, so this can
    run independently of the watchlist full/bid-ask cycle above."""
    cmd = [
        "powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(TOPFLOP_SCRIPT),
        "-Python", sys.executable,
    ]
    _log(f"running {TOPFLOP_SCRIPT.name}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        _log(f"WARNING: {TOPFLOP_SCRIPT.name} exited with code {result.returncode}")
        return False
    return True


def run_topflop_loop():
    """Runs forever in its own thread: every hour, 08:00-17:00, every day."""
    while True:
        _sleep_until(TOPFLOP_START_TIME)
        _log(f"starting topflop hourly loop (until {TOPFLOP_END_TIME:%H:%M})")
        while datetime.now().time() < TOPFLOP_END_TIME:
            run_topflop_update()
            sync_dashboard_db()
            next_run = datetime.now() + timedelta(minutes=TOPFLOP_INTERVAL_MINUTES)
            while datetime.now() < next_run and datetime.now().time() < TOPFLOP_END_TIME:
                time.sleep(30)
        _log("topflop hourly loop finished for today")


def _sleep_until(run_time: dtime):
    now = datetime.now()
    target = datetime.combine(now.date(), run_time)
    if now >= target:
        target += timedelta(days=1)
    seconds = (target - now).total_seconds()
    _log(f"sleeping until {target:%Y-%m-%d %H:%M:%S} ({seconds / 3600:.1f}h)")
    time.sleep(seconds)


if __name__ == "__main__":
    if not EQUITY_DIR.is_dir():
        raise SystemExit(f"equity folder not found: {EQUITY_DIR}")
    if not ETF_DIR.is_dir():
        raise SystemExit(f"etf folder not found: {ETF_DIR}")

    threading.Thread(target=run_topflop_loop, daemon=True).start()

    while True:
        _sleep_until(DAILY_START_TIME)
        run_daily_full_updates()
        run_bid_ask_loop()
