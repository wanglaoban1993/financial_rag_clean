"""Best-effort %-change + TA-signal fields for a newly added EQUITY/ETF row.

Resolves the ISIN to a Yahoo ticker via Yahoo's public search endpoint — the
same one csv_analyse_watchlist.py's search_yahoo_symbol() calls — which works
for an arbitrary new ISIN, not just ones in a pre-built mapping. The %-change
math mirrors that script's _percent_change()/_compute_difference_columns(),
but skips its outlier-robust median smoothing: a first-cut live value here is
superseded by the next scheduled full enrichment (run_watchlist_daily_equity.sh
for equity; the ETF/fund equivalent runs on the same twice-daily cadence)
anyway, so it isn't worth forking that extra logic.

The QUANTITY column follows the same "good enough until the next cron"
philosophy: despite its name it holds a 3-character MACD+Stochastic TA signal
code (see csv_analyse_watchlist.py's _apply_ta_signal/_compute_ta_signal_with_
source), not a share count. _compute_ta_quantity() below reimplements that
same MACD(12,26,9) + Stochastic Slow(14,3,3) crossover logic against the
1y history already fetched for the %-change fields (sliced to the same
~183-day lookback the batch script uses, so a live-added row's code closely
matches what the next scheduled enrichment would compute) — a second, tiny
pure-pandas implementation rather than importing csv_analyse_watchlist.py
itself, consistent with why this module doesn't reuse that script's onvista/
yfinance helpers either (heavy openpyxl/config_loader deps not worth adding
to the lightweight api image).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import yaml
import yfinance as yf

YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
REQUEST_TIMEOUT = 10

# Mirrors csv_analyse_watchlist.py's TA_MACD_*/TA_STOCH_*/TA_LOOKBACK_DAYS.
TA_MACD_FAST, TA_MACD_SLOW, TA_MACD_SIGNAL = 12, 26, 9
TA_STOCH_K, TA_STOCH_D, TA_STOCH_SMOOTH = 14, 3, 3
TA_LOOKBACK_DAYS = 183

# Mirrors csv_analyse_watchlist.py's ENRICHABLE_FUND_TYPES — Yahoo's
# quoteType for an ETF is "ETF", for a mutual fund "MUTUALFUND" or "FUND".
QUOTE_TYPES_BY_INSTRUMENT_TYPE = {
    "stock": {"EQUITY"},
    "etf": {"ETF", "MUTUALFUND", "FUND"},
    "fund": {"ETF", "MUTUALFUND", "FUND"},
}


def resolve_ticker(isin: str, instrument_type: str) -> Optional[str]:
    allowed_quote_types = QUOTE_TYPES_BY_INSTRUMENT_TYPE.get(instrument_type, {"EQUITY"})
    try:
        response = requests.get(
            YAHOO_SEARCH_URL,
            params={"q": isin, "quotesCount": 8, "newsCount": 0},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        quotes = response.json().get("quotes", [])
    except Exception:
        return None
    for quote in quotes:
        if str(quote.get("quoteType") or "").upper() in allowed_quote_types and quote.get("symbol"):
            return str(quote["symbol"]).upper()
    return None


def _percent_change(current: Optional[float], reference: Optional[float]) -> Optional[float]:
    if current is None or reference is None or reference == 0:
        return None
    return ((current / reference) - 1.0) * 100.0


def _value_on_or_before(close: "pd.Series", target: datetime) -> Optional[float]:
    eligible = close[close.index <= pd.Timestamp(target)]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1])


def _last_signal_char(buy: "pd.Series", sell: "pd.Series") -> str:
    """'!' = most recent crossover was a buy, '?' = sell, '-' = neither seen."""
    last_buy = buy[buy].index[-1] if buy.any() else None
    last_sell = sell[sell].index[-1] if sell.any() else None
    if last_buy is None and last_sell is None:
        return "-"
    if last_buy is None:
        return "?"
    if last_sell is None:
        return "!"
    return "!" if last_buy > last_sell else "?"


def _compute_ta_quantity(history: "pd.DataFrame") -> Optional[str]:
    """3-char QUANTITY code (e.g. "101", "-02") from MACD+Stochastic
    crossovers, or None if there isn't enough history. Mirrors
    csv_analyse_watchlist.py's _compute_ta_signal_with_source +
    _map_ta_signal_for_display + _apply_ta_signal's "insert a 0" formatting."""
    if history.empty:
        return None

    cutoff = pd.Timestamp(datetime.today() - timedelta(days=TA_LOOKBACK_DAYS))
    index = history.index.tz_localize(None) if history.index.tz is not None else history.index
    windowed = history.set_axis(index, axis=0)
    windowed = windowed[windowed.index >= cutoff]

    close = windowed["Close"].dropna()
    high = windowed["High"]
    low = windowed["Low"]
    if len(close) < TA_MACD_SLOW + TA_MACD_SIGNAL + 5:
        return None

    ema_fast = close.ewm(span=TA_MACD_FAST, adjust=False, min_periods=TA_MACD_FAST).mean()
    ema_slow = close.ewm(span=TA_MACD_SLOW, adjust=False, min_periods=TA_MACD_SLOW).mean()
    macd_line = ema_fast - ema_slow
    macd_sig = macd_line.ewm(span=TA_MACD_SIGNAL, adjust=False, min_periods=TA_MACD_SIGNAL).mean()
    macd_buy = ((macd_line > macd_sig) & (macd_line.shift(1) <= macd_sig.shift(1))).fillna(False)
    macd_sell = ((macd_line < macd_sig) & (macd_line.shift(1) >= macd_sig.shift(1))).fillna(False)

    ll = low.rolling(window=TA_STOCH_K, min_periods=TA_STOCH_K).min()
    hh = high.rolling(window=TA_STOCH_K, min_periods=TA_STOCH_K).max()
    denom = (hh - ll).replace(0, pd.NA)
    fast_k = 100 * (close - ll) / denom
    stoch_k = fast_k.rolling(window=TA_STOCH_SMOOTH, min_periods=TA_STOCH_SMOOTH).mean()
    stoch_d = stoch_k.rolling(window=TA_STOCH_D, min_periods=TA_STOCH_D).mean()
    stoch_buy = ((stoch_k > stoch_d) & (stoch_k.shift(1) <= stoch_d.shift(1))).fillna(False)
    stoch_sell = ((stoch_k < stoch_d) & (stoch_k.shift(1) >= stoch_d.shift(1))).fillna(False)

    raw_signal = _last_signal_char(macd_buy, macd_sell) + _last_signal_char(stoch_buy, stoch_sell)
    mapped = raw_signal.translate(str.maketrans({"!": "1", "?": "2"}))
    return f"{mapped[0]}0{mapped[1:]}"


def fetch_return_fields(isin: str, instrument_type: str = "stock") -> dict:
    """instrument_type: "stock" | "etf" | "fund". Never raises — a failed
    lookup just leaves fields empty for the caller to fall back on the next
    scheduled full enrichment."""
    result: dict = {
        "ticker": None,
        "return_1d": None,
        "return_1m": None,
        "return_1y": None,
        "quantity": None,
        "error": "",
    }

    ticker = resolve_ticker(isin, instrument_type)
    if not ticker:
        result["error"] = "no Yahoo ticker found"
        return result
    result["ticker"] = ticker

    try:
        history = yf.Ticker(ticker).history(period="1y", interval="1d")
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if history.empty:
        result["error"] = "no price history"
        return result

    result["quantity"] = _compute_ta_quantity(history)

    close = history["Close"].dropna()
    if close.empty:
        result["error"] = "no price history"
        return result
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)

    current_price = float(close.iloc[-1])
    previous_close = float(close.iloc[-2]) if len(close) >= 2 else None
    month_reference = _value_on_or_before(close, datetime.today() - timedelta(days=30))
    year_reference = _value_on_or_before(close, datetime.today() - timedelta(days=365))

    result["return_1d"] = _percent_change(current_price, previous_close)
    result["return_1m"] = _percent_change(current_price, month_reference)
    result["return_1y"] = _percent_change(current_price, year_reference)
    return result


def upsert_symbol_map(path: Path, isin: str, symbol: str, name: str, instrument_type: str = "equity") -> bool:
    """Append a new ISIN->Yahoo-symbol entry to isin_symbol_candidates.yaml —
    the file server_app/history.py reads for the dashboard's "price history"
    chart — so that chart works immediately for a newly added instrument
    instead of waiting for the offline batch script that normally populates
    this file. A no-op if the ISIN is already mapped (that entry may be
    OPENFIGI/justETF-sourced and more authoritative — leave it alone rather
    than fight over which one wins on a later re-parse). Returns whether it
    wrote anything.
    """
    isin = isin.strip().upper()
    key_line = f"{isin}:"
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.rstrip("\n") == key_line:
                    return False
    except OSError:
        return False

    entry_text = yaml.safe_dump(
        {
            isin: {
                "instrument_type": instrument_type,
                "name": name or "",
                "note": "",
                "source": "YAHOO_SEARCH",
                "symbol": symbol,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        },
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry_text)
    return True
