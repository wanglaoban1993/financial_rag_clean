#!/usr/bin/env python
"""One-time cleanup of Bid/Ask values that fit nothing the row knows.

2026-10-28 around 17:50, after the Xetra close, the onvista parser read digits of knock-out
certificate product codes as a price (Siemens Ask 74 at about 269, ASML 66 at 1468, Infineon 0).
The parser is fixed (docs/reference.md "Bid/Ask guards"), but the rows that were written keep their
value until onvista has a real quote again. This blanks them instead of leaving nonsense:

  a Bid or Ask that is no price next to REF_PREV_CLOSE (csv_analyse_watchlist._is_garbage_quote_side):
  <= 0, or 3x / a third of the previous close and a whole number or contradicting an other side
  that fits it -> that side and its time are emptied;
  a bad Ask also empties what was computed from it: "% Differenz zum Vortageskurs", DIFF_BASIS
    and, for stocks (not ETFs / rows whose COMMENT2 holds running costs), Kurspotential,
    Kurspotential % and COMMENT3.

Far from the previous close alone is NOT enough, and those rows are only listed, not changed:
the stored previous close is sometimes wrong (three US stocks had 0.366929), and a consistent
Bid/Ask pair is more likely a real quote than a product code.

The next --bid-ask-only run fills them from a real quote. EQUITY, ETF, BUYLIST and DEPOTS are
scanned; the Top list and the INDEX lists are rewritten by their own jobs. A row without
REF_PREV_CLOSE, REF_CURRENCY or DEV, or whose currency cannot be converted, is left alone.

Dry run by default. --apply backs every changed file up to csv_update/_cleanup_backup_<time>/
(the importer skips folders starting with "_"), takes the locks of the bid/ask cron jobs so
nothing writes the files meanwhile, and edits only the affected fields of the affected lines.
--import also imports the changed files into PostgreSQL; without it the next scheduled import
(every 15 minutes) does. The .xlsx copies next to the CSVs are not touched.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import csv_analyse_watchlist as watchlist  # noqa: E402

GROUPS = ("EQUITY", "ETF", "BUYLIST", "DEPOTS")
LOCK_PATHS = (
    "/tmp/run_bid_ask_equity_scheduled.lock",
    "/tmp/run_bid_ask_etf_scheduled.lock",
    "/tmp/run_shared_lists.lock",
)
LOCK_WAIT_SECONDS = 180

SIDE_COLUMNS = {"Bid": ("Bid", "Bid Time"), "Ask": ("Ask", "Ask Time")}
# Computed from the Ask. Kurspotential and COMMENT3 only for stocks: an ETF's COMMENT3 is not
# a Kurspotential text (see csv_analyse_watchlist._clear_kurspotential).
ASK_DERIVED = ("% Differenz zum Vortageskurs", "DIFF_BASIS")
ASK_DERIVED_STOCK = ("Kurspotential", "Kurspotential %", "COMMENT3")

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

Convert = Callable[[float, str, str], Optional[float]]


def parse_number(value: object) -> Optional[float]:
    """German "1.468,50" and the dot-decimal "542.84" both occur in these files."""
    text = str(value or "").strip()
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def judge_sides(row: dict[str, str], convert: Convert, normalize: Callable[[object], str]) -> tuple[dict, dict]:
    """(garbage, doubtful): {side: (value, reference)} for the Bid/Ask of `row` that are no price
    next to its previous close, and those that are only far from it."""
    reference = parse_number(row.get("REF_PREV_CLOSE"))
    reference_currency = normalize(row.get("REF_CURRENCY"))
    currency = normalize(row.get("DEV"))
    if reference is None or reference <= 0 or not reference_currency or not currency:
        return {}, {}

    raw, converted = {}, {}
    for side in SIDE_COLUMNS:
        value = parse_number(row.get(side))
        if value is None:
            continue
        raw[side] = value
        if value > 0:
            converted[side] = value if currency == reference_currency else convert(value, currency, reference_currency)

    garbage, doubtful = {}, {}
    for side, other_side in (("Bid", "Ask"), ("Ask", "Bid")):
        if side not in raw:
            continue
        price = converted.get(side)
        if raw[side] <= 0:
            garbage[side] = (raw[side], reference)
        elif price is not None and price > 0:
            if watchlist._is_garbage_quote_side(price, converted.get(other_side), reference):
                garbage[side] = (raw[side], reference)
            elif watchlist._is_far_from_reference(price, reference):
                doubtful[side] = (raw[side], reference)
    return garbage, doubtful


def clean_lines(
    lines: list[str], group: str, convert: Convert, normalize: Callable[[object], str]
) -> tuple[list[str], list[dict], list[dict]]:
    """(new lines, what changed, what was doubtful and left). Only affected fields of affected
    lines are touched."""
    if not lines:
        return lines, [], []
    header = lines[0].rstrip("\r\n").split(";")
    if "ISIN" not in header:
        return lines, [], []
    isin_at = header.index("ISIN")
    out, changes, doubts = list(lines), [], []
    for number, line in enumerate(lines[1:], start=1):
        ending = line[len(line.rstrip("\r\n")):]
        fields = line.rstrip("\r\n").split(";")
        if isin_at >= len(fields) or not _ISIN_RE.fullmatch(fields[isin_at].strip().upper()):
            continue  # the MNEMONIC marker line, blank lines
        row = {column: fields[i] for i, column in enumerate(header) if i < len(fields) and column}
        bad, doubtful = judge_sides(row, convert, normalize)
        for side, (value, reference) in doubtful.items():
            doubts.append({"isin": fields[isin_at], "name": row.get("Name", ""), "side": side, "value": value, "reference": reference})
        if not bad:
            continue

        to_clear = [column for side in bad for column in SIDE_COLUMNS[side]]
        if "Ask" in bad:
            to_clear += ASK_DERIVED
            comment2 = row.get("COMMENT2", "").strip()
            is_stock = group != "ETF" and not comment2.endswith("%") and comment2 != "-"
            if is_stock:
                to_clear += ASK_DERIVED_STOCK
        for column in to_clear:
            if column in header and header.index(column) < len(fields):
                fields[header.index(column)] = ""
        out[number] = ";".join(fields) + ending
        for side, (value, reference) in bad.items():
            changes.append({"isin": fields[isin_at], "name": row.get("Name", ""), "side": side, "value": value, "reference": reference})
    return out, changes, doubts


@contextlib.contextmanager
def cron_locks(paths=LOCK_PATHS, wait: float = LOCK_WAIT_SECONDS):
    """The locks the bid/ask cron jobs hold while they run, so none writes the files meanwhile.
    A cron job that finds its lock taken skips that run."""
    handles = []
    try:
        for path in paths:
            handle = open(path, "a")
            handles.append(handle)
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{path} still held after {wait}s; a bid/ask job is running, try again")
                    time.sleep(1)
        yield
    finally:
        for handle in handles:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def watchlist_files(root: Path) -> list[tuple[str, Path]]:
    found = []
    for group in GROUPS:
        folder = root / group
        if folder.is_dir():
            found += [(group, path) for path in sorted(folder.glob("*.csv")) if not path.name.endswith("_top.csv")]
    return found


def scan(root: Path, convert: Convert, normalize, read_lines) -> dict[Path, tuple[str, list[str], list[dict], list[dict]]]:
    result = {}
    for group, path in watchlist_files(root):
        new_lines, changes, doubts = clean_lines(read_lines(path), group, convert, normalize)
        if changes or doubts:
            result[path] = (group, new_lines, changes, doubts)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT / "csv_update")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: only list them)")
    parser.add_argument("--import", dest="import_pg", action="store_true", help="with --apply: import the changed files into PostgreSQL")
    args = parser.parse_args()

    from server_app.shared_lists import atomic_write, read_lines

    def convert(value: float, source: str, target: str) -> Optional[float]:
        return watchlist._convert_price_to_currency(value, source, target)

    def run() -> dict:
        return scan(args.root, convert, watchlist._normalize_price_currency, read_lines)

    if not args.apply:
        found = run()
        report(found)
        print("\nDry run: nothing written. Run again with --apply to clear the values listed as 'clear'.")
        return 0

    with cron_locks():
        found = run()  # read under the locks, so what is written is what was read
        report(found)
        changed = {path: item for path, item in found.items() if item[2]}
        if not changed:
            return 0
        backup = args.root / f"_cleanup_backup_{datetime.now():%Y%m%d_%H%M%S}"
        for path, (group, new_lines, _, _) in changed.items():
            target = backup / group / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            atomic_write(path, new_lines)
    print(f"\nCleared {sum(len(item[2]) for item in changed.values())} values in {len(changed)} files. Backup: {backup}")

    if args.import_pg:
        from fetch_finanzen_topflop import import_live_csv

        for path in changed:
            print(f"  imported {path.name}: {import_live_csv(path)} rows")
    else:
        print("The scheduled import (every 15 min) brings them to PostgreSQL; --import does it now.")
    return 0


def _line(change: dict) -> str:
    value, reference = change["value"], change["reference"]
    factor = "no price" if value <= 0 else f"x{max(value, reference) / min(value, reference):.1f}"
    return f"  {change['isin']}  {change['name'][:28]:28}  {change['side']:3} {value:>10g}  (previous close {reference:g}, {factor})"


def report(found: dict) -> None:
    cleared = doubtful = 0
    for path, (group, _, changes, doubts) in found.items():
        print(f"\n{group}/{path.name}")
        for change in changes:
            cleared += 1
            print("  clear" + _line(change)[2:])
        for change in doubts:
            doubtful += 1
            print("  left " + _line(change)[2:])
    print(f"\n{cleared} values to clear, {doubtful} far from the previous close but left alone, in {len(found)} files.")


if __name__ == "__main__":
    sys.exit(main())
