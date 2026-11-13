"""Is the US cash market (NYSE/Nasdaq) trading right now?

Used by fetch_finanzen_topflop.py to skip the per-symbol EODHD real-time quotes
for the iShares-based US indices while the market is closed: those quotes only
repeat the previous close, and EODHD bills one API call per symbol.

Early-close days (day after Thanksgiving, Christmas Eve, ...) are treated as full
days; a few redundant refreshes on those days are harmless.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.easter import easter

NEW_YORK = ZoneInfo("America/New_York")
OPEN = time(9, 30)
# EODHD's US quotes lag by up to ~15 minutes, so stay "open" past the 16:00 bell
# until a run has picked up the closing prices.
CLOSE = time(16, 30)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Holidays on a weekend are observed on the Friday before / Monday after."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def holidays(year: int) -> set[date]:
    days = {
        _nth_weekday(year, 1, 0, 3),                 # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                 # Presidents' Day
        easter(year) - timedelta(days=2),            # Good Friday
        _last_weekday(year, 5, 0),                   # Memorial Day
        _nth_weekday(year, 9, 0, 1),                 # Labor Day
        _nth_weekday(year, 11, 3, 4),                # Thanksgiving
        _observed(date(year, 7, 4)),                 # Independence Day
        _observed(date(year, 12, 25)),               # Christmas
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))       # Juneteenth
    # New Year's Day observed on a Saturday falls in the previous year; NYSE
    # doesn't close on that Friday, so only Sunday -> Monday applies.
    new_year = date(year, 1, 1)
    days.add(new_year + timedelta(days=1) if new_year.weekday() == 6 else new_year)
    return days


def is_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if now.weekday() >= 5 or now.date() in holidays(now.year):
        return False
    return OPEN <= now.time() <= CLOSE
