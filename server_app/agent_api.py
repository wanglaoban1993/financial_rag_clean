from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ingestion.market_fetcher import MarketFetcher
from rag.orchestrator import Orchestrator
from rag.embedder import Embedder
from rag.vector_store import FAISSVectorStore
from config_loader import load_config

app = FastAPI(title="Financial RAG Agent API", version="0.1.0")

_agent: Orchestrator | None = None
# Serializes /chat requests against the single shared Orchestrator instance —
# its conversation history is mutable instance state, so concurrent requests
# from different dashboard sessions must not interleave.
_agent_lock = threading.Lock()

# Global daily cap on LLM calls, not per-visitor: every /chat request reaches this
# service from the *dashboard* container regardless of which browser session
# triggered it (Streamlit calls out server-side), so a per-client-IP limit here
# would only ever see one caller. A global budget is what actually bounds cost
# on a publicly-linked demo. Per-session fairness is enforced in dashboard.py instead.
DAILY_MESSAGE_LIMIT = int(os.getenv("DEMO_DAILY_MESSAGE_LIMIT", "0"))  # 0 = unlimited
_quota_lock = threading.Lock()
_quota_date: date | None = None
_quota_count = 0


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class Source(BaseModel):
    kind: str  # pdf | email | market | watchlist | quote | file | document
    title: str
    detail: str = ""
    snippet: str = ""


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source] = []
    # Numbers/dates in the answer not found in the retrieved sources (heuristic).
    grounding_warnings: list[str] = []


@app.on_event("startup")
def startup() -> None:
    global _agent
    cfg = load_config(strict=True)

    embedder = Embedder(cfg["rag"]["embedding_model"])
    # main.py (a separate process) keeps adding to the index; pick that up live.
    store = FAISSVectorStore(embedder=embedder, dim=embedder.dim, auto_reload=True)
    market_fetcher = MarketFetcher(
        cfg.get("market", {}).get("symbols", []),
        cfg.get("market", {}),
    )
    _agent = Orchestrator(embedder, store, market_fetcher, cfg)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "agent_ready": _agent is not None}


def _check_and_consume_daily_quota() -> bool:
    """Return True if under budget (and count this call), False if today's cap is used up."""
    if DAILY_MESSAGE_LIMIT <= 0:
        return True

    global _quota_date, _quota_count
    today = date.today()
    with _quota_lock:
        if _quota_date != today:
            _quota_date = today
            _quota_count = 0
        if _quota_count >= DAILY_MESSAGE_LIMIT:
            return False
        _quota_count += 1
        return True


_NOT_READY_MESSAGE = "Agent is still starting up, please retry shortly."
_QUOTA_MESSAGE = (
    "This demo has reached its message limit for today. Please check back tomorrow, "
    "or see the README for how to run the full project yourself."
)


def _run_chat(
    request: ChatRequest, on_event: Callable[[str], None] | None = None
) -> tuple[str, list[dict[str, str]], list[str]]:
    # Lock covers set_history + query + reading last_sources/last_grounding_warnings:
    # all of them touch the shared Orchestrator's per-request state.
    with _agent_lock:
        _agent.set_history([m.model_dump() for m in request.history])
        answer = _agent.query(request.message, on_event=on_event)
        return answer, list(_agent.last_sources), list(_agent.last_grounding_warnings)


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    if _agent is None:
        return ChatResponse(answer=_NOT_READY_MESSAGE)

    if not _check_and_consume_daily_quota():
        return ChatResponse(answer=_QUOTA_MESSAGE)

    answer, sources, warnings = _run_chat(request)
    return ChatResponse(answer=answer, sources=sources, grounding_warnings=warnings)


def _ndjson(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


@app.post("/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Same as /chat, but streams newline-delimited JSON events while the agent
    works: {"type":"status","text":...} progress lines, then exactly one
    {"type":"answer","answer":...,"sources":[...],"grounding_warnings":[...]} (or {"type":"error",...})."""
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    def single(answer: str) -> StreamingResponse:
        return StreamingResponse(
            iter([_ndjson({"type": "answer", "answer": answer, "sources": []})]),
            media_type="application/x-ndjson",
            headers=headers,
        )

    if _agent is None:
        return single(_NOT_READY_MESSAGE)
    if not _check_and_consume_daily_quota():
        return single(_QUOTA_MESSAGE)

    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def worker() -> None:
        try:
            answer, sources, warnings = _run_chat(
                request, on_event=lambda text: events.put({"type": "status", "text": text})
            )
            events.put({
                "type": "answer", "answer": answer, "sources": sources,
                "grounding_warnings": warnings,
            })
        except Exception as exc:
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream() -> Iterator[str]:
        while (event := events.get()) is not None:
            yield _ndjson(event)

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers=headers)
