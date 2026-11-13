import re
import time
import difflib
import sys
import argparse
from ingestion.market_fetcher import MarketFetcher
from ingestion.csv_watchlist import lookup_by_isin_or_wkn
from rag.embedder import Embedder
from rag.vector_store import FAISSVectorStore
from rag.retriever import RAGRetriever
from config_loader import load_config

cfg = load_config(strict=True)

parser = argparse.ArgumentParser(description="Financial RAG query interface")
parser.add_argument("--agent", action="store_true", help="Use the agentic MiniMax tool-calling backend")
parser.add_argument(
    "--llm",
    choices=["auto", "ollama", "minimax"],
    default="auto",
    help="Choose the answer model for pipeline RAG: auto (default), ollama, or minimax",
)
args = parser.parse_args()

_use_agent = args.agent
_llm_mode = args.llm

embedder = Embedder(cfg["rag"]["embedding_model"])
store = FAISSVectorStore(embedder=embedder, dim=embedder.dim)
market_fetcher = MarketFetcher(
    cfg.get("market", {}).get("symbols", []),
    cfg.get("market", {}),
)

# Choose retriever backend
if _use_agent:
    print("\n[Mode] Multi-Agent RAG (Orchestrator: DocumentAgent + MarketAgent)")
    from rag.orchestrator import Orchestrator
    retriever = Orchestrator(embedder, store, market_fetcher, cfg)
else:
    if _llm_mode == "minimax":
        print("\n[Mode] Pipeline RAG (Fixed retrieval → MiniMax cloud generation)")
    elif _llm_mode == "ollama":
        print("\n[Mode] Pipeline RAG (Fixed retrieval → Ollama local generation)")
    else:
        print("\n[Mode] Pipeline RAG (Fixed retrieval → generation)")
    retriever = RAGRetriever(embedder, store, cfg)
QUOTE_CACHE_TTL_SECONDS = int(cfg.get("market", {}).get("quote_cache_ttl_seconds", 20))
_LIVE_COMMODITY_KEYS = {
    "WTI", "BRENT", "BZ=F", "CL=F", "XAUUSD=X", "XAGUSD=X",
    "BRENT CRUDE OIL", "BRENT CRUDE", "CRUDE OIL", "GOLD", "SILVER",
}


def _is_live_quote_part(part: str) -> bool:
    forced_asset_class, raw = market_fetcher._split_forced_asset_prefix(part)
    upper = _normalize_quote_token(raw).upper()
    if forced_asset_class == "commodity":
        return True
    if upper in _LIVE_COMMODITY_KEYS:
        return True
    return market_fetcher._resolve_commodity_alias(raw) is not None


_STRICT_TICKER_PATTERNS = [
    r"\b[A-Za-z]{2}[A-Za-z0-9]{9}\d\b",      # ISIN
    r"\b[A-Za-z]{1,6}-[A-Za-z0-9]{1,6}\b",    # BRK-B, BTC-USD
    r"\b\d{4,6}\.[A-Za-z]{1,4}\b",          # 0700.HK, 000001.SS
    r"\b[A-Za-z]{1,6}\.[A-Za-z0-9]{1,4}\b",  # RDS.A
    r"\b[A-Z]{2,5}\b",                        # AAPL, QQQ
]


# Words that indicate the user is asking about document *content*, not requesting a live quote.
# If any of these appear in the question, _has_explicit_quote_trigger should NOT fire.
_RAG_CONTEXT_WORDS = frozenset({
    "document", "sentence", "text", "found", "original", "file", "chunk", "passage",
    "dokument", "satz", "passage", "abschnitt", "quelle", "aus dem", "laut dem",
})


def _has_explicit_quote_trigger(raw_question: str) -> bool:
    q = (raw_question or "").strip().lower()
    if not q:
        return False
    # If the question is about document content, it's a RAG question, not a live quote request.
    if any(w in q for w in _RAG_CONTEXT_WORDS):
        return False
    trigger_patterns = (
        # Bare "price"/"quote" alone is too broad (appears in RAG questions too).
        # Require either a qualifier (stock/etf) or a connector (of/for).
        r"\b(stock\s+price|etf\s+price)\b",
        r"\b(price|quote)\s+(of|for)\b",
        r"(股价|价格|行情|报价)",
        r"\b(kurs|preis)\b",
    )
    return any(re.search(p, q, flags=re.IGNORECASE) for p in trigger_patterns)


def _normalize_quote_token(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = t.strip("\"'()[]{}")
    t = re.sub(r"[，。！？；：、,.!?;:]+$", "", t)
    return t.strip()


def _make_quote_cache_key(parts: list[str]) -> str:
    normalized = [_normalize_quote_token(p).upper() for p in parts if _normalize_quote_token(p)]
    return "|".join(sorted(normalized))


def _is_market_intent_question(raw_question: str) -> bool:
    raw = (raw_question or "").strip()
    q = raw.lower()
    if not q:
        return False

    if _has_explicit_quote_trigger(raw):
        return True

    keywords = (
        "stock", "etf", "isin", "quote", "price", "market",
        "股价", "价格", "行情", "报价", "股票", "基金",
        "aktie", "aktienkurs", "kurs", "preis", "markt",
    )
    if not any(k in q for k in keywords):
        return False
    return any(re.search(p, raw) for p in _STRICT_TICKER_PATTERNS)


#原理说明
#桥接的核心思路：在 retriever.py 中增加一个 _last_market_context 字段，行情查询完成后把结果注入进去。
# 下次用户问RAG问题时，prompt里自动附加一个 "Recent Market Data" 区块，让LLM能看到刚才查到的价格

def _format_market_context(docs: list[dict]) -> str:
    """Format quote docs into a concise text block for RAG prompt injection."""
    if not docs:
        return ""
    lines = ["The following live market prices were fetched in this session:"]
    for doc in docs:
        text = doc.get("text", "")
        symbol = doc.get("symbol", "?")
        current = _extract_line(text, "Current price:")
        change = _extract_line(text, "Change:")
        provider = _extract_line(text, "Data source:")
        if current and current != "N/A":
            lines.append(f"  {symbol}: {current}  change {change}  (source: {provider})")
        else:
            lines.append(f"  {symbol}: no data available")
    return "\n".join(lines)


def _extract_line(text: str, prefix: str) -> str:
    for line in (text or "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


class QuoteSession:
    """Holds per-run quote cache/state, replacing the previous module-level globals.

    Keeping this state on an instance (instead of module globals) avoids it leaking
    across reruns/imports of this module, e.g. under a test runner or a future
    long-lived server process that imports query.py more than once.
    """

    def __init__(self, market_fetcher, retriever, cache_ttl_seconds: int):
        self.market_fetcher = market_fetcher
        self.retriever = retriever
        self.cache_ttl_seconds = cache_ttl_seconds
        self.quote_cache: dict[str, tuple[float, list[dict]]] = {}
        self.last_quote_docs: list[dict] = []
        # Session-wide accumulator: symbol -> latest doc (deduplicates re-queries of same symbol)
        self.session_quote_map: dict[str, dict] = {}
        # Session-permanent fetch cache: cache_key -> docs (no TTL; same query never re-hits API in same session)
        self.session_fetch_cache: dict[str, list[dict]] = {}

    def fetch_quotes_with_cache(self, parts: list[str]) -> tuple[list[dict], bool]:
        cache_key = _make_quote_cache_key(parts)
        now = time.monotonic()
        use_session_permanent_cache = not any(_is_live_quote_part(part) for part in parts)

        # 1. Short-term TTL cache (same batch repeated within N seconds)
        if cache_key and cache_key in self.quote_cache:
            ts, cached_docs = self.quote_cache[cache_key]
            if now - ts <= self.cache_ttl_seconds:
                return cached_docs, True

        # 2. Session-permanent cache (same query combination, any time this session — no API call)
        if use_session_permanent_cache and cache_key and cache_key in self.session_fetch_cache:
            return self.session_fetch_cache[cache_key], True

        docs = self.market_fetcher.fetch_for_queries(parts)
        if cache_key:
            self.quote_cache[cache_key] = (now, docs)
            if use_session_permanent_cache:
                self.session_fetch_cache[cache_key] = docs
        return docs, False

    def set_last_quote_docs(self, docs: list[dict]):
        self.last_quote_docs = list(docs or [])
        # Merge into session-wide accumulator; latest result wins for same symbol.
        for doc in (docs or []):
            sym = doc.get("symbol", "")
            if sym:
                self.session_quote_map[sym] = doc
        # Bridge: inject all session quotes into RAG retriever context.
        all_session_docs = list(self.session_quote_map.values())
        self.retriever.set_market_context(_format_market_context(all_session_docs))

    def print_quote_summary(self):
        # Use session-wide history if available, else fall back to last batch only.
        all_docs = list(self.session_quote_map.values()) if self.session_quote_map else self.last_quote_docs
        if not all_docs:
            print("No recent quote results found. Please run /quote first.")
            return

        success_rows: list[tuple[str, str, str, str]] = []
        failed_rows: list[tuple[str, str]] = []

        for doc in all_docs:
            symbol = doc.get("symbol", "N/A")
            text = doc.get("text", "")
            current = _extract_line(text, "Current price:")
            provider = _extract_line(text, "Data source:") or "n/a"
            note = _extract_line(text, "Data source note:")

            if not current or current == "N/A":
                reason = note or "No data available from providers"
                failed_rows.append((symbol, reason))
            else:
                change = _extract_line(text, "Change:") or "N/A"
                success_rows.append((symbol, current, provider, change))

        total = len(all_docs)
        success = len(success_rows)
        failed = len(failed_rows)
        success_rate = (success / total * 100.0) if total else 0.0

        print("\n Quote Summary (session total):\n")
        print(f"Total: {total} | Success: {success} | Failed: {failed} | Success rate: {success_rate:.1f}%\n")

        if success_rows:
            print("Successful lookups:")
            print(f"{'Symbol':<14} {'Current':<18} {'Provider':<10} {'Change'}")
            print("-" * 70)
            for symbol, current, provider, change in success_rows:
                print(f"{symbol:<14} {current:<18} {provider:<10} {change}")
            print()

        if failed_rows:
            print("Failed lookups:")
            print(f"{'Symbol':<14} Reason")
            print("-" * 70)
            for symbol, reason in failed_rows:
                print(f"{symbol:<14} {reason}")
            print()


_quote_session = QuoteSession(market_fetcher, retriever, QUOTE_CACHE_TTL_SECONDS)


def _is_quote_summary_request(raw_question: str) -> bool:
    q = (raw_question or "").strip().lower()
    if not q:
        return False
    if q.startswith("/quote_summary"):
        return True

    # Heuristic NLP-style intent detection for natural-language summary requests.
    if _looks_like_quote_summary_intent(q):
        return True

    summary_words = ("summary", "summarize", "recap", "overview", "总结", "汇总", "概览")
    quote_words = ("quote", "quotes", "price", "prices", "result", "results", "行情", "股价", "报价")
    return any(w in q for w in summary_words) and any(w in q for w in quote_words)


def _tokenize_for_intent(text: str) -> set[str]:
    # Keep CJK characters and latin words; normalize separators to spaces.
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (text or "").lower())
    return {tok for tok in cleaned.split() if tok}


def _fuzzy_contains(query: str, candidates: tuple[str, ...], threshold: float = 0.86) -> bool:
    for cand in candidates:
        if cand in query:
            return True
        ratio = difflib.SequenceMatcher(None, query, cand).ratio()
        if ratio >= threshold:
            return True
    return False


def _looks_like_quote_summary_intent(q: str) -> bool:
    # 1) Strong phrase patterns
    strong_patterns = (
        r"\b(summarize|summary|recap|overview)\b.*\b(quote|quotes|price|prices|result|results)\b",
        r"\b(which|what)\b.*\b(succeeded|failed|success|failure)\b",
        r"\b(show|list)\b.*\b(success|failed|failure)\b",
        r"(总结|汇总|概览).*(报价|行情|结果|成功|失败)",
        r"(哪些|哪个).*(成功|失败|查到|没查到)",
    )
    if any(re.search(p, q, flags=re.IGNORECASE) for p in strong_patterns):
        return True

    # 2) Token-overlap similarity against intent templates
    templates = [
        "please summarize the quote results above",
        "summarize latest quote results",
        "which quotes succeeded and failed",
        "show me success and failed lookups",
        "总结上面的行情结果",
        "汇总最近一次报价结果",
        "哪些查到了哪些没查到",
    ]
    q_tokens = _tokenize_for_intent(q)
    if q_tokens:
        for tmpl in templates:
            t_tokens = _tokenize_for_intent(tmpl)
            if not t_tokens:
                continue
            overlap = len(q_tokens & t_tokens) / len(t_tokens)
            if overlap >= 0.45:
                return True

    # 3) Contextual cues: reference to previous output + summary verb.
    deictic_words = ("above", "previous", "latest", "last", "上面", "刚才", "前面", "最近")
    summary_verbs = ("summarize", "summary", "recap", "overview", "总结", "汇总", "概览", "归纳")
    if any(w in q for w in deictic_words) and any(w in q for w in summary_verbs):
        return True

    # 4) Fuzzy match for very short typo-prone asks.
    short_targets = (
        "summarize quote results",
        "quote summary",
        "总结报价结果",
    )
    if len(q) <= 40 and _fuzzy_contains(q, short_targets):
        return True

    return False


def _handle_quote_summary_request(raw_question: str) -> bool:
    if not _is_quote_summary_request(raw_question):
        return False
    _quote_session.print_quote_summary()
    return True


_ASSET_CLASS_FOLDERS = {"equity": "EQUITY", "etf": "ETF", "fond": "FONDS"}
_ASSET_CLASS_MENU = {
    "1": "equity", "equity": "equity", "aktie": "equity", "stock": "equity",
    "2": "etf", "etf": "etf",
    "3": "fond", "fond": "fond", "fonds": "fond", "fund": "fond",
}
_ASSET_API_PREFIX = {"equity": "stock:", "etf": "etf:", "fond": ""}


def _is_price_intent(raw_question: str) -> bool:
    q = (raw_question or "").strip()
    q_lower = q.lower()
    if q_lower.startswith("/quote_summary"):
        return False
    if q_lower.startswith("/quote"):
        return True
    upper = q.upper()
    if re.fullmatch(r"(?:ISIN[-:\s])?([A-Z]{2}[A-Z0-9]{9}\d)", upper):
        return True
    if re.fullmatch(r"WKN[-:\s]([A-Z0-9]{6})", upper):
        return True
    return _is_market_intent_question(q)


def _prompt_asset_class() -> str | None:
    print("\n请选择资产类型 / Please choose the asset class:")
    print("  1) Equity 股票")
    print("  2) ETF")
    print("  3) Fond 基金")
    choice = input("请输入 1/2/3 或 equity/etf/fond: ").strip().lower()
    asset_class = _ASSET_CLASS_MENU.get(choice)
    if not asset_class:
        print("未识别的资产类型,已取消本次查询。")
    return asset_class


def _prompt_identifier() -> str | None:
    identifier = input("请输入 ISIN 或 WKN: ").strip()
    if not identifier:
        print("未输入 ISIN/WKN,已取消本次查询。")
        return None
    return identifier


def _format_local_quote_text(identifier: str, asset_class: str, row: dict) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M")
    lines = [f"=== {identifier} Market Data ({ts}) ===", f"Asset class: {asset_class}"]
    lines.append(
        f"Data source: Local watchlist (csv_update/{_ASSET_CLASS_FOLDERS[asset_class]}/{row['_source_file']})"
    )
    if row.get("name"):
        lines.append(f"Name: {row['name']}")
    if row.get("isin"):
        lines.append(f"ISIN: {row['isin']}")
    if row.get("wkn"):
        lines.append(f"WKN: {row['wkn']}")
    lines.append(f"Current price: {row['ask']} EUR" if row.get("ask") else "Current price: N/A")
    if row.get("bid"):
        lines.append(f"Bid: {row['bid']} EUR")
    change = row.get("change_prev_day")
    lines.append(f"Change: {change}%" if change else "Change: N/A")
    if row.get("change_1m"):
        lines.append(f"Change (1M): {row['change_1m']}%")
    if row.get("change_1y"):
        lines.append(f"Change (1Y): {row['change_1y']}%")
    return "\n".join(lines)


def _handle_price_query(raw_question: str) -> bool:
    """Interactive price flow: ask asset class, then ISIN/WKN.

    Checks the local csv_update watchlist first (free, no API quota used);
    falls back to the live provider chain (Finnhub -> EODHD -> Alpha Vantage
    -> yfinance, already wired up in MarketFetcher) only if not found locally.
    """
    if not _is_price_intent(raw_question):
        return False

    asset_class = _prompt_asset_class()
    if not asset_class:
        return True
    identifier = _prompt_identifier()
    if not identifier:
        return True

    local_row = lookup_by_isin_or_wkn(identifier, asset_class)
    if local_row:
        text = _format_local_quote_text(identifier, asset_class, local_row)
        _quote_session.set_last_quote_docs([{"symbol": local_row.get("name") or identifier, "text": text, "source": "csv_local"}])
        print(f"\n 本地 watchlist 命中 (csv_update/{_ASSET_CLASS_FOLDERS[asset_class]}):\n")
        print(text)
        print("-" * 50)
        return True

    prefix = _ASSET_API_PREFIX.get(asset_class, "")
    docs, from_cache = _quote_session.fetch_quotes_with_cache([f"{prefix}{identifier}"])
    if not docs:
        print(f"本地 watchlist 未找到 {identifier},外部数据源也未查询到相应行情。")
        return True

    _quote_session.set_last_quote_docs(docs)
    print("\n 本地 watchlist 未命中,已改用外部数据源 (Finnhub -> EODHD -> Alpha Vantage -> yfinance):\n")
    if from_cache:
        print(f"(cached within {QUOTE_CACHE_TTL_SECONDS}s)\n")
    for doc in docs:
        text = doc.get("text", "").strip()
        if text:
            print(text)
            print("-" * 50)

    return True

print("=" * 60)
print("  Financial Intelligence RAG System")
print("=" * 60)
print("""
I am your personal financial assistant, powered by a local RAG
(Retrieval-Augmented Generation) system. Here is what I can do:

  📄 Knowledge base
     Answer questions based on your ingested documents
     (PDFs, emails). Just ask naturally in English, German,
     or Chinese.

  📈 Live market prices  (local watchlist + Finnhub/EODHD/Alpha Vantage/yfinance)
     Ask about a price (e.g. "/quote", "What is the price of ...",
     or a bare ISIN/WKN) and you'll be asked to:
       1) choose the asset class: Equity / ETF / Fond
       2) enter the ISIN or WKN
     The local csv_update watchlist is checked first (no API calls);
     if not found there, it falls back to the live data providers.

  💬 Commands
       clear   — clear conversation history
      /quote_summary — summarize the latest quote results
       quit    — exit
""")
print("=" * 60)
print("  What would you like to know?")
print("=" * 60)

while True:
    try:
        question = input("\n Your question: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nBYE!")
        break
    
    if not question:
        continue
    if question.lower() == "quit":
        break
    if question.lower() == "clear":
        retriever.clear_history()
        continue
    if not _use_agent:
        if _handle_quote_summary_request(question):
            continue
        if _handle_price_query(question):
            continue

    print("\n Answer:\n")
    if _use_agent:
        response = retriever.query(question, stream=True)
    else:
        response = retriever.query(question, stream=True, force_cloud=(_llm_mode == "minimax"))
    if response and not any(c.isalnum() for c in response):
        print("(empty response)")