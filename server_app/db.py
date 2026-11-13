from __future__ import annotations

import difflib
import re
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from server_app.settings import db_settings


SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.watchlist_imports (
    id bigserial PRIMARY KEY,
    source_path text NOT NULL UNIQUE,
    watchlist_name text NOT NULL,
    asset_group text,
    source_mtime timestamptz,
    row_count integer NOT NULL DEFAULT 0,
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.watchlist_rows (
    id bigserial PRIMARY KEY,
    import_id bigint NOT NULL REFERENCES app.watchlist_imports(id) ON DELETE CASCADE,
    source_path text NOT NULL,
    watchlist_name text NOT NULL,
    asset_group text,
    row_number integer NOT NULL,
    wkn text,
    isin text,
    name text,
    stock_id text,
    comment text,
    tmp text,
    comment3 text,
    bid_text text,
    ask_text text,
    ask_time text,
    return_1y numeric,
    return_1m numeric,
    return_1d numeric,
    price_date text,
    raw json NOT NULL DEFAULT '{}'::json,
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.instrument_notes (
    isin text PRIMARY KEY,
    note text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_watchlist_rows_watchlist
    ON app.watchlist_rows (watchlist_name);
CREATE INDEX IF NOT EXISTS idx_watchlist_rows_isin
    ON app.watchlist_rows (isin);
CREATE INDEX IF NOT EXISTS idx_watchlist_rows_wkn
    ON app.watchlist_rows (wkn);
CREATE INDEX IF NOT EXISTS idx_watchlist_rows_name
    ON app.watchlist_rows USING gin (to_tsvector('simple', coalesce(name, '')));

-- `raw` used to be jsonb, which does not preserve object key order (it
-- reorders/repacks keys on storage) — the dashboard renders `raw` as a table
-- with one column per key, in key order, so it needs the CSV's original
-- column order preserved exactly. `json` stores the original text and keeps
-- insertion order, so existing installs are migrated in place here.
DO $$
BEGIN
    IF (
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'app' AND table_name = 'watchlist_rows' AND column_name = 'raw'
    ) = 'jsonb' THEN
        -- tableau_watchlist_latest selects raw and blocks the type change;
        -- ensure_schema() recreates it right after this block runs.
        DROP VIEW IF EXISTS app.tableau_watchlist_latest;
        ALTER TABLE app.watchlist_rows ALTER COLUMN raw TYPE json USING raw::text::json;
        ALTER TABLE app.watchlist_rows ALTER COLUMN raw SET DEFAULT '{}'::json;
    END IF;
END $$;

-- `comment2` was renamed `tmp` (the analyst target price / running costs shown
-- as TMP). Existing installs are migrated in place; the view selects the
-- column by name and can't rename an output column in CREATE OR REPLACE, so it
-- is dropped here and ensure_schema() recreates it right after.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'app' AND table_name = 'watchlist_rows' AND column_name = 'comment2'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'app' AND table_name = 'watchlist_rows' AND column_name = 'tmp'
    ) THEN
        DROP VIEW IF EXISTS app.tableau_watchlist_latest;
        ALTER TABLE app.watchlist_rows RENAME COLUMN comment2 TO tmp;
    END IF;
END $$;
"""


def build_tableau_view_sql() -> str:
    """Create a Tableau-friendly view over the latest imported watchlist rows."""
    return """
CREATE OR REPLACE VIEW app.tableau_watchlist_latest AS
SELECT
    wr.id,
    wr.import_id,
    wi.source_path,
    wi.watchlist_name,
    wi.asset_group,
    wi.source_mtime,
    wr.row_number,
    wr.wkn,
    wr.isin,
    wr.name,
    wr.stock_id,
    wr.comment,
    wr.tmp,
    wr.comment3,
    wr.bid_text,
    wr.ask_text,
    wr.ask_time,
    wr.return_1y,
    wr.return_1m,
    wr.return_1d,
    wr.price_date,
    wr.raw,
    wr.imported_at AS row_imported_at,
    wi.imported_at AS import_timestamp
FROM app.watchlist_rows AS wr
JOIN (
    SELECT DISTINCT ON (source_path)
        source_path,
        id,
        watchlist_name,
        asset_group,
        source_mtime,
        imported_at,
        row_count
    FROM app.watchlist_imports
    ORDER BY source_path, imported_at DESC, id DESC
) AS wi
    ON wi.id = wr.import_id;
""".strip()


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    settings = db_settings()
    with psycopg.connect(settings.conninfo, row_factory=dict_row) as conn:
        yield conn


def ensure_schema() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(build_tableau_view_sql())


def list_tableau_view_rows(limit: int = 50) -> list[dict]:
    """Return a small sample of rows from the Tableau view for quick validation."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT watchlist_name, asset_group, name, isin, wkn, comment, bid_text, ask_text,
                       return_1y, return_1m, return_1d, price_date, import_timestamp
                FROM app.tableau_watchlist_latest
                ORDER BY import_timestamp DESC, watchlist_name, row_number
                LIMIT %s
                """,
                [limit],
            )
            return list(cur.fetchall())


# top_liste_latest holds the top/flop stocks of each index, so one stock can have a row per
# index (PACIRA under both S&P 400 and Russell 2000). The index and the rank inside it exist
# only in the CSV's original columns, kept in the `raw` json column; without them those rows
# look identical. Lists without such a column simply return NULL. Fixed text, never user input.
_INDEX_COLUMNS = """raw ->> 'Index' AS "index", raw ->> 'Rank' AS rank_in_index"""


_ISIN_RE = re.compile(r"^[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]$")


def build_search_clause(kw: str) -> tuple[str, list[str]]:
    """WHERE clause + params for a keyword search. Fixed SQL, user text only as params.

    Matches name and comment/sector tag always. A keyword shaped like an ISIN is also matched
    exactly against `isin` (case-insensitive); anything else is matched as a substring of
    `isin` and `wkn` so partial identifiers ('US6706', a WKN) work too."""
    like = f"%{kw}%"
    if _ISIN_RE.match(kw):
        return (
            "name ILIKE %s OR comment ILIKE %s OR upper(isin) = %s",
            [like, like, kw.upper()],
        )
    return (
        "name ILIKE %s OR comment ILIKE %s OR isin ILIKE %s OR wkn ILIKE %s",
        [like, like, like, like],
    )


def search_watchlist_rows(keyword: str, limit: int = 10) -> list[dict]:
    """Keyword search over app.watchlist_rows (name, comment/sector tag, ISIN or WKN).

    Same data the dashboard tables read from, so RAG answers about the
    watchlist stay consistent with what's shown on screen — unlike the old
    implementation which read csv_update/*.csv directly on a separate
    import schedule.
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    where, params = build_search_clause(kw)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    watchlist_name, asset_group, name, isin, wkn,
                    comment, tmp, comment3, bid_text, ask_text,
                    return_1y, return_1m, return_1d, price_date,
                    {_INDEX_COLUMNS}
                FROM app.watchlist_rows
                WHERE {where}
                ORDER BY watchlist_name, row_number
                LIMIT %s
                """,
                [*params, limit],
            )
            return list(cur.fetchall())


# ---- watchlist lookup by list name (agent tools) ---------------------------
# The dashboard shows one tab per asset_group (TOP_LIST / EQUITY / ETF / INDEX / BUYLIST / DEPOTS)
# with one list per watchlist_name inside it. Keyword search over instrument
# names alone can never select a list, so these helpers resolve what the user
# calls a list ("top_list", "top list latest", "defense") to real names.

_SORT_COLUMNS = {
    "return_1d": "return_1d", "1d": "return_1d", "day": "return_1d", "daily": "return_1d", "today": "return_1d",
    "return_1m": "return_1m", "1m": "return_1m", "month": "return_1m", "monthly": "return_1m",
    "return_1y": "return_1y", "1y": "return_1y", "year": "return_1y", "yearly": "return_1y",
    "name": "name",
}
_FUZZY_CUTOFF = 0.8


def normalize_list_name(text: str) -> str:
    """Lowercase and drop everything but letters/digits, so 'TOP_LIST',
    'top list' and 'Top-List' compare equal."""
    return re.sub(r"[\W_]+", "", (text or "").lower())


def match_watchlists(keyword: str, lists: list[dict]) -> list[dict]:
    """Lists (dicts with watchlist_name / asset_group) that `keyword` refers to.

    Exact normalized name or group first, then substring (so
    'top_list' finds 'top_liste_latest'), then fuzzy (so a typo like
    'top_list_latest' still finds it). The first tier that matches wins, so a
    precise name is never diluted by looser matches."""
    kw = normalize_list_name(keyword)
    if not kw:
        return []

    def names(item: dict) -> tuple[str, str]:
        return normalize_list_name(item["watchlist_name"]), normalize_list_name(item.get("asset_group") or "")

    exact = [i for i in lists if kw in names(i)]
    if exact:
        return exact
    # One direction only: 'toplist' inside 'toplistelatest' is a real hit, but
    # a short group name inside a long keyword is not ('test' is inside 'latest').
    contains = [i for i in lists if any(kw in n for n in names(i))]
    if contains:
        return contains
    return [
        i for i in lists
        if any(difflib.SequenceMatcher(None, kw, n).ratio() >= _FUZZY_CUTOFF for n in names(i) if n)
    ]


def list_watchlists(asset_group: str | None = None) -> list[dict]:
    """Every list with its tab (asset_group) and row count."""
    group = (asset_group or "").strip()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT asset_group, watchlist_name, count(*) AS rows
                FROM app.watchlist_rows
                WHERE (%s = '' OR asset_group ILIKE %s)
                GROUP BY asset_group, watchlist_name
                ORDER BY asset_group, watchlist_name
                """,
                [group, group],
            )
            return list(cur.fetchall())


def get_watchlist_rows(
    watchlist_name: str,
    asset_group: str | None = None,
    sort_by: str = "",
    descending: bool = True,
    limit: int = 10,
) -> dict:
    """Rows of one list, optionally ranked by a return column.

    Returns {"watchlist_name", "asset_group", "total_rows", "rows"} for a single
    resolved list, {"ambiguous": [...]} when the name fits several lists (e.g.
    'US BRA' exists under both EQUITY and ETF), or {"not_found": True,
    "available": [...]} when nothing fits."""
    candidates = list_watchlists()
    if asset_group and asset_group.strip():
        group = normalize_list_name(asset_group)
        candidates = [c for c in candidates if normalize_list_name(c["asset_group"] or "") == group]

    matched = match_watchlists(watchlist_name, candidates)
    if not matched:
        return {"not_found": True, "available": [c["watchlist_name"] for c in candidates]}
    if len(matched) > 1:
        return {"ambiguous": [{"watchlist_name": m["watchlist_name"], "asset_group": m["asset_group"],
                               "rows": m["rows"]} for m in matched]}

    target = matched[0]
    column = _SORT_COLUMNS.get((sort_by or "").strip().lower())
    if column:
        direction = "DESC" if descending else "ASC"
        order_by = f"{column} {direction} NULLS LAST, row_number"
    else:
        order_by = "row_number"
    limit = max(1, min(int(limit), 30))

    with connect() as conn:
        with conn.cursor() as cur:
            # order_by comes from the fixed _SORT_COLUMNS whitelist above, never from user text.
            cur.execute(
                f"""
                SELECT watchlist_name, asset_group, name, isin, wkn,
                       comment, tmp, comment3, bid_text, ask_text,
                       return_1y, return_1m, return_1d, price_date,
                       {_INDEX_COLUMNS}
                FROM app.watchlist_rows
                WHERE watchlist_name = %s AND asset_group IS NOT DISTINCT FROM %s
                ORDER BY {order_by}
                LIMIT %s
                """,
                [target["watchlist_name"], target["asset_group"], limit],
            )
            rows = list(cur.fetchall())
    return {
        "watchlist_name": target["watchlist_name"],
        "asset_group": target["asset_group"],
        "total_rows": target["rows"],
        "sorted_by": column,
        "rows": rows,
    }

