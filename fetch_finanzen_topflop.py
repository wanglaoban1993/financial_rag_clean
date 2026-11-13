"""Fetch Top tables for stock indices from finanzen.net.

Outputs one XLSX file with one sheet per index.
Optionally writes a combined CSV with --csv.
"""

import argparse
import contextlib
import fcntl
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yaml
import prev_close_cache
import quote_cache
import target_cache
import us_market
from lxml import etree
from config_loader import resolve_config_path
from csv_analyse_watchlist import (
    DISPLAY_CURRENCIES,
    _convert_history_to_eur,
    _convert_price_to_currency,
    _convert_price_to_eur_at_date,
    _normalize_price_currency,
    fetch_chartmill_target_data,
    fetch_onvista_fields,
    _is_target_price_plausible,
)

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_URL = "https://www.finanzen.net"
TOP_N = 15  # rows kept per index; finanzen.net pages list 10-25 rows, so smaller indices yield fewer
INDEX_SLUGS = {
    "DAX": "dax",
    "MDAX": "mdax",
    "TecDAX": "tecdax",
    "SDAX": "sdax",
    "Dow Jones": "dow_jones",
    "NASDAQ 100": "nasdaq_100",
    "S&P 500": "s%26p_500",
    "S&P 400": "s%26p_400_midcap",
    "S&P 600": "s%26p_600_small_cap",
    "EURO STOXX 50": "euro_stoxx_50",
    "SMI": "smi",
    "Nikkei 225": "nikkei_225",
}
INDEX_URL_PATHS = {
    "S&P 400": ["index/s%26p_400_midcap/topflop", "index/s%26p_400_midcap"],
    "S&P 600": ["index/s%26p_600_small_cap/topflop", "index/s%26p_600_small_cap"],
}
ISHARES_INDEX_FUNDS = {
    "S&P 400": {"ticker": "IJH", "portfolio_id": "239763"},
    "S&P 600": {"ticker": "IJR", "portfolio_id": "239774"},
}
US_FINANZEN_INDICES = {"Dow Jones", "NASDAQ 100", "S&P 500"}
INDEX_ALIASES = {
    "da": "DAX",
    "dax": "DAX",
    "mdax": "MDAX",
    "tecdax": "TecDAX",
    "tec dax": "TecDAX",
    "sdax": "SDAX",
    "dow": "Dow Jones",
    "dow jones": "Dow Jones",
    "dow_jones": "Dow Jones",
    "nasdaq": "NASDAQ 100",
    "nasdaq 100": "NASDAQ 100",
    "nasdaq_100": "NASDAQ 100",
    "s&p": "S&P 500",
    "s&p 500": "S&P 500",
    "sp500": "S&P 500",
    "s&p_500": "S&P 500",
    "s&p 400": "S&P 400",
    "sp400": "S&P 400",
    "s&p_400": "S&P 400",
    "s&p 400 midcap": "S&P 400",
    "s&p 600": "S&P 600",
    "sp600": "S&P 600",
    "s&p_600": "S&P 600",
    "s&p 600 smallcap": "S&P 600",
    "eurostoxx": "EURO STOXX 50",
    "eurostoxx 50": "EURO STOXX 50",
    "euro stoxx": "EURO STOXX 50",
    "euro stoxx 50": "EURO STOXX 50",
    "euro_stoxx_50": "EURO STOXX 50",
    "smi": "SMI",
    "nikkei": "Nikkei 225",
    "nikkei 225": "Nikkei 225",
    "nikkei_225": "Nikkei 225",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{BASE_URL}/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}
ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{10}\b")
NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d{3})*(?:,\d+)?|[+-]?\d+(?:\.\d+)?")
OUTPUT_COLUMNS = [
    "Index",
    "Rank",
    "Name",
    "ISIN",
    "Bid",
    "Bid Time",
    "Ask",
    "Ask Time",
    "DEV",
    "% Differenz zum Vortageskurs",
    "% Differenz ein Monat",
    "% Differenz ein Jahr",
    "Pot",
    "Pot %",
    "TMP",
    "Diff Basis",
]
DIFF_BASIS_COLUMN = "Diff Basis"
# The Top list only: the day change as the ranking source reported it (the
# finanzen.net "+/- %" column, EODHD's change_p for S&P 400/600), kept next to
# the recomputed "% Differenz zum Vortageskurs" so the two can be compared, and
# a flag when they are further apart than DIFF_FLAG_PP percentage points.
# Not in OUTPUT_COLUMNS: fetch_dax40.py shares that list for the INDEX files.
SOURCE_CHANGE_COLUMN = "Source %"
DIFF_FLAG_COLUMN = "Diff Flag"
CHECK_COLUMNS = [SOURCE_CHANGE_COLUMN, DIFF_FLAG_COLUMN]
DIFF_FLAG_PP = 3.0
# Indices whose exchange is closed while the fetch runs (Tokyo closes at about
# 07:00 German time). onvista quotes the German listing of those stocks, which
# keeps trading after the close, and its Ask carries a wide spread, so Ask
# against the home close is not a day change (Nikkei rows came out ~2 pp above
# the page, Advantest +6.3 % above the Tokyo close). Their day change is the
# last session's close against the one before, in the listing's own currency.
SESSION_CLOSE_INDICES = {"Nikkei 225"}
PERIOD_CHANGE_DAYS = {
    "% Differenz zum Vortageskurs": 1,
    "% Differenz ein Monat": 30,
    "% Differenz ein Jahr": 365,
}
TOP_TABLE_COLUMNS = {"Name ISIN", "Letzter Vortag", "+/- %", "Zeit Datum"}
EODHD_BASE_URL = "https://eodhd.com/api"
ISHARES_DOWNLOAD_URL = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
)
CONFIG_PATH = resolve_config_path()
EODHD_US_ISIN_CACHE_PATH = SCRIPT_DIR / "data" / "eodhd_us_isin_map.json"
SYMBOL_OVERRIDES_PATH = SCRIPT_DIR / "isin_symbol_overrides.yaml"
SYMBOL_CANDIDATES_PATH = SCRIPT_DIR / "isin_symbol_candidates.yaml"
YAHOO_TARGET_CACHE: dict[str, dict] = {}
EODHD_HISTORY_CACHE: dict[str, pd.DataFrame] = {}
YAHOO_HISTORY_CACHE: dict[str, pd.DataFrame] = {}
ISIN_SYMBOL_MAP_CACHE: dict[str, str] | None = None


def make_session():
    if curl_requests is not None:
        session = curl_requests.Session(impersonate="chrome")
        session._finanzen_client = "curl_cffi"
    else:
        session = requests.Session()
        session._finanzen_client = "requests"

    try:
        # finanzen.net may set a bot-information cookie even when the home page
        # itself answers 403. Keeping that cookie allows index pages to load.
        session.get(BASE_URL + "/", headers=HEADERS, timeout=15)
    except Exception:
        pass
    return session


def load_eodhd_api_key() -> str:
    env_key = os.getenv("EODHD_API_KEY", "").strip()
    if env_key:
        return env_key
    if not CONFIG_PATH.exists():
        return ""
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except Exception:
        return ""
    return str(config.get("market", {}).get("eodhd_api_key", "") or "").strip()


def extract_isin(value: object) -> str:
    match = ISIN_RE.search(str(value or "").upper())
    return match.group(0) if match else ""


def _symbol_from_mapping_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("symbol") or "").strip()
    return ""


def normalize_mapped_yahoo_symbol(symbol: object) -> str:
    text = str(symbol or "").strip()
    if not text or text == "-":
        return ""
    text = text.replace("/", "-").replace(" ", "-").upper()
    if re.fullmatch(r".+\.[A-Z]{1,5}", text):
        return text
    return text.replace(".", "-")


def _load_symbol_map_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    mapping: dict[str, str] = {}
    for raw_isin, value in raw.items():
        isin = extract_isin(raw_isin)
        symbol = _symbol_from_mapping_value(value)
        if isin and symbol:
            mapping[isin] = normalize_mapped_yahoo_symbol(symbol)
    return mapping


def load_isin_symbol_map() -> dict[str, str]:
    global ISIN_SYMBOL_MAP_CACHE
    if ISIN_SYMBOL_MAP_CACHE is not None:
        return ISIN_SYMBOL_MAP_CACHE

    mapping = _load_symbol_map_file(SYMBOL_CANDIDATES_PATH)
    mapping.update(_load_symbol_map_file(SYMBOL_OVERRIDES_PATH))
    ISIN_SYMBOL_MAP_CACHE = mapping
    return mapping


def index_urls(index_name: str) -> list[str]:
    paths = INDEX_URL_PATHS.get(index_name, [f"index/{INDEX_SLUGS[index_name]}/topflop"])
    return [f"{BASE_URL}/{path}" for path in paths]


def fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, headers=HEADERS, timeout=20)
    if response.status_code == 403:
        session.get(BASE_URL + "/", headers=HEADERS, timeout=15)
        response = session.get(url, headers=HEADERS, timeout=20)
    if response.status_code >= 400:
        client = getattr(session, "_finanzen_client", "requests")
        detail = re.sub(r"\s+", " ", response.text or "").strip()[:220]
        extra = ""
        if response.status_code == 403 and client == "requests":
            extra = " Install curl_cffi or run `pip install -r requirements.txt` to use browser impersonation."
        raise RuntimeError(
            f"Failed to fetch {url}: HTTP {response.status_code} using {client}.{extra} "
            f"Response preview: {detail}"
        )
    return response.text


def to_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def split_numeric_pair(value: object) -> tuple[float | None, float | None]:
    numbers = NUMBER_RE.findall(str(value or ""))
    if len(numbers) < 2:
        return None, None
    return to_float(numbers[-2]), to_float(numbers[-1])


def split_name_isin(value: object) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    match = ISIN_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()].strip(), match.group(0)


def split_time_date(value: object) -> tuple[str, str]:
    parts = str(value or "").strip().split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    if parts:
        return parts[0], ""
    return "", ""


def normalize_us_symbol_for_eodhd(symbol: object) -> str:
    text = str(symbol or "").strip()
    if not text or text == "-":
        return ""
    text = text.replace("/", "-").replace(".", "-").replace(" ", "-")
    return f"{text}.US"


def normalize_us_symbol_for_yahoo(symbol: object) -> str:
    text = str(symbol or "").strip()
    if not text or text == "-":
        return ""
    return text.replace("/", "-").replace(".", "-").replace(" ", "-")


def ticker_key(symbol: object) -> str:
    return normalize_us_symbol_for_yahoo(symbol).upper()


def load_cached_us_isin_map(max_age_hours: int = 24) -> dict[str, str]:
    if not EODHD_US_ISIN_CACHE_PATH.exists():
        return {}
    try:
        with EODHD_US_ISIN_CACHE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        fetched_at = datetime.fromisoformat(str(payload.get("fetched_at")))
        if datetime.now() - fetched_at > timedelta(hours=max_age_hours):
            return {}
        data = payload.get("data", {})
        if isinstance(data, dict):
            return {str(key).upper(): str(value) for key, value in data.items() if value}
    except Exception:
        return {}
    return {}


def save_us_isin_map(isin_map: dict[str, str]) -> None:
    EODHD_US_ISIN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "data": dict(sorted(isin_map.items())),
    }
    with EODHD_US_ISIN_CACHE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def fetch_us_isin_map(api_key: str) -> dict[str, str]:
    cached = load_cached_us_isin_map()
    if cached:
        return cached
    if not api_key:
        return {}
    try:
        response = requests.get(
            f"{EODHD_BASE_URL}/exchange-symbol-list/US",
            params={"api_token": api_key, "fmt": "json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"Warning: EODHD US symbol list failed; ISIN values may be empty: {redact_api_token(exc)}")
        return {}

    isin_map = {}
    for item in data or []:
        code = ticker_key(item.get("Code"))
        isin = str(item.get("Isin") or "").strip().upper()
        if code and ISIN_RE.fullmatch(isin):
            isin_map[code] = isin
    save_us_isin_map(isin_map)
    return isin_map


def parse_ishares_holdings_xml(content: bytes, index_name: str, source_url: str) -> pd.DataFrame:
    root = etree.fromstring(content, parser=etree.XMLParser(recover=True))
    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    holdings_sheet = None
    for worksheet in root.findall("ss:Worksheet", ns):
        name = worksheet.get("{urn:schemas-microsoft-com:office:spreadsheet}Name")
        if name == "Holdings":
            holdings_sheet = worksheet
            break
    if holdings_sheet is None:
        raise ValueError("iShares holdings workbook has no Holdings sheet")

    rows = []
    for row in holdings_sheet.findall(".//ss:Row", ns):
        values = []
        for cell in row.findall("ss:Cell", ns):
            data = cell.find("ss:Data", ns)
            values.append("" if data is None or data.text is None else data.text.strip())
        rows.append(values)

    header_idx = next((idx for idx, row in enumerate(rows) if row[:2] == ["Ticker", "Name"]), None)
    if header_idx is None:
        raise ValueError("iShares holdings workbook has no Ticker/Name header")

    header = rows[header_idx]
    data_rows = [row for row in rows[header_idx + 1 :] if len(row) >= len(header)]
    holdings = pd.DataFrame(data_rows, columns=header)
    if "Type" in holdings.columns:
        equity_mask = holdings["Type"].astype(str).str.upper().eq("EQUITY")
    elif "Asset Class" in holdings.columns:
        equity_mask = holdings["Asset Class"].astype(str).str.upper().eq("EQUITY")
    else:
        equity_mask = pd.Series(True, index=holdings.index)
    holdings = holdings[equity_mask & holdings["Ticker"].astype(str).str.strip().ne("")]
    holdings = holdings[holdings["Ticker"].astype(str).str.strip().ne("-")]
    holdings = holdings.drop_duplicates(subset=["Ticker"]).reset_index(drop=True)
    holdings["Index"] = index_name
    holdings["SourceUrl"] = source_url
    return holdings


def fetch_ishares_holdings(session: requests.Session, index_name: str) -> pd.DataFrame:
    fund = ISHARES_INDEX_FUNDS[index_name]
    params = {
        "appType": "PRODUCT_PAGE",
        "appSubType": "ISHARES",
        "targetSite": "us-ishares",
        "locale": "en_US",
        "portfolioId": fund["portfolio_id"],
        "component": "fundDownload",
        "userType": "individual",
    }
    response = session.get(ISHARES_DOWNLOAD_URL, params=params, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return parse_ishares_holdings_xml(response.content, index_name, response.url)


def redact_api_token(error: object) -> str:
    """Error text with any `api_token=...` query value masked (requests puts the full URL in it)."""
    return re.sub(r"(api_token=)[^&\s'\")]+", r"\1***", str(error))


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[idx : idx + size] for idx in range(0, len(values), size)]


def fetch_eodhd_quotes(symbols: list[str], api_key: str) -> dict[str, dict]:
    quotes = {}
    if not api_key:
        return quotes
    session = requests.Session()
    for group in chunks(symbols, 20):
        if not group:
            continue
        params = {"api_token": api_key, "fmt": "json"}
        if len(group) > 1:
            params["s"] = ",".join(group[1:])
        try:
            response = session.get(f"{EODHD_BASE_URL}/real-time/{group[0]}", params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            print(f"Warning: EODHD quote batch failed for {group[0]}...: {redact_api_token(exc)}")
            continue
        if isinstance(data, dict):
            data = [data]
        for item in data or []:
            code = str(item.get("code") or "").strip()
            if code:
                quotes[code] = item
    return quotes


def fetch_yahoo_quote(symbol: str) -> dict | None:
    if yf is None or not symbol:
        return None
    try:
        ticker = yf.Ticker(symbol)
        fast_info = ticker.fast_info
        last = fast_info.get("last_price")
        previous = fast_info.get("previous_close")
    except Exception:
        return None
    if last is None or previous in (None, 0):
        return None
    change_abs = float(last) - float(previous)
    return {
        "close": float(last),
        "previousClose": float(previous),
        "change": change_abs,
        "change_p": change_abs / float(previous) * 100,
    }


HISTORY_CACHE_DIR = Path(os.environ.get("HISTORY_CACHE_DIR") or Path(__file__).resolve().parent / "csv_update" / "_history_cache")
# A cached history is reused for the rest of the day it was fetched on. When a
# fresh fetch fails (Yahoo rate limit, EODHD 402), an older one up to this many
# days is better than leaving the % Differenz columns empty.
HISTORY_CACHE_STALE_DAYS = 7
# A history fetched today that does not yet contain the previous session's close
# (Yahoo sometimes returns the series without it) would make the Vortageskurs
# reference a day old for the rest of the day. It is refetched once it is this
# old; the wait keeps a market holiday, when the close never appears, from
# refetching ~700 symbols on every run.
HISTORY_CACHE_RECHECK = timedelta(hours=2)

def _history_cache_path(source: str, symbol: str) -> Path:
    return HISTORY_CACHE_DIR / f"{source}_{re.sub(r'[^A-Za-z0-9._-]', '_', symbol)}.csv"


def _previous_weekday(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _lacks_previous_session(frame: pd.DataFrame, now: datetime) -> bool:
    """True when the history has no row for the last weekday ahead of today.

    Checking for that row, not just for a last date that is recent enough: a
    history fetched during the day ends with today's partial bar, which would
    hide a missing previous close. Market holidays look the same; the recheck
    interval in read_history_cache keeps that from costing a refetch on every
    run."""
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna().dt.date
    return _previous_weekday(now.date()) not in set(dates)


def read_history_cache(source: str, symbol: str, allow_stale: bool = False) -> pd.DataFrame | None:
    path = _history_cache_path(source, symbol)
    try:
        fetched = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
    now = datetime.now()
    age = now.date() - fetched.date()
    if age.days > (HISTORY_CACHE_STALE_DAYS if allow_stale else 0):
        return None
    try:
        frame = pd.read_csv(path, parse_dates=["date"])
    except Exception:
        return None
    if frame.empty or not {"date", "close"} <= set(frame.columns):
        return None
    if not allow_stale and now - fetched > HISTORY_CACHE_RECHECK and _lacks_previous_session(frame, now):
        return None
    return frame


def write_history_cache(source: str, symbol: str, frame: pd.DataFrame) -> None:
    path = _history_cache_path(source, symbol)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        frame[["date", "close"]].to_csv(temp, index=False)
        temp.replace(path)
    except OSError as exc:
        print(f"Warning: could not cache {source} history for {symbol}: {exc}")


# The iShares-based US indices cost one EODHD call per constituent (~3,000 per
# run for all three), so they are not refetched while the US market is closed:
# the quotes would only repeat the previous close. The skipped index keeps its
# last rows.
INDEX_FRAME_MAX_AGE = timedelta(days=5)  # longer than any weekend + holiday gap
INDEX_FRAME_CACHE_DIR = HISTORY_CACHE_DIR / "index_frames"


def _index_frame_path(index_name: str) -> Path:
    return INDEX_FRAME_CACHE_DIR / f"{re.sub(r'[^A-Za-z0-9._-]', '_', index_name)}.pkl"


def write_index_frame_cache(index_name: str, frame: pd.DataFrame) -> None:
    path = _index_frame_path(index_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        frame.to_pickle(temp)
        temp.replace(path)
    except OSError as exc:
        print(f"Warning: could not cache {index_name} rows: {exc}")


def reuse_cached_index(index_name: str, now: datetime | None = None) -> tuple[pd.DataFrame, str] | None:
    """(last rows, why they are reused) for an iShares index whose refetch would
    spend EODHD calls for nothing, or None when it should be fetched."""
    if index_name not in ISHARES_INDEX_FUNDS:
        return None
    path = _index_frame_path(index_name)
    try:
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime)
        frame = pd.read_pickle(path)
    except Exception:
        return None
    now = now or datetime.now()
    age = now - fetched_at
    if frame.empty or age > INDEX_FRAME_MAX_AGE:
        return None
    if not us_market.is_open(now.astimezone()):
        return frame.head(TOP_N), "US market closed"
    return None


def fetch_eodhd_history(symbol: str, api_key: str, days: int = 400) -> pd.DataFrame:
    if not symbol or not api_key:
        return pd.DataFrame()
    if symbol in EODHD_HISTORY_CACHE:
        return EODHD_HISTORY_CACHE[symbol]
    cached = read_history_cache("eodhd", symbol)
    if cached is not None:
        EODHD_HISTORY_CACHE[symbol] = cached
        return cached
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        response = requests.get(
            f"{EODHD_BASE_URL}/eod/{symbol}",
            params={"api_token": api_key, "fmt": "json", "from": from_date, "order": "a"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"Warning: EODHD history failed for {symbol}: {redact_api_token(exc)}")
        stale = read_history_cache("eodhd", symbol, allow_stale=True)
        return stale if stale is not None else pd.DataFrame()
    if not data or isinstance(data, dict):
        return pd.DataFrame()
    frame = pd.DataFrame(data)
    if "date" not in frame.columns:
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame.get("adjusted_close", frame.get("close")), errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    EODHD_HISTORY_CACHE[symbol] = frame
    write_history_cache("eodhd", symbol, frame)
    return frame


def fetch_yahoo_history(symbol: str, days: int = 400) -> pd.DataFrame:
    if yf is None or not symbol:
        return pd.DataFrame()
    if symbol in YAHOO_HISTORY_CACHE:
        return YAHOO_HISTORY_CACHE[symbol]
    cached = read_history_cache("yahoo", symbol)
    if cached is not None:
        YAHOO_HISTORY_CACHE[symbol] = cached
        return cached
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            hist = yf.Ticker(symbol).history(period=f"{days}d", auto_adjust=True)
    except Exception as exc:
        print(f"Warning: Yahoo history failed for {symbol}: {exc}")
        stale = read_history_cache("yahoo", symbol, allow_stale=True)
        return stale if stale is not None else pd.DataFrame()
    if hist is None or hist.empty or "Close" not in hist.columns:
        stale = read_history_cache("yahoo", symbol, allow_stale=True)
        return stale if stale is not None else pd.DataFrame()
    frame = hist.reset_index()
    date_column = "Date" if "Date" in frame.columns else frame.columns[0]
    frame["date"] = pd.to_datetime(frame[date_column], errors="coerce").dt.tz_localize(None)
    frame["close"] = pd.to_numeric(frame["Close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    YAHOO_HISTORY_CACHE[symbol] = frame
    write_history_cache("yahoo", symbol, frame)
    return frame


def calculate_history_period_change(
    history: pd.DataFrame,
    current_price: float | None,
    days: int,
    current_currency: str,
    history_currency: str,
    use_latest_close: bool = False,
    keep_today_bar: bool = False,
) -> float | None:
    # use_latest_close measures from the history's own latest close instead of
    # current_price (close-to-close, as the Monat/Jahr columns are defined).
    # Yahoo's history ends with today's unfinished bar while the market is open
    # (EODHD's does not), and the per-day history cache would freeze it, so the
    # latest close is the last row dated before today. keep_today_bar is for
    # exchanges that have already closed for today when the fetch runs (Tokyo).
    if history.empty or not history_currency:
        return None
    if not use_latest_close and (current_price is None or not current_currency):
        return None
    target_date = pd.Timestamp(datetime.now() - timedelta(days=days))
    history_dates = pd.to_datetime(history["date"], errors="coerce").dt.tz_localize(None)
    closes = pd.Series(pd.to_numeric(history["close"], errors="coerce").to_numpy(), index=history_dates)
    euro_closes = _convert_history_to_eur(closes, history_currency).dropna().sort_index()
    eligible = euro_closes.loc[euro_closes.index <= target_date]
    if eligible.empty:
        eligible = euro_closes.head(1)
    reference = to_float(eligible.iloc[-1]) if not eligible.empty else None
    if reference in (None, 0):
        return None
    if use_latest_close:
        completed = euro_closes
        if not keep_today_bar:
            completed = euro_closes.loc[pd.DatetimeIndex(euro_closes.index).normalize() < pd.Timestamp(datetime.now().date())]
        current_eur = to_float(completed.iloc[-1]) if not completed.empty else None
    else:
        current_eur = _convert_price_to_eur_at_date(current_price, current_currency, datetime.now())
    if current_eur is None:
        return None
    return (current_eur / reference - 1) * 100


def currency_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none"} else text.upper()


YAHOO_SUFFIX_CURRENCY = {
    ".DE": "EUR", ".F": "EUR", ".PA": "EUR", ".MI": "EUR", ".AS": "EUR", ".MC": "EUR",
    ".BR": "EUR", ".VI": "EUR", ".HE": "EUR", ".LS": "EUR", ".IR": "EUR",
    ".L": "GBp", ".SW": "CHF",
}
YAHOO_HISTORY_CURRENCY_CACHE: dict[str, str] = {}


def yahoo_history_currency(symbol: str) -> str:
    """Currency of a Yahoo symbol's price history, "" when it cannot be told.
    The history is in the currency of that listing, which is not necessarily the
    currency of the onvista quote (OII trades in USD on Yahoo but onvista often
    quotes the German EUR listing) nor of the analyst target."""
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return ""
    for suffix, currency in YAHOO_SUFFIX_CURRENCY.items():
        if symbol.endswith(suffix):
            return currency
    if "." not in symbol:
        return "USD"  # a bare ticker is the US listing
    if symbol not in YAHOO_HISTORY_CURRENCY_CACHE:
        currency = ""
        if yf is not None:
            try:
                currency = str(yf.Ticker(symbol).fast_info.get("currency") or "")
            except Exception:
                currency = ""
        YAHOO_HISTORY_CURRENCY_CACHE[symbol] = currency
    return YAHOO_HISTORY_CURRENCY_CACHE[symbol]


def listing_currency(eodhd_symbol: str, yahoo_symbol: str) -> str:
    """Currency of the listing the symbols point at, "" when unknown."""
    if eodhd_symbol.upper().endswith(".US"):
        return "USD"
    return yahoo_history_currency(yahoo_symbol)


def calculate_period_change(
    last: float | None,
    days: int,
    eodhd_symbol: str,
    yahoo_symbol: str,
    api_key: str,
    current_currency: str,
    history_currency: str,
    use_latest_close: bool = False,
    keep_today_bar: bool = False,
) -> float | None:
    # history_currency is only the caller's fallback: each history is in the
    # currency of the listing it comes from, which the symbol tells.
    if eodhd_symbol:
        value = calculate_history_period_change(
            fetch_eodhd_history(eodhd_symbol, api_key),
            last,
            days,
            current_currency,
            listing_currency(eodhd_symbol, "") or history_currency,
            use_latest_close,
            keep_today_bar,
        )
        if value is not None:
            return value
    if yahoo_symbol:
        return calculate_history_period_change(
            fetch_yahoo_history(yahoo_symbol),
            last,
            days,
            current_currency,
            yahoo_history_currency(yahoo_symbol) or history_currency,
            use_latest_close,
            keep_today_bar,
        )
    return None


def calculate_last_session_change(eodhd_symbol: str, yahoo_symbol: str, api_key: str) -> float | None:
    """Latest close against the one before it, in percent. Both come from the
    same history, so no currency conversion is involved."""
    histories = []
    if eodhd_symbol:
        histories.append(fetch_eodhd_history(eodhd_symbol, api_key))
    if yahoo_symbol:
        histories.append(fetch_yahoo_history(yahoo_symbol))
    for history in histories:
        if history.empty:
            continue
        closes = pd.to_numeric(history.sort_values("date")["close"], errors="coerce").dropna()
        if len(closes) >= 2 and closes.iloc[-2] != 0:
            return float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100)
    return None


def add_diff_flag(frame: pd.DataFrame) -> pd.DataFrame:
    """Flag rows whose recomputed day change is far from the source's own."""
    if SOURCE_CHANGE_COLUMN not in frame.columns:
        frame[SOURCE_CHANGE_COLUMN] = None
    ours = pd.to_numeric(frame.get("% Differenz zum Vortageskurs"), errors="coerce")
    source = pd.to_numeric(frame[SOURCE_CHANGE_COLUMN], errors="coerce")
    flags = []
    for mine, theirs in zip(ours, source):
        if pd.isna(theirs):
            flags.append("")
        elif pd.isna(mine):
            flags.append("?")  # the source has a value, the recomputation does not
        else:
            flags.append("!" if abs(mine - theirs) > DIFF_FLAG_PP else "")
    frame[DIFF_FLAG_COLUMN] = flags
    return frame


def latest_completed_close(history: pd.DataFrame, keep_today_bar: bool = False) -> tuple[str, float] | None:
    """(date, close) of the last row dated before today, in the history's own currency; the same
    row calculate_history_period_change takes as the previous close."""
    if history is None or history.empty or "close" not in history.columns:
        return None
    dates = pd.to_datetime(history["date"], errors="coerce").dt.tz_localize(None)
    closes = pd.to_numeric(history["close"], errors="coerce")
    rows = pd.DataFrame({"date": dates, "close": closes}).dropna()
    if not keep_today_bar:
        rows = rows[rows["date"].dt.normalize() < pd.Timestamp(datetime.now().date())]
    if rows.empty:
        return None
    last = rows.sort_values("date").iloc[-1]
    return last["date"].date().isoformat(), float(last["close"])


def previous_close_details(
    eodhd_symbol: str, yahoo_symbol: str, api_key: str, history_currency: str, keep_today_bar: bool = False
) -> dict | None:
    """The raw previous close (value, currency, date, source) behind the day change. Reads the
    histories calculate_period_change already fetched and cached today, so it costs no request."""
    if eodhd_symbol:
        found = latest_completed_close(fetch_eodhd_history(eodhd_symbol, api_key), keep_today_bar)
        currency = listing_currency(eodhd_symbol, "") or history_currency
        if found and currency:
            return {"value": found[1], "currency": currency, "date": found[0], "source": "EODHD"}
    if yahoo_symbol:
        found = latest_completed_close(fetch_yahoo_history(yahoo_symbol), keep_today_bar)
        currency = yahoo_history_currency(yahoo_symbol) or history_currency
        if found and currency:
            return {"value": found[1], "currency": currency, "date": found[0], "source": "Yahoo"}
    return None


def remember_previous_close(
    references: dict[str, dict],
    isin: object,
    current_price: float | None,
    currency: str,
    change: float | None,
    close: dict | None = None,
) -> None:
    """Keep the EUR close the day change was measured against, so that
    refresh_toplist_bid_ask.py can recompute it from a fresh Ask. It is the exact inverse of
    calculate_history_period_change: change = (price_eur / reference - 1) * 100."""
    isin = extract_isin(isin)
    if not isin or change is None or current_price is None or change <= -100:
        return
    price_eur = _convert_price_to_eur_at_date(current_price, currency, datetime.now())
    if price_eur is None or price_eur <= 0:
        return
    prev_close_cache.remember(references, isin, price_eur / (1 + change / 100), close=close)


def add_period_changes(frame: pd.DataFrame, api_key: str) -> pd.DataFrame:
    if frame.empty:
        for column in PERIOD_CHANGE_DAYS:
            frame[column] = None
        frame[DIFF_BASIS_COLUMN] = None
        return frame
    bases = []
    session_bases: dict[int, str] = {}
    references: dict[str, dict] = {}
    for column, days in PERIOD_CHANGE_DAYS.items():
        values = []
        bases = []
        for position, (_, row) in enumerate(frame.iterrows()):
            if days == 1 and row.get("Index") in SESSION_CLOSE_INDICES:
                change = calculate_last_session_change(
                    str(row.get("EodhdSymbol") or "").strip(),
                    str(row.get("YahooSymbol") or "").strip(),
                    api_key,
                )
                basis = "Close"
                if change is None:
                    # No history: the source's own figure beats a wrong Ask-based one.
                    change, basis = to_float(row.get(SOURCE_CHANGE_COLUMN)), "Page"
                values.append(change)
                bases.append(basis if change is not None else None)
                session_bases[position] = basis
                continue
            ask = to_float(row.get("Ask"))
            bid = to_float(row.get("Bid"))
            quote_currency = currency_value(row.get("DEV"))
            # Not the quote currency: onvista often quotes another listing than
            # the one the history comes from, so an unknown history currency
            # leaves the change blank instead of assuming it matches the quote.
            history_currency = currency_value(row.get("TMP_CURRENCY"))
            # Vortageskurs: Ask, else Bid (shown in "Diff Basis"), else the page's last price.
            current_price, basis = ask, "Ask"
            current_currency = quote_currency or history_currency
            if current_price is None:
                current_price, basis = bid, "Bid"
            if current_price is None:
                current_price, basis = to_float(row.get("Last")), "Last"
                # The page/EODHD last price is in the listing's currency.
                current_currency = (
                    history_currency
                    or listing_currency(
                        str(row.get("EodhdSymbol") or "").strip(),
                        str(row.get("YahooSymbol") or "").strip(),
                    )
                    or quote_currency
                )
            bases.append(basis if current_price is not None else None)
            change = calculate_period_change(
                current_price,
                days,
                str(row.get("EodhdSymbol") or "").strip(),
                str(row.get("YahooSymbol") or "").strip(),
                api_key,
                current_currency or history_currency,
                history_currency,
                # Only Vortageskurs follows the live quote; Monat and Jahr
                # are latest close vs the close 30/365 days ago.
                use_latest_close=days > 1,
                **({"keep_today_bar": True} if days > 1 and row.get("Index") in SESSION_CLOSE_INDICES else {}),
            )
            values.append(change)
            if days == 1:
                close = None
                if change is not None:
                    close = previous_close_details(
                        str(row.get("EodhdSymbol") or "").strip(),
                        str(row.get("YahooSymbol") or "").strip(),
                        api_key,
                        history_currency,
                    )
                remember_previous_close(
                    references, row.get("ISIN"), current_price, current_currency or history_currency, change, close
                )
        frame[column] = values
        if days == 1 and references:
            try:
                prev_close_cache.save(references)
            except OSError as exc:
                print(f"Warning: previous-close cache not written: {exc}")
    # bases holds the last column's pass (the year); the day change decides what
    # "Diff Basis" says, so the rows it took from a close/page figure override it.
    for position, basis in session_bases.items():
        bases[position] = basis if to_float(frame.iloc[position]["% Differenz zum Vortageskurs"]) is not None else None
    frame[DIFF_BASIS_COLUMN] = bases
    return frame


# A real book never has Bid above Ask; onvista showed Medios at Bid 11.88 / Ask 10.46
# right after the Xetra close. A little slack covers rounding between venues.
MAX_CROSSED_RATIO = 1.02


def is_crossed(bid: float | None, ask: float | None) -> bool:
    return bid is not None and ask is not None and ask > 0 and bid > ask * MAX_CROSSED_RATIO


def add_onvista_bid_ask(frame: pd.DataFrame, session: requests.Session) -> pd.DataFrame:
    """Bid/Ask from onvista. A quote with both sides is remembered per ISIN
    (quote_cache). When a fetch gets no two-sided quote (after a German stock's
    close onvista drops its Geld/Brief lines for a while, and an Ask without a
    Bid is not trusted) the last remembered one stays, with the time it was
    read, instead of blanking the row. With nothing remembered the row keeps
    what was fetched."""
    if frame.empty or "ISIN" not in frame.columns:
        for column in ("Bid", "Ask", "Bid Time", "Ask Time", "DEV"):
            frame[column] = None
        return frame

    remembered = quote_cache.load()
    fresh_quotes: dict[str, dict] = {}
    kept = 0
    bids, asks, bid_times, ask_times, currencies = [], [], [], [], []
    for _, row in frame.iterrows():
        isin = str(row.get("ISIN") or "").strip()
        name = str(row.get("Name") or "").strip()
        bid = ask = None
        currency = ""
        if isin:
            details = fetch_onvista_fields(isin, "equity", session, wkn=None, name=name)
            bid = to_float(details.get("Bid"))
            ask = to_float(details.get("Ask"))
            # Needed to resolve Pot/Pot % to a common currency below — the
            # current price used there is this same onvista Ask, so its
            # currency has to travel with it.
            currency = str(details.get("QUOTE_CURRENCY") or "").strip().upper()
            if is_crossed(bid, ask):
                # Treated like "no quote": the last remembered one stays below.
                print(f"  Crossed quote for {isin} ({name}): Bid {bid} > Ask {ask}; ignored")
                bid = ask = None
        fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bid_time = fetched_at if bid is not None else None
        ask_time = fetched_at if ask is not None else None

        if quote_cache.is_two_sided(bid, ask):
            quote_cache.remember(
                fresh_quotes, isin, bid=bid, ask=ask, bid_time=bid_time, ask_time=ask_time, currency=currency
            )
        elif last := quote_cache.fresh_entry(remembered, isin):
            bid, ask = last["bid"], last["ask"]
            bid_time, ask_time, currency = last["bid_time"], last["ask_time"], last["currency"]
            kept += 1

        bids.append(bid)
        asks.append(ask)
        bid_times.append(bid_time)
        ask_times.append(ask_time)
        currencies.append(currency)

    if kept:
        print(f"  No live bid/ask for {kept} of {len(frame)} rows; kept their last two-sided quote")
    if fresh_quotes:
        try:
            quote_cache.save(fresh_quotes)
        except OSError as exc:
            print(f"Warning: quote cache not written: {exc}")

    frame["Bid"] = bids
    frame["Ask"] = asks
    frame["Bid Time"] = bid_times
    frame["Ask Time"] = ask_times
    frame["DEV"] = currencies
    return frame


def fetch_yahoo_target_data(symbol: str) -> dict:
    if yf is None or not symbol:
        return {}
    if symbol in YAHOO_TARGET_CACHE:
        return YAHOO_TARGET_CACHE[symbol]
    last_error = None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        last_error = exc
        try:
            info = yf.Ticker(symbol).info or {}
        except Exception as retry_exc:
            last_error = retry_exc
            info = {}
    if not info and last_error is not None:
        print(f"Warning: Yahoo target lookup failed for {symbol}: {last_error}")
    data = {
        "target_mean": to_float(info.get("targetMeanPrice")),
        # Yahoo quotes targetMeanPrice in the ticker's own listing currency,
        # which doesn't always match the onvista Ask used as the current
        # price below (e.g. a US stock's primary Yahoo listing in USD vs.
        # its onvista quote on a EUR-denominated German exchange).
        "currency": info.get("currency"),
    }
    YAHOO_TARGET_CACHE[symbol] = data
    return data


def add_yahoo_symbols_from_isin(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "ISIN" not in frame.columns:
        return frame
    if "YahooSymbol" not in frame.columns:
        frame["YahooSymbol"] = ""

    symbol_map = load_isin_symbol_map()
    if not symbol_map:
        return frame

    missing_symbol = frame["YahooSymbol"].fillna("").astype(str).str.strip().eq("")
    resolved = frame.loc[missing_symbol, "ISIN"].map(lambda isin: symbol_map.get(extract_isin(isin), ""))
    frame.loc[missing_symbol, "YahooSymbol"] = resolved
    for isin, symbol in zip(frame["ISIN"], frame["YahooSymbol"]):
        isin_text, symbol_text = extract_isin(isin), str(symbol or "").strip().upper()
        if isin_text.startswith("DE") and symbol_text and not symbol_text.endswith((".DE", ".F")):
            print(f"[symbol] WARNING {isin_text} maps to {symbol_text}, not a German listing; "
                  "fix it in isin_symbol_overrides.yaml")
    return frame


def add_yahoo_target_prices(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "YahooSymbol" not in frame.columns:
        frame["TMP"] = None
        frame["TMP_CURRENCY"] = None
        return frame

    # TMP comes from the shared cache refresh_targets.py fills at 10:00 and
    # 16:30, so it matches the other lists and only changes then; the live
    # Yahoo lookup is the fallback for ISINs the cache does not have.
    cached_targets = target_cache.load()
    isins = frame["ISIN"].fillna("") if "ISIN" in frame.columns else [""] * len(frame)
    target_prices = []
    target_currencies = []
    for symbol, isin in zip(frame["YahooSymbol"].fillna(""), isins):
        entry = target_cache.fresh_entry(cached_targets, extract_isin(isin))
        data = entry if entry else fetch_yahoo_target_data(str(symbol).strip())
        target_mean = to_float(data.get("target_mean"))
        target_prices.append(round(target_mean, 2) if target_mean is not None else None)
        target_currencies.append(data.get("currency"))
    frame["TMP"] = target_prices
    frame["TMP_CURRENCY"] = target_currencies
    return frame


def normalize_display_currency(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse the Bid/Ask currency and TMP_CURRENCY into the single DEV
    column the EQUITY/ETF tables use. A row quoted in USD or EUR keeps that
    currency; anything else is converted to EUR at today's rate. Bid, Ask and
    TMP are all expressed in the resulting DEV. Must run after
    add_period_changes/add_potential_pct, which still need the raw currencies."""
    if frame.empty or "DEV" not in frame.columns:
        return frame

    for idx, row in frame.iterrows():
        quote_currency = _normalize_price_currency(row.get("DEV"))
        tmp_currency = _normalize_price_currency(row.get("TMP_CURRENCY"))
        source_currency = quote_currency or tmp_currency
        if not source_currency:
            continue
        target_currency = source_currency if source_currency in DISPLAY_CURRENCIES else "EUR"

        def convert(value: object, from_currency: str) -> float | None:
            number = to_float(value)
            if number is None:
                return None
            if from_currency == target_currency:
                return number
            converted = _convert_price_to_currency(number, from_currency, target_currency)
            return None if converted is None else round(converted, 2)

        if quote_currency:
            for column in ("Bid", "Ask"):
                if column in frame.columns:
                    frame.at[idx, column] = convert(row.get(column), quote_currency)
        if "TMP" in frame.columns:
            frame.at[idx, "TMP"] = convert(row.get("TMP"), tmp_currency or source_currency)
        frame.at[idx, "DEV"] = target_currency
    return frame


def add_potential_pct(frame: pd.DataFrame) -> pd.DataFrame:
    """Compute Potential and Potential % from target/current values in EUR."""
    if frame.empty:
        frame["Pot"] = None
        frame["Pot %"] = None
        return frame
    if "TMP" not in frame.columns:
        frame["Pot"] = None
        frame["Pot %"] = None
        return frame

    pots, pot_pcts = [], []
    for idx, row in frame.iterrows():
        today = datetime.now()
        target_price = _convert_price_to_eur_at_date(
            row.get("TMP"), row.get("TMP_CURRENCY"), today
        )
        quote_currency = currency_value(row.get("DEV")) or currency_value(row.get("TMP_CURRENCY"))
        current_quote = to_float(row.get("Ask"))
        if current_quote is None:
            current_quote = to_float(row.get("Last"))
            quote_currency = currency_value(row.get("TMP_CURRENCY"))
        current_price = _convert_price_to_eur_at_date(current_quote, quote_currency, today)

        if target_price is not None and current_price and not _is_target_price_plausible(target_price, current_price):
            # Yahoo's target looks corrupted — cross-check ChartMill before
            # giving up on Pot/Pot % for this row.
            bad_target_price = target_price
            chartmill_data = fetch_chartmill_target_data(str(row.get("YahooSymbol") or "").strip())
            chartmill_target = _convert_price_to_eur_at_date(
                chartmill_data.get("target_mean"), chartmill_data.get("currency"), today
            )
            if chartmill_target is not None and _is_target_price_plausible(chartmill_target, current_price):
                target_price = chartmill_target
                # Set TMP right here, next to the value it's overriding —
                # not in a separate pass — so a future edit to one can't
                # drift from the other the way it did the first time.
                frame.at[idx, "TMP"] = chartmill_data.get("target_mean")
                frame.at[idx, "TMP_CURRENCY"] = chartmill_data.get("currency")
                print(
                    f"    Yahoo target {bad_target_price:.2f} EUR for {row.get('Name')} unplausibel, "
                    f"using ChartMill {chartmill_target:.2f} EUR"
                )
            else:
                target_price = None

        if target_price is not None and current_price:
            pots.append(round(target_price - current_price, 2))
            pot_pcts.append(round((target_price / current_price - 1.0) * 100.0, 2))
        else:
            pots.append(None)
            pot_pcts.append(None)

    frame["Pot"] = pots
    frame["Pot %"] = pot_pcts
    return frame


def build_ishares_top(index_name: str, session: requests.Session, eodhd_api_key: str) -> pd.DataFrame:
    holdings = fetch_ishares_holdings(session, index_name)
    print(f"  {index_name}: loaded {len(holdings)} iShares equity holdings")

    holdings["EodhdSymbol"] = holdings["Ticker"].map(normalize_us_symbol_for_eodhd)
    holdings["YahooSymbol"] = holdings["Ticker"].map(normalize_us_symbol_for_yahoo)
    us_isin_map = fetch_us_isin_map(eodhd_api_key)
    eodhd_symbols = holdings["EodhdSymbol"].dropna().astype(str).loc[lambda s: s.ne("")].tolist()
    quotes = fetch_eodhd_quotes(eodhd_symbols, eodhd_api_key)
    print(f"  {index_name}: EODHD returned {len(quotes)} quotes")

    rows = []
    for _, holding in holdings.iterrows():
        quote = quotes.get(holding["EodhdSymbol"])
        if not quote:
            quote = fetch_yahoo_quote(holding["YahooSymbol"])
        if not quote:
            continue

        last = to_float(quote.get("close"))
        previous = to_float(quote.get("previousClose"))
        change_pct = to_float(quote.get("change_p"))
        if last is None:
            continue
        if change_pct is None and previous not in (None, 0):
            change_pct = (last - previous) / previous * 100
        if change_pct is None:
            continue

        rows.append(
            {
                "Index": index_name,
                "Rank": 0,
                "Name": holding.get("Name", ""),
                "ISIN": us_isin_map.get(ticker_key(holding.get("Ticker")), ""),
                "Last": last,
                "% Differenz zum Vortageskurs": change_pct,
                "% Differenz ein Monat": None,
                "% Differenz ein Jahr": None,
                "TMP": None,
                "Pot": None,
                "Pot %": None,
                "SourceUrl": holding.get("SourceUrl", ""),
                "EodhdSymbol": holding.get("EodhdSymbol", ""),
                "YahooSymbol": holding.get("YahooSymbol", ""),
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        print(f"Warning: no EODHD/Yahoo quote rows found for {index_name}; falling back to finanzen.net")
        return fetch_index_top_from_finanzen(session, index_name, eodhd_api_key)
    frame = frame.sort_values("% Differenz zum Vortageskurs", ascending=False, na_position="last").head(TOP_N).reset_index(drop=True)
    frame["Rank"] = frame.index + 1
    return frame


def normalize_table(raw_df: pd.DataFrame, index_name: str, direction: str, source_url: str) -> pd.DataFrame:
    rows = []
    for rank, (_, row) in enumerate(raw_df.iterrows(), start=1):
        name, isin = split_name_isin(row.get("Name ISIN", ""))
        last, _previous = split_numeric_pair(row.get("Letzter Vortag", ""))
        _change_abs, change_pct = split_numeric_pair(row.get("+/- %", ""))
        rows.append(
            {
                "Index": index_name,
                "Rank": rank,
                "Name": name,
                "ISIN": isin,
                "Last": last,
                "% Differenz zum Vortageskurs": change_pct,
                "% Differenz ein Monat": None,
                "% Differenz ein Jahr": None,
                "TMP": None,
                "Pot": None,
                "Pot %": None,
                "SourceUrl": source_url,
            }
        )
    return pd.DataFrame(rows)


def select_top_table(tables: list[pd.DataFrame], url: str, warn: bool = True) -> pd.DataFrame:
    candidates = [
        table
        for table in tables
        if TOP_TABLE_COLUMNS.issubset({str(column) for column in table.columns})
    ]
    if not candidates:
        if warn:
            print(f"Warning: no Top table found on {url}; writing an empty sheet.")
        return pd.DataFrame(columns=list(TOP_TABLE_COLUMNS))
    populated = [table for table in candidates if not table.empty]
    if populated:
        return populated[0]
    if warn:
        print(f"Warning: no Top rows found on {url}; writing an empty sheet.")
    return candidates[0]


def add_eodhd_symbols_for_us_finanzen_index(frame: pd.DataFrame, index_name: str, api_key: str) -> pd.DataFrame:
    if frame.empty or index_name not in US_FINANZEN_INDICES:
        return frame
    us_isin_map = fetch_us_isin_map(api_key)
    isin_to_ticker = {isin: ticker for ticker, isin in us_isin_map.items() if isin}
    frame["EodhdSymbol"] = frame["ISIN"].map(
        lambda isin: normalize_us_symbol_for_eodhd(isin_to_ticker.get(extract_isin(isin), ""))
    )
    return frame


def fetch_index_top_from_finanzen(session: requests.Session, index_name: str, eodhd_api_key: str) -> pd.DataFrame:
    empty_frame = pd.DataFrame(columns=OUTPUT_COLUMNS)
    empty_url = ""
    for url in index_urls(index_name):
        html = fetch_html(session, url)
        tables = pd.read_html(io.StringIO(html))
        top_table = select_top_table(tables, url, warn=False)
        frame = normalize_table(top_table, index_name, "Top", url)
        if frame.empty:
            empty_frame = frame
            empty_url = url
            continue
        # Rank and the Top N cut follow finanzen.net's own "+/- %" here. The
        # column is recomputed later (add_period_changes, Ask vs previous
        # close) and the rows are deliberately not re-sorted by that value, so
        # a row can show a small negative change and still be in the list.
        frame = frame.sort_values("% Differenz zum Vortageskurs", ascending=False, na_position="last").head(TOP_N).reset_index(drop=True)
        frame["Rank"] = frame.index + 1
        frame = add_eodhd_symbols_for_us_finanzen_index(frame, index_name, eodhd_api_key)
        if url != index_urls(index_name)[0]:
            print(f"Info: used fallback index page for {index_name}: {url}")
        return frame

    warning_url = empty_url or index_urls(index_name)[-1]
    print(f"Warning: no Top rows found for {index_name}; writing an empty sheet. Last checked: {warning_url}")
    return empty_frame


def fetch_index_top(session: requests.Session, index_name: str, eodhd_api_key: str) -> pd.DataFrame:
    if index_name in ISHARES_INDEX_FUNDS:
        frame = build_ishares_top(index_name, session, eodhd_api_key)
    else:
        frame = fetch_index_top_from_finanzen(session, index_name, eodhd_api_key)
    frame = add_yahoo_symbols_from_isin(frame)
    frame = add_onvista_bid_ask(frame, session)
    frame = add_yahoo_target_prices(frame)
    # The source's day change, before add_period_changes overwrites the column.
    frame[SOURCE_CHANGE_COLUMN] = frame["% Differenz zum Vortageskurs"]
    frame = add_period_changes(frame, eodhd_api_key)
    frame = add_potential_pct(frame)
    frame = normalize_display_currency(frame)
    frame = add_diff_flag(frame)
    return frame.reindex(columns=OUTPUT_COLUMNS + CHECK_COLUMNS)


def auto_fit_columns(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    worksheet = writer.sheets[sheet_name]
    worksheet.freeze_panes = "A2"
    for idx, column in enumerate(df.columns, start=1):
        values = [str(column), *(str(value) for value in df[column].head(200).fillna(""))]
        width = min(max(len(value) for value in values) + 2, 48)
        worksheet.column_dimensions[worksheet.cell(row=1, column=idx).column_letter].width = width


TWO_DECIMAL_NUMBER_FORMAT = "0.00"
RED_NEGATIVE_NUMBER_FORMAT = '0.00;[Red]-0.00'
RED_NEGATIVE_COLUMNS = {
    "% Differenz zum Vortageskurs",
    "% Differenz ein Monat",
    "% Differenz ein Jahr",
    "Pot",
    "Pot %",
    SOURCE_CHANGE_COLUMN,
}
TWO_DECIMAL_COLUMNS = {"Bid", "Ask", "TMP"}


def format_red_negative_columns(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    worksheet = writer.sheets[sheet_name]
    for column in RED_NEGATIVE_COLUMNS:
        if column not in df.columns:
            continue
        column_idx = df.columns.get_loc(column) + 1
        for row_idx in range(2, len(df) + 2):
            worksheet.cell(row=row_idx, column=column_idx).number_format = RED_NEGATIVE_NUMBER_FORMAT
    for column in TWO_DECIMAL_COLUMNS:
        if column not in df.columns:
            continue
        column_idx = df.columns.get_loc(column) + 1
        for row_idx in range(2, len(df) + 2):
            worksheet.cell(row=row_idx, column=column_idx).number_format = TWO_DECIMAL_NUMBER_FORMAT


def write_xlsx(frames: dict[str, pd.DataFrame], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name, df in frames.items():
            export_df = df.copy()
            safe_sheet_name = sheet_name[:31]
            export_df.to_excel(writer, sheet_name=safe_sheet_name, index=False)
            auto_fit_columns(writer, safe_sheet_name, export_df)
            format_red_negative_columns(writer, safe_sheet_name, export_df)


GERMAN_DECIMAL_COLUMNS = [
    "Bid",
    "Ask",
    "% Differenz zum Vortageskurs",
    "% Differenz ein Monat",
    "% Differenz ein Jahr",
    "TMP",
    "Pot",
    "Pot %",
    SOURCE_CHANGE_COLUMN,
]


def format_german_decimal(value: object) -> object:
    number = to_float(value)
    if number is None:
        return value
    return repr(number).replace(".", ",")


def format_german_decimal_fixed(value: object, decimals: int = 2) -> object:
    number = to_float(value)
    if number is None:
        return value
    return f"{number:.{decimals}f}".replace(".", ",")


def prepare_csv_export(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    csv_df = pd.concat(frames.values(), ignore_index=True)
    # server_app.import_watchlists.parse_decimal() and dashboard.py's
    # parse_de_number() both assume German-locale numbers ("." thousands,
    # "," decimal) — the convention every other watchlist CSV in this
    # pipeline follows. Values computed here are plain Python floats, so
    # they must be reformatted before writing, or "-9.68" round-trips as
    # "-968" once those parsers strip the "." as a thousands separator.
    for column in GERMAN_DECIMAL_COLUMNS:
        if column not in csv_df.columns:
            continue
        # TMP specifically gets a fixed 2 decimal places (e.g. "8075,00"
        # instead of repr()'s "8075,0") — yfinance's targetMeanPrice varies
        # wildly in precision (0-4+ decimals) depending on the ticker.
        formatter = format_german_decimal_fixed if column == "TMP" else format_german_decimal
        csv_df[column] = csv_df[column].map(formatter)
    return csv_df


# Live publishing: every finished index goes into top_liste_latest.csv (and
# PostgreSQL) right away, instead of the whole list appearing when the last
# index is done.
PG_IMPORT_LOCK_PATH = os.environ.get("PG_IMPORT_LOCK") or "/tmp/pg_import.lock"
PG_IMPORT_LOCK_WAIT_SECONDS = 60


def merge_live_csv(frames: dict[str, pd.DataFrame], indices: list[str], previous_csv: Path) -> pd.DataFrame:
    """Rows of the live CSV, in the order of `indices`: the new rows of every
    index fetched so far, the rows the last run left for the others."""
    previous = None
    if previous_csv.exists():
        try:
            previous = pd.read_csv(previous_csv, dtype=str, encoding="utf-8-sig").fillna("")
        except Exception:
            previous = None
    parts = []
    for index_name in indices:
        if index_name in frames:
            parts.append(prepare_csv_export({index_name: frames[index_name]}))
        elif previous is not None and "Index" in previous.columns:
            parts.append(previous[previous["Index"] == index_name])
    columns = OUTPUT_COLUMNS + CHECK_COLUMNS
    if not parts:
        return pd.DataFrame(columns=columns)
    return pd.concat(parts, ignore_index=True).reindex(columns=columns)


def write_csv_atomic(frame: pd.DataFrame, path: Path) -> None:
    # Replaced in one step: the importer and the dashboard read this file at
    # any time, and must never see it half written.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    frame.to_csv(temp_path, index=False, encoding="utf-8-sig")
    os.replace(temp_path, path)


def import_live_csv(path: Path) -> int:
    """Import `path` into PostgreSQL under /tmp/pg_import.lock, the lock
    scripts/update_watchlists_to_pg.sh takes. Two imports of the same file at
    once would leave its rows twice in the database."""
    from server_app.import_watchlists import import_file

    deadline = time.monotonic() + PG_IMPORT_LOCK_WAIT_SECONDS
    with open(PG_IMPORT_LOCK_PATH, "a") as lock_handle:
        while True:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{PG_IMPORT_LOCK_PATH} still held after {PG_IMPORT_LOCK_WAIT_SECONDS}s")
                time.sleep(0.5)
        try:
            return import_file(path, path.parent.parent)
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def publish_live(frames: dict[str, pd.DataFrame], indices: list[str], live_csv: Path, to_pg: bool) -> None:
    """Never raises: the fetch goes on, and the normal import after the run
    catches up, if a live write fails."""
    try:
        write_csv_atomic(merge_live_csv(frames, indices, live_csv), live_csv)
    except Exception as exc:
        print(f"Warning: live CSV not written: {exc}")
        return
    if not to_pg:
        return
    try:
        count = import_live_csv(live_csv)
        print(f"  Live import: {count} rows in PostgreSQL ({len(frames)}/{len(indices)} indices new)")
    except Exception as exc:
        print(f"Warning: live PostgreSQL import failed: {exc}")


def parse_indices(raw_indices: list[str] | None) -> list[str]:
    if not raw_indices:
        return list(INDEX_SLUGS)
    selected = []
    for raw in raw_indices:
        key = re.sub(r"\s+", " ", raw.strip().lower())
        index_name = INDEX_ALIASES.get(key)
        if not index_name:
            raise ValueError(f"Unsupported index {raw!r}; choose from {', '.join(INDEX_SLUGS)}")
        if index_name not in selected:
            selected.append(index_name)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch stock-index Top tables from finanzen.net")
    parser.add_argument(
        "--indices",
        nargs="+",
        help=(
            "Indices to fetch. Default: all homepage Aktien Top indices. "
            "Examples: DAX MDAX TecDAX SDAX \"Dow Jones\" \"NASDAQ 100\" \"S&P 500\" "
            "\"S&P 400\" \"S&P 600\" \"EURO STOXX 50\" SMI \"Nikkei 225\""
        ),
    )
    parser.add_argument("--list-indices", action="store_true", help="Print supported index names and exit")
    parser.add_argument("--output", help="Output XLSX path")
    parser.add_argument("--csv", action="store_true", help="Also write one combined CSV next to the XLSX")
    parser.add_argument(
        "--live-csv",
        help="After each finished index, also write the list so far to this CSV (replaced atomically)",
    )
    parser.add_argument(
        "--live-pg",
        action="store_true",
        help="With --live-csv: also import that CSV into PostgreSQL after each finished index",
    )
    args = parser.parse_args()

    if args.list_indices:
        print("Supported indices:")
        for index_name in INDEX_SLUGS:
            print(f"  {index_name}")
        return

    try:
        indices = parse_indices(args.indices)
    except ValueError as exc:
        parser.error(str(exc))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_path = Path(args.output) if args.output else SCRIPT_DIR / f"finanzen_topflop_{timestamp}.xlsx"

    session = make_session()
    eodhd_api_key = load_eodhd_api_key()
    if yf is None:
        print("Warning: yfinance is not installed; TMP values will be empty.")
    frames: dict[str, pd.DataFrame] = {}
    for index_name in indices:
        reused = reuse_cached_index(index_name)
        if reused is not None:
            frames[index_name] = reused[0]
            print(f"Skipping {index_name} ({reused[1]}); keeping its last {len(reused[0])} rows")
        else:
            print(f"Fetching {index_name} Top ...")
            frames[index_name] = fetch_index_top(session, index_name, eodhd_api_key)
            if index_name in ISHARES_INDEX_FUNDS and not frames[index_name].empty:
                write_index_frame_cache(index_name, frames[index_name])
        if args.live_csv:
            publish_live(frames, indices, Path(args.live_csv), args.live_pg)

    write_xlsx(frames, output_path)
    print(f"Saved XLSX: {output_path}")

    if args.csv:
        csv_path = output_path.with_suffix(".csv")
        prepare_csv_export(frames).to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"Saved CSV : {csv_path}")


if __name__ == "__main__":
    main()
