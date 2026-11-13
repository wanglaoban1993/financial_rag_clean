"""Analyst recommendation counts per month (Yahoo's "Analyst Recommendations" chart).

Fetched live when someone opens a target-price dialog, not by the twice-daily target
refresh: one Yahoo request per click keeps the batch job's traffic unchanged. Results are
kept in memory for an hour, and any failure just means "no chart".
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Iterable, Optional

CACHE_TTL_SECONDS = 3600
FIELDS = ("strongBuy", "buy", "hold", "sell", "strongSell")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _month_label(today: date, months_back: int) -> str:
    index = (today.year * 12 + today.month - 1) - months_back
    return MONTHS[index % 12]


def parse_recommendations(records: Iterable[dict], today: Optional[date] = None) -> list[dict[str, Any]]:
    """Yahoo rows ({"period": "0m" | "-1m" ..., "strongBuy": 5, ...}) as a list ordered
    oldest to newest, each with a month label. Rows without a usable period or with
    no analysts at all are skipped."""
    today = today or date.today()
    rows = []
    for record in records:
        try:
            months_back = -int(str(record.get("period")).strip().rstrip("m"))
        except ValueError:
            continue
        if months_back < 0:
            continue
        counts = {field: max(int(record.get(field) or 0), 0) for field in FIELDS}
        if not sum(counts.values()):
            continue
        rows.append({"period": record.get("period"), "month": _month_label(today, months_back),
                     "months_back": months_back, **counts})
    rows.sort(key=lambda row: -row["months_back"])
    for row in rows:
        del row["months_back"]
    return rows


def fetch_recommendations(symbol: str) -> list[dict[str, Any]]:
    symbol = str(symbol or "").strip()
    if not symbol:
        return []
    cached = _cache.get(symbol)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        import yfinance as yf

        frame = yf.Ticker(symbol).recommendations
        rows = parse_recommendations(frame.to_dict("records")) if frame is not None else []
    except Exception:  # unofficial API, rate limits, delisted symbols: no chart, no error page
        return []  # not cached, so the next click tries again
    _cache[symbol] = (time.time(), rows)
    return rows
