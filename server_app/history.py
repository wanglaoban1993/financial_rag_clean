from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests
import yaml
import yfinance as yf

# Populated offline by csv_analyse_watchlist.py (Yahoo search / justETF lookups),
# then mounted read-only into the api container. Not every ISIN in the
# watchlists has an entry yet.
SYMBOL_MAP_PATH = Path(
    os.getenv("ISIN_SYMBOL_MAP_PATH")
    or (Path(__file__).resolve().parent.parent / "isin_symbol_candidates.yaml")
)

HISTORY_CACHE_TTL_SECONDS = int(os.getenv("HISTORY_CACHE_TTL_SECONDS", "3600"))

# Fallback only (yfinance is unofficial and gets rate-limited/blocked by Yahoo
# from time to time). Same key used by ingestion/market_fetcher.py's EODHD
# fallback; left empty this fallback is simply skipped.
EODHD_API_KEY = os.getenv("EODHD_API_KEY", "").strip()
EODHD_BASE_URL = "https://eodhd.com/api"

# yfinance/standard suffix -> EODHD exchange code (mirrors
# ingestion/market_fetcher.py's _SUFFIX_TO_EODHD so both stay in sync).
_SUFFIX_TO_EODHD = {
    "DE": "XETRA",
    "L": "LSE",
    "PA": "PA",
    "AS": "AS",
    "SW": "SW",
    "HK": "HK",
    "T": "T",
    "US": "US",
}

_symbol_map_cache: dict[str, str] | None = None
_symbol_map_mtime: float | None = None
_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _load_symbol_map() -> dict[str, str]:
    global _symbol_map_cache, _symbol_map_mtime

    try:
        mtime = SYMBOL_MAP_PATH.stat().st_mtime
    except OSError:
        return {}

    if _symbol_map_cache is not None and _symbol_map_mtime == mtime:
        return _symbol_map_cache

    with SYMBOL_MAP_PATH.open() as f:
        raw = yaml.safe_load(f) or {}

    symbol_map = {
        isin: entry["symbol"]
        for isin, entry in raw.items()
        if isinstance(entry, dict) and entry.get("symbol")
    }
    _symbol_map_cache = symbol_map
    _symbol_map_mtime = mtime
    return symbol_map


def resolve_symbol(isin: str) -> str | None:
    return _load_symbol_map().get(isin.strip().upper())


def _to_eodhd_symbol(symbol: str) -> str:
    """Convert a Yahoo-style symbol (e.g. SAP.DE) to EODHD format (SAP.XETRA)."""
    if "." in symbol:
        base, suffix = symbol.rsplit(".", 1)
        return f"{base}.{_SUFFIX_TO_EODHD.get(suffix.upper(), suffix.upper())}"
    return f"{symbol}.US"


def _fetch_history_via_yfinance(symbol: str) -> list[dict[str, Any]]:
    ticker = yf.Ticker(symbol)
    frame = ticker.history(period="1y", interval="1d")
    if frame.empty:
        return []
    return [
        {
            "date": index.strftime("%Y-%m-%d"),
            "close": round(float(row["Close"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
        }
        for index, row in frame.iterrows()
    ]


def _fetch_history_via_eodhd(symbol: str) -> list[dict[str, Any]]:
    if not EODHD_API_KEY:
        return []
    eodhd_symbol = _to_eodhd_symbol(symbol)
    from_date = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 366 * 86400))
    try:
        resp = requests.get(
            f"{EODHD_BASE_URL}/eod/{eodhd_symbol}",
            params={"api_token": EODHD_API_KEY, "fmt": "json", "from": from_date, "order": "a"},
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [
        {
            "date": row["date"],
            "close": round(float(row["close"]), 4),
            # Optional: only the Stochastic needs them; the chart skips it without.
            "high": round(float(row["high"]), 4) if row.get("high") is not None else None,
            "low": round(float(row["low"]), 4) if row.get("low") is not None else None,
        }
        for row in rows
        if row.get("date") is not None and row.get("close") is not None
    ]


def _fetch_history_uncached(symbol: str) -> list[dict[str, Any]]:
    try:
        points = _fetch_history_via_yfinance(symbol)
    except Exception:
        # yfinance is an unofficial scraper of Yahoo's internal endpoints and
        # raises (rather than returning empty) when rate-limited/blocked.
        points = []
    if points:
        return points
    # Empty result (no data, or the exception case above): fall back to
    # EODHD (needs EODHD_API_KEY set) rather than showing "no chart".
    return _fetch_history_via_eodhd(symbol)


def fetch_history(symbol: str) -> list[dict[str, Any]]:
    now = time.time()
    cached = _history_cache.get(symbol)
    if cached and now - cached[0] < HISTORY_CACHE_TTL_SECONDS:
        return cached[1]

    points = _fetch_history_uncached(symbol)
    _history_cache[symbol] = (now, points)
    return points
