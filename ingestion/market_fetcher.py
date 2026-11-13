from datetime import datetime
from urllib.parse import quote
from typing import Optional
import re
import random
import time
import requests
from collections import deque

try:
    import yfinance as yf
    _YFINANCE_AVAILABLE = True
except ImportError:
    _YFINANCE_AVAILABLE = False

try:
    import finnhub as _finnhub_mod
    _FINNHUB_AVAILABLE = True
except ImportError:
    _FINNHUB_AVAILABLE = False


class AmbiguousInstrumentError(RuntimeError):
    """A name matched several instruments and none is a primary listing."""


class MarketFetcher:
    _COMMODITY_ALIASES = {
        "BRENT": "BRENT",
        "BRENT OIL": "BRENT",
        "BRENT CRUDE": "BRENT",
        "BRENT CRUDE OIL": "BRENT",
        "WTI": "WTI",
        "WTI OIL": "WTI",
        "WTI CRUDE": "WTI",
        "WTI CRUDE OIL": "WTI",
        "CRUDE OIL": "WTI",
        "GOLD": "XAUUSD=X",
        "SPOT GOLD": "XAUUSD=X",
        "XAU": "XAUUSD=X",
        "SILVER": "XAGUSD=X",
        "SPOT SILVER": "XAGUSD=X",
        "XAG": "XAGUSD=X",
    }
    _AMBIGUOUS_ASSET_QUERIES = {
        "GOLD": {
            "commodity_example": "gold",
            "stock_example": "GOLD",
        },
    }
    _EODHD_COMMODITY_SYMBOLS = {"BRENT", "WTI"}

    def __init__(self, symbols: list[str], market_cfg: Optional[dict] = None):
        self.symbols = symbols or []
        cfg = market_cfg or {}
        self.max_requests_per_minute = int(cfg.get("quote_max_requests_per_minute", 20))
        self.retry_max_attempts = int(cfg.get("quote_retry_max_attempts", 3))
        self.retry_base_delay_seconds = float(cfg.get("quote_retry_base_delay_seconds", 1.0))
        self.retry_verbose = bool(cfg.get("quote_retry_verbose", True))
        self._request_timestamps: deque = deque()
        self._alpha_vantage_api_key = cfg.get("alpha_vantage_api_key", "").strip()
        self._alpha_vantage_base_url = "https://www.alphavantage.co/query"
        # [Leeway disabled]
        # self._leeway_api_key = cfg.get("leeway_api_key", "").strip()
        # self._leeway_base_url = cfg.get("leeway_base_url", "").strip().rstrip("/")
        # self._leeway_timeout_seconds = float(cfg.get("leeway_timeout_seconds", 8.0))
        # self._leeway_resolve_all = bool(cfg.get("leeway_resolve_all", False))
        # self._leeway_session = requests.Session() if self._leeway_api_key and self._leeway_base_url else None
        self._leeway_session = None
        self._openfigi_api_key = cfg.get("openfigi_api_key", "").strip()
        self._openfigi_session = requests.Session()
        self._eodhd_api_key = cfg.get("eodhd_api_key", "").strip()
        self._eodhd_base_url = "https://eodhd.com/api"
        self._eodhd_timeout_seconds = float(cfg.get("eodhd_timeout_seconds", 8.0))
        self._eodhd_session = requests.Session() if self._eodhd_api_key else None

        finnhub_api_key = cfg.get("finnhub_api_key", "").strip()
        self._finnhub_client = None
        if finnhub_api_key and _FINNHUB_AVAILABLE:
            self._finnhub_client = _finnhub_mod.Client(api_key=finnhub_api_key)
        elif finnhub_api_key and not _FINNHUB_AVAILABLE:
            print("[MarketFetcher] finnhub-python not installed. Run: pip install finnhub-python")

    # ── Public interface ──────────────────────────────────────────────────────

    def fetch(self) -> list[dict]:
        return self.fetch_for_symbols(self.symbols)

    def fetch_for_queries(self, queries: list[str]) -> list[dict]:
        symbols: list[str] = []
        meta_by_symbol: dict[str, dict] = {}
        for q in queries:
            token = self._normalize_query_token(q)
            if not token:
                continue
            forced_asset_class, _ = self._split_forced_asset_prefix(token)
            sym = self.resolve_symbol(token)
            if sym and sym not in symbols:
                symbols.append(sym)
                meta_by_symbol[sym] = {
                    "requested_query": token,
                    "forced_asset_class": forced_asset_class,
                }
        return self._build_results_for_symbols(symbols, meta_by_symbol)

    def fetch_for_symbols(self, symbols: list[str]) -> list[dict]:
        return self._build_results_for_symbols(symbols, {})

    def _build_results_for_symbols(self, symbols: list[str], meta_by_symbol: dict[str, dict]) -> list[dict]:
        cleaned = []
        for s in symbols:
            normalized = self._normalize_query_token(s)
            if normalized:
                sym = normalized.upper()
                if sym not in cleaned:
                    cleaned.append(sym)
        if not cleaned:
            return []
        quote_map = self._fetch_quotes(cleaned)
        now = datetime.now().isoformat()
        results = []
        for sym in cleaned:
            quote = quote_map.get(sym)
            meta = meta_by_symbol.get(sym, {})
            forced_asset_class = meta.get("forced_asset_class")
            asset_class = self._infer_asset_class(sym, quote, forced_asset_class)
            if isinstance(quote, dict):
                quote["_resolved_symbol"] = sym
                quote["_asset_class"] = asset_class
                if meta.get("requested_query"):
                    quote["_requested_query"] = meta["requested_query"]
            text = self._format_market_text(sym, quote, None)
            q = quote if isinstance(quote, dict) else {}
            results.append({
                "symbol": sym,
                "text": text,
                "timestamp": now,
                "source": "market_live",
                # Structured copy of what `text` renders, so tool callers (agent
                # get_market_quote) don't have to parse the text. change_percent
                # is a fraction (0.0123 == 1.23%), same as the quote dicts.
                "name": q.get("name") or "",
                "price": q.get("current"),
                "currency": q.get("currency") or "",
                "price_usd": q.get("price_usd"),
                "price_eur": q.get("price_eur"),
                "change": q.get("change"),
                "change_percent": q.get("change_percent"),
                "provider": q.get("_provider") or "",
                "error": q.get("error") or "",
            })
        return results

    # ── EODHD data beyond live quotes (profile, daily history, news, sentiment) ──

    def _eodhd_get(self, path: str, params: Optional[dict] = None):
        """GET <eodhd>/<path> as JSON. Every error is re-raised as a RuntimeError whose
        text never contains the API key: requests' own errors embed the full request URL."""
        if not self._eodhd_session:
            raise RuntimeError("EODHD API key is not configured.")
        full_params = {"api_token": self._eodhd_api_key, "fmt": "json", **(params or {})}
        try:
            resp = self._eodhd_session.get(
                f"{self._eodhd_base_url}/{path}", params=full_params, timeout=self._eodhd_timeout_seconds
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "?")
            if status == 403:
                raise RuntimeError("EODHD: this data is not included in the current subscription plan (HTTP 403).") from None
            raise RuntimeError(f"EODHD request failed (HTTP {status}).") from None
        except (requests.RequestException, ValueError):
            raise RuntimeError("EODHD request failed (no usable response).") from None

    def search_instruments(self, query: str, limit: int = 10) -> list[dict]:
        """Every exchange listing EODHD knows for a name, ticker or ISIN (its /search
        endpoint, which every plan includes)."""
        query = (query or "").strip()
        if not query:
            return []
        data = self._eodhd_get(f"search/{quote(query, safe='')}", {"limit": int(limit)})
        return data if isinstance(data, list) else []

    def resolve_eodhd_symbol(self, query: str) -> str:
        """'SPCX.US' style symbol for a ticker, ISIN or company name. A symbol that already
        has an exchange suffix is only translated (SAP.DE -> SAP.XETRA); anything else is
        looked up and the primary listing wins."""
        query = (query or "").strip()
        if not query:
            raise RuntimeError("No symbol given.")
        if "." in query and " " not in query:
            return self._to_eodhd_symbol(query.upper())
        items = self.search_instruments(query, limit=10)
        if not items:
            raise RuntimeError(f"No listing found for '{query}'.")
        main = next((i for i in items if i.get("isPrimary")), items[0] if len(items) == 1 else None)
        if main is None:
            # E.g. the bare name "SpaceX" only finds depositary receipts, not the real listing:
            # guessing the first one would report data for the wrong instrument.
            candidates = ", ".join(f"{i.get('Code')}.{i.get('Exchange')} ({i.get('Name')})" for i in items[:4])
            raise AmbiguousInstrumentError(
                f"'{query}' matches several instruments and none is a primary listing: {candidates}. "
                "Pass an ISIN or the exact ticker."
            )
        return f"{main.get('Code')}.{main.get('Exchange')}"

    def fetch_eod_history(self, eodhd_symbol: str, start: str, end: str) -> list[dict]:
        """Daily bars, oldest first."""
        data = self._eodhd_get(
            f"eod/{quote(eodhd_symbol, safe='.')}", {"from": start, "to": end, "period": "d", "order": "a"}
        )
        return data if isinstance(data, list) else []

    def fetch_news(self, eodhd_symbol: str, limit: int = 5) -> list[dict]:
        data = self._eodhd_get("news", {"s": eodhd_symbol, "limit": int(limit)})
        return data if isinstance(data, list) else []

    def fetch_sentiment(self, eodhd_symbol: str, start: str, end: str) -> list[dict]:
        """Daily news-sentiment rows for one symbol, newest first as EODHD returns them."""
        data = self._eodhd_get("sentiments", {"s": eodhd_symbol, "from": start, "to": end})
        rows = data.get(eodhd_symbol) if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []

    def resolve_symbol(self, query: str) -> Optional[str]:
        if not query:
            return None
        forced_asset_class, raw_query = self._split_forced_asset_prefix(query)
        q = self._normalize_query_token(raw_query)
        if not q:
            return None

        if forced_asset_class != "stock" and forced_asset_class != "etf":
            alias = self._resolve_commodity_alias(q)
            if alias:
                if self.retry_verbose:
                    print(f"[MarketFetcher] {q} -> {alias} via commodity alias")
                return alias

        if forced_asset_class == "commodity" and self._looks_like_ticker(q):
            return q.upper()

        # OpenFIGI for ISIN/WKN -> ticker resolution.
        if self._looks_like_isin(q) or self._looks_like_wkn(q):
            resolved = self._resolve_with_openfigi(q)
            if resolved:
                if self.retry_verbose:
                    print(f"[MarketFetcher] {q} -> {resolved} via OpenFIGI")
                return resolved

        if self._looks_like_ticker(q):
            return q.upper()

        # Finnhub symbol search
        if self._finnhub_client:
            try:
                result = self._finnhub_client.symbol_lookup(q)
                for row in (result.get("result") or []):
                    sym = row.get("symbol")
                    type_ = (row.get("type") or "").lower()
                    if forced_asset_class == "stock":
                        allow = {"common stock", ""}
                    elif forced_asset_class == "etf":
                        allow = {"etf", ""}
                    else:
                        allow = {"common stock", "etf", ""}
                    if sym and type_ in allow:
                        return str(sym).upper()
            except Exception:
                pass

        # yfinance search fallback
        if _YFINANCE_AVAILABLE:
            for candidate in self._search_candidates(q):
                try:
                    search = self._with_retry(
                        lambda c=candidate: yf.Search(c, max_results=8, news_count=0),
                        symbol=q,
                        operation="search",
                    )
                    for row in (search.quotes or []):
                        symbol = row.get("symbol")
                        quote_type = (row.get("quoteType") or "").lower()
                        if forced_asset_class == "stock":
                            allow = {"equity", "mutualfund", ""}
                        elif forced_asset_class == "etf":
                            allow = {"etf", ""}
                        else:
                            allow = {"equity", "etf", "mutualfund", ""}
                        if symbol and quote_type in allow:
                            return str(symbol).upper()
                except Exception:
                    continue

        return None

    @classmethod
    def _resolve_commodity_alias(cls, query: str) -> Optional[str]:
        normalized = re.sub(r"[\-_]+", " ", query.strip().upper())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return cls._COMMODITY_ALIASES.get(normalized)

    @classmethod
    def get_ambiguous_asset_hint(cls, query: str) -> Optional[dict[str, str]]:
        forced_asset_class, raw = cls._split_forced_asset_prefix(query)
        if forced_asset_class:
            return None
        normalized = cls._normalize_query_token(raw).upper()
        hint = cls._AMBIGUOUS_ASSET_QUERIES.get(normalized)
        if not hint:
            return None
        return {
            "query": raw.strip(),
            "commodity_example": hint["commodity_example"],
            "stock_example": hint["stock_example"],
        }

    @staticmethod
    def _split_forced_asset_prefix(text: str) -> tuple[Optional[str], str]:
        raw = (text or "").strip()
        match = re.match(r"^(commodity|stock|etf)\s*:\s*(.+)$", raw, flags=re.IGNORECASE)
        if not match:
            return None, raw
        return match.group(1).lower(), match.group(2).strip()

    def _infer_asset_class(self, symbol: str, quote: Optional[dict], forced_asset_class: Optional[str]) -> str:
        if forced_asset_class:
            return forced_asset_class
        upper = (symbol or "").upper()
        if upper in self._EODHD_COMMODITY_SYMBOLS:
            return "commodity"
        if upper in {"XAUUSD=X", "XAGUSD=X"}:
            return "commodity"
        if upper.endswith("=F"):
            return "commodity"
        name = ""
        if isinstance(quote, dict):
            name = (quote.get("name") or "").upper()
        if " ETF" in name or name.startswith("ETF"):
            return "etf"
        return "stock"

    # [Leeway disabled] _resolve_with_leeway removed.

    def _resolve_with_openfigi(self, query: str) -> Optional[str]:
        """Map WKN or ISIN to a tradable ticker via OpenFIGI /v3/mapping."""
        upper = query.strip().upper()
        if self._looks_like_isin(upper):
            id_type = "ID_ISIN"
        elif self._looks_like_wkn(upper):
            id_type = "ID_WERTPAPIER"
        else:
            return None

        url = "https://api.openfigi.com/v3/mapping"
        headers = {"Content-Type": "application/json"}
        if self._openfigi_api_key:
            headers["X-OPENFIGI-APIKEY"] = self._openfigi_api_key

        try:
            resp = self._openfigi_session.post(
                url,
                json=[{"idType": id_type, "idValue": upper}],
                headers=headers,
                timeout=8,
            )
            if resp.status_code >= 400:
                return None
            payload = resp.json()
        except Exception:
            return None

        # Response is a list with one element per request item.
        if not isinstance(payload, list) or not payload:
            return None
        data = payload[0].get("data") or []

        # OpenFIGI exchCode → yfinance suffix (used by Finnhub/yfinance too).
        exch_map = {
            "GS": "DE",   # Germany XETRA
            "LN": "L",    # London LSE
            "US": "",     # NYSE (no suffix)
            "UQ": "",     # NASDAQ (no suffix)
            "UR": "",     # NYSE Arca (no suffix)
            "HK": "HK",  # Hong Kong
            "TT": "T",   # Tokyo
            "SW": "SW",  # Swiss SIX
            "FP": "PA",  # Euronext Paris
            "NA": "AS",  # Euronext Amsterdam
        }
        # For US ISINs prefer US exchanges; for all others prefer primary home market.
        is_us_isin = upper.startswith("US")
        if is_us_isin:
            preferred_order = ["US", "UQ", "UR", "GS", "LN", "HK", "TT", "FP", "NA", "SW"]
        else:
            preferred_order = ["GS", "LN", "HK", "TT", "FP", "NA", "SW", "US", "UQ", "UR"]

        candidates = []
        for item in data:
            ticker = item.get("ticker")
            exch_code = item.get("exchCode") or ""
            sec_type = (item.get("securityType") or item.get("securityType2") or "").lower()
            if not ticker or sec_type in {"option", "future", "warrant"}:
                continue
            suffix = exch_map.get(exch_code, exch_code)
            symbol = f"{ticker}.{suffix}".upper() if suffix else ticker.upper()
            priority = preferred_order.index(exch_code) if exch_code in preferred_order else 99
            candidates.append((priority, symbol))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    # [Leeway disabled] _extract_symbol_from_leeway_items removed.

    # ── Core fetch: Finnhub → EODHD → Alpha Vantage → yfinance fallback ────────

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, Optional[dict]]:
        quote_map: dict[str, Optional[dict]] = {}
        shared_fx_cache: dict[tuple[str, str], Optional[float]] = {}

        remaining_after_finnhub = []
        if self._finnhub_client:
            for sym in symbols:
                quote = self._fetch_finnhub_quote(sym, shared_fx_cache)
                if quote is not None and quote.get("current") is not None:
                    quote["_provider"] = "finnhub"
                    quote_map[sym] = quote
                    if self.retry_verbose:
                        print(f"[MarketFetcher] {sym} <- Finnhub")
                else:
                    remaining_after_finnhub.append(sym)
                    if self.retry_verbose:
                        print(f"[MarketFetcher] {sym} Finnhub miss -> trying EODHD")
        else:
            remaining_after_finnhub = list(symbols)

        remaining_after_eodhd = []
        if self._eodhd_session:
            for sym in remaining_after_finnhub:
                quote = self._fetch_eodhd_quote(sym, shared_fx_cache)
                if quote is not None and quote.get("current") is not None:
                    quote["_provider"] = "eodhd"
                    quote_map[sym] = quote
                    if self.retry_verbose:
                        print(f"[MarketFetcher] {sym} <- EODHD")
                else:
                    remaining_after_eodhd.append(sym)
                    if self.retry_verbose:
                        print(f"[MarketFetcher] {sym} EODHD miss -> no data")
        else:
            remaining_after_eodhd = list(remaining_after_finnhub)

        # yfinance fallback (3rd priority)
        remaining_after_yfinance = []
        if _YFINANCE_AVAILABLE and remaining_after_eodhd:
            yf_map, _ = self._fetch_yahoo_quotes(remaining_after_eodhd, shared_fx_cache)
            for sym in remaining_after_eodhd:
                q = yf_map.get(sym, {})
                if q and q.get("current") is not None:
                    q["_provider"] = "yfinance"
                    quote_map[sym] = q
                    if self.retry_verbose:
                        print(f"[MarketFetcher] {sym} <- yfinance")
                else:
                    remaining_after_yfinance.append(sym)
                    if self.retry_verbose:
                        print(f"[MarketFetcher] {sym} yfinance miss -> trying Alpha Vantage")
        else:
            remaining_after_yfinance = list(remaining_after_eodhd)

        # Alpha Vantage fallback (4th priority)
        for sym in remaining_after_yfinance:
            quote = self._fetch_alpha_vantage_quote(sym, shared_fx_cache)
            if quote is not None and quote.get("current") is not None:
                quote["_provider"] = "alpha_vantage"
                quote_map[sym] = quote
                if self.retry_verbose:
                    print(f"[MarketFetcher] {sym} <- Alpha Vantage")
            else:
                quote_map[sym] = {"error": "No data available from Finnhub, EODHD, yfinance, or Alpha Vantage."}

        return quote_map

    # [Leeway disabled] _YFINANCE_TO_LEEWAY, _to_leeway_symbol, _fetch_leeway_quote removed.

    # yfinance/standard suffix → EODHD exchange code.
    _SUFFIX_TO_EODHD = {
        "DE": "XETRA",
        "L":  "LSE",
        "PA": "PA",
        "AS": "AS",
        "SW": "SW",
        "HK": "HK",
        "T":  "T",
        "US": "US",
    }

    def _to_eodhd_symbol(self, symbol: str) -> str:
        """Convert standard symbol (e.g. SAP.DE) to EODHD format (e.g. SAP.XETRA).
        Bare US tickers (e.g. AAPL) become AAPL.US."""
        if "." in symbol:
            base, suffix = symbol.rsplit(".", 1)
            eodhd_exch = self._SUFFIX_TO_EODHD.get(suffix.upper(), suffix.upper())
            return f"{base}.{eodhd_exch}"
        # No suffix → assume US
        return f"{symbol}.US"

    def _fetch_eodhd_quote(self, symbol: str, fx_cache: dict[tuple[str, str], Optional[float]]) -> Optional[dict]:
        if not self._eodhd_session:
            return None

        upper_symbol = (symbol or "").strip().upper()
        if upper_symbol in self._EODHD_COMMODITY_SYMBOLS:
            return self._fetch_eodhd_commodity_quote(upper_symbol)

        eodhd_symbol = self._to_eodhd_symbol(symbol)
        url = f"{self._eodhd_base_url}/real-time/{eodhd_symbol}"
        params = {"api_token": self._eodhd_api_key, "fmt": "json"}

        try:
            resp = self._eodhd_session.get(url, params=params, timeout=self._eodhd_timeout_seconds)
            if resp.status_code >= 400:
                return None
            data = resp.json()
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        current = self._safe_float(data.get("close") or data.get("last"))
        if current is None or current == 0:
            return None

        prev_close = self._safe_float(data.get("previousClose"))
        change = self._safe_float(data.get("change"))
        change_pct_raw = self._safe_float(data.get("change_p"))
        change_pct = change_pct_raw / 100.0 if change_pct_raw is not None else None

        # EODHD real-time does not return currency or name — derive from suffix.
        suffix = eodhd_symbol.rsplit(".", 1)[-1].upper() if "." in eodhd_symbol else "US"
        currency_map = {
            "XETRA": "EUR", "PA": "EUR", "AS": "EUR", "SW": "CHF",
            "LSE": "GBP", "HK": "HKD", "T": "JPY", "US": "USD",
        }
        currency = currency_map.get(suffix)

        price_usd = self._convert_currency(current, currency, "USD", fx_cache)
        price_eur = self._convert_currency(current, currency, "EUR", fx_cache)

        return {
            "name": "",
            "current": current,
            "currency": currency,
            "price_usd": price_usd,
            "price_eur": price_eur,
            "prev_close": prev_close,
            "change": change,
            "change_percent": change_pct,
            "market_cap": None,
        }

    def _fetch_eodhd_commodity_quote(self, symbol: str) -> Optional[dict]:
        url = f"{self._eodhd_base_url}/commodities/historical/{symbol}"
        params = {
            "api_token": self._eodhd_api_key,
            "fmt": "json",
            "interval": "daily",
        }
        try:
            resp = self._eodhd_session.get(url, params=params, timeout=self._eodhd_timeout_seconds)
            if resp.status_code >= 400:
                return None
            payload = resp.json()
        except Exception:
            return None

        rows = payload if isinstance(payload, list) else payload.get("data") if isinstance(payload, dict) else None
        if not rows or not isinstance(rows, list):
            return None

        latest = rows[0] if isinstance(rows[0], dict) else None
        if not latest:
            return None

        current = self._safe_float(latest.get("close") or latest.get("price") or latest.get("value"))
        if current is None:
            return None

        prev_close = None
        if len(rows) > 1 and isinstance(rows[1], dict):
            prev_close = self._safe_float(rows[1].get("close") or rows[1].get("price") or rows[1].get("value"))

        change = None
        change_pct = None
        if prev_close is not None and prev_close != 0:
            change = current - prev_close
            change_pct = change / prev_close

        return {
            "name": f"{symbol} Crude Oil" if symbol in {"BRENT", "WTI"} else symbol,
            "current": current,
            "currency": "USD",
            "price_usd": current,
            "price_eur": None,
            "prev_close": prev_close,
            "change": change,
            "change_percent": change_pct,
            "market_cap": None,
        }

    def _fetch_finnhub_quote(self, symbol: str, fx_cache: dict[tuple[str, str], Optional[float]]) -> Optional[dict]:
        # Guard: commodity symbols should be resolved by EODHD commodities endpoint,
        # not by Finnhub equity universe (e.g., WTI may map to an unrelated stock).
        if (symbol or "").strip().upper() in self._EODHD_COMMODITY_SYMBOLS:
            return None
        # Finnhub uses its own symbol universe — exchange suffixes like .DE/.L are not supported.
        # Skip Finnhub entirely for suffixed symbols to avoid returning data for wrong exchange.
        if "." in symbol:
            return None

        result = self._fetch_finnhub_quote_single(symbol, fx_cache)
        return result

    def _fetch_finnhub_quote_single(self, symbol: str, fx_cache: dict[tuple[str, str], Optional[float]]) -> Optional[dict]:
        try:
            q = self._finnhub_client.quote(symbol)
            if not q or q.get("c", 0) == 0:
                return None

            current = q.get("c")
            prev_close = q.get("pc")
            change = q.get("d")
            change_pct = q.get("dp")  # e.g. 1.23 means 1.23%

            name = ""
            currency = None
            market_cap = None
            try:
                profile = self._finnhub_client.company_profile2(symbol=symbol)
                name = profile.get("name", "")
                currency = (profile.get("currency") or "").upper() or None
                mc = profile.get("marketCapitalization")  # millions USD
                market_cap = mc * 1e6 if mc else None
            except Exception:
                pass
            # For bare tickers, Finnhub is ambiguous: the quote might be from any exchange,
            # and profile currency reflects company home market, not quote price currency.
            # Only trust Finnhub if profile currency is explicitly USD (US-listed company).
            # Otherwise defer to EODHD which handles exchange-specific queries properly.
            if not currency:
                currency = "USD"
            elif currency != "USD":
                # Non-USD but bare ticker = ambiguous/unreliable, skip to EODHD
                return None

            price_usd = self._convert_currency(current, currency, "USD", fx_cache)
            price_eur = self._convert_currency(current, currency, "EUR", fx_cache)

            return {
                "name": name,
                "current": current,
                "currency": currency,
                "price_usd": price_usd,
                "price_eur": price_eur,
                "prev_close": prev_close,
                "change": change,
                "change_percent": (change_pct / 100.0) if change_pct is not None else None,
                "market_cap": market_cap,
            }
        except Exception:
            return None

    def _fetch_alpha_vantage_quote(self, symbol: str, fx_cache: dict[tuple[str, str], Optional[float]]) -> Optional[dict]:
        if not self._alpha_vantage_api_key:
            return None

        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": self._alpha_vantage_api_key,
        }

        try:
            resp = requests.get(self._alpha_vantage_base_url, params=params, timeout=12)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return None

        # Alpha Vantage returns NOTE/Information messages when rate limited.
        if not isinstance(payload, dict):
            return None
        if payload.get("Note") or payload.get("Information"):
            return None

        quote = payload.get("Global Quote") or {}
        if not quote:
            return None

        current = self._safe_float(quote.get("05. price"))
        prev_close = self._safe_float(quote.get("08. previous close"))
        change = self._safe_float(quote.get("09. change"))

        pct_text = (quote.get("10. change percent") or "").replace("%", "").strip()
        change_pct = self._safe_float(pct_text)
        if change_pct is not None:
            change_pct = change_pct / 100.0

        if current is None:
            return None

        # Alpha GLOBAL_QUOTE does not reliably provide company name/currency.
        currency = None
        price_usd = self._convert_currency(current, currency, "USD", fx_cache)
        price_eur = self._convert_currency(current, currency, "EUR", fx_cache)

        return {
            "name": "",
            "current": current,
            "currency": currency,
            "price_usd": price_usd,
            "price_eur": price_eur,
            "prev_close": prev_close,
            "change": change,
            "change_percent": change_pct,
            "market_cap": None,
        }

    # ── yfinance fallback ─────────────────────────────────────────────────────

    def _fetch_yahoo_quotes(
        self,
        symbols: list[str],
        fx_cache: Optional[dict[tuple[str, str], Optional[float]]] = None,
    ) -> tuple[dict, Optional[str]]:
        quote_map: dict[str, Optional[dict]] = {}
        fx_cache = fx_cache or {}

        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                current = None
                prev_close = None
                market_cap = None
                currency = None
                name = ""

                try:
                    info = self._with_retry(
                        lambda t=ticker: t.fast_info, symbol=sym, operation="fast_info"
                    )
                    current = self._safe_attr(info, "last_price")
                    prev_close = self._safe_attr(info, "previous_close")
                    market_cap = self._safe_attr(info, "market_cap")
                    currency = self._safe_str_attr(info, "currency")
                except Exception:
                    pass

                try:
                    full_info = self._with_retry(
                        lambda t=ticker: t.info, symbol=sym, operation="info"
                    )
                    name = full_info.get("longName") or full_info.get("shortName") or ""
                    if current is None:
                        current = self._safe_float(full_info.get("regularMarketPrice"))
                    if prev_close is None:
                        prev_close = self._safe_float(full_info.get("previousClose"))
                    if market_cap is None:
                        market_cap = self._safe_float(full_info.get("marketCap"))
                    if not currency:
                        raw_currency = full_info.get("currency")
                        currency = str(raw_currency).upper() if raw_currency else None
                except Exception:
                    pass

                if current is None:
                    try:
                        hist = self._with_retry(
                            lambda t=ticker: t.history(period="5d"),
                            symbol=sym, operation="history"
                        )
                        if hist is not None and not hist.empty and "Close" in hist:
                            closes = hist["Close"].dropna()
                            if not closes.empty:
                                current = float(closes.iloc[-1])
                                if len(closes) >= 2 and prev_close is None:
                                    prev_close = float(closes.iloc[-2])
                    except Exception:
                        pass

                change = None
                change_pct = None
                if current is not None and prev_close is not None and prev_close != 0:
                    change = current - prev_close
                    change_pct = change / prev_close

                price_usd = self._convert_currency(current, currency, "USD", fx_cache)
                price_eur = self._convert_currency(current, currency, "EUR", fx_cache)

                quote_map[sym] = {
                    "name": name,
                    "current": current,
                    "currency": currency,
                    "price_usd": price_usd,
                    "price_eur": price_eur,
                    "prev_close": prev_close,
                    "change": change,
                    "change_percent": change_pct,
                    "market_cap": market_cap,
                }
                if current is None:
                    quote_map[sym]["error"] = (
                        "No valid last price from Yahoo for this symbol. "
                        "Try another venue suffix (e.g. 0700.HK or 7203.T)."
                    )
            except Exception as exc:
                if self._is_rate_limit_error(exc):
                    quote_map[sym] = {
                        "error": (
                            "Yahoo rate limit hit (Too Many Requests). "
                            "Please retry after a short delay. "
                            "Yahoo 接口限流，请稍后重试。"
                        )
                    }
                else:
                    quote_map[sym] = {"error": f"Yahoo fetch failed: {type(exc).__name__}: {exc}"}
                continue

        return quote_map, None

    # ── Utility helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_query_token(text: str) -> str:
        if not text:
            return ""
        t = str(text).strip()
        t = t.strip("\"'()[]{}")
        t = re.sub(r"[，。！？；：、,.!?;:]+$", "", t)
        return t.strip()

    @staticmethod
    def _looks_like_ticker(text: str) -> bool:
        t = (text or "").strip()
        if not t or " " in t:
            return False
        if re.match(r"^\d{4,6}\.[A-Za-z]{1,4}$", t):
            return True
        if re.match(r"^[A-Za-z]{1,6}-[A-Za-z0-9]{1,6}$", t):
            return True
        if re.match(r"^[A-Za-z]{1,6}\.[A-Za-z0-9]{1,4}$", t):
            return True
        # Pure alphabetic tickers must be uppercase and short to avoid sentence words.
        if re.match(r"^[A-Z]{2,5}$", t):
            return True
        # Futures contracts: BZ=F, CL=F, GC=F, NG=F etc.
        if re.match(r"^[A-Z]{1,4}=F$", t):
            return True
        # Yahoo spot/FX symbols: XAUUSD=X, XAGUSD=X, EURUSD=X.
        if re.match(r"^[A-Z0-9]{3,12}=X$", t):
            return True
        return False

    @staticmethod
    def _looks_like_isin(text: str) -> bool:
        t = (text or "").strip().upper()
        return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", t))

    @staticmethod
    def _looks_like_wkn(text: str) -> bool:
        t = (text or "").strip().upper()
        return bool(re.fullmatch(r"[A-Z0-9]{6}", t))

    @staticmethod
    def _search_candidates(q: str) -> list[str]:
        candidates = [q]
        upper_q = q.upper()
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", upper_q):
            prefixed = f"ISIN {upper_q}"
            if prefixed not in candidates:
                candidates.append(prefixed)
        return candidates

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _safe_attr(obj, attr: str) -> Optional[float]:
        try:
            val = getattr(obj, attr, None)
            return float(val) if val is not None else None
        except Exception:
            return None

    @staticmethod
    def _safe_str_attr(obj, attr: str) -> Optional[str]:
        try:
            val = getattr(obj, attr, None)
            if val is None:
                return None
            text = str(val).strip().upper()
            return text or None
        except Exception:
            return None

    def _convert_currency(
        self,
        amount: Optional[float],
        from_currency: Optional[str],
        to_currency: str,
        fx_cache: dict,
    ) -> Optional[float]:
        if amount is None:
            return None
        src = (from_currency or "").upper()
        dst = (to_currency or "").upper()
        if not src or not dst:
            return None
        if src == dst:
            return amount
        key = (src, dst)
        if key not in fx_cache:
            fx_cache[key] = self._fetch_fx_rate(src, dst)
        rate = fx_cache.get(key)
        if rate is None:
            return None
        return amount * rate

    def _fetch_fx_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        # EODHD Forex (primary).
        if self._eodhd_session:
            rate = self._fetch_fx_rate_eodhd(from_currency, to_currency)
            if rate is not None and rate > 0:
                return rate
        # [yfinance FX fallback disabled]
        return None

    def _fetch_fx_rate_eodhd(self, from_currency: str, to_currency: str) -> Optional[float]:
        symbol = f"{from_currency}{to_currency}.FOREX"
        try:
            resp = self._eodhd_session.get(
                f"{self._eodhd_base_url}/real-time/{symbol}",
                params={"api_token": self._eodhd_api_key, "fmt": "json"},
                timeout=self._eodhd_timeout_seconds,
            )
            if resp.status_code >= 400:
                # Try inverse.
                symbol_inv = f"{to_currency}{from_currency}.FOREX"
                resp = self._eodhd_session.get(
                    f"{self._eodhd_base_url}/real-time/{symbol_inv}",
                    params={"api_token": self._eodhd_api_key, "fmt": "json"},
                    timeout=self._eodhd_timeout_seconds,
                )
                if resp.status_code >= 400:
                    return None
                data = resp.json()
                rate = self._safe_float(data.get("close") or data.get("last"))
                return (1.0 / rate) if rate and rate > 0 else None
            data = resp.json()
            return self._safe_float(data.get("close") or data.get("last"))
        except Exception:
            return None

    def _fetch_last_price_for_symbol(self, symbol: str) -> Optional[float]:
        if not _YFINANCE_AVAILABLE:
            return None
        try:
            ticker = yf.Ticker(symbol)
            info = self._with_retry(
                lambda t=ticker: t.fast_info, symbol=symbol, operation="fx_fast_info"
            )
            price = self._safe_attr(info, "last_price")
            if price is not None and price > 0:
                return price
        except Exception:
            pass
        try:
            ticker = yf.Ticker(symbol)
            hist = self._with_retry(
                lambda t=ticker: t.history(period="5d"), symbol=symbol, operation="fx_history"
            )
            if hist is not None and not hist.empty and "Close" in hist:
                val = hist["Close"].dropna()
                if not val.empty:
                    close = float(val.iloc[-1])
                    if close > 0:
                        return close
        except Exception:
            pass
        return None

    def _format_market_text(self, sym: str, quote: Optional[dict], api_error: Optional[str]) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"=== {sym} Market Data ({ts}) ===", f"Quote time: {ts}"]
        provider = (quote or {}).get("_provider")
        resolved_symbol = (quote or {}).get("_resolved_symbol")
        asset_class = (quote or {}).get("_asset_class")
        if resolved_symbol:
            lines.append(f"Resolved symbol: {resolved_symbol}")
        if asset_class:
            lines.append(f"Asset class: {asset_class}")
        if provider:
            lines.append(f"Data source: {provider}")

        if api_error:
            lines.append(f"Data source error: {api_error}")

        if not quote:
            lines.extend([
                "Data source error: No quote available — please check the symbol format",
                "Current price: N/A", "Price (USD): N/A", "Price (EUR): N/A",
                "Change: N/A", "Market cap: N/A",
            ])
            return "\n".join(lines)

        if quote.get("error"):
            lines.append(f"Data source note: {quote['error']}")

        if quote.get("name"):
            lines.append(f"Name: {quote['name']}")

        price = quote.get("current")
        currency = quote.get("currency")
        price_usd = quote.get("price_usd")
        price_eur = quote.get("price_eur")
        pct = quote.get("change_percent")
        change = quote.get("change")
        market_cap = quote.get("market_cap")

        if price is not None and currency:
            lines.append(f"Current price: {price:.2f} {currency}")
        elif price is not None:
            lines.append(f"Current price: {price:.2f}")
        else:
            lines.append("Current price: N/A")

        lines.append(f"Price (USD): {price_usd:.2f}" if price_usd is not None else "Price (USD): N/A")
        lines.append(f"Price (EUR): {price_eur:.2f}" if price_eur is not None else "Price (EUR): N/A")

        if isinstance(change, (int, float)) and isinstance(pct, (int, float)):
            direction = "+" if change >= 0 else ""
            lines.append(f"Change: {direction}{change:.2f} ({direction}{pct:.2%})")
        else:
            lines.append("Change: N/A")

        lines.append(
            f"Market cap: {self._format_cap(market_cap)}" if market_cap is not None else "Market cap: N/A"
        )
        return "\n".join(lines)

    @staticmethod
    def _format_cap(value: float) -> str:
        if value >= 1e12:
            return f"{value / 1e12:.2f}T"
        if value >= 1e9:
            return f"{value / 1e9:.2f}B"
        if value >= 1e6:
            return f"{value / 1e6:.2f}M"
        if value >= 1e3:
            return f"{value / 1e3:.2f}K"
        return f"{value:,.0f}"

    def _with_retry(self, call, symbol: str, operation: str):
        attempts = max(1, self.retry_max_attempts)
        last_exc = None
        for attempt in range(1, attempts + 1):
            self._wait_for_rate_slot()
            self._request_timestamps.append(time.monotonic())
            try:
                return call()
            except Exception as exc:
                last_exc = exc
                if self._is_rate_limit_error(exc) and attempt < attempts:
                    if self.retry_verbose:
                        print(
                            f"[Yahoo Retry] symbol={symbol} op={operation} "
                            f"attempt={attempt}/{attempts} reason=rate_limit"
                        )
                    self._sleep_with_backoff(attempt)
                    continue
                raise
        if last_exc:
            raise last_exc

    def _wait_for_rate_slot(self):
        limit = max(1, self.max_requests_per_minute)
        window = 60.0
        now = time.monotonic()
        while self._request_timestamps and now - self._request_timestamps[0] > window:
            self._request_timestamps.popleft()
        if len(self._request_timestamps) < limit:
            return
        sleep_seconds = window - (now - self._request_timestamps[0]) + 0.01
        if sleep_seconds > 0:
            if self.retry_verbose:
                print(
                    f"[Yahoo Throttle] client-side limit reached "
                    f"({limit}/min), waiting {sleep_seconds:.2f}s"
                )
            time.sleep(sleep_seconds)

    def _sleep_with_backoff(self, attempt: int):
        base = max(0.1, self.retry_base_delay_seconds)
        delay = base * (2 ** (attempt - 1))
        jitter = random.uniform(0.0, base * 0.25)
        total_delay = delay + jitter
        if self.retry_verbose:
            print(f"[Yahoo Backoff] waiting {total_delay:.2f}s before retry")
        time.sleep(total_delay)

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if "ratelimit" in name or "rate_limit" in name:
            return True
        return any(p in msg for p in ("too many requests", "rate limited", "429"))
