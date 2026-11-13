"""Shared analyst-target (TMP) cache, keyed by ISIN.

refresh_targets.py fills it twice a day (10:00 and 16:30). Every list that shows
a target price (EQUITY watchlists' COMMENT2, the Top list's and the INDEX lists'
TMP) reads its target from here, so one instrument shows the same TMP everywhere
and it only changes when the cache is refreshed. Consumers only read; the file
is replaced atomically, so a reader never sees a half-written cache.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

CACHE_PATH = Path(os.environ.get("TARGET_CACHE_PATH") or Path(__file__).resolve().parent / "csv_update" / "_target_cache.json")

# The refresh runs Monday to Friday, so Monday morning still reads Friday
# 16:30. An entry older than this means the refresh job has been failing; the
# consumers then fall back to their own live lookup.
MAX_AGE = timedelta(days=4)

# Fields of fetch_analyst_data() that belong to the target itself.
TARGET_FIELDS = ("target_mean", "target_high", "target_low", "analysts")


def _key(isin: object) -> str:
    return str(isin or "").strip().upper()


def load(path: Optional[Path] = None) -> dict[str, dict]:
    try:
        raw = json.loads(Path(path or CACHE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("targets") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def save(entries: dict[str, dict], path: Optional[Path] = None) -> None:
    target = Path(path or CACHE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "targets": entries}
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temp, target)


def fresh_entry(entries: dict[str, dict], isin: object, now: Optional[datetime] = None) -> Optional[dict]:
    """The cached target for isin, or None if absent or older than MAX_AGE."""
    entry = entries.get(_key(isin))
    if not isinstance(entry, dict):
        return None
    try:
        fetched_at = datetime.fromisoformat(str(entry.get("fetched_at")))
    except ValueError:
        return None
    if (now or datetime.now()) - fetched_at > MAX_AGE:
        return None
    return entry


def apply_to_data(data: dict, entry: dict) -> dict:
    """data (a live fetch_analyst_data() result) with its target fields replaced
    by the cached ones. Currency and source are only taken from the cache when
    the live lookup has none, so the target and its currency stay a pair. If the
    live lookup resolved a different listing (another currency), the cached
    target does not describe it and data is returned unchanged."""
    merged = dict(data or {})
    if merged.get("currency") and entry.get("currency") and merged["currency"] != entry["currency"]:
        return merged
    for field in TARGET_FIELDS:
        merged[field] = entry.get(field)
    for field in ("currency", "source"):
        if not merged.get(field):
            merged[field] = entry.get(field)
    return merged
