"""Last two-sided (Bid and Ask) onvista quote per ISIN, for the Top list and the INDEX lists.

After a German stock's exchange closes, onvista's page drops its Geld/Brief lines for a
while, and a fetch then finds no quote. Rather than blank the Bid and Ask until the quote is
back, the fetch keeps the last two-sided quote it read; its Bid Time / Ask Time still say
when that was. The file is replaced atomically, and a save merges with what is on disk (the
newest entry per ISIN wins), because the Top list and the INDEX job both write it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

CACHE_PATH = Path(os.environ.get("QUOTE_CACHE_PATH") or Path(__file__).resolve().parent / "csv_update" / "_quote_cache.json")

# Monday morning still shows Friday's last quote; older means the instrument has had no
# two-sided quote for days and a blank is more honest.
MAX_AGE = timedelta(days=4)
# Entries nobody has refreshed for this long are dropped on save (ISINs that left the lists).
PRUNE_AGE = timedelta(days=14)

FIELDS = ("bid", "ask", "bid_time", "ask_time", "currency")


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
    entries = raw.get("quotes") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


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
    payload = {"updated_at": now.isoformat(timespec="seconds"), "quotes": merged}
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temp, target)


def is_two_sided(bid: object, ask: object) -> bool:
    try:
        return float(bid) > 0 and float(ask) > 0
    except (TypeError, ValueError):
        return False


def remember(
    entries: dict[str, dict],
    isin: object,
    *,
    bid: float,
    ask: float,
    bid_time: str,
    ask_time: str,
    currency: str,
    now: Optional[datetime] = None,
) -> None:
    entries[_key(isin)] = {
        "bid": bid,
        "ask": ask,
        "bid_time": bid_time,
        "ask_time": ask_time,
        "currency": currency,
        "fetched_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }


def fresh_entry(entries: dict[str, dict], isin: object, now: Optional[datetime] = None) -> Optional[dict]:
    """The remembered quote for isin, or None if absent or older than MAX_AGE."""
    entry = entries.get(_key(isin))
    if not isinstance(entry, dict) or not all(field in entry for field in FIELDS):
        return None
    stamp = _parse(entry.get("fetched_at"))
    if stamp is None or (now or datetime.now()) - stamp > MAX_AGE:
        return None
    return entry
