"""Previous-session close per ISIN, in EUR, as the Top list's day change used it.

fetch_finanzen_topflop.py measures "% Differenz zum Vortageskurs" from the Ask against the last
close before today. That close is the same all day, so the full fetch leaves it here and
refresh_toplist_bid_ask.py recomputes the day change from a fresh Ask without any history
lookup. An entry is only valid on the day it was written: tomorrow's reference is another
close. The file is replaced atomically and a save merges with what is on disk (the newest entry
per ISIN wins), like quote_cache.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

CACHE_PATH = Path(os.environ.get("PREV_CLOSE_CACHE_PATH") or Path(__file__).resolve().parent / "csv_update" / "_prev_close_cache.json")
PRUNE_AGE = timedelta(days=5)


def _key(isin: object) -> str:
    return str(isin or "").strip().upper()


def _parse(stamp: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(stamp))
    except ValueError:
        return None


def load(path: Optional[Path] = None) -> dict[str, dict]:
    try:
        raw = json.loads(Path(path or CACHE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("references") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def remember(
    entries: dict[str, dict],
    isin: object,
    ref_eur: float,
    now: Optional[datetime] = None,
    close: Optional[dict] = None,
) -> None:
    """close, when known, is the raw close the reference came from, for the dashboard's
    calculation dialog: {"value": float, "currency": "USD", "date": "2026-11-05", "source": "EODHD"}."""
    now = now or datetime.now()
    entry = {"ref_eur": float(ref_eur), "day": now.date().isoformat(), "fetched_at": now.isoformat(timespec="seconds")}
    if close:
        entry["close"] = dict(close)
    entries[_key(isin)] = entry


def save(entries: dict[str, dict], path: Optional[Path] = None, now: Optional[datetime] = None) -> None:
    target = Path(path or CACHE_PATH)
    now = now or datetime.now()
    merged = load(target)
    for isin, entry in entries.items():
        current = merged.get(isin)
        ours, theirs = _parse(entry.get("fetched_at")), _parse((current or {}).get("fetched_at"))
        if current is None or theirs is None or (ours is not None and ours >= theirs):
            merged[isin] = entry
    merged = {
        isin: entry
        for isin, entry in merged.items()
        if (stamp := _parse(entry.get("fetched_at"))) is not None and now - stamp <= PRUNE_AGE
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    payload = {"updated_at": now.isoformat(timespec="seconds"), "references": merged}
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temp, target)


def todays_entry(entries: dict[str, dict], isin: object, today: Optional[date] = None) -> Optional[dict]:
    """The whole entry written today for isin (reference plus the raw close, if known), or None."""
    entry = entries.get(_key(isin))
    if not isinstance(entry, dict) or entry.get("day") != (today or date.today()).isoformat():
        return None
    return entry


def todays_reference(entries: dict[str, dict], isin: object, today: Optional[date] = None) -> Optional[float]:
    """The EUR reference close written today for isin, or None."""
    entry = entries.get(_key(isin))
    if not isinstance(entry, dict) or entry.get("day") != (today or date.today()).isoformat():
        return None
    try:
        ref = float(entry["ref_eur"])
    except (KeyError, TypeError, ValueError):
        return None
    return ref if ref > 0 else None
