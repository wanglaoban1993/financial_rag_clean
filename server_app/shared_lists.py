"""Helpers for the BUYLIST / DEPOTS tabs.

Those two tabs hold a mix of equities and ETFs, and the same ISIN usually also
sits in an EQUITY or ETF list that already gets refreshed on its own schedule.
Rather than fetching such an ISIN a second time, its market fields are copied
from the list that owns it (see refresh_shared_lists.py); only ISINs that no
EQUITY/ETF list carries are fetched. Plain-stdlib on purpose: the api container
imports this too, and it has none of the batch pipeline's dependencies.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

SHARED_GROUPS = ("BUYLIST", "DEPOTS")
SOURCE_GROUPS = ("EQUITY", "ETF")

# Where an instrument may be copied to, by the group it sits in now. An equity stays among
# equities and an ETF among ETFs (COMMENT2 and the Potential columns mean different things
# there); both may go into BUYLIST/DEPOTS, which hold either; those copy among themselves, but
# nothing goes back to EQUITY/ETF. TOP_LIST and INDEX are copy sources only (their stocks may
# go to an EQUITY list, BUYLIST or DEPOTS); the scrapers rewrite them, so nothing goes in.
SCRAPED_GROUPS = ("TOP_LIST", "INDEX")
COPY_TARGETS = {
    "TOP_LIST": ("EQUITY", "BUYLIST", "DEPOTS"),
    "INDEX": ("EQUITY", "BUYLIST", "DEPOTS"),
    "EQUITY": ("EQUITY", "BUYLIST", "DEPOTS"),
    "ETF": ("ETF", "BUYLIST", "DEPOTS"),
    "BUYLIST": ("BUYLIST", "DEPOTS"),
    "DEPOTS": ("BUYLIST", "DEPOTS"),
}

CSV_ENCODING = "utf-8"
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

# Columns whose value belongs to the instrument, not to the list, so they follow
# the owning row. COMMENT is deliberately absent: it is the per-list tag/comment.
COPY_COLUMNS = (
    "QUANTITY", "COMMENT2", "COMMENT3", "Name", "Bid", "Bid Time", "Ask", "Ask Time",
    "% Differenz zum Vortageskurs", "% Differenz ein Monat", "% Differenz ein Jahr",
    "Kurspotential", "Kurspotential %", "DEV", "REF_PREV_CLOSE", "REF_CURRENCY", "DIFF_BASIS",
)
# Only filled in when the shared row has none (a user may have typed their own).
FILL_IF_BLANK_COLUMNS = ("WKN", "STOCK_ID")


def watchlist_csvs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.csv") if not p.name.endswith("_top.csv"))


def read_lines(csv_path: Path) -> list[str]:
    with csv_path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        return handle.readlines()


def _fields(line: str, width: int) -> list[str]:
    fields = line.rstrip("\r\n").split(";")
    return fields + [""] * (width - len(fields))


def _header(lines: list[str]) -> list[str]:
    return lines[0].rstrip("\r\n").split(";") if lines else []


def _isin_of(fields: list[str], isin_col: int | None) -> str:
    if isin_col is None or isin_col >= len(fields):
        return ""
    value = fields[isin_col].strip().upper()
    return value if _ISIN_RE.fullmatch(value) else ""


def iter_rows(csv_path: Path):
    """(ISIN, row dict, raw line) for every data row that has a valid ISIN."""
    lines = read_lines(csv_path)
    header = _header(lines)
    if "ISIN" not in header:
        return
    isin_col = header.index("ISIN")
    for line in lines[1:]:
        fields = _fields(line, len(header))
        isin = _isin_of(fields, isin_col)
        if isin:
            yield isin, dict(zip(header, fields)), line


def has_quote(row: dict[str, str]) -> bool:
    return bool(row.get("Bid", "").strip() or row.get("Ask", "").strip())


def index_rows(csv_root: Path, groups: tuple[str, ...] = SOURCE_GROUPS) -> dict[str, dict[str, str]]:
    """ISIN -> row, over every list of `groups`, for rows that carry a quote.
    When several lists hold the ISIN the most recently quoted one wins."""
    found: dict[str, dict[str, str]] = {}
    for group in groups:
        for path in watchlist_csvs(csv_root / group):
            for isin, row, _ in iter_rows(path):
                if not has_quote(row):
                    continue
                known = found.get(isin)
                if known is None or row.get("Bid Time", "") > known.get("Bid Time", ""):
                    found[isin] = row
    return found


def find_in_sources(csv_root: Path, isin: str) -> dict[str, str] | None:
    return index_rows(csv_root).get(isin.strip().upper())


def shared_files(csv_root: Path) -> list[Path]:
    return [p for group in SHARED_GROUPS for p in watchlist_csvs(csv_root / group)]


def collect_pool(
    files: list[Path], owned: set[str]
) -> tuple[str, str, list[str]] | None:
    """The rows still needing a fetch: one raw line per ISIN that is neither in
    `owned` nor already collected from another shared list. Returns
    (header line, marker line, raw lines), or None when there is nothing to fetch."""
    header_line = marker_line = ""
    pool: dict[str, tuple[bool, str]] = {}
    for path in files:
        lines = read_lines(path)
        if len(lines) < 2:
            continue
        if not header_line:
            header_line, marker_line = lines[0].rstrip("\r\n") + "\n", lines[1].rstrip("\r\n") + "\n"
        for isin, row, line in iter_rows(path):
            if isin in owned:
                continue
            quoted = has_quote(row)
            if isin not in pool or (quoted and not pool[isin][0]):
                pool[isin] = (quoted, line.rstrip("\r\n") + "\n")
    if not pool:
        return None
    return header_line, marker_line, [line for _, line in pool.values()]


def apply_values(csv_path: Path, values_by_isin: dict[str, dict[str, str]]) -> int:
    """Copy the instrument-level columns of `values_by_isin` into the matching
    rows of `csv_path`, rewriting the file only if something changed. Returns
    the number of rows changed. Every other line is written back byte for byte."""
    lines = read_lines(csv_path)
    header = _header(lines)
    if "ISIN" not in header:
        return 0
    isin_col = header.index("ISIN")
    copy_cols = [c for c in COPY_COLUMNS if c in header]
    fill_cols = [c for c in FILL_IF_BLANK_COLUMNS if c in header]

    changed = 0
    for i, line in enumerate(lines[1:], start=1):
        fields = _fields(line, len(header))
        source = values_by_isin.get(_isin_of(fields, isin_col))
        if source is None:
            continue
        before = list(fields)
        for col in copy_cols:
            fields[header.index(col)] = source.get(col, "")
        for col in fill_cols:
            if not fields[header.index(col)].strip() and source.get(col, "").strip():
                fields[header.index(col)] = source[col]
        if fields != before:
            changed += 1
            lines[i] = ";".join(fields) + "\n"

    if changed:
        atomic_write(csv_path, lines)
    return changed


def atomic_write(path: Path, lines: list[str]) -> None:
    # Dashboard edits and the scheduled importer read these files too, so never
    # leave a half-written one behind.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=CSV_ENCODING, newline="") as handle:
            handle.writelines(lines)
        shutil.copymode(path, tmp_name)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
