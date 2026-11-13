"""Web pages the analyst target (TMP) of an ISIN comes from.

refresh_targets.py stores, per ISIN, the Yahoo symbol it looked up in
csv_update/_target_cache.json. The api image only contains server_app/, so this
reads that JSON directly instead of importing target_cache.py. When the cache
has no entry yet, the ISIN->symbol map used for the price history is the
fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

CACHE_FILENAME = "_target_cache.json"


def _cached_entry(csv_root: Path, isin: str) -> dict[str, Any]:
    try:
        raw = json.loads((Path(csv_root) / CACHE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    targets = raw.get("targets") if isinstance(raw, dict) else None
    entry = targets.get(isin.strip().upper()) if isinstance(targets, dict) else None
    return entry if isinstance(entry, dict) else {}


def target_links(csv_root: Path, isin: str, fallback_symbol: str | None = None) -> dict[str, Any]:
    entry = _cached_entry(csv_root, isin)
    symbol = str(entry.get("symbol") or fallback_symbol or "").strip()
    if not symbol:
        return {"isin": isin, "symbol": None, "links": [], "error": "no_symbol"}

    encoded = quote(symbol, safe="")
    # Yahoo is where TMP is read from. ChartMill is only used when Yahoo's target
    # looks implausible against the price, and the cache does not record when that
    # happened, so it is offered as the second page to check.
    links = [
        {
            "label": "Yahoo Finance – Quote page (\"1y Target Est\", Analyst Price Targets)",
            "url": f"https://finance.yahoo.com/quote/{encoded}/",
            "primary": True,
        },
        {
            "label": "ChartMill – Analyst ratings (fallback source)",
            "url": f"https://www.chartmill.com/stock/quote/{encoded}/analyst-ratings",
            "primary": False,
        },
    ]
    return {
        "isin": isin,
        "symbol": symbol,
        "target_mean": entry.get("target_mean"),
        "target_high": entry.get("target_high"),
        "target_low": entry.get("target_low"),
        "analysts": entry.get("analysts"),
        "currency": entry.get("currency"),
        "fetched_at": entry.get("fetched_at"),
        "links": links,
    }
