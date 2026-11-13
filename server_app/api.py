from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel

from server_app.db import connect, ensure_schema
from server_app.history import SYMBOL_MAP_PATH, fetch_history, resolve_symbol
from server_app.import_watchlists import import_file
from server_app import prev_close
from server_app.onvista_lookup import detect_instrument_type, lookup as onvista_lookup
from server_app.shared_lists import SCRAPED_GROUPS, SHARED_GROUPS, apply_values, find_in_sources
from server_app.fx import convert_amounts
from server_app.recommendations import fetch_recommendations
from server_app.target_links import target_links
from server_app.watchlist_edit import (
    CSV_ROOT,
    add_instrument,
    create_watchlist,
    delete_watchlist,
    find_watchlist_csv,
    remove_instrument,
    copy_instrument,
    update_comment,
)
from server_app.yahoo_lookup import fetch_return_fields, upsert_symbol_map

app = FastAPI(title="Financial RAG API", version="0.1.0")


def _resolve_isin(cur: Any, isin: str | None, wkn: str | None) -> str | None:
    """ISIN if given directly, otherwise the most recent ISIN seen for this WKN.

    Shared by /history and /notes: both key off ISIN, but the dashboard only
    reliably has WKN for some rows (e.g. TOP_LIST rows have no WKN column at
    all, so this just falls through to None there).
    """
    resolved = (isin or "").strip() or None
    if resolved or not wkn:
        return resolved
    cur.execute(
        """
        SELECT isin FROM app.watchlist_rows
        WHERE wkn = %s AND isin IS NOT NULL
        ORDER BY imported_at DESC
        LIMIT 1
        """,
        [wkn.strip()],
    )
    row = cur.fetchone()
    return row["isin"] if row else None


@app.on_event("startup")
def startup() -> None:
    ensure_schema()


@app.get("/health")
def health() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS count FROM app.watchlist_rows")
            row_count = cur.fetchone()["count"]
            cur.execute("SELECT count(*) AS count FROM app.watchlist_imports")
            import_count = cur.fetchone()["count"]
    return {"status": "ok", "database": "ok", "watchlist_rows": row_count, "imports": import_count}


@app.get("/watchlists")
def watchlists() -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    watchlist_name,
                    asset_group,
                    source_path,
                    row_count,
                    imported_at
                FROM app.watchlist_imports
                ORDER BY coalesce(asset_group, ''), watchlist_name
                """
            )
            return list(cur.fetchall())


@app.get("/rows")
def rows(
    watchlist: str | None = None,
    asset_group: str | None = None,
    query: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if watchlist:
        where.append("wr.watchlist_name = %s")
        params.append(watchlist)
    # Watchlist names are only unique per asset group (EQUITY and ETF both
    # have "EUR BRA"/"US BRA"), so callers showing one group must pass it.
    if asset_group:
        where.append("wr.asset_group = %s")
        params.append(asset_group)
    if query:
        where.append("(wr.name ILIKE %s OR wr.isin ILIKE %s OR wr.wkn ILIKE %s OR wr.comment ILIKE %s)")
        like = f"%{query}%"
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS count FROM app.watchlist_rows wr {where_sql}", params)
            total = cur.fetchone()["count"]
            cur.execute(
                f"""
                SELECT
                    wr.watchlist_name, wr.asset_group, wr.row_number, wr.wkn, wr.isin, wr.name, wr.stock_id,
                    wr.comment, wr.tmp, wr.comment3, wr.bid_text, wr.ask_text, wr.ask_time,
                    wr.return_1y, wr.return_1m, wr.return_1d, wr.price_date, wr.raw,
                    n.note
                FROM app.watchlist_rows wr
                LEFT JOIN app.instrument_notes n ON n.isin = wr.isin
                {where_sql}
                ORDER BY wr.watchlist_name, wr.row_number
                LIMIT %s OFFSET %s
                """,
                [*params, limit, offset],
            )
            items = list(cur.fetchall())
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/history")
def history(isin: str | None = None, wkn: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            resolved_isin = _resolve_isin(cur, isin, wkn)

    if not resolved_isin:
        return {"isin": isin, "wkn": wkn, "symbol": None, "points": [], "error": "no_isin"}

    symbol = resolve_symbol(resolved_isin)
    if not symbol:
        return {"isin": resolved_isin, "wkn": wkn, "symbol": None, "points": [], "error": "no_symbol_mapping"}

    try:
        points = fetch_history(symbol)
    except Exception as exc:  # yfinance/network failures shouldn't 500 the dashboard
        return {"isin": resolved_isin, "wkn": wkn, "symbol": symbol, "points": [], "error": str(exc)}

    return {"isin": resolved_isin, "wkn": wkn, "symbol": symbol, "points": points}


@app.get("/target-source")
def target_source(isin: str | None = None, wkn: str | None = None, to_currency: str | None = None) -> dict[str, Any]:
    """Pages the TMP (analyst target) of this instrument comes from. With to_currency (the row's
    DEV) the targets also come back converted into it as "display", so the dashboard can put
    the row's current price on the same axis."""
    with connect() as conn:
        with conn.cursor() as cur:
            resolved_isin = _resolve_isin(cur, isin, wkn)
    if not resolved_isin:
        return {"isin": isin, "wkn": wkn, "symbol": None, "links": [], "error": "no_isin"}
    result = target_links(CSV_ROOT, resolved_isin, resolve_symbol(resolved_isin))
    result["recommendations"] = fetch_recommendations(result.get("symbol") or "")
    if to_currency and result.get("target_mean") is not None and result.get("currency"):
        result["display"] = convert_amounts(
            {key: result.get(key) for key in ("target_low", "target_mean", "target_high")},
            result["currency"],
            to_currency,
        )
    return result


@app.get("/prev-close")
def prev_close_reference(isin: str | None = None) -> dict[str, Any]:
    """The previous close behind today's "% Differenz zum Vortageskurs" (EUR reference plus the
    raw EODHD close with its date), as the last full run stored it. found=false when there is none."""
    return prev_close.lookup(CSV_ROOT, isin)


class NoteIn(BaseModel):
    isin: str | None = None
    wkn: str | None = None
    note: str


@app.post("/notes")
def save_note(payload: NoteIn) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            resolved_isin = _resolve_isin(cur, payload.isin, payload.wkn)
            if not resolved_isin:
                return {"isin": None, "note": None, "error": "no_isin"}
            cur.execute(
                """
                INSERT INTO app.instrument_notes (isin, note, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (isin) DO UPDATE SET note = EXCLUDED.note, updated_at = now()
                """,
                [resolved_isin, payload.note.strip()],
            )
    return {"isin": resolved_isin, "note": payload.note.strip()}


class WatchlistIn(BaseModel):
    asset_group: str
    name: str


@app.post("/watchlists")
def create_watchlist_row(payload: WatchlistIn) -> dict[str, Any]:
    """Create a new, empty EQUITY/ETF/BUYLIST/DEPOTS watchlist (a header-only CSV + Postgres
    registration). Instruments are then added via POST /instruments."""
    return create_watchlist(payload.asset_group, payload.name)


@app.delete("/watchlists")
def delete_watchlist_row(asset_group: str, name: str) -> dict[str, Any]:
    """Delete a whole EQUITY/ETF/BUYLIST/DEPOTS watchlist. Its CSV is moved to
    csv_update/_deleted/ (not erased) and its rows leave Postgres."""
    return delete_watchlist(asset_group, name)


class InstrumentIn(BaseModel):
    watchlist_name: str
    asset_group: str
    isin: str
    wkn: str = ""
    comment: str = ""


@app.post("/instruments")
def add_instrument_row(payload: InstrumentIn) -> dict[str, Any]:
    """Add a new ISIN to an EQUITY/ETF/BUYLIST/DEPOTS watchlist's CSV, auto-filling Name/WKN/
    Bid/Ask/currency/%-change/QUANTITY fields (+ Laufende Kosten for ETF) from
    live onvista.de and Yahoo Finance lookups (both best-effort — a failed
    lookup just leaves those fields blank, to be filled by the next scheduled
    enrichment cron). QUANTITY, despite its name, holds a MACD+Stochastic TA
    signal code, not a share count — see yahoo_lookup._compute_ta_quantity().

    Kurspotential/Kurspotential % are deliberately not live-filled here: that
    needs the batch pipeline's analyst-target-price + currency-normalization
    logic, which shouldn't be forked into a second implementation that can
    drift out of sync — it's left to the twice-daily equity enrichment cron.
    """
    asset_group = payload.asset_group.strip().upper()
    isin = payload.isin.strip().upper()

    # BUYLIST/DEPOTS mix equities and ETFs, and usually repeat an ISIN some
    # EQUITY/ETF list already tracks — copy that row's market data instead of
    # looking it up again (refresh_shared_lists.py keeps doing the same later).
    if asset_group in SHARED_GROUPS:
        owner = find_in_sources(CSV_ROOT, isin)
        if owner:
            return _add_from_owner(payload, asset_group, isin, owner)
        instrument_type = detect_instrument_type(isin)
    else:
        instrument_type = "stock" if asset_group == "EQUITY" else "etf"
    found = onvista_lookup(payload.isin, instrument_type)

    yahoo = fetch_return_fields(payload.isin, instrument_type)
    returns = {k: yahoo[k] for k in ("return_1y", "return_1m", "return_1d")}

    result = add_instrument(
        payload.asset_group,
        payload.watchlist_name,
        isin=payload.isin,
        wkn=payload.wkn or found["wkn"],
        comment=payload.comment,
        name=found["name"],
        bid=found["bid"],
        ask=found["ask"],
        currency=found["currency"],
        laufende_kosten=found["laufende_kosten"],
        quantity=yahoo["quantity"] or "",
        **returns,
    )
    if result.get("error"):
        return result

    if yahoo["ticker"]:
        # Best-effort: lets the dashboard's price-history chart work for this
        # ISIN right away instead of waiting for the offline batch script.
        symbol_map_type = "equity" if instrument_type == "stock" else "etf"
        upsert_symbol_map(SYMBOL_MAP_PATH, payload.isin, yahoo["ticker"], found["name"], symbol_map_type)

    return {
        "isin": isin,
        "name": found["name"],
        "wkn": payload.wkn or found["wkn"],
        "bid": found["bid"],
        "ask": found["ask"],
        "currency": found["currency"],
        "laufende_kosten": found["laufende_kosten"],
        "quantity": yahoo["quantity"],
        "lookup_error": found["error"],
        **returns,
    }


def _add_from_owner(payload: InstrumentIn, asset_group: str, isin: str, owner: dict[str, str]) -> dict[str, Any]:
    result = add_instrument(
        payload.asset_group,
        payload.watchlist_name,
        isin=isin,
        wkn=payload.wkn or owner.get("WKN", ""),
        comment=payload.comment,
        name=owner.get("Name", ""),
        bid=None,
        ask=None,
        currency="",
    )
    if result.get("error"):
        return result

    csv_path = find_watchlist_csv(asset_group, payload.watchlist_name)
    if csv_path is not None and apply_values(csv_path, {isin: owner}):
        import_file(csv_path, CSV_ROOT)
    return {
        "isin": isin,
        "name": owner.get("Name", ""),
        "wkn": payload.wkn or owner.get("WKN", ""),
        "copied_from_list": True,
        "lookup_error": "",
    }


@app.delete("/instruments")
def remove_instrument_row(watchlist_name: str, asset_group: str, isin: str) -> dict[str, Any]:
    """Remove an ISIN from an EQUITY/ETF/BUYLIST/DEPOTS watchlist's CSV (and re-sync Postgres)."""
    return remove_instrument(asset_group, watchlist_name, isin=isin)


class CopyIn(BaseModel):
    source_group: str
    source_watchlist: str
    target_group: str
    target_watchlist: str
    isin: str


@app.post("/instruments/copy")
def copy_instrument_row(payload: CopyIn) -> dict[str, Any]:
    """Copy an ISIN's row from one EQUITY/ETF/BUYLIST/DEPOTS watchlist to another, leaving the
    source as it is (and re-sync Postgres). Which groups may go where is
    `shared_lists.COPY_TARGETS`. A TOP_LIST or INDEX row has no WKN, so that one is looked up on onvista
    (best-effort: a failed lookup leaves it blank, as for POST /instruments)."""
    wkn = ""
    if payload.source_group.strip().upper() in SCRAPED_GROUPS:
        try:
            wkn = onvista_lookup(payload.isin, "stock").get("wkn") or ""
        except Exception:
            wkn = ""
    return copy_instrument(
        payload.source_group,
        payload.source_watchlist,
        payload.target_group,
        payload.target_watchlist,
        isin=payload.isin,
        wkn=wkn,
    )


class CommentIn(BaseModel):
    watchlist_name: str
    asset_group: str
    isin: str
    comment: str


@app.post("/instruments/comment")
def update_instrument_comment(payload: CommentIn) -> dict[str, Any]:
    """Update the CSV's COMMENT column for an existing EQUITY/ETF/BUYLIST/DEPOTS row (and
    re-sync Postgres). Distinct from POST /notes, which saves to the separate
    app.instrument_notes table shown in the price-history dialog."""
    return update_comment(
        payload.asset_group,
        payload.watchlist_name,
        isin=payload.isin,
        comment=payload.comment,
    )


@app.get("/summary")
def summary() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) AS rows,
                    count(DISTINCT watchlist_name) AS watchlists,
                    count(DISTINCT isin) FILTER (WHERE isin IS NOT NULL) AS instruments,
                    avg(return_1y) AS avg_return_1y,
                    max(imported_at) AS last_imported_at
                FROM app.watchlist_rows
                """
            )
            return dict(cur.fetchone())
