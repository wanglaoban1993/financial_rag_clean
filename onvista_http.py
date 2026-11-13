"""Every request to onvista.de goes through get(): it notices being limited and backs off.

onvista sits behind Vercel, whose protection answers 429 (or 403, 503, a challenge page marked
`x-vercel-mitigated`) to a client that asks too often. Until now such an answer was parsed like a
page, found no Geld/Brief and ended as a silent blank, while the next job kept hammering. Here:

* a limited answer is retried twice, after the server's Retry-After (at most 60 s) or 5 s, 10 s;
* when it stays limited, onvista is paused for 5 minutes, then 10, 20, up to 30 for every
  further strike, and the pause is kept in a file so the other jobs (EQUITY, ETF, Top list, ...)
  stop at once instead of each finding out for itself;
* during a pause get() raises OnvistaBlocked immediately, so a run finishes quickly with the
  rows' last values (and the reason in ENRICH_NOTE) instead of waiting or hammering; the next
  run tries again;
* a successful request after the pause ends the strikes.

OnvistaBlocked is a requests.RequestException, so the callers that already swallow request
errors keep working. last_block() tells the caller why a lookup came back empty.
"""

from __future__ import annotations

import atexit
import json
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

COOLDOWN_PATH = Path(os.environ.get("ONVISTA_COOLDOWN_PATH") or Path(__file__).resolve().parent / "csv_update" / "_onvista_cooldown.json")

BLOCK_STATUS = {429, 403, 503}
MAX_ATTEMPTS = 3
RETRY_DELAY = 5.0          # then doubled
MAX_RETRY_AFTER = 60.0
COOLDOWN_BASE = 300.0      # doubled per strike
COOLDOWN_MAX = 1800.0
COOLDOWN_REREAD = 2.0      # seconds between looks at the file

sleep = time.sleep
clock = time.time


class OnvistaBlocked(requests.RequestException):
    """onvista is limiting us, or a pause after that is still running."""


_lock = threading.Lock()
_local = threading.local()
_stats = {"requests": 0, "retried": 0, "blocked": 0, "skipped": 0}
_cooldown = {"until": 0.0, "strikes": 0, "status": 0, "read_at": 0.0}
_warned = False


def reset() -> None:
    """Forget in-memory state (tests)."""
    global _warned
    with _lock:
        _stats.update(requests=0, retried=0, blocked=0, skipped=0)
        _cooldown.update(until=0.0, strikes=0, status=0, read_at=0.0)
        _warned = False
    _local.message = ""


def stats() -> dict:
    return dict(_stats)


def last_block() -> str:
    """Why the last request of this thread was refused (cleared by reading it)."""
    message = getattr(_local, "message", "")
    _local.message = ""
    return message


def _read_cooldown(now: float) -> None:
    if now - _cooldown["read_at"] < COOLDOWN_REREAD:
        return
    _cooldown["read_at"] = now
    try:
        raw = json.loads(COOLDOWN_PATH.read_text(encoding="utf-8"))
        _cooldown.update(until=float(raw.get("until", 0)), strikes=int(raw.get("strikes", 0)), status=int(raw.get("status", 0)))
    except (OSError, ValueError):
        _cooldown.update(until=0.0, strikes=0, status=0)


def _write_cooldown(until: float, strikes: int, status: int) -> None:
    try:
        COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = COOLDOWN_PATH.with_name(COOLDOWN_PATH.name + f".{os.getpid()}.tmp")
        temp.write_text(json.dumps({"until": until, "strikes": strikes, "status": status}), encoding="utf-8")
        os.replace(temp, COOLDOWN_PATH)
    except OSError:
        pass  # the pause still holds in this process


def _clock_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def _is_block(response) -> bool:
    headers = getattr(response, "headers", None) or {}
    return response.status_code in BLOCK_STATUS or bool(headers.get("x-vercel-mitigated"))


def _retry_delay(response, attempt: int) -> float:
    headers = getattr(response, "headers", None) or {}
    try:
        return min(max(float(headers.get("retry-after")), 0.0), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return RETRY_DELAY * (2 ** attempt) * random.uniform(0.9, 1.1)


def _refuse(message: str) -> None:
    _local.message = message
    raise OnvistaBlocked(message)


def get(session, url: str, **kwargs):
    """session.get(url, **kwargs), with the backoff and pause described above."""
    global _warned
    with _lock:
        now = clock()
        _read_cooldown(now)
        if _cooldown["until"] > now:
            _stats["skipped"] += 1
            message = f"onvista paused until {_clock_text(_cooldown['until'])} (HTTP {_cooldown['status']} earlier)"
        else:
            message = ""
    if message:
        _refuse(message)

    status = 0
    for attempt in range(MAX_ATTEMPTS):
        response = session.get(url, **kwargs)
        with _lock:
            _stats["requests"] += 1
        if not _is_block(response):
            _note_success()
            return response
        status = response.status_code
        if attempt + 1 < MAX_ATTEMPTS:
            with _lock:
                _stats["retried"] += 1
            sleep(_retry_delay(response, attempt))

    with _lock:
        _stats["blocked"] += 1
        _read_cooldown(clock() + COOLDOWN_REREAD)  # pick up a strike another job just wrote
        strikes = _cooldown["strikes"] + 1
        pause = min(COOLDOWN_BASE * 2 ** (strikes - 1), COOLDOWN_MAX)
        until = clock() + pause
        _cooldown.update(until=until, strikes=strikes, status=status, read_at=clock())
        _write_cooldown(until, strikes, status)
        first_warning, _warned = not _warned, True
    message = f"onvista limited the requests (HTTP {status}); paused until {_clock_text(until)} (strike {strikes})"
    if first_warning:
        print(f"Warning: {message}", flush=True)
    _refuse(message)


def _note_success() -> None:
    with _lock:
        if _cooldown["strikes"] == 0 or _cooldown["until"] > clock():
            return
        _cooldown.update(until=0.0, strikes=0, status=0)
    try:
        COOLDOWN_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def summary() -> Optional[str]:
    if not _stats["requests"] and not _stats["skipped"]:
        return None
    return (
        f"onvista: {_stats['requests']} requests, {_stats['retried']} retried, "
        f"{_stats['blocked']} limited, {_stats['skipped']} skipped during a pause"
    )


@atexit.register
def _print_summary() -> None:
    text = summary()
    if text:
        print(text, flush=True)
