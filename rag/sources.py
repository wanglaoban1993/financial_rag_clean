# ============================================================
#  rag/sources.py — turn raw tool results into citable sources
#
#  The tools in rag/agent_tools.py already return where their data came
#  from (PDF filename + page, email subject, quote provider, watchlist row),
#  but only as JSON text the LLM may or may not repeat. This pulls those
#  fields out deterministically so the API can return them next to the answer.
# ============================================================

from __future__ import annotations

import json
import re

_SNIPPET_CHARS = 240
_LABEL_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<rest>.*)$")


def _snippet(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= _SNIPPET_CHARS else text[:_SNIPPET_CHARS].rstrip() + "…"


def _from_search_documents(payload: dict) -> list[dict]:
    sources = []
    for passage in payload.get("passages") or []:
        label = str(passage.get("source") or "")
        match = _LABEL_RE.match(label)
        kind = match.group("tag").strip().lower() if match else "document"
        title = match.group("rest").strip() if match else label
        date = str(passage.get("date") or "")
        sources.append({
            "kind": kind,
            "title": title or label or "unknown",
            "detail": "" if date == "unknown" else date,
            "snippet": _snippet(passage.get("text")),
        })
    return sources


def _from_watchlist(payload: dict) -> list[dict]:
    sources = []
    for row in payload.get("matches") or []:
        bits = [row.get("watchlist_name"), f"ISIN {row['isin']}" if row.get("isin") else None, row.get("index")]
        sources.append({
            "kind": "watchlist",
            "title": str(row.get("name") or "unknown"),
            "detail": " · ".join(str(b) for b in bits if b),
            "snippet": _snippet(row.get("comment")),
        })
    return sources


def _from_watchlist_rows(payload: dict) -> list[dict]:
    """get_watchlist: cite the list plus its first few rows, not all 30."""
    rows = payload.get("rows") or []
    sources = _from_watchlist({"matches": rows[:5]})
    if payload.get("watchlist_name"):
        ranking = f" · ranked by {payload['sorted_by']}" if payload.get("sorted_by") else ""
        sources.insert(0, {
            "kind": "watchlist",
            "title": str(payload["watchlist_name"]),
            "detail": f"{payload.get('asset_group') or ''} list{ranking}".strip(),
            "snippet": "",
        })
    return sources


def _from_quotes(payload: dict) -> list[dict]:
    sources = []
    for quote in payload.get("quotes") or []:
        price = quote.get("price")
        price_text = f"{price} {quote.get('currency') or ''}".strip() if price is not None else ""
        bits = [price_text, quote.get("timestamp"), quote.get("source")]
        sources.append({
            "kind": "quote",
            "title": str(quote.get("symbol") or "?"),
            "detail": " · ".join(str(b) for b in bits if b),
            "snippet": "",
        })
    return sources


def _from_profile(payload: dict) -> list[dict]:
    listings = payload.get("listings") or []
    if not listings:
        return []
    main = listings[0]
    bits = [main.get("symbol"), main.get("type"), main.get("isin")]
    return [{
        "kind": "instrument",
        "title": str(main.get("name") or main.get("symbol") or "unknown"),
        "detail": " · ".join(str(b) for b in bits if b),
        "snippet": _snippet(payload.get("summary")),
    }]


def _from_price_history(payload: dict) -> list[dict]:
    latest = payload.get("latest")
    if not isinstance(latest, dict):
        return []
    change = latest.get("change_pct")
    bits = [f"close {latest.get('close')}", f"{change:+.2f}%" if isinstance(change, (int, float)) else None, latest.get("date")]
    return [{
        "kind": "quote",
        "title": f"{payload.get('symbol', '?')} daily history",
        "detail": " · ".join(str(b) for b in bits if b),
        "snippet": "",
    }]


def _from_news(payload: dict) -> list[dict]:
    return [{
        "kind": "news",
        "title": str(a.get("title") or "untitled"),
        "detail": str(a.get("date") or ""),
        "snippet": _snippet(a.get("excerpt")),
    } for a in (payload.get("articles") or [])[:5]]


def _from_sentiment(payload: dict) -> list[dict]:
    daily = payload.get("daily") or []
    if not daily:
        return []
    return [{
        "kind": "sentiment",
        "title": f"{payload.get('symbol', '?')} news sentiment",
        "detail": f"{daily[0].get('date')} to {daily[-1].get('date')}",
        "snippet": "",
    }]


def _from_file(payload: dict) -> list[dict]:
    if not payload.get("file"):
        return []
    return [{"kind": "file", "title": str(payload["file"]), "detail": "", "snippet": ""}]


def _from_email(payload: dict) -> list[dict]:
    if not payload.get("subject"):
        return []
    date = str(payload.get("date") or "")
    return [{
        "kind": "email",
        "title": f"Subject: {payload['subject']}",
        "detail": "" if date == "unknown" else date,
        "snippet": "",
    }]


_EXTRACTORS = {
    "search_documents": _from_search_documents,
    "search_watchlist": _from_watchlist,
    "get_watchlist": _from_watchlist_rows,
    "get_market_quote": _from_quotes,
    "get_instrument_profile": _from_profile,
    "get_price_history": _from_price_history,
    "get_company_news": _from_news,
    "get_news_sentiment": _from_sentiment,
    "read_file": _from_file,
    "read_email": _from_email,
}


def extract_sources(tool_name: str, result: str) -> list[dict]:
    """Sources cited by one tool result. Returns [] for tools that don't
    produce citable data (list_indexed_sources), error/empty results, or
    anything that isn't the expected JSON."""
    extractor = _EXTRACTORS.get(tool_name)
    if extractor is None:
        return []
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    return extractor(payload)


def merge_sources(existing: list[dict], new: list[dict]) -> list[dict]:
    """Append `new` to `existing`, skipping entries already present."""
    seen = {(s["kind"], s["title"], s["detail"]) for s in existing}
    merged = list(existing)
    for source in new:
        key = (source["kind"], source["title"], source["detail"])
        if key not in seen:
            seen.add(key)
            merged.append(source)
    return merged
