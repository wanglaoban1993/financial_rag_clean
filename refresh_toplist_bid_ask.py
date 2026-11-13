"""Refresh only Bid / Ask of the Top list (csv_update/TOP_LIST/top_liste_latest.csv).

fetch_finanzen_topflop.py rebuilds the whole list (rank, changes, history, targets) and takes
about ten minutes. This script leaves all of that alone and only re-reads the onvista quote of
every ISIN already in the file, so it can run more often than the full fetch.

Written: Bid, Bid Time, Ask, Ask Time (and DEV when the row has none), plus what depends on the
Ask: Pot / Pot % (from the row's TMP) and % Differenz zum Vortageskurs with its Diff Basis and
Diff Flag (from the previous close the full run left in prev_close_cache.py). Everything else,
Rank, Monat, Jahr, TMP, Source %, stays as the last full run left it. Until the full run has
written today's previous close, the day change is left as it is.

A row keeps its current values when onvista gives no two-sided quote, when the quote cannot be
converted to the row's DEV, when it jumps implausibly against the current Ask, or when the
row already holds a newer quote. If onvista pauses us (HTTP 429 / 403), the run stops at once
and writes what it has; the pause is shared with every other job (see onvista_http.py).

Does not overlap itself (own lock). Not safe against a full fetch writing the same file in the
same instant, so the file is re-read right before the write and only the cells of this script
change; a full run that finishes later rewrites its own rows with its own (older) quotes.
"""

import argparse
import fcntl
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

import onvista_http
import prev_close_cache
import quote_cache
from csv_analyse_watchlist import (
    DISPLAY_CURRENCIES,
    _convert_price_to_currency,
    _convert_price_to_eur_at_date,
    _normalize_price_currency,
    fetch_onvista_fields,
)
from fetch_finanzen_topflop import (
    DIFF_FLAG_COLUMN,
    DIFF_FLAG_PP,
    DIFF_BASIS_COLUMN,
    SOURCE_CHANGE_COLUMN,
    format_german_decimal,
    import_live_csv,
    is_crossed,
    make_session,
    to_float,
    write_csv_atomic,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = SCRIPT_DIR / "csv_update" / "TOP_LIST" / "top_liste_latest.csv"
LOCK_PATH = os.environ.get("TOPLIST_BID_ASK_LOCK") or "/tmp/toplist_bid_ask.lock"
QUOTE_COLUMNS = ["Bid", "Bid Time", "Ask", "Ask Time", "DEV"]
POT_COLUMNS = ["Pot", "Pot %"]
DAY_CHANGE_COLUMN = "% Differenz zum Vortageskurs"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# A new Ask more than this many times above / below the current one is taken for a scraping
# slip (or a different listing) and ignored; the full fetch's own check is only the spread.
MAX_QUOTE_JUMP_RATIO = 3.0


@dataclass
class Quote:
    bid: float
    ask: float
    currency: str
    fetched_at: str


def parse_time(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value or "").strip(), TIME_FORMAT)
    except ValueError:
        return None


def target_currency_for(row_dev: object, quote_currency: str) -> str:
    """The currency Bid/Ask are written in: the row's DEV (TMP is expressed in it), else the
    quote's own if it is USD/EUR, else EUR — the rule normalize_display_currency applies."""
    current = _normalize_price_currency(row_dev)
    if current:
        return current
    return quote_currency if quote_currency in DISPLAY_CURRENCIES else "EUR"


def convert_quote(quote: Quote, target_currency: str) -> tuple[float, float] | None:
    """(bid, ask) in target_currency, or None when the conversion has no rate."""
    quote_currency = _normalize_price_currency(quote.currency)
    if not quote_currency or quote_currency == target_currency:
        return quote.bid, quote.ask
    values = []
    for price in (quote.bid, quote.ask):
        converted = _convert_price_to_currency(price, quote_currency, target_currency)
        if converted is None:
            return None
        values.append(round(converted, 2))
    return values[0], values[1]


def is_implausible_jump(old_ask: object, new_ask: float) -> bool:
    old = to_float(old_ask)
    if old is None or old <= 0 or new_ask <= 0:
        return False
    return max(old, new_ask) / min(old, new_ask) > MAX_QUOTE_JUMP_RATIO


def potential(tmp: object, ask: float) -> tuple[float, float] | None:
    """(Pot, Pot %) as fetch_finanzen_topflop.add_potential_pct computes them: target minus
    Ask, and target over Ask minus one in percent. TMP in the file is already in the row's
    DEV, the currency the new Ask is in, so no conversion is needed."""
    target = to_float(tmp)
    if target is None or target <= 0 or ask <= 0:
        return None
    return round(target - ask, 2), round((target / ask - 1.0) * 100.0, 2)


def day_change(ask: float, currency: str, ref_eur: float) -> float | None:
    """The full run's formula: the Ask in EUR (today's rate) against the previous close in EUR."""
    ask_eur = _convert_price_to_eur_at_date(ask, currency, datetime.now())
    if ask_eur is None or ask_eur <= 0 or ref_eur <= 0:
        return None
    return (ask_eur / ref_eur - 1.0) * 100.0


def diff_flag(mine: float, source: object) -> str:
    """add_diff_flag for one row: '!' when the day change is further than DIFF_FLAG_PP points from
    the source's own, nothing when the source has no figure."""
    theirs = to_float(source)
    if theirs is None:
        return ""
    return "!" if abs(mine - theirs) > DIFF_FLAG_PP else ""


def read_top_list(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    for column in ("ISIN", "Name"):
        if column not in frame.columns:
            raise ValueError(f"{path} has no {column} column")
    for column in QUOTE_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    return frame


def onvista_paused_until() -> str | None:
    """Clock time (HH:MM) of a running onvista pause, else None. Reads the file onvista_http
    keeps, so a pause another job started is seen before the first request."""
    onvista_http._read_cooldown(onvista_http.clock() + onvista_http.COOLDOWN_REREAD)
    until = onvista_http._cooldown["until"]
    if until > onvista_http.clock():
        return onvista_http._clock_text(until)
    return None


def fetch_quotes(
    isins: dict[str, str], session: requests.Session, sleep_seconds: float
) -> tuple[dict[str, Quote], str]:
    """One onvista lookup per ISIN. Returns the two-sided quotes found and a stop reason
    ('' when every ISIN was asked)."""
    quotes: dict[str, Quote] = {}
    for position, (isin, name) in enumerate(isins.items(), start=1):
        before = onvista_http.stats()
        details = fetch_onvista_fields(isin, "equity", session, wkn=None, name=name)
        after = onvista_http.stats()
        if after["blocked"] > before["blocked"] or after["skipped"] > before["skipped"]:
            return quotes, f"onvista limits the requests (stopped at {position}/{len(isins)}: {isin})"
        bid, ask = to_float(details.get("Bid")), to_float(details.get("Ask"))
        if quote_cache.is_two_sided(bid, ask) and not is_crossed(bid, ask):
            quotes[isin] = Quote(
                bid=bid,
                ask=ask,
                currency=str(details.get("QUOTE_CURRENCY") or "").strip().upper(),
                fetched_at=datetime.now().strftime(TIME_FORMAT),
            )
        if sleep_seconds > 0 and position < len(isins):
            time.sleep(sleep_seconds)
    return quotes, ""


def apply_quotes(
    frame: pd.DataFrame, quotes: dict[str, Quote], references: dict[str, dict] | None = None
) -> dict[str, int]:
    """Write the quotes into frame in place; only the QUOTE_COLUMNS, Pot / Pot % and the day
    change (with its Diff Basis / Diff Flag) of rows whose ISIN has a quote can change.
    references is prev_close_cache.load(); without it the day change is not touched."""
    stats = {"updated": 0, "pot": 0, "day": 0, "no_rate": 0, "jump": 0, "newer_in_file": 0}
    for idx, row in frame.iterrows():
        quote = quotes.get(str(row["ISIN"]).strip())
        if quote is None:
            continue
        # iterrows may hand out a view of the frame: read what is needed before writing.
        old_dev, old_ask, old_ask_time = (str(row[c]).strip() for c in ("DEV", "Ask", "Ask Time"))
        tmp = str(row["TMP"]).strip() if "TMP" in frame.columns else ""
        source_change = row[SOURCE_CHANGE_COLUMN] if SOURCE_CHANGE_COLUMN in frame.columns else ""
        target = target_currency_for(old_dev, _normalize_price_currency(quote.currency))
        converted = convert_quote(quote, target)
        if converted is None:
            stats["no_rate"] += 1
            continue
        bid, ask = converted
        if is_implausible_jump(old_ask, ask):
            print(f"  skipped {row['Name']} ({row['ISIN']}): Ask {old_ask} -> {ask} {target} looks wrong")
            stats["jump"] += 1
            continue
        file_time, new_time = parse_time(old_ask_time), parse_time(quote.fetched_at)
        if file_time is not None and new_time is not None and file_time > new_time:
            stats["newer_in_file"] += 1
            continue
        new_values = {
            "Bid": format_german_decimal(bid),
            "Bid Time": quote.fetched_at,
            "Ask": format_german_decimal(ask),
            "Ask Time": quote.fetched_at,
            "DEV": target,
        }
        for column, value in new_values.items():
            frame.at[idx, column] = value
        stats["updated"] += 1
        # Without a DEV before this run the currency of TMP is unknown: leave Pot as it is.
        if old_dev and all(column in frame.columns for column in ("TMP", *POT_COLUMNS)):
            result = potential(tmp, ask)
            if result is not None:
                frame.at[idx, "Pot"] = format_german_decimal(result[0])
                frame.at[idx, "Pot %"] = format_german_decimal(result[1])
                stats["pot"] += 1
        reference = prev_close_cache.todays_reference(references or {}, row["ISIN"])
        if reference is not None and DAY_CHANGE_COLUMN in frame.columns:
            change = day_change(ask, target, reference)
            if change is not None:
                frame.at[idx, DAY_CHANGE_COLUMN] = format_german_decimal(change)
                if DIFF_BASIS_COLUMN in frame.columns:
                    frame.at[idx, DIFF_BASIS_COLUMN] = "Ask"
                if DIFF_FLAG_COLUMN in frame.columns and SOURCE_CHANGE_COLUMN in frame.columns:
                    frame.at[idx, DIFF_FLAG_COLUMN] = diff_flag(change, source_change)
                stats["day"] += 1
    return stats


def refresh(path: Path, sleep_seconds: float, limit: int | None, dry_run: bool, to_pg: bool) -> int:
    paused = onvista_paused_until()
    if paused:
        print(f"onvista is paused until {paused}; nothing to do")
        return 0

    frame = read_top_list(path)
    isins: dict[str, str] = {}
    for _, row in frame.iterrows():
        isin = str(row["ISIN"]).strip()
        if isin and isin not in isins:
            isins[isin] = str(row["Name"]).strip()
    if limit:
        isins = dict(list(isins.items())[:limit])
    print(f"{len(frame)} rows, {len(isins)} ISINs to look up")

    session = make_session()
    quotes, stopped = fetch_quotes(isins, session, sleep_seconds)
    if stopped:
        print(f"Warning: {stopped}")
    if not quotes:
        print("No two-sided quote read; file left as it is")
        return 0

    # Re-read: a full fetch may have rewritten the file while onvista was being asked.
    frame = read_top_list(path)
    stats = apply_quotes(frame, quotes, prev_close_cache.load())
    print(
        f"{len(quotes)}/{len(isins)} ISINs with a two-sided quote; rows updated {stats['updated']} (Pot recomputed {stats['pot']}, day change {stats['day']}), "
        f"skipped: {stats['jump']} implausible jump, {stats['no_rate']} no FX rate, "
        f"{stats['newer_in_file']} newer quote already in file"
    )

    remembered = {}
    for isin, quote in quotes.items():
        quote_cache.remember(
            remembered, isin, bid=quote.bid, ask=quote.ask, bid_time=quote.fetched_at,
            ask_time=quote.fetched_at, currency=quote.currency,
        )
    if dry_run:
        print("Dry run: nothing written")
        return 0
    try:
        quote_cache.save(remembered)
    except OSError as exc:
        print(f"Warning: quote cache not written: {exc}")

    write_csv_atomic(frame, path)
    print(f"Wrote {path}")
    if to_pg:
        try:
            count = import_live_csv(path)
            print(f"Imported {count} rows into PostgreSQL")
        except Exception as exc:
            print(f"Warning: PostgreSQL import failed: {exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh only Bid/Ask of the Top list CSV from onvista")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Top list CSV (default: %(default)s)")
    parser.add_argument(
        "--sleep", type=float, default=float(os.getenv("REQUEST_SLEEP_SECONDS", "0.6")),
        help="Seconds between two onvista lookups (default: REQUEST_SLEEP_SECONDS or 0.6)",
    )
    parser.add_argument("--limit", type=int, help="Only look up the first N distinct ISINs (testing)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report, write nothing")
    parser.add_argument("--pg", action="store_true", help="Import the CSV into PostgreSQL after writing it")
    args = parser.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"{path} does not exist", file=sys.stderr)
        return 1

    with open(LOCK_PATH, "a") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("previous Top list Bid/Ask run still in progress, skipping")
            return 0
        return refresh(path, args.sleep, args.limit, args.dry_run, args.pg)


if __name__ == "__main__":
    sys.exit(main())
