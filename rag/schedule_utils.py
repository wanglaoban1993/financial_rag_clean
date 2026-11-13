from __future__ import annotations

import re

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_check_times(values) -> list[tuple[int, int]]:
    """["08:30", "13:00"] -> [(8, 30), (13, 0)], sorted and de-duplicated.

    None / empty -> [] (caller falls back to an interval). A malformed entry
    raises ValueError so a typo in config.yaml fails at startup instead of
    silently never fetching."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    times: set[tuple[int, int]] = set()
    for raw in values:
        match = _HHMM.match(str(raw))
        if not match:
            raise ValueError(f"email.check_times: {raw!r} is not HH:MM")
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"email.check_times: {raw!r} is not a valid time of day")
        times.add((hour, minute))
    return sorted(times)
