"""Read-only view of csv_update/_prev_close_cache.json for the dashboard's calculation dialog.

fetch_finanzen_topflop.py writes that file once per full run, from the EODHD (else Yahoo) history it
already cached for the day, so serving it costs no new data request. An entry is only valid on
the day it was written: tomorrow's reference is another close.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

CACHE_FILE = "_prev_close_cache.json"


def lookup(csv_root: Path, isin: Optional[str], today: Optional[date] = None) -> dict[str, Any]:
    key = (isin or "").strip().upper()
    result: dict[str, Any] = {"isin": key or None, "found": False}
    if not key:
        return result
    try:
        raw = json.loads((Path(csv_root) / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    entries = raw.get("references") if isinstance(raw, dict) else None
    entry = entries.get(key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict) or entry.get("day") != (today or date.today()).isoformat():
        return result
    try:
        ref_eur = float(entry["ref_eur"])
    except (KeyError, TypeError, ValueError):
        return result
    if ref_eur <= 0:
        return result
    result.update(found=True, ref_eur=ref_eur, fetched_at=entry.get("fetched_at"))
    close = entry.get("close")
    if isinstance(close, dict):
        try:
            result["close"] = {
                "value": float(close["value"]),
                "currency": str(close.get("currency") or "").upper(),
                "date": str(close.get("date") or ""),
                "source": str(close.get("source") or ""),
            }
        except (KeyError, TypeError, ValueError):
            pass
    return result
