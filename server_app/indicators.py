"""Technical indicators for the dashboard's price-history chart.

The parameters and formulas match the TA signal the batch pipeline and
server_app/yahoo_lookup.py compute (MACD 12/26/9, Slow Stochastic 14/3/3), so
the lines drawn here are the ones that signal is derived from.
"""

from __future__ import annotations

import pandas as pd

EMA_SPAN = 20
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
STOCH_K, STOCH_SMOOTH, STOCH_D = 14, 3, 3


def ema(close: pd.Series, span: int = EMA_SPAN) -> pd.Series:
    """Exponential moving average: alpha = 2 / (span + 1), blank until `span` values exist."""
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def macd(close: pd.Series) -> pd.DataFrame:
    """MACD line (EMA12 - EMA26), its signal line (EMA9 of the MACD line) and the histogram between them."""
    line = ema(close, MACD_FAST) - ema(close, MACD_SLOW)
    signal = line.ewm(span=MACD_SIGNAL, adjust=False, min_periods=MACD_SIGNAL).mean()
    return pd.DataFrame({"MACD": line, "Signal": signal, "Histogram": line - signal})


def stochastic(close: pd.Series, high: pd.Series, low: pd.Series) -> pd.DataFrame:
    """Slow Stochastic: %K is the 3-day mean of the raw 14-day %K, %D the 3-day mean of %K."""
    lowest = low.rolling(window=STOCH_K, min_periods=STOCH_K).min()
    highest = high.rolling(window=STOCH_K, min_periods=STOCH_K).max()
    raw_k = 100 * (close - lowest) / (highest - lowest).where(lambda span: span != 0)
    k = raw_k.rolling(window=STOCH_SMOOTH, min_periods=STOCH_SMOOTH).mean()
    d = k.rolling(window=STOCH_D, min_periods=STOCH_D).mean()
    return pd.DataFrame({"%K": k, "%D": d})


def crossovers(line: pd.Series, signal: pd.Series) -> pd.DataFrame:
    """Where `line` crosses `signal`, as rows (date, value, kind): "buy" when it moves
    above, "sell" when it moves below. Same rule as the TA signal in yahoo_lookup.py
    and the batch pipeline; a blank (NaN) on either day is never a cross."""
    previous_line, previous_signal = line.shift(1), signal.shift(1)
    buy = (line > signal) & (previous_line <= previous_signal)
    sell = (line < signal) & (previous_line >= previous_signal)
    frame = pd.concat([
        pd.DataFrame({"value": line[buy], "kind": "buy"}),
        pd.DataFrame({"value": line[sell], "kind": "sell"}),
    ])
    frame.index.name = "date"
    return frame.sort_index().reset_index()
