"""Best-effort onvista.de lookup used to auto-fill Name/WKN/Bid/Ask/currency
(+ Laufende Kosten for ETF/fund) for a newly added ISIN in the dashboard's
"Add instrument" form.

Deliberately a small, self-contained scraper rather than a reuse of
csv_analyse_watchlist.py's fetch_onvista_fields: that script is a large batch
CLI (XLSX writing, marketscreener/duckduckgo fallbacks, openpyxl/config_loader
dependencies) not meant to run inside the lightweight api/dashboard containers,
and its onvista extractor doesn't capture the instrument Name anyway (in that
pipeline Name comes pre-filled from the broker's watchlist export, not from
onvista). This mirrors its price-extraction approach and adds a Name lookup
via the page's <h1>.
"""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

REQUEST_TIMEOUT = 12


def _find_detail_url(isin: str, instrument_type: str, session: requests.Session) -> str | None:
    if instrument_type in {"etf", "fund"}:
        for path in ("etf", "fonds"):
            try:
                response = session.get(
                    f"https://www.onvista.de/{path}/{isin}",
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                )
            except requests.RequestException:
                continue
            if response.status_code == 200 and isin in response.url:
                return response.url

    try:
        response = session.get(
            f"https://www.onvista.de/suche/?searchValue={isin}",
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None

    if isin in response.url and "/suche/" not in response.url:
        return response.url

    soup = BeautifulSoup(response.text, "html.parser")
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if isin in href and any(segment in href for segment in ("/aktien/", "/etf/", "/fonds/")):
            return href if href.startswith("http") else f"https://www.onvista.de{href}"
    return None


def detect_instrument_type(isin: str) -> str:
    """"stock" or "etf", for lists (BUYLIST/DEPOTS) that mix both: an ISIN
    onvista files under /etf/ or /fonds/ is an ETF, anything else a stock. Never
    raises; "stock" when onvista can't say."""
    with requests.Session() as session:
        url = _find_detail_url(isin.strip().upper(), "etf", session)
    return "etf" if url and any(segment in url for segment in ("/etf/", "/fonds/")) else "stock"


def _price_near_label(text: str, label: str, max_gap: int = 40) -> tuple[float | None, str]:
    """First price (and optional currency code) found within max_gap chars
    after any occurrence of `label` — onvista repeats "Geld"/"Brief" once per
    exchange listing, so the first occurrence isn't always followed by a value."""
    for match in re.finditer(re.escape(label), text):
        window = text[match.end() : match.end() + max_gap]
        price_match = re.search(r"([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]+)?)\s*([A-Z]{3})?", window)
        if price_match:
            raw, currency = price_match.groups()
            try:
                return float(raw.replace(".", "").replace(",", ".")), currency or ""
            except ValueError:
                continue
    return None, ""


def _line_value_after_label(lines: list[str], label: str, max_lookahead: int = 4) -> str:
    """First bare-number line within `max_lookahead` lines after a line that
    is exactly `label` — onvista renders "Laufende Kosten" as its own text
    node, immediately followed by the number and then a separate "%" node."""
    normalized_label = label.strip().lower()
    for i, line in enumerate(lines):
        if line.strip().lower() != normalized_label:
            continue
        for candidate in lines[i + 1 : i + 1 + max_lookahead]:
            if re.fullmatch(r"[0-9]+(?:,[0-9]+)?", candidate.strip()):
                return candidate.strip()
    return ""


def lookup(isin: str, instrument_type: str) -> dict:
    """instrument_type: "stock" | "etf" | "fund". Never raises — a failed or
    partial lookup just leaves those fields empty for the caller to fall back
    on the CSV's normal daily enrichment cron."""
    result: dict = {
        "name": "",
        "wkn": "",
        "bid": None,
        "ask": None,
        "currency": "",
        "laufende_kosten": "",
        "url": "",
        "error": "",
    }
    isin = isin.strip().upper()

    with requests.Session() as session:
        url = _find_detail_url(isin, instrument_type, session)
        if not url:
            result["error"] = "onvista URL not found"
            return result
        result["url"] = url

        try:
            response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            result["error"] = str(exc)
            return result

        # onvista's <h1> text nodes are split by empty hydration comments
        # (e.g. "Apple<!-- --> <!-- --> Aktie"), which get_text() concatenates
        # with no space and which also carries a trailing "Aktie"/"Fonds" noun
        # for equities; the h1's title="" attribute already has the clean
        # name without either problem.
        title_tag = soup.find("h1")
        if title_tag and title_tag.get("title", "").strip():
            result["name"] = title_tag["title"].strip()
        elif title_tag and title_tag.get_text(separator=" ", strip=True):
            result["name"] = title_tag.get_text(separator=" ", strip=True)

        # The <title> tag reads "{Name} • {WKN} • {ISIN} • onvista" — the WKN
        # is the only "•"-delimited part that's a bare 6-char alphanumeric
        # code (the ISIN segment is 12 chars: 2-letter country + 10 chars).
        if soup.title and soup.title.string:
            for part in soup.title.string.split("•"):
                candidate = part.strip()
                if re.fullmatch(r"[A-Z0-9]{6}", candidate):
                    result["wkn"] = candidate
                    break

        compact_text = soup.get_text(separator=" ")
        bid, bid_currency = _price_near_label(compact_text, "Geld")
        ask, ask_currency = _price_near_label(compact_text, "Brief")
        result["bid"] = bid
        result["ask"] = ask
        result["currency"] = bid_currency or ask_currency or ""

        if instrument_type in {"etf", "fund"}:
            lines = [line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip()]
            kosten = _line_value_after_label(lines, "Laufende Kosten")
            if kosten:
                result["laufende_kosten"] = f"{kosten} %"

    return result
