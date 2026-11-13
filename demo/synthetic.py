"""Invented data for the public demo: price history, analyst targets and recommendations.

None of it is real. Everything is derived from the instrument's ISIN and its sample price, so the
same instrument always gets the same numbers, on every start and on every machine (the seed is a
CRC32, not Python's hash(), which changes with each process). No network, no files.
"""

from __future__ import annotations

import zlib
from typing import Any, Optional

import numpy as np
import pandas as pd

DAY_CHANGE_COLUMN = "% Differenz zum Vortageskurs"
RECOMMENDATION_KEYS = ("strongBuy", "buy", "hold", "sell", "strongSell")


def seed(*parts: str) -> int:
    return zlib.crc32("|".join(parts).encode("utf-8"))


def price_history(isin: str, current_price: float, days: int = 252, today: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """A made-up year of daily closes ending at current_price: `close` column, indexed by `date`."""
    rng = np.random.default_rng(seed("history", str(isin)))
    path = np.cumprod(1 + rng.normal(loc=0.0003, scale=0.015, size=days))
    path = path / path[-1] * float(current_price)
    end = pd.Timestamp(today if today is not None else pd.Timestamp.today()).normalize()
    return pd.DataFrame({"close": path}, index=pd.bdate_range(end=end, periods=days, name="date"))


def analyst_targets(isin: str, ask: float) -> dict[str, Any]:
    """Made-up analyst target: mean, low, high (in the quote's currency) and the number of analysts."""
    rng = np.random.default_rng(seed("target", str(isin)))
    upside = float(rng.uniform(-0.06, 0.38))
    mean = float(ask) * (1 + upside)
    return {
        "mean": mean,
        "low": mean * float(rng.uniform(0.68, 0.88)),
        "high": mean * float(rng.uniform(1.12, 1.38)),
        "analysts": int(rng.integers(9, 33)),
        "upside": upside,
    }


def month_labels(count: int = 4, today: Optional[pd.Timestamp] = None) -> list[str]:
    now = pd.Timestamp(today if today is not None else pd.Timestamp.today())
    return [(now - pd.DateOffset(months=k)).strftime("%b") for k in range(count - 1, -1, -1)]


def recommendations(isin: str, upside: float, analysts: int, today: Optional[pd.Timestamp] = None) -> list[dict[str, Any]]:
    """Four months of made-up recommendation counts, oldest first. Each month adds up to `analysts`;
    the more upside the target shows, the more buys. Earlier months differ by a move or two."""
    rng = np.random.default_rng(seed("recs", str(isin)))
    bullish = float(np.clip(0.35 + upside, 0.05, 0.85))
    weights = np.array([0.12 + 0.40 * bullish, 0.22 + 0.30 * bullish, 0.38 - 0.30 * bullish,
                        0.16 - 0.12 * bullish, 0.12 - 0.10 * bullish]).clip(min=0.02)
    counts = rng.multinomial(analysts, weights / weights.sum())
    months = [counts.copy()]
    for _ in range(3):  # one month back each time: shift a few analysts between neighbouring ratings
        previous = months[-1].copy()
        for _ in range(int(rng.integers(0, 3))):
            source = int(rng.choice(np.flatnonzero(previous)))
            target = int(np.clip(source + rng.choice([-1, 1]), 0, 4))
            previous[source] -= 1
            previous[target] += 1
        months.append(previous)
    rows = []
    for label, row in zip(month_labels(4, today), reversed(months)):
        rows.append({"month": label, **{key: int(value) for key, value in zip(RECOMMENDATION_KEYS, row)}})
    return rows


def add_target_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """The equity table's TMP, Potential % and DEV columns, like the full dashboard's."""
    out = frame.copy()
    targets = [analyst_targets(isin, ask)["mean"] for isin, ask in zip(out["ISIN"], out["Ask"])]
    out["TMP"] = np.round(targets, 2)
    out["Potential %"] = (out["TMP"] / out["Ask"] - 1) * 100
    out["DEV"] = "EUR"
    return out


def day_change_input(row: pd.Series) -> dict[str, Any]:
    """The raw values the day-change dialog explains. The demo has no real previous close, so the
    close is the one that makes the table's percentage come out: ask / (1 + pct / 100)."""
    ask, pct = float(row["Ask"]), float(row[DAY_CHANGE_COLUMN])
    return {
        "Ask": ask,
        "Bid": float(row["Bid"]),
        "DEV": "EUR",
        "REF_PREV_CLOSE": round(ask / (1 + pct / 100), 3),
        "REF_CURRENCY": "EUR",
        "DIFF_BASIS": "Ask",
        DAY_CHANGE_COLUMN: pct,
    }
