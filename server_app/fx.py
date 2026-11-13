"""Currency conversion for the target-price dialog.

The table shows a row in its DEV currency (mostly EUR) while Yahoo states the analyst target
in the currency of the stock's primary listing (USD, JPY, HKD ...). To draw the current price
(the row's Ask) on the same axis as the targets, the targets are converted into DEV here.

Rates come from Yahoo's currency pairs (EURUSD=X style) through yfinance, the same source the
batch jobs use, and are kept in memory for an hour. Any failure returns None and the dialog
falls back to not drawing the current price.
"""

from __future__ import annotations

import time
from typing import Any, Optional

RATE_TTL_SECONDS = 3600

# Yahoo quotes some listings in a subunit: pence, South African cents, agorot.
_SUBUNITS = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

_rates: dict[tuple[str, str], tuple[float, float]] = {}


def normalize(currency: object) -> tuple[str, float]:
    """(ISO currency, divisor): 'GBp' is pence, i.e. ('GBP', 100)."""
    raw = str(currency or "").strip()
    if raw in _SUBUNITS:
        return _SUBUNITS[raw]
    return raw.upper(), 1.0


def _last_close(symbol: str) -> Optional[float]:
    import yfinance as yf

    frame = yf.Ticker(symbol).history(period="5d")
    closes = frame["Close"].dropna() if frame is not None and "Close" in frame else None
    if closes is None or closes.empty:
        return None
    value = float(closes.iloc[-1])
    return value if value > 0 else None


def rate(source: str, target: str) -> Optional[float]:
    """Units of target per unit of source (ISO codes), None when Yahoo has no usable pair."""
    if not source or not target:
        return None
    if source == target:
        return 1.0
    cached = _rates.get((source, target))
    if cached and time.time() - cached[0] < RATE_TTL_SECONDS:
        return cached[1]
    value = None
    try:
        direct = _last_close(f"{source}{target}=X")
        if direct:
            value = direct
        else:  # some pairs only exist the other way round
            inverse = _last_close(f"{target}{source}=X")
            value = 1.0 / inverse if inverse else None
    except Exception:  # unofficial API, rate limits: no conversion rather than an error page
        return None
    if value:
        _rates[(source, target)] = (time.time(), value)
    return value


def convert_amounts(
    amounts: dict[str, Any], from_currency: object, to_currency: object
) -> Optional[dict[str, Any]]:
    """The amounts (None values stay None) converted to to_currency, with the currency and the
    factor used: {"currency": "EUR", "rate": 0.8906, **amounts}. None when no rate is available."""
    source, source_divisor = normalize(from_currency)
    target, target_divisor = normalize(to_currency)
    if not source or not target:
        return None
    pair_rate = rate(source, target)
    if pair_rate is None:
        return None
    factor = pair_rate / source_divisor * target_divisor
    converted = {key: (None if value is None else float(value) * factor) for key, value in amounts.items()}
    return {"currency": str(to_currency).strip(), "rate": factor, **converted}
