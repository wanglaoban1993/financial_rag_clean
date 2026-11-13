"""Refresh csv_update/BUYLIST and csv_update/DEPOTS without re-fetching ISINs
that another list already keeps up to date.

  * ISIN also in an EQUITY/ETF list -> its market fields are copied from there
    (those lists have their own bid/ask and full-enrichment schedules).
  * Any other ISIN -> fetched once through csv_analyse_watchlist.py, even when it
    sits in several BUYLIST/DEPOTS lists, and the result is copied to all of them.

    python refresh_shared_lists.py bid-ask   # quotes only (every few minutes)
    python refresh_shared_lists.py full      # full enrichment (twice a day)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from server_app.shared_lists import (
    SOURCE_GROUPS,
    apply_values,
    collect_pool,
    index_rows,
    iter_rows,
    shared_files,
)

PROJECT_ROOT = Path(__file__).resolve().parent
CSV_ROOT = Path(os.getenv("WATCHLIST_CSV_ROOT", PROJECT_ROOT / "csv_update"))


def fetch_pool(mode: str, header: str, marker: str, lines: list[str], sleep: str) -> dict[str, dict[str, str]]:
    """Run the batch pipeline once over the not-owned ISINs; ISIN -> refreshed row."""
    with tempfile.TemporaryDirectory(prefix="shared_pool_") as tmp:
        pool_csv = Path(tmp) / "pool.csv"
        out_xlsx = Path(tmp) / "out.xlsx"
        pool_csv.write_text(header + marker + "".join(lines), encoding="utf-8")

        cmd = [
            sys.executable, str(PROJECT_ROOT / "csv_analyse_watchlist.py"), str(pool_csv), str(out_xlsx),
            "--skip-isin-links", "--skip-subcategories", "--classification-provider", "none",
            "--sleep", sleep,
        ]
        if mode == "bid-ask":
            cmd.append("--bid-ask-only")
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
        out_csv = Path(tmp) / "out.csv"
        if result.returncode != 0 or not out_csv.exists():
            print(result.stdout[-2000:])
            print(result.stderr[-2000:], file=sys.stderr)
            raise SystemExit(f"pipeline failed (exit {result.returncode})")
        print(result.stdout[-600:].rstrip())

        return {isin: row for isin, row, _ in iter_rows(out_csv)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["bid-ask", "full"])
    parser.add_argument("--sleep", default=os.getenv("REQUEST_SLEEP_SECONDS", "0.6"),
                        help="seconds between remote calls (default 0.6)")
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied/fetched, change nothing")
    args = parser.parse_args()

    files = shared_files(CSV_ROOT)
    if not files:
        print("No BUYLIST/DEPOTS lists yet — nothing to do.")
        return 0

    owned = index_rows(CSV_ROOT, SOURCE_GROUPS)
    pool = collect_pool(files, set(owned))
    pool_count = len(pool[2]) if pool else 0
    print(f"{len(files)} list(s); {pool_count} ISIN(s) to fetch, the rest copied from {'/'.join(SOURCE_GROUPS)}")
    if args.dry_run:
        return 0

    values = dict(owned)
    if pool:
        values.update(fetch_pool(args.mode, *pool, args.sleep))

    changed = sum(apply_values(path, values) for path in files)
    print(f"Updated {changed} row(s) across {len(files)} list(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
