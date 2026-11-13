"""Adds a manually-entered instrument (EQUITY/ETF/BUYLIST/DEPOTS only — TOP_LIST is scraped
wholesale from finanzen.net and isn't meant to be hand-edited) to a watchlist.

Writes to the CSV first, then re-imports just that file into Postgres via
server_app.import_watchlists — the same path the scheduled cron uses — so the
new row survives the next `update_watchlists_to_pg.sh` resync, which fully
deletes and re-inserts app.watchlist_rows per source CSV.
"""

from __future__ import annotations

import csv
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from server_app.db import connect
from server_app.import_watchlists import compute_source_path, import_file, watchlist_name
from server_app.shared_lists import COPY_TARGETS, SCRAPED_GROUPS, atomic_write, read_lines

CSV_ROOT = Path(os.getenv("WATCHLIST_CSV_ROOT", "csv_update"))
SUPPORTED_ASSET_GROUPS = {"EQUITY", "ETF", "BUYLIST", "DEPOTS"}
CSV_ENCODING = "utf-8"  # confirmed via byte-inspection (UTF-8 euro-sign sequences), no BOM

MAX_WATCHLIST_NAME_LENGTH = 60
# Every EQUITY/ETF/BUYLIST/DEPOTS CSV starts with this header + a "MNEMONIC:WKN" marker line;
# used only when the group has no existing file to copy the two lines from.
_DEFAULT_HEADER = (
    "WKN;STOCK_ID;QUANTITY;COMMENT;COMMENT2;COMMENT3;ISIN;Name;Bid;Bid Time;Ask;Ask Time;"
    "% Differenz zum Vortageskurs;% Differenz ein Monat;% Differenz ein Jahr;"
    "Kurspotential;Kurspotential %;Time;Date;DEV;\n"
)
_DEFAULT_MNEMONIC_LINE = "MNEMONIC:WKN; ;;;;; ;;;;;;;;;;\n"
_FORBIDDEN_NAME_CHARS = set('/\\;:*?"<>|')


def find_watchlist_csv(asset_group: str, target_watchlist_name: str) -> Path | None:
    group_dir = CSV_ROOT / asset_group
    if not group_dir.is_dir():
        return None
    for path in sorted(group_dir.glob("*.csv")):
        if path.name.endswith("_top.csv"):
            continue
        if watchlist_name(path) == target_watchlist_name:
            return path
    return None


def _format_decimal(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}".replace(".", ",")


def _isin_column_index(header: list[str]) -> int | None:
    try:
        return header.index("ISIN")
    except ValueError:
        return None


def _read_lines(csv_path: Path) -> tuple[list[str], list[str]]:
    """(header fields, raw data lines incl. line ending) — lines are kept as
    exact text so rewriting the file after a removal doesn't reformat rows
    it isn't touching."""
    with csv_path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        lines = handle.readlines()
    header = lines[0].rstrip("\r\n").split(";") if lines else []
    return header, lines[1:]


def isin_exists(csv_path: Path, isin: str) -> bool:
    header, data_lines = _read_lines(csv_path)
    isin_index = _isin_column_index(header)
    if isin_index is None:
        return False
    for line in data_lines:
        fields = line.rstrip("\r\n").split(";")
        if len(fields) > isin_index and fields[isin_index].strip().upper() == isin:
            return True
    return False


def _append_row(
    csv_path: Path,
    *,
    isin: str,
    wkn: str,
    comment: str,
    name: str,
    bid: float | None,
    ask: float | None,
    currency: str,
    quote_time: str,
    return_1y: float | None,
    return_1m: float | None,
    return_1d: float | None,
    laufende_kosten: str,
    quantity: str,
) -> None:
    with csv_path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        header = handle.readline().rstrip("\r\n").split(";")

    values = {
        "WKN": wkn,
        "COMMENT": comment,
        # QUANTITY: despite the name, this column holds a 3-character MACD+
        # Stochastic TA signal code (e.g. "101"), not a share count — see
        # server_app/yahoo_lookup.py's _compute_ta_quantity(). Best-effort:
        # if that live lookup failed, this is "" and the next scheduled full
        # enrichment (csv_analyse_watchlist.py's _apply_ta_signal) fills it.
        "QUANTITY": quantity,
        # COMMENT2 is displayed as "Laufende Kosten" for ETF and as the
        # analyst-target-price note for EQUITY (server_app/dashboard.py's
        # per-asset-group column rename) — laufende_kosten is only ever
        # non-empty for an ETF/fund lookup, so this is safe for both.
        "COMMENT2": laufende_kosten,
        "ISIN": isin,
        "Name": name,
        "Bid": _format_decimal(bid),
        "Bid Time": quote_time if bid is not None else "",
        "Ask": _format_decimal(ask),
        "Ask Time": quote_time if ask is not None else "",
        "DEV": currency,
        "% Differenz ein Jahr": _format_decimal(return_1y),
        "% Differenz ein Monat": _format_decimal(return_1m),
        "% Differenz zum Vortageskurs": _format_decimal(return_1d),
    }
    row = [values.get(column, "") for column in header]

    with csv_path.open("a", encoding=CSV_ENCODING, newline="\n") as handle:
        handle.write(";".join(row) + "\n")


def add_instrument(
    asset_group: str,
    target_watchlist_name: str,
    *,
    isin: str,
    wkn: str,
    comment: str,
    name: str,
    bid: float | None,
    ask: float | None,
    currency: str,
    return_1y: float | None = None,
    return_1m: float | None = None,
    return_1d: float | None = None,
    laufende_kosten: str = "",
    quantity: str = "",
) -> dict[str, Any]:
    asset_group = asset_group.strip().upper()
    if asset_group not in SUPPORTED_ASSET_GROUPS:
        return {"error": "unsupported_asset_group"}

    isin = isin.strip().upper()
    if not isin:
        return {"error": "missing_isin"}

    csv_path = find_watchlist_csv(asset_group, target_watchlist_name)
    if csv_path is None:
        return {"error": "watchlist_not_found"}
    if isin_exists(csv_path, isin):
        return {"error": "isin_already_present"}

    _append_row(
        csv_path,
        isin=isin,
        wkn=wkn,
        comment=comment,
        name=name,
        bid=bid,
        ask=ask,
        currency=currency,
        quote_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        return_1y=return_1y,
        return_1m=return_1m,
        return_1d=return_1d,
        laufende_kosten=laufende_kosten,
        quantity=quantity,
    )
    import_file(csv_path, CSV_ROOT)
    return {}


def _match_isin_rows(header: list[str], data_lines: list[str], isin: str) -> list[int]:
    isin_index = _isin_column_index(header)
    if isin_index is None:
        return []
    return [
        i for i, line in enumerate(data_lines)
        if len(fields := line.rstrip("\r\n").split(";")) > isin_index
        and fields[isin_index].strip().upper() == isin
    ]


def remove_instrument(asset_group: str, target_watchlist_name: str, *, isin: str) -> dict[str, Any]:
    asset_group = asset_group.strip().upper()
    if asset_group not in SUPPORTED_ASSET_GROUPS:
        return {"error": "unsupported_asset_group"}

    isin = isin.strip().upper()
    if not isin:
        return {"error": "missing_isin"}

    csv_path = find_watchlist_csv(asset_group, target_watchlist_name)
    if csv_path is None:
        return {"error": "watchlist_not_found"}

    header, data_lines = _read_lines(csv_path)
    matches = _match_isin_rows(header, data_lines, isin)
    if not matches:
        return {"error": "isin_not_found"}
    if len(matches) > 1:
        # Refuse rather than guess which physical row to drop — safer for a
        # real holdings file than silently deleting every matching line.
        return {"error": "ambiguous_isin"}

    remaining = [line for i, line in enumerate(data_lines) if i != matches[0]]
    header_line = ";".join(header) + "\n"
    with csv_path.open("w", encoding=CSV_ENCODING, newline="") as handle:
        handle.write(header_line)
        handle.writelines(remaining)

    import_file(csv_path, CSV_ROOT)
    return {}


# Columns of the scraped lists (TOP_LIST, INDEX) under the name the other lists give the same value.
SCRAPED_COLUMNS = {
    "TMP": "COMMENT2",
    "Pot": "Kurspotential",
    "Pot %": "Kurspotential %",
    "Diff Basis": "DIFF_BASIS",
}


SCRAPED_MARKET_COLUMNS = (
    "Bid", "Ask", "% Differenz zum Vortageskurs", "% Differenz ein Monat", "% Differenz ein Jahr", "Pot", "Pot %", "TMP",
)


def _scraped_row(csv_path: Path, isin: str) -> dict[str, str] | None:
    """The most complete row with this ISIN from a TOP_LIST or INDEX file, keyed by the other
    lists' column names. Those files are comma separated (TOP_LIST with a BOM), unlike the
    semicolon lists. In the Top list a stock in several indices has one row per index and the
    rows differ: after the Xetra close the DAX row of
    Deutsche Bank had no Bid/Ask/day change while its EURO STOXX 50 row had them. So the row
    with the most filled-in market values wins, the later Ask Time on a tie (an INDEX file has one row per stock). A ';' would shift
    the target's fields, so it becomes ','."""
    candidates = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            if (record.get("ISIN") or "").strip().upper() == isin:
                candidates.append(record)
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda record: (
            sum(bool((record.get(column) or "").strip()) for column in SCRAPED_MARKET_COLUMNS),
            record.get("Ask Time") or "",
        ),
    )
    return {
        SCRAPED_COLUMNS.get(column, column): (value or "").replace(";", ",").replace("\n", " ").replace("\r", " ")
        for column, value in best.items()
        if column
    }


def copy_instrument(
    source_group: str,
    source_watchlist_name: str,
    target_group: str,
    target_watchlist_name: str,
    *,
    isin: str,
    wkn: str = "",
) -> dict[str, Any]:
    """Copy one row to another list; the source keeps its row. `COPY_TARGETS` says which groups
    may go where. The row is carried over as it is, per-list COMMENT and the TMP/Laufende Kosten value in
    COMMENT2 included, matched to the target's columns by name (the BUYLIST header, for one, has
    no REF_* columns). From TOP_LIST or INDEX the row has no WKN, so `wkn` (looked up by the caller) fills it."""
    source_group = source_group.strip().upper()
    target_group = target_group.strip().upper()
    if target_group not in COPY_TARGETS.get(source_group, ()):
        return {"error": "copy_not_allowed"}

    isin = isin.strip().upper()
    if not isin:
        return {"error": "missing_isin"}

    source_path = find_watchlist_csv(source_group, source_watchlist_name)
    if source_path is None:
        return {"error": "watchlist_not_found"}
    target_path = find_watchlist_csv(target_group, target_watchlist_name)
    if target_path is None:
        return {"error": "target_watchlist_not_found"}
    if source_path == target_path:
        return {"error": "same_watchlist"}

    if source_group in SCRAPED_GROUPS:
        row = _scraped_row(source_path, isin)
        if row is None:
            return {"error": "isin_not_found"}
        row["WKN"] = wkn.strip()
        target_header, target_data = _read_lines(target_path)
        if _match_isin_rows(target_header, target_data, isin):
            return {"error": "isin_already_present"}
    else:
        source_header, source_data = _read_lines(source_path)
        matches = _match_isin_rows(source_header, source_data, isin)
        if not matches:
            return {"error": "isin_not_found"}
        if len(matches) > 1:
            return {"error": "ambiguous_isin"}
        target_header, target_data = _read_lines(target_path)
        if _match_isin_rows(target_header, target_data, isin):
            return {"error": "isin_already_present"}

        fields = source_data[matches[0]].rstrip("\r\n").split(";")
        row = dict(zip(source_header, fields + [""] * (len(source_header) - len(fields))))
    new_line = ";".join(row.get(column, "") if column else "" for column in target_header) + "\n"

    target_lines = read_lines(target_path)
    if target_lines and not target_lines[-1].endswith("\n"):
        target_lines[-1] += "\n"
    atomic_write(target_path, target_lines + [new_line])
    import_file(target_path, CSV_ROOT)
    return {}


def update_comment(asset_group: str, target_watchlist_name: str, *, isin: str, comment: str) -> dict[str, Any]:
    asset_group = asset_group.strip().upper()
    if asset_group not in SUPPORTED_ASSET_GROUPS:
        return {"error": "unsupported_asset_group"}

    isin = isin.strip().upper()
    if not isin:
        return {"error": "missing_isin"}

    csv_path = find_watchlist_csv(asset_group, target_watchlist_name)
    if csv_path is None:
        return {"error": "watchlist_not_found"}

    header, data_lines = _read_lines(csv_path)
    try:
        comment_index = header.index("COMMENT")
    except ValueError:
        return {"error": "isin_not_found"}

    matches = _match_isin_rows(header, data_lines, isin)
    if not matches:
        return {"error": "isin_not_found"}
    if len(matches) > 1:
        return {"error": "ambiguous_isin"}

    # No quoting in this file format — a literal ";" would shift every field
    # after it out of alignment, so it can't be allowed through verbatim.
    safe_comment = comment.replace(";", ",").replace("\n", " ").replace("\r", " ")

    idx = matches[0]
    fields = data_lines[idx].rstrip("\r\n").split(";")
    while len(fields) <= comment_index:
        fields.append("")
    fields[comment_index] = safe_comment
    data_lines[idx] = ";".join(fields) + "\n"

    header_line = ";".join(header) + "\n"
    with csv_path.open("w", encoding=CSV_ENCODING, newline="") as handle:
        handle.write(header_line)
        handle.writelines(data_lines)

    import_file(csv_path, CSV_ROOT)
    return {}


def validate_watchlist_name(raw_name: str) -> tuple[str, str | None]:
    """(cleaned name, error code or None). The name becomes the CSV filename and
    the dashboard label, so it must be a safe single path component that the
    importer and the cron scripts will actually pick up."""
    name = " ".join(raw_name.split())
    if not name:
        return name, "missing_name"
    if len(name) > MAX_WATCHLIST_NAME_LENGTH:
        return name, "name_too_long"
    if any(ch in _FORBIDDEN_NAME_CHARS or ord(ch) < 32 for ch in name):
        return name, "invalid_name"
    # Leading "_" / "." are skipped by the importer (underscore-prefixed paths are
    # treated as internal working dirs); "_top"/"_merged" suffixes are reserved
    # for generated files.
    if name.startswith(("_", ".")) or name.lower().endswith(("_top", "_merged")):
        return name, "invalid_name"
    return name, None


def create_watchlist(asset_group: str, name: str) -> dict[str, Any]:
    """Create an empty watchlist CSV (header only) and register it in
    Postgres. The scheduled enrichment / bid-ask crons glob the group folder for
    *.csv, so the new list is picked up there without further wiring."""
    asset_group = asset_group.strip().upper()
    if asset_group not in SUPPORTED_ASSET_GROUPS:
        return {"error": "unsupported_asset_group"}

    name, error = validate_watchlist_name(name)
    if error:
        return {"error": error}

    group_dir = CSV_ROOT / asset_group
    if not group_dir.is_dir():
        return {"error": "watchlist_not_found"}

    existing = sorted(p for p in group_dir.glob("*.csv") if not p.name.endswith("_top.csv"))
    if any(watchlist_name(p).casefold() == name.casefold() for p in existing):
        return {"error": "watchlist_already_exists"}

    header_line, mnemonic_line = _DEFAULT_HEADER, _DEFAULT_MNEMONIC_LINE
    if existing:
        with existing[0].open("r", encoding=CSV_ENCODING, newline="") as handle:
            first, second = handle.readline(), handle.readline()
        if first.strip() and second.strip():
            header_line, mnemonic_line = first.rstrip("\r\n") + "\n", second.rstrip("\r\n") + "\n"

    csv_path = group_dir / f"{name}.csv"
    try:
        # "x" fails instead of overwriting if a file appeared since the check above.
        with csv_path.open("x", encoding=CSV_ENCODING, newline="\n") as handle:
            handle.write(header_line + mnemonic_line)
    except FileExistsError:
        return {"error": "watchlist_already_exists"}

    import_file(csv_path, CSV_ROOT)
    return {"watchlist_name": name, "asset_group": asset_group}


TRASH_DIR_NAME = "_deleted"  # underscore-prefixed: the importer and the cron jobs skip it


def _unregister_watchlist(csv_path: Path) -> None:
    """Drop the list (and, by cascade, its rows) from Postgres."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.watchlist_imports WHERE source_path = %s",
                (compute_source_path(csv_path, CSV_ROOT),),
            )


def delete_watchlist(asset_group: str, target_watchlist_name: str) -> dict[str, Any]:
    """Remove a whole watchlist. The CSV (and the XLSX the batch jobs keep next
    to it) is moved to csv_update/_deleted/<group>/ with a timestamp rather than
    erased, so a list deleted by mistake can be put back by moving it out again
    and re-importing. The cron jobs glob the group folder for *.csv, so a
    deleted list is no longer refreshed."""
    asset_group = asset_group.strip().upper()
    if asset_group not in SUPPORTED_ASSET_GROUPS:
        return {"error": "unsupported_asset_group"}

    csv_path = find_watchlist_csv(asset_group, target_watchlist_name)
    if csv_path is None:
        return {"error": "watchlist_not_found"}

    trash_dir = CSV_ROOT / TRASH_DIR_NAME / asset_group
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for source in (csv_path, csv_path.with_suffix(".xlsx")):
        if source.exists():
            shutil.move(str(source), str(trash_dir / f"{source.stem}_{stamp}{source.suffix}"))

    _unregister_watchlist(csv_path)
    return {"watchlist_name": target_watchlist_name, "asset_group": asset_group}
