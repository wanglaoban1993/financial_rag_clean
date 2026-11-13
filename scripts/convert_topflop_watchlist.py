#!/usr/bin/env python3
"""Convert a fetch_finanzen_topflop.py output CSV (FetchedAt,Index,Rank,Name,
ISIN,...) into the ActiveTrader-style ISIN/Name/WKN CSV that
csv_analyse_watchlist.py's _read_watchlist_csv() requires. TOP_LIST rows have
no WKN, so that column is left blank; rows are deduplicated by ISIN since the
same instrument can appear under multiple index rankings in one snapshot.
"""
from __future__ import annotations

import csv
import sys


def convert(src_path: str, dst_path: str) -> int:
    seen: dict[str, str] = {}
    with open(src_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            isin = (row.get("ISIN") or "").strip()
            name = (row.get("Name") or "").strip()
            if isin and isin not in seen:
                seen[isin] = name

    with open(dst_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["ISIN", "Name", "WKN"])
        for isin, name in seen.items():
            writer.writerow([isin, name, ""])

    return len(seen)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: convert_topflop_watchlist.py <src.csv> <dst.csv>", file=sys.stderr)
        sys.exit(1)
    count = convert(sys.argv[1], sys.argv[2])
    print(f"Converted {count} unique ISIN row(s): {sys.argv[1]} -> {sys.argv[2]}")
