"""Refresh the shared analyst-target (TMP) cache.

Run at 10:00 and 16:30 (see README, "Target price (TMP) refresh"). Collects the
ISINs of every list that shows a target price (EQUITY watchlists, INDEX lists
and the Top list), looks each one up on Yahoo once and writes the result to
target_cache.py's file. It never touches the list CSVs, so it cannot collide
with the 6-minute bid/ask job or the 30-minute Top/INDEX job, which only read
the cache.

A failed lookup keeps the previous cache entry, so a Yahoo hiccup never blanks
a TMP; the entry just gets older until the next run.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

import target_cache
from csv_analyse_watchlist import fetch_analyst_data, resolve_equity_ticker

CSV_ROOT = Path(__file__).resolve().parent / "csv_update"
ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
# (folder, file pattern, delimiter). ETF lists are left out: they carry no target.
SOURCES = (
    ("EQUITY", "*.csv", ";"),
    ("INDEX", "*.csv", ","),
    ("TOP_LIST", "top_liste_latest.csv", ","),
)


def collect_isins(root: Path = CSV_ROOT) -> list[str]:
    isins: dict[str, None] = {}
    for folder, pattern, delimiter in SOURCES:
        for path in sorted((root / folder).glob(pattern)):
            if path.name.endswith("_top.csv"):
                continue
            with open(path, encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle, delimiter=delimiter):
                    # The cell can be a =HYPERLINK("url","ISIN") formula.
                    match = ISIN_RE.search(str(row.get("ISIN") or "").upper())
                    if match:
                        isins[match.group(0)] = None
    return list(isins)


def fetch_target(isin: str, previous_symbol: Optional[str], sleep: float) -> Optional[dict]:
    """Fresh cache entry for isin, or None when Yahoo gave nothing."""
    session = requests.Session()
    symbols = []
    if previous_symbol:
        symbols.append(previous_symbol)
    for attempt in range(2):
        if attempt == 1:
            # The stored symbol returned nothing (delisted, renamed): resolve again.
            resolved, _source, _hint = resolve_equity_ticker(isin, session)
            if not resolved or resolved in symbols:
                return None
            symbols.append(resolved)
        elif not symbols:
            continue
        data = fetch_analyst_data(symbols[-1])
        time.sleep(sleep)
        if data:
            return {
                "symbol": symbols[-1],
                "target_mean": data.get("target_mean"),
                "target_high": data.get("target_high"),
                "target_low": data.get("target_low"),
                "analysts": data.get("analysts"),
                "currency": data.get("currency"),
                "source": data.get("source"),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2, help="Parallel Yahoo lookups (separate processes)")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds each worker waits after a lookup")
    parser.add_argument("--limit", type=int, default=0, help="Only look up the first N ISINs (for testing)")
    args = parser.parse_args()

    isins = collect_isins()
    if args.limit:
        isins = isins[: args.limit]
    entries = target_cache.load()
    print(f"{len(isins)} ISINs to look up, {len(entries)} already cached")

    started = time.time()
    updated = failed = 0
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            isin: pool.submit(fetch_target, isin, (entries.get(isin) or {}).get("symbol"), args.sleep)
            for isin in isins
        }
        for position, (isin, future) in enumerate(futures.items(), start=1):
            try:
                entry = future.result()
            except Exception as exc:  # one bad ISIN must not stop the run
                print(f"  ! {isin}: {exc}")
                entry = None
            if entry:
                entries[isin] = entry
                updated += 1
            else:
                failed += 1
            if position % 200 == 0:
                target_cache.save(entries)
                print(f"  {position}/{len(isins)} done ({updated} updated, {failed} kept)")
    target_cache.save(entries)
    print(f"Done in {time.time() - started:.0f}s: {updated} updated, {failed} kept their previous entry")
    print(f"Cache: {target_cache.CACHE_PATH} ({len(entries)} entries)")


if __name__ == "__main__":
    main()
