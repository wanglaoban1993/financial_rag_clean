"""Fetch index constituents (DAX, TecDAX, MDAX, SDAX, ...) from finanzen.net and enrich them with quotes."""

from __future__ import annotations

import argparse
import io
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from csv_analyse_watchlist import search_yahoo_symbol
from fetch_finanzen_topflop import (
    OUTPUT_COLUMNS,
    add_onvista_bid_ask,
    add_period_changes,
    add_potential_pct,
    add_yahoo_symbols_from_isin,
    add_yahoo_target_prices,
    fetch_html,
    import_live_csv,
    normalize_display_currency,
    load_eodhd_api_key,
    make_session,
    split_name_isin,
    to_float,
    yf,
)


PROJECT_ROOT = Path(__file__).resolve().parent
INDEX_DIR = PROJECT_ROOT / "csv_update" / "INDEX"
# Bid/Ask come from onvista by ISIN. An index entry may carry a `quote_url` (a finanzen.net
# real-time quote page) to take them from there instead; DAX and TecDAX did until 2026-11-02,
# when finanzen.net put an Akamai bot-check page in front of those two pages. It answers HTTP 200
# without a table, pandas then reports "Missing optional dependency 'html5lib'" (lxml finds no
# table and pandas falls back to html5lib), and both CSVs stayed 9 days old. The constituents
# always come from the (paginated) `/index/<slug>/werte` page, which is not affected;
# `min_count` is used for indices whose size finanzen.net does not report
# reliably (Nasdaq 100 share classes, Hang Seng).
INDEX_SOURCES = {
    "DAX": {
        "source_url": "https://www.finanzen.net/index/dax/40-werte",
        "constituent_count": 40,
        "output": "DAX40.csv",
    },
    "TecDAX": {
        "source_url": "https://www.finanzen.net/index/tecdax/werte",
        "constituent_count": 30,
        "output": "TecDAX.csv",
    },
    "MDAX": {
        "source_url": "https://www.finanzen.net/index/mdax/werte",
        "constituent_count": 50,
        "output": "MDAX.csv",
    },
    "SDAX": {
        "source_url": "https://www.finanzen.net/index/sdax/werte",
        "constituent_count": 70,
        "output": "SDAX.csv",
    },
    "EURO STOXX 50": {
        "source_url": "https://www.finanzen.net/index/euro_stoxx_50/werte",
        "constituent_count": 50,
        "output": "EURO STOXX 50.csv",
    },
    "Hang Seng": {
        "source_url": "https://www.finanzen.net/index/hang_seng/werte",
        "min_count": 40,
        "output": "Hang Seng.csv",
    },
    "Dow Jones": {
        "source_url": "https://www.finanzen.net/index/dow_jones/werte",
        "constituent_count": 30,
        "output": "Dow Jones.csv",
    },
    "Nasdaq100": {
        "source_url": "https://www.finanzen.net/index/nasdaq_100/werte",
        "min_count": 95,
        "output": "Nasdaq100.csv",
    },
}
MAX_CONSTITUENT_PAGES = 5


def _name_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def fetch_constituent_table(session: requests.Session, config: dict) -> pd.DataFrame:
    """Return the constituents table, following finanzen.net's `?p=N` pagination.

    Paging stops at the first page that adds no new ISIN (finanzen.net repeats
    page 1 for out-of-range pages on short indices).
    """
    source_url = config["source_url"]
    pages: list[pd.DataFrame] = []
    seen: set[str] = set()
    for page in range(1, MAX_CONSTITUENT_PAGES + 1):
        url = source_url if page == 1 else f"{source_url}?p={page}"
        tables = pd.read_html(io.StringIO(fetch_html(session, url)), decimal=",", thousands=".")
        table = next((t for t in tables if "Name ISIN" in {str(c) for c in t.columns}), None)
        if table is None:
            break
        isins = table["Name ISIN"].map(lambda value: split_name_isin(value)[1])
        fresh = ~isins.isin(seen) & isins.ne("")
        if not fresh.any():
            break
        pages.append(table.loc[fresh])
        seen.update(isins[fresh])
    if not pages:
        raise RuntimeError(f"Could not find a constituents table at {source_url}")
    return pd.concat(pages, ignore_index=True)


def fetch_quote_rows(session: requests.Session, index_name: str, config: dict) -> dict:
    """Real-time quotes by normalized name, for indices with a finanzen.net `quote_url`."""
    constituent_count = config["constituent_count"]
    quote_tables = pd.read_html(
        io.StringIO(fetch_html(session, config["quote_url"])),
        decimal=",",
        thousands=".",
    )
    quote_table = next(
        (
            candidate
            for candidate in quote_tables
            if len(candidate) == constituent_count + 1 and "Name" in candidate.columns
        ),
        None,
    )
    if quote_table is None:
        raise RuntimeError(f"Could not find the {index_name} real-time quote table on finanzen.net")
    return {
        _name_key(row.get("Name")): row
        for _, row in quote_table.iterrows()
        if _text(row.get("Name")).casefold() != index_name.casefold()
    }


# onvista's currency field is scraped from page text and sometimes yields junk
# such as "VJB"; anything outside this set is treated as an unusable quote.
KNOWN_QUOTE_CURRENCIES = {"EUR", "USD", "GBP", "CHF", "HKD", "JPY", "CNY", "DKK", "SEK", "NOK", "CAD", "AUD"}


def fill_missing_quotes_from_yahoo(frame: pd.DataFrame) -> pd.DataFrame:
    """Replace unusable onvista quotes with Yahoo's latest price.

    A quote is unusable when Bid or Ask is missing (onvista often shows only
    one side) or its currency is not a real currency code. Yahoo exposes no
    bid/ask here, so both fields get the last price, marked with "(Yahoo)" in
    the Bid/Ask Time columns. Needs `YahooSymbol`.
    """
    if yf is None:
        return frame
    unusable = (
        frame["Bid"].isna()
        | frame["Ask"].isna()
        | ~frame["DEV"].fillna("").astype(str).str.upper().isin(KNOWN_QUOTE_CURRENCIES)
    )
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for idx in frame.index[unusable]:
        symbol = _text(frame.at[idx, "YahooSymbol"])
        if not symbol:
            continue
        try:
            info = yf.Ticker(symbol).fast_info
            last, currency = info["lastPrice"], info["currency"]
        except Exception:
            continue
        if not last or not currency:
            continue
        frame.at[idx, "Bid"] = frame.at[idx, "Ask"] = round(float(last), 4)
        frame.at[idx, "Bid Time"] = frame.at[idx, "Ask Time"] = f"{fetched_at} (Yahoo)"
        frame.at[idx, "DEV"] = str(currency).upper()
    return frame


def check_constituent_count(index_name: str, table: pd.DataFrame, config: dict, previous_path: Path | None = None) -> None:
    """Fail on too few constituents, only warn on too many.

    Too few means finanzen.net's page was cut off, and a partial list must not
    overwrite the file. Too many is a real index change (a spin-off is added
    next to its parent, tk accelis in MDAX on 2026-10-28), not a scrape error:
    paging already drops repeated ISINs. The warning names the constituents
    that are not in the previous file.
    """
    expected = config.get("constituent_count")
    minimum = config.get("min_count", expected)
    if len(table) < minimum:
        raise RuntimeError(
            f"Expected {expected or 'at least ' + str(minimum)} {index_name} constituents, got {len(table)}"
        )
    if expected is None or len(table) <= expected:
        return
    added = ""
    if previous_path is not None and previous_path.exists():
        previous = set(pd.read_csv(previous_path, decimal=",")["ISIN"].astype(str))
        names = [
            name
            for name, isin in table["Name ISIN"].map(split_name_isin)
            if isin not in previous
        ]
        if names:
            added = f"; not in the previous file: {', '.join(names)}"
    print(f"Warning: {index_name} has {len(table)} constituents, expected {expected}{added}")


def fetch_index(index_name: str, previous_path: Path | None = None) -> pd.DataFrame:
    config = INDEX_SOURCES[index_name]
    session = make_session()
    table = fetch_constituent_table(session, config)
    check_constituent_count(index_name, table, config, previous_path)

    quotes = fetch_quote_rows(session, index_name, config) if "quote_url" in config else None
    today = datetime.now().strftime("%Y-%m-%d")

    rows = []
    for rank, (_, source_row) in enumerate(table.iterrows(), start=1):
        name, isin = split_name_isin(source_row.get("Name ISIN", ""))
        if not name or not isin:
            raise RuntimeError(
                f"Invalid {index_name} constituent at source row {rank}: {source_row.get('Name ISIN')!r}"
            )

        bid = ask = None
        quote_timestamp = ""
        if quotes is not None:
            quote = quotes.get(_name_key(name))
            if quote is None:
                raise RuntimeError(f"No real-time quote found for {index_name} constituent {name!r}")
            quote_time = _text(quote.get("Zeit"))
            quote_timestamp = f"{today} {quote_time}" if quote_time else ""
            bid, ask = to_float(quote.get("Bid")), to_float(quote.get("Ask"))

        rows.append(
            {
                "Index": index_name,
                "Rank": rank,
                "Name": name,
                "ISIN": isin,
                "Bid": bid,
                "Bid Time": quote_timestamp,
                "Ask": ask,
                "Ask Time": quote_timestamp,
                "% Differenz zum Vortageskurs": None,
                "% Differenz ein Monat": None,
                "% Differenz ein Jahr": None,
                "Pot": None,
                "Pot %": None,
                "TMP": None,
            }
        )

    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if result["ISIN"].nunique() != len(result):
        raise RuntimeError(f"Duplicate ISINs among {index_name} constituents")

    if quotes is None:
        result = add_onvista_bid_ask(result, session)
    else:
        result["DEV"] = "EUR"
    result = add_yahoo_symbols_from_isin(result)
    missing_symbols = result["YahooSymbol"].fillna("").astype(str).str.strip().eq("")
    if missing_symbols.any():
        yahoo_session = requests.Session()
        for index, row in result.loc[missing_symbols].iterrows():
            symbol, _quote_type = search_yahoo_symbol(row["Name"], yahoo_session, {"EQUITY"})
            if symbol:
                result.at[index, "YahooSymbol"] = symbol

    if quotes is None:
        result = fill_missing_quotes_from_yahoo(result)
        no_quote = result["Ask"].isna()
        if no_quote.any():
            print(f"Warning: no quote for {int(no_quote.sum())} {index_name} constituents: "
                  f"{', '.join(result.loc[no_quote, 'Name'])}")
    result["Last"] = result["Ask"]

    result = add_yahoo_target_prices(result)
    result = add_period_changes(result, load_eodhd_api_key())
    result = add_potential_pct(result)
    result = normalize_display_currency(result)
    for column in (
        "% Differenz zum Vortageskurs",
        "% Differenz ein Monat",
        "% Differenz ein Jahr",
        "Pot",
        "Pot %",
    ):
        result[column] = result[column].map(to_float)
    return result.reindex(columns=OUTPUT_COLUMNS)


def fetch_dax40() -> pd.DataFrame:
    return fetch_index("DAX")


def fetch_tecdax() -> pd.DataFrame:
    return fetch_index("TecDAX")


def write_csv(frame: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so the Postgres import (every 15 min)
    # never reads a half-written file.
    temp_path = output_path.with_name(output_path.name + ".tmp")
    frame.to_csv(temp_path, index=False, decimal=",")
    temp_path.replace(output_path)
    print(f"Wrote {len(frame)} constituents to {output_path}")


def import_live(path: Path) -> None:
    """Never raises: the next index is still fetched, and the scheduled import
    (update_watchlists_to_pg.sh) catches up if this one fails."""
    try:
        print(f"  Live import: {import_live_csv(path)} rows from {path.name} in PostgreSQL")
    except Exception as exc:
        print(f"Warning: live PostgreSQL import of {path.name} failed: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch index constituents from finanzen.net")
    parser.add_argument(
        "--indices",
        nargs="+",
        choices=list(INDEX_SOURCES),
        default=["DAX", "TecDAX"],
        help=f"Indices to fetch (default: DAX TecDAX). Available: {', '.join(INDEX_SOURCES)}",
    )
    parser.add_argument("--output-dir", type=Path, default=INDEX_DIR, help="Directory for the index CSVs")
    parser.add_argument("--output", type=Path, help="DAX40 CSV output path (overrides --output-dir)")
    parser.add_argument("--tecdax-output", type=Path, help="TecDAX CSV output path (overrides --output-dir)")
    parser.add_argument(
        "--live-pg",
        action="store_true",
        help="Import each index CSV into PostgreSQL right after it is written, instead of waiting for the next scheduled import",
    )
    args = parser.parse_args()

    overrides = {"DAX": args.output, "TecDAX": args.tecdax_output}
    failed = []
    for index_name in args.indices:
        output_path = overrides.get(index_name) or args.output_dir / INDEX_SOURCES[index_name]["output"]
        try:
            write_csv(fetch_index(index_name, previous_path=output_path), output_path)
            if args.live_pg:
                import_live(output_path)
        except Exception as exc:
            failed.append(index_name)
            print(f"Error: {index_name} failed: {exc}")
    if failed:
        raise SystemExit(f"Failed indices: {', '.join(failed)}")


if __name__ == "__main__":
    main()
