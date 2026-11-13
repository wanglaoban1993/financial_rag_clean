"""Look up ISIN/WKN in the locally maintained csv_update watchlist.

The user periodically refreshes csv_update/EQUITY, csv_update/ETF and
csv_update/FONDS with exported broker CSVs (semicolon-separated, columns
include WKN, ISIN, Name, Bid, Ask, Date). This module is the first
lookup stop before falling back to a live market data API, so repeated
price queries for symbols the user already tracks don't burn API quota.
"""

import csv
from pathlib import Path
from typing import Optional

_ASSET_CLASS_FOLDERS = {
    "equity": "EQUITY",
    "etf": "ETF",
    "fond": "FONDS",
}

# Keyword search (search_watchlist_by_keyword) also covers active_trader —
# same semicolon-delimited schema as EQUITY/ETF/FONDS, just mixed instrument
# types per file (kept updated by watchlist_scheduler.py). Not included in
# _ASSET_CLASS_FOLDERS since lookup_by_isin_or_wkn needs a single class per
# folder, which active_trader doesn't have.
_KEYWORD_SEARCH_FOLDERS = {**_ASSET_CLASS_FOLDERS, "active_trader": "active_trader"}

# TOP_LIST uses a different (comma-delimited) schema entirely — no WKN/COMMENT
# columns — so it's searched separately, only the latest snapshot (the
# timestamped files under the same folder are historical duplicates).
_TOP_LIST_LATEST_FILE = "top_liste_latest.csv"

_CSV_UPDATE_DIR = Path(__file__).resolve().parent.parent / "csv_update"


def _iter_csv_rows(folder: Path, delimiter: str = ";"):
    for csv_path in sorted(folder.glob("*.csv")):
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f, delimiter=delimiter)
                for row in reader:
                    yield csv_path.name, row
        except OSError:
            continue


def lookup_by_isin_or_wkn(identifier: str, asset_class: str) -> Optional[dict]:
    """Search the local csv_update watchlist folder for a matching ISIN or WKN.

    asset_class must be one of "equity", "etf", "fond" — selects which
    csv_update subfolder to search. Returns the matching row as a dict
    (plus "_source_file"), or None if no local match was found.
    """
    folder_name = _ASSET_CLASS_FOLDERS.get(asset_class)
    if not folder_name:
        return None
    folder = _CSV_UPDATE_DIR / folder_name
    if not folder.is_dir():
        return None

    ident = (identifier or "").strip().upper()
    if not ident:
        return None

    for filename, row in _iter_csv_rows(folder):
        isin = (row.get("ISIN") or "").strip().upper()
        wkn = (row.get("WKN") or "").strip().upper()
        if ident != isin and ident != wkn:
            continue
        return {
            "isin": isin,
            "wkn": wkn,
            "name": (row.get("Name") or "").strip(),
            "bid": (row.get("Bid") or "").strip(),
            "ask": (row.get("Ask") or "").strip(),
            "change_1y": (row.get("% Differenz ein Jahr") or "").strip(),
            "change_1m": (row.get("% Differenz ein Monat") or "").strip(),
            "change_prev_day": (row.get("% Differenz zum Vortageskurs") or "").strip(),
            "_source_file": filename,
        }
    return None


def _search_top_list(kw: str, cap: int) -> list[dict]:
    folder = _CSV_UPDATE_DIR / "TOP_LIST"
    latest = folder / _TOP_LIST_LATEST_FILE
    if not latest.is_file():
        return []

    matches: list[dict] = []
    try:
        with open(latest, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=",")
            for row in reader:
                name = (row.get("Name") or "").strip()
                index = (row.get("Index") or "").strip()
                if kw not in name.lower() and kw not in index.lower():
                    continue
                matches.append({
                    "asset_class": "index_top_list",
                    "name": name,
                    "isin": (row.get("ISIN") or "").strip().upper(),
                    "wkn": "",
                    "sector": index,
                    "bid": "",
                    "ask": (row.get("Last") or "").strip(),
                    "_source_file": _TOP_LIST_LATEST_FILE,
                })
                if len(matches) >= cap:
                    break
    except OSError:
        return []
    return matches


def search_watchlist_by_keyword(keyword: str, limit: int = 10) -> list[dict]:
    """Keyword search across all csv_update watchlists (EQUITY/ETF/FONDS,
    active_trader, and the finanzen.net TOP_LIST snapshot).

    Matches the keyword (case-insensitive substring) against the instrument
    Name and the COMMENT sector/tag column (or Index, for TOP_LIST). Intended
    for "what do I have related to X" style questions — a plain substring
    match, not a semantic search, since Name/ISIN/WKN/sector tags are the
    stable fields here (Bid/Ask change on every refresh, so they're not
    embedded into the vector index).
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return []

    # Cap per group first, so an equity-heavy match (e.g. many single stocks
    # tagged "China") can't crowd out ETF/FONDS/TOP_LIST hits before they're
    # ever reached — each group gets a fair share of the overall limit.
    group_count = len(_KEYWORD_SEARCH_FOLDERS) + 1  # +1 for TOP_LIST
    per_group_cap = max(1, -(-limit // group_count))  # ceil division
    by_group: dict[str, list[dict]] = {}

    for asset_class, folder_name in _KEYWORD_SEARCH_FOLDERS.items():
        folder = _CSV_UPDATE_DIR / folder_name
        if not folder.is_dir():
            continue
        matches: list[dict] = []
        for filename, row in _iter_csv_rows(folder):
            name = (row.get("Name") or "").strip()
            comment = (row.get("COMMENT") or "").strip()
            if kw not in name.lower() and kw not in comment.lower():
                continue
            matches.append({
                "asset_class": asset_class,
                "name": name,
                "isin": (row.get("ISIN") or "").strip().upper(),
                "wkn": (row.get("WKN") or "").strip().upper(),
                "sector": comment,
                "bid": (row.get("Bid") or "").strip(),
                "ask": (row.get("Ask") or "").strip(),
                "_source_file": filename,
            })
            if len(matches) >= per_group_cap:
                break
        by_group[asset_class] = matches

    by_group["index_top_list"] = _search_top_list(kw, per_group_cap)

    results = [row for rows in by_group.values() for row in rows]
    return results[:limit]
