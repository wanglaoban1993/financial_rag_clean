"""The onvista detail page of an ISIN, remembered so a refresh needs one request, not up to five.

Finding the page costs a search request for a stock, and for an ETF up to two direct tries and a
search more (an ETC fails both direct tries first), on top of the request for the page itself,
and the address of an instrument practically never changes. The addresses are kept in
csv_update/_onvista_url_cache.json. Only an address whose page was read and showed the ISIN is
stored, so a wrong page never gets in; if a cached address later stops showing its ISIN (onvista
answers an unknown address with its search page, status 200) the caller forgets it and looks it up
again. An entry older than MAX_AGE is looked up again anyway.

Several jobs write the file (EQUITY and ETF runs, the Top list), so a flush merges with what is on
disk, the newest entry per ISIN winning, and the file is replaced atomically.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

CACHE_PATH = Path(os.environ.get("ONVISTA_URL_CACHE_PATH") or Path(__file__).resolve().parent / "csv_update" / "_onvista_url_cache.json")

MAX_AGE = timedelta(days=30)
FLUSH_EVERY = 100  # new entries, so a killed job loses little

_lock = threading.Lock()
_entries: Optional[dict[str, dict]] = None
_pending: dict[str, dict] = {}
_forgotten: set[str] = set()
_stats = {"hits": 0, "lookups": 0, "stored": 0, "stale": 0}


def _key(isin: object) -> str:
    return str(isin or "").strip().upper()


def _parse(stamp: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(stamp))
    except ValueError:
        return None


def _load_file() -> dict[str, dict]:
    try:
        raw = json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("urls") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def reset() -> None:
    """Forget in-memory state (tests)."""
    global _entries
    with _lock:
        _entries = None
        _pending.clear()
        _forgotten.clear()
        _stats.update(hits=0, lookups=0, stored=0, stale=0)


def stats() -> dict:
    return dict(_stats)


def get(isin: object, now: Optional[datetime] = None) -> Optional[str]:
    global _entries
    key = _key(isin)
    with _lock:
        if _entries is None:
            _entries = _load_file()
        _stats["lookups"] += 1
        entry = _pending.get(key) or _entries.get(key)
        if key in _forgotten or not isinstance(entry, dict) or not entry.get("url"):
            return None
        stamp = _parse(entry.get("checked_at"))
        if stamp is None or (now or datetime.now()) - stamp > MAX_AGE:
            return None
        _stats["hits"] += 1
        return str(entry["url"])


def put(isin: object, url: str, now: Optional[datetime] = None) -> None:
    key = _key(isin)
    if not key or not url:
        return
    entry = {"url": url, "checked_at": (now or datetime.now()).isoformat(timespec="seconds")}
    with _lock:
        _pending[key] = entry
        _forgotten.discard(key)
        _stats["stored"] += 1
        flush_now = len(_pending) >= FLUSH_EVERY
    if flush_now:
        flush()


def forget(isin: object) -> None:
    """The cached address no longer shows the ISIN."""
    key = _key(isin)
    with _lock:
        _pending.pop(key, None)
        _forgotten.add(key)
        _stats["stale"] += 1


def flush() -> None:
    global _entries
    with _lock:
        pending, forgotten = dict(_pending), set(_forgotten)
        if not pending and not forgotten:
            return
        merged = _load_file()
        for key in forgotten:
            merged.pop(key, None)
        for key, entry in pending.items():
            current = merged.get(key)
            ours, theirs = _parse(entry.get("checked_at")), _parse((current or {}).get("checked_at"))
            if current is None or theirs is None or (ours is not None and ours >= theirs):
                merged[key] = entry
        try:
            Path(CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
            temp = Path(CACHE_PATH).with_name(Path(CACHE_PATH).name + f".{os.getpid()}.tmp")
            temp.write_text(
                json.dumps({"updated_at": datetime.now().isoformat(timespec="seconds"), "urls": merged}, ensure_ascii=False, indent=0, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp, CACHE_PATH)
        except OSError:
            return  # keep pending; the next flush tries again
        _entries = merged
        _pending.clear()
        _forgotten.clear()


def summary() -> Optional[str]:
    if not _stats["lookups"]:
        return None
    return (
        f"onvista url cache: {_stats['hits']} of {_stats['lookups']} lookups answered from it, "
        f"{_stats['stored']} stored, {_stats['stale']} stale"
    )


@atexit.register
def _flush_and_print() -> None:
    flush()
    text = summary()
    if text:
        print(text, flush=True)
