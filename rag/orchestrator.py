# ============================================================
#  rag/orchestrator.py — LangGraph supervisor over DocumentAgent and
#  MarketAgent, with an LLM router and a critic-driven feedback loop.
#
#  Drop-in replacement for AgentRetriever: same query()/clear_history()/
#  set_history()/set_market_context() surface, so query.py only needs to
#  swap the import.
#
#  Graph shape: route -> {document | resolve_identifier -> market} -> critic
#  -> either loop back into document/market for a targeted follow-up, or
#  fall through to synthesize -> END. "both" questions enter through
#  document (like plain document questions) rather than running both agents
#  blindly — the critic is what turns "run both agents once and concatenate"
#  into a real feedback loop: it reads what document found and can send
#  market a follow-up seeded by that (e.g. which ETF to price), instead of
#  market guessing at the raw question before document has narrowed it down.
# ============================================================

from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from typing import Callable, TypedDict

from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langgraph.graph import StateGraph, END

from rag.tool_calling_agent import build_minimax_llm, ToolCallingAgent, PARTIAL_PREFIX
from rag.document_agent import DocumentAgent
from rag.market_agent import MarketAgent
from rag.agent_tools import init_tools
from rag.local_llm import OllamaJSONClient, DEFAULT_BASE_URL, enum_validator, parse_json_object
from rag.grounding import check_numeric_grounding, RETRY_SUFFIX_TEMPLATE
from rag.sources import merge_sources
from rag.history import UntrustedAnswers, is_error_answer, sanitize_history

Route = str  # "document" | "market" | "both" | "chitchat"
_VALID_ROUTES = {"document", "market", "both", "chitchat"}

# Only used when cfg["agent"]["router_mode"] == "rules" (the LLM router is the
# default). query.py already has a much more complete set of regexes for this
# (_is_market_intent_question, _has_explicit_quote_trigger, _STRICT_TICKER_PATTERNS,
# _RAG_CONTEXT_WORDS) — extract those into a shared rag/intent.py if the rules
# path needs to get more accurate; duplicating them here is only OK as a cheap
# opt-out fallback.
_MARKET_KEYWORDS = (
    "price", "quote", "stock", "etf", "isin", "market", "kurs", "preis",
    "news", "sentiment", "yesterday", "performance", "nachrichten", "stimmung",
    "股价", "价格", "行情", "报价", "股票", "新闻", "情绪", "昨天", "表现",
)
_DOCUMENT_KEYWORDS = (
    "document", "report", "email", "newsletter", "pdf", "文档", "报告", "邮件",
)
_ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
_MAX_IDENTIFIER_CHARS = 40


def _clean_identifier(reply: str | None) -> str | None:
    """The ISIN/WKN/ticker in the DocumentAgent's reply to the lookup directive, or None.
    The reply can also be NOT_FOUND, an "[Incomplete]" notice, an error message or a
    sentence; none of those may be passed on to the MarketAgent as if it were an identifier."""
    text = (reply or "").strip()
    if not text or "NOT_FOUND" in text.upper():
        return None
    if text.startswith(PARTIAL_PREFIX) or is_error_answer(text):
        return None
    isin = _ISIN.search(text)
    if isin:
        return isin.group()
    return text if len(text) <= _MAX_IDENTIFIER_CHARS and "\n" not in text else None


_ISIN_OR_TICKER = re.compile(r"\b[A-Za-z]{2}[A-Za-z0-9]{9}\d\b|\b[A-Z]{2,5}\b")

_ROUTER_CLASSIFY_PROMPT = """Classify the user's question into exactly one label.

Labels:
- document: about ingested PDFs/emails/reports, or the user's watchlist (stocks/ETFs/funds they track)
- market: needs live market data for a specific instrument: a current price/quote, recent daily price
  history or performance ("yesterday", "this week"), news or news sentiment about it, or what an
  instrument is / where it is listed (an ISIN or ticker lookup)
- both: needs a live price AND something from a document/report/watchlist to answer
- chitchat: greeting, thanks, or anything unrelated to documents or market data

A question about which instruments the user holds or tracks is "document", even when it names
stocks or ETFs. Choose "market" or "both" only if it asks for market data on a specific instrument
(price, price history, news, sentiment, or what/where it is listed).
A short follow-up ("i mean X", "and the second one?") continues the recent conversation shown
below: classify it by what the conversation is about, not as chitchat.

{history}Question: {question}"""

_GRADE_PROMPT = """You are grading whether retrieved passages are enough to answer a question, for a
financial document-search agent (PDFs, emails, watchlist).

## Question
{question}

## Retrieved passages (JSON from search_documents)
{passages}

Give your verdict as "sufficient" or "insufficient". For "insufficient", put what's missing or why
the passages don't answer the question in "reason" (max 20 words); otherwise leave "reason" empty.

Only say insufficient if the passages are clearly off-topic or missing something concrete the
question needs — not just because more detail would be nice."""

_CRITIC_PROMPT = """You are the internal quality-checker for a financial assistant team made of a
DocumentAgent (searches the user's PDFs/emails/watchlist) and a MarketAgent (fetches live prices, daily price history, news, sentiment and listing info).
Decide whether the findings so far already answer the original question.

## Original question
{question}

## Document research so far
{document_result}

## Market data so far
{market_result}

Give your verdict: "complete", "need_document" or "need_market". For need_document / need_market,
put a specific follow-up question for that agent in "follow_up" (max 30 words); otherwise leave
"follow_up" empty.

Only ask for a follow-up if something concrete and necessary is still missing — do not ask
just to double-check or add extra detail."""

_SYNTHESIZE_PROMPT = """Merge the following specialist findings into one clear, well-structured
answer to the user's original question. Use only the facts given below — do not invent numbers,
dates, or claims. If something is still missing, say so plainly.

## Original question
{question}

## Document research
{document_result}

## Market data
{market_result}

## Final answer
"""

# Output schemas for the local model (see rag/local_llm.py). Property order is
# the order the model fills them in, so the verdict always comes before the
# free-text part. Token caps only bound that free text; the enum part ends the
# object on its own.
_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": sorted(_VALID_ROUTES)}},
    "required": ["label"],
}
_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["sufficient", "insufficient"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
}
_CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["complete", "need_document", "need_market"]},
        "follow_up": {"type": "string"},
    },
    "required": ["verdict", "follow_up"],
}
_NUM_PREDICT_ROUTE = 24
_NUM_PREDICT_GRADE = 80
_NUM_PREDICT_CRITIC = 120

# Prompt-size caps: the local model runs on CPU, so every prompt token costs time.
_GRADER_PASSAGE_CHARS = 400
_CRITIC_RESULT_CHARS = 2500


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …[truncated]"


_ROUTER_HISTORY_MESSAGES = 4
_ROUTER_HISTORY_CHARS = 200


def _router_history(context_messages: list[BaseMessage] | None) -> str:
    """Last few turns, clipped, so a follow-up is routed by its topic. Kept short:
    the local model reads every token of this on CPU."""
    recent = (context_messages or [])[-_ROUTER_HISTORY_MESSAGES:]
    if not recent:
        return ""
    lines = []
    for m in recent:
        who = "User" if isinstance(m, HumanMessage) else "Assistant"
        text = re.sub(r"\s+", " ", str(m.content)).strip()
        lines.append(f"{who}: {_clip(text, _ROUTER_HISTORY_CHARS)}")
    return "Recent conversation:\n" + "\n".join(lines) + "\n\n"


# Phrases that mean a specialist did not actually answer (nothing found, error,
# asking the user for more). Deliberately broad: a false hit only costs one
# critic call, a miss would skip the check that was meant to catch it.
_FAILURE_SIGNAL_RE = re.compile(
    r"\b(?:no\s+(?:relevant|matching|results?|data|entries|documents?|quotes?|information|watchlist)"
    r"|not\s+found|could\s*n[o']t|can\s*n[o']t|cannot|unable|unavailable|don'?t\s+have|do\s+not\s+have"
    r"|no\s+access|error|failed|missing|not\s+available"
    r"|please\s+(?:provide|specify|clarify)|could\s+you|which\s+(?:symbol|ticker|isin|wkn)"
    r"|nicht\s+gefunden|keine?|bitte\s+angeben)\b"
    r"|未找到|没有找到|找不到|无法|请提供|请问",
    re.IGNORECASE,
)


def _compact_passages(search_result_json: str) -> str:
    """The grader only needs to judge relevance, not read 800-char passages
    wrapped in pretty-printed JSON -- keep source, date and the start of each."""
    raw = search_result_json
    try:
        payload = json.loads(raw)
    except ValueError:
        return _clip(raw, 2000)
    passages = payload.get("passages") if isinstance(payload, dict) else None
    if not passages:
        return _clip(raw, 2000)
    lines = [f"best_relevance_score: {payload.get('best_relevance_score')}"]
    for p in passages:
        text = re.sub(r"\s+", " ", str(p.get("text") or "")).strip()
        lines.append(f"{p.get('rank')}. {p.get('source')} ({p.get('date')}): {_clip(text, _GRADER_PASSAGE_CHARS)}")
    return "\n".join(lines)


# qwen3:8b (and other "thinking" Qwen3 models) routinely take 15-20s to answer
# even a one-word classification prompt, since it burns tokens on an internal
# <think> block before the final answer. 8s (fine as a rare keyword-miss
# fallback) is not enough now that the LLM router runs on every query by
# default -> give it real headroom.
_ROUTER_TIMEOUT_SECONDS = 30


class OrchestratorState(TypedDict, total=False):
    question: str
    context_messages: list[BaseMessage]
    route: Route
    resolved_identifier: str | None
    document_result: str | None
    market_result: str | None
    follow_up_question: str | None
    critic_verdict: str
    iteration: int
    final_answer: str


class Orchestrator:
    # Defaults for objects built without __init__ (tests); __init__ overrides from config.
    _critic_backend = "cloud"
    _critic_skip_single_route = True
    _critic_skipped = False

    def __init__(self, embedder, store, market_fetcher, cfg: dict):
        self.cfg = cfg
        self.history: list[BaseMessage] = []
        # Answers this process produced that failed or were flagged by the grounding
        # check; they are withheld from later turns' context (see rag/history.py).
        self._untrusted = UntrustedAnswers()

        top_k = cfg.get("rag", {}).get("top_k", 5)
        init_tools(store, market_fetcher, top_k)
        # First search would otherwise pay for the BM25 build and reranker load.
        try:
            store.warmup()
        except Exception as exc:
            print(f"[Orchestrator] retrieval warmup skipped: {exc}")

        llm = build_minimax_llm(cfg)
        self._cloud_llm = llm  # shared MiniMax client, reused for the router/critic cloud fallback below

        agent_cfg = cfg.get("agent", {})

        # Router/critic/grader: local Ollama model, deliberately separate from
        # cfg["llm"]["ollama_model"] (which rag/retriever.py's pipeline mode
        # uses for actual answer generation). Router/critic/grader only do
        # cheap classification/judgment ("document or market?", "is this
        # enough?", "are these passages relevant?"), so a smaller/faster
        # model here doesn't cost answer quality -> no need for the paid
        # MiniMax cloud model either, keeping those decisions off a
        # third-party API for the common case.
        # Decoding is constrained to a JSON schema (see rag/local_llm.py), so
        # the model can neither ramble nor skip the label. The old
        # router_num_predict setting is no longer used.
        self._local_llm = OllamaJSONClient(
            model=agent_cfg.get("router_model", cfg["llm"]["ollama_model"]),
            base_url=agent_cfg.get("router_base_url")
            or cfg["llm"].get("ollama_base_url")
            or DEFAULT_BASE_URL,
            keep_alive=agent_cfg.get("router_keep_alive"),
        )

        self._router_mode: str = agent_cfg.get("router_mode", "llm")
        self._max_feedback_iterations: int = int(agent_cfg.get("max_feedback_iterations", 2))
        # Same grounding heuristic and single-retry policy as pipeline mode
        # (see rag/grounding.py), applied to synthesize's merged answer
        # against what the two specialists actually reported -- the critic
        # loop above only checks completeness, never whether synthesize's
        # own wording invented a figure while merging.
        self._grounding_retry_enabled: bool = bool(agent_cfg.get("grounding_retry_enabled", True))
        self._grounding_max_retries: int = max(0, int(agent_cfg.get("grounding_max_retries", 1)))
        # The check itself (as opposed to the synthesize retry above) runs on
        # every answer, single-source or merged, against the raw tool output
        # the specialists were shown -- see _grounding_warnings.
        self._grounding_check_enabled: bool = bool(agent_cfg.get("grounding_check_enabled", True))
        self._router_timeout_seconds: int = int(
            agent_cfg.get("router_timeout_seconds", _ROUTER_TIMEOUT_SECONDS)
        )
        self._critic_timeout_seconds: int = int(
            agent_cfg.get("critic_timeout_seconds", _ROUTER_TIMEOUT_SECONDS)
        )
        # Critic: judging "is this complete, and what is missing?" is harder than the
        # router's one-label choice and a wrong "need_*" costs a whole extra specialist
        # run, so it defaults to MiniMax ("cloud"); "local" keeps it on Ollama.
        backend = str(agent_cfg.get("critic_backend", "cloud")).strip().lower()
        self._critic_backend: str = backend if backend in ("cloud", "local") else "cloud"
        self._critic_skip_single_route: bool = bool(agent_cfg.get("critic_skip_single_route", True))
        self._critic_skipped: bool = False
        self._local_timeout_fallback_to_cloud: bool = bool(
            agent_cfg.get("local_timeout_fallback_to_cloud", True)
        )

        # Agent-mode analog of rag/retriever.py's route_stats.jsonl (same
        # JSONL-append + periodic-summary pattern, disabled by default).
        # Pipeline mode logs local-vs-cloud per query; that distinction is
        # per-hop here (route/critic/grader can each fall back independently),
        # so this logs routing + reflection-loop telemetry instead, with a
        # per-query cloud-fallback count as the closest cost signal.
        self._enable_route_stats: bool = bool(agent_cfg.get("enable_route_stats", False))
        self._route_stats_log_path: str = agent_cfg.get(
            "route_stats_log_path", "data/agent_route_stats.jsonl"
        )
        self._route_stats_print_every: int = int(agent_cfg.get("route_stats_print_every", 0))
        self._query_count: int = 0
        self._cloud_fallback_count: int = 0
        self._grader_insufficient_count: int = 0

        # Sources cited by the most recent query() (read by the API after the
        # call returns), and the optional per-request progress callback.
        self.last_sources: list[dict] = []
        # Numbers/dates in the most recent answer that were not found in the raw
        # evidence (heuristic, see rag/grounding.py); read by the API like last_sources.
        self.last_grounding_warnings: list[str] = []
        # Raw tool output + context seen by the specialists during this query().
        self._evidence: list[str] = []
        self._on_event: Callable[[str], None] | None = None

        # grader is bound after the timeout/fallback config above so it can
        # reuse _call_json; DocumentAgent only calls it later
        # during query(), so construction order here is safe either way.
        self.document_agent = DocumentAgent(llm, grader=self._grade_passages)
        self.market_agent = MarketAgent(llm)

        self.graph = self._build_graph()

    def _status(self, text: str):
        if self._on_event:
            try:
                self._on_event(text)
            except Exception:
                pass  # progress reporting must never break an answer

    # ---- shared decision helper (used by the router, critic and grader) ----

    def _try_local_json(
        self, prompt: str, schema: dict, timeout_seconds: int, num_predict: int, is_valid
    ) -> dict | None:
        result = self._local_llm.generate(prompt, schema, num_predict, max(1, timeout_seconds))
        if result is not None and is_valid(result):
            return result
        if result is not None:
            print(f"[Orchestrator] local LLM returned an out-of-schema value: {result}")
        return None

    def _try_cloud_json(self, prompt: str, schema: dict, is_valid) -> dict | None:
        cloud_prompt = (
            f"{prompt}\n\nReply with only a JSON object matching this JSON schema, "
            f"no other text:\n{json.dumps(schema)}"
        )
        try:
            response = self._cloud_llm.invoke(cloud_prompt)
            text = response.content if hasattr(response, "content") else str(response)
            parsed = parse_json_object(ToolCallingAgent._strip_think(text))
        except Exception as exc:
            print(f"[Orchestrator] cloud LLM call failed: {exc}")
            return None
        return parsed if parsed is not None and is_valid(parsed) else None

    def _call_json(
        self, prompt: str, schema: dict, timeout_seconds: int, num_predict: int, prefer: str = "local"
    ) -> dict | None:
        """One schema-constrained decision, from the preferred backend first.

        prefer="local" (router, grader): the local Ollama model, escalating to
        the same MiniMax client the specialists use on timeout / error /
        invalid result (mirrors rag/retriever.py's local_timeout_fallback_to_cloud
        for pipeline mode; counted in _cloud_fallback_count).
        prefer="cloud" (critic): MiniMax first, the local model only if the
        cloud call fails.

        Returns None only if every allowed backend fails, so the caller's
        fail-open default (route='document', critic verdict='complete',
        grader 'sufficient') is the last resort, not the first one."""
        is_valid = enum_validator(schema)

        if prefer == "cloud":
            result = self._try_cloud_json(prompt, schema, is_valid)
            if result is not None:
                return result
            print("[Orchestrator] cloud LLM unavailable -> trying the local model")
            return self._try_local_json(prompt, schema, timeout_seconds, num_predict, is_valid)

        result = self._try_local_json(prompt, schema, timeout_seconds, num_predict, is_valid)
        if result is not None:
            return result
        if not self._local_timeout_fallback_to_cloud:
            return None
        print("[Orchestrator] local LLM unavailable -> falling back to cloud (MiniMax)")
        self._cloud_fallback_count += 1
        return self._try_cloud_json(prompt, schema, is_valid)

    # ---- retrieval grader (passed into DocumentAgent, called from its own hop loop) ----

    def _grade_passages(self, question: str, search_result_json: str) -> str:
        """Cheap local relevance check run inside DocumentAgent's own hop
        loop, right after each search_documents call and before it writes an
        answer -- the tool-calling model never notices a low
        best_relevance_score on its own. Returns 'sufficient' or
        'insufficient: <reason>' (the form ToolCallingAgent expects). Fails
        open to 'sufficient' so a stuck judge never blocks a good result."""
        prompt = _GRADE_PROMPT.format(
            question=question, passages=_compact_passages(search_result_json)
        )
        result = self._call_json(
            prompt, _GRADE_SCHEMA, self._router_timeout_seconds, _NUM_PREDICT_GRADE
        )
        if result and result["verdict"] == "insufficient":
            self._grader_insufficient_count += 1
            reason = (result.get("reason") or "").strip()
            return f"insufficient: {reason}" if reason else "insufficient"
        return "sufficient"

    # ---- grounding: compare the final answer against raw tool output ----

    def _collect_evidence(self, agent: ToolCallingAgent):
        self._evidence.extend(agent.evidence)

    def _grounding_warnings(self, answer: str) -> list[str]:
        """Numbers/dates in `answer` absent from everything the specialists were
        actually shown. Compared against raw tool results, not the specialists'
        written answers, so a figure one specialist invented can't vouch for
        itself downstream. Runs on every answer, including single-source ones
        that skip synthesize."""
        if not self._grounding_check_enabled or not answer:
            return []
        return check_numeric_grounding(answer, "\n".join(self._evidence))

    # ---- node: route ----

    def _node_route(self, state: OrchestratorState) -> dict:
        question = state["question"]
        self._status("Understanding your question…")

        if self._router_mode == "rules":
            q = question.lower()
            wants_market = any(k in q for k in _MARKET_KEYWORDS)
            wants_document = any(k in q for k in _DOCUMENT_KEYWORDS)
            if wants_market and wants_document:
                route = "both"
                print("[Orchestrator] rule match: market + document keywords -> both")
            elif wants_market:
                route = "market"
                print("[Orchestrator] rule match: market keyword -> market")
            elif wants_document:
                route = "document"
                print("[Orchestrator] rule match: document keyword -> document")
            else:
                print("[Orchestrator] no keyword match -> falling back to LLM router")
                route = self._llm_classify_route(question, state.get("context_messages"))
        else:
            route = self._llm_classify_route(question, state.get("context_messages"))

        print(f"[Orchestrator] -> route = {route}")
        return {"route": route}

    def _llm_classify_route(self, question: str, context_messages: list[BaseMessage] | None = None) -> Route:
        """Fails open to 'document' (the safer default — it can say 'I don't
        have that' instead of a market agent guessing a ticker) only if both
        the local model and the MiniMax cloud fallback are unavailable, or
        the reply can't be parsed."""
        prompt = _ROUTER_CLASSIFY_PROMPT.format(
            question=question, history=_router_history(context_messages)
        )
        result = self._call_json(
            prompt, _ROUTE_SCHEMA, self._router_timeout_seconds, _NUM_PREDICT_ROUTE
        )
        if result is None:
            print("[Orchestrator] router unavailable (local + cloud both failed) -> document")
            return "document"
        return result["label"]

    def _route_entry(self, state: OrchestratorState) -> str:
        # "market" starts with a price lookup; everything else ("document",
        # "both", "chitchat") starts with document research. For "both" this
        # is deliberate: document runs first, then the critic (which can see
        # the price-related part of the question is still unanswered) sends
        # a *targeted* follow-up into market — seeded by what document found
        # (e.g. which ETF) — rather than running market blind on the raw
        # question before document has had a chance to narrow it down.
        if state["route"] == "market":
            return "resolve_identifier"
        return "document"

    # ---- shared agent-invocation bodies (reused by multiple nodes) ----

    def _run_document_agent(self, state: OrchestratorState) -> dict:
        question = state.get("follow_up_question") or state["question"]
        self._status("Consulting document specialist…")
        result = self.document_agent.query(question, state["context_messages"])
        self.last_sources = merge_sources(self.last_sources, self.document_agent.sources)
        self._collect_evidence(self.document_agent)
        existing = state.get("document_result")
        merged = f"{existing}\n\n---\n\n{result}" if existing else result
        return {"document_result": merged, "follow_up_question": None}

    def _run_market_agent(self, state: OrchestratorState) -> dict:
        question = state.get("follow_up_question") or state["question"]
        resolved = state.get("resolved_identifier")
        if resolved:
            question = f"{question} (resolved identifier: {resolved})"
        self._status("Consulting market-data specialist…")
        result = self.market_agent.query(question, state["context_messages"])
        self.last_sources = merge_sources(self.last_sources, self.market_agent.sources)
        self._collect_evidence(self.market_agent)
        existing = state.get("market_result")
        merged = f"{existing}\n\n---\n\n{result}" if existing else result
        return {"market_result": merged, "follow_up_question": None}

    # ---- node: run one specialist agent ----

    def _node_document(self, state: OrchestratorState) -> dict:
        return self._run_document_agent(state)

    def _node_market(self, state: OrchestratorState) -> dict:
        return self._run_market_agent(state)

    # ---- node: resolve a bare instrument name before quoting it ----

    def _node_resolve_identifier(self, state: OrchestratorState) -> dict:
        question = state.get("follow_up_question") or state["question"]
        if _ISIN_OR_TICKER.search(question):
            return {}  # already looks like a ticker/ISIN, nothing to resolve

        resolved = _clean_identifier(self.document_agent.resolve_identifier(question))
        self._collect_evidence(self.document_agent)
        return {"resolved_identifier": resolved} if resolved else {}

    # ---- node: critic — the feedback-loop mechanism ----

    def _critic_skip_reason(self, state: OrchestratorState) -> str | None:
        """Why the critic can be skipped for this state, or None if it must run.

        The critic earns its call when the question needed both specialists, or
        when the one specialist that ran did not really answer. A single-route
        question whose specialist produced a normal answer gets nothing from
        it but latency (and a chance to request a needless follow-up run)."""
        # A specialist that said the lookup is incomplete would only repeat the same
        # lookup if asked again (and spend another full step budget doing it).
        # Not for route "both" before the second specialist has run: the critic is
        # what sends the question on to it.
        if any((state.get(k) or "").lstrip().startswith(PARTIAL_PREFIX)
               for k in ("document_result", "market_result")):
            if state.get("route") != "both" or (state.get("document_result") and state.get("market_result")):
                return "a specialist reported an incomplete lookup"
        if not self._critic_skip_single_route or state.get("iteration", 0) > 0:
            return None
        route = state.get("route")
        if route not in ("document", "market", "chitchat"):
            return None  # "both" is the case the critic exists for

        own, other = ("market_result", "document_result") if route == "market" else ("document_result", "market_result")
        answer = (state.get(own) or "").strip()
        if not answer or state.get(other):
            return None
        if answer.startswith(("[DocumentAgent error]", "[MarketAgent error]")):
            return None
        if _FAILURE_SIGNAL_RE.search(answer):
            return None
        return f"single route {route!r}, specialist answered with no failure signal"

    def _node_critic(self, state: OrchestratorState) -> dict:
        iteration = state.get("iteration", 0)
        if iteration >= self._max_feedback_iterations:
            print(f"[Orchestrator][Critic] hit max_feedback_iterations={self._max_feedback_iterations} -> complete")
            return {"critic_verdict": "complete"}

        skip_reason = self._critic_skip_reason(state)
        if skip_reason:
            print(f"[Orchestrator][Critic] skipped: {skip_reason}")
            self._critic_skipped = True
            return {"critic_verdict": "complete", "follow_up_question": None}

        self._status("Checking the answer is complete…")
        prompt = _CRITIC_PROMPT.format(
            question=state["question"],
            document_result=_clip(state.get("document_result") or "(none yet)", _CRITIC_RESULT_CHARS),
            market_result=_clip(state.get("market_result") or "(none yet)", _CRITIC_RESULT_CHARS),
        )
        result = self._call_json(
            prompt, _CRITIC_SCHEMA, self._critic_timeout_seconds, _NUM_PREDICT_CRITIC,
            prefer=self._critic_backend,
        )
        if result is None:
            print("[Orchestrator][Critic] unavailable (local + cloud both failed) -> complete")
            return {"critic_verdict": "complete", "iteration": iteration + 1}

        verdict = result["verdict"]
        follow_up = (result.get("follow_up") or "").strip() or None
        if verdict == "complete":
            follow_up = None
        print(f"[Orchestrator][Critic] verdict={verdict!r} follow_up={follow_up!r}")
        return {"critic_verdict": verdict, "follow_up_question": follow_up, "iteration": iteration + 1}

    def _critic_route(self, state: OrchestratorState) -> str:
        verdict = state.get("critic_verdict", "complete")
        if verdict == "need_document":
            return "document"
        if verdict == "need_market":
            return "resolve_identifier"
        return "synthesize"

    # ---- node: merge specialists' findings into one reply ----

    def _node_synthesize(self, state: OrchestratorState) -> dict:
        document_result = state.get("document_result")
        market_result = state.get("market_result")

        if document_result and market_result:
            self._status("Combining findings into one answer…")
            final_answer = self._synthesize_with_llm(state["question"], document_result, market_result)
        else:
            final_answer = document_result or market_result or ""
            self.last_grounding_warnings = self._grounding_warnings(final_answer)

        return {"final_answer": final_answer}

    def _synthesize_with_llm(self, question: str, document_result: str, market_result: str) -> str:
        """Only called when both agents actually contributed (real multi-hop
        case) — cheaper single-source answers skip this and pass through
        directly, matching the codebase's existing rule of not paying for
        cloud calls that aren't needed (see force_local_for_market in
        rag/retriever.py)."""
        prompt = _SYNTHESIZE_PROMPT.format(
            question=question, document_result=document_result, market_result=market_result
        )
        fallback = f"{market_result}\n\n{document_result}"
        text = self._call_synthesize_llm(prompt)
        if not text:
            self.last_grounding_warnings = self._grounding_warnings(fallback)
            return fallback

        # Grounding check: synthesize is the one step that puts new wording
        # around the specialists' findings, so it's the one place a merged
        # figure can drift from what either specialist actually reported.
        # Same heuristic + single-retry policy as pipeline mode.
        # Raw tool output, not the specialists' paraphrases (see _grounding_warnings).
        evidence = "\n".join(self._evidence)
        unverified = check_numeric_grounding(text, evidence) if self._grounding_check_enabled else []
        retries_left = self._grounding_max_retries if self._grounding_retry_enabled else 0
        while unverified and retries_left > 0:
            retries_left -= 1
            print(f"[Orchestrator] synthesize unverified figures: {unverified} -> regenerating once")
            retry_prompt = prompt + RETRY_SUFFIX_TEMPLATE.format(unverified=", ".join(unverified))
            retried = self._call_synthesize_llm(retry_prompt)
            if not retried:
                break  # regeneration itself failed -- keep the current draft
            text = retried
            unverified = check_numeric_grounding(text, evidence)

        if unverified:
            print(f"[Orchestrator] synthesize still has unverified figures after retry: {unverified}")

        self.last_grounding_warnings = unverified
        return text or fallback

    def _call_synthesize_llm(self, prompt: str) -> str | None:
        try:
            response = self._cloud_llm.invoke(prompt)
            text = response.content if hasattr(response, "content") else str(response)
            return ToolCallingAgent._strip_think(text).strip() or None
        except Exception as exc:
            print(f"[Orchestrator] synthesis call failed: {exc}")
            return None

    # ---- graph assembly ----

    def _build_graph(self):
        builder = StateGraph(OrchestratorState)

        builder.add_node("document", self._node_document)
        builder.add_node("resolve_identifier", self._node_resolve_identifier)
        builder.add_node("market", self._node_market)
        builder.add_node("critic", self._node_critic)
        builder.add_node("synthesize", self._node_synthesize)
        builder.add_node("route", self._node_route)

        builder.set_entry_point("route")
        builder.add_conditional_edges(
            "route",
            self._route_entry,
            {"document": "document", "resolve_identifier": "resolve_identifier"},
        )
        builder.add_edge("document", "critic")
        builder.add_edge("resolve_identifier", "market")
        builder.add_edge("market", "critic")
        builder.add_conditional_edges(
            "critic",
            self._critic_route,
            {"document": "document", "resolve_identifier": "resolve_identifier", "synthesize": "synthesize"},
        )
        builder.add_edge("synthesize", END)

        return builder.compile()

    # ---- public entry point ----

    def query(
        self,
        question: str,
        stream: bool = False,
        on_event: Callable[[str], None] | None = None,
    ) -> str:
        """Answer one question. `on_event(text)`, if given, receives short
        progress lines while the graph runs (used by the streaming API);
        sources for the answer are left in `self.last_sources`."""
        self._cloud_fallback_count = 0
        self._grader_insufficient_count = 0
        self._critic_skipped = False
        self.last_sources = []
        self.last_grounding_warnings = []
        # Earlier turns count as evidence too: a follow-up may legitimately
        # restate a figure from a previous answer. Only answers that held up are
        # kept as-is; failed or flagged ones become a placeholder first, so they
        # can neither be repeated as fact nor vouch for their own figures.
        context_messages = sanitize_history(self.history[-6:], self._untrusted)
        self._evidence = [str(m.content) for m in context_messages]
        self._on_event = on_event
        self.document_agent.on_event = on_event
        self.market_agent.on_event = on_event

        initial_state: OrchestratorState = {
            "question": question,
            "context_messages": context_messages,
            "route": "",
            "resolved_identifier": None,
            "document_result": None,
            "market_result": None,
            "follow_up_question": None,
            "critic_verdict": "",
            "iteration": 0,
            "final_answer": "",
        }

        print(f"\n[Orchestrator] Routing: {question}\n")
        try:
            final_state = self.graph.invoke(initial_state, config={"recursion_limit": 50})
        finally:
            self._on_event = None
            self.document_agent.on_event = None
            self.market_agent.on_event = None
        answer = final_state.get("final_answer", "")
        if is_error_answer(answer) or self.last_grounding_warnings:
            self._untrusted.remember(answer)

        self.history.append(HumanMessage(content=question))
        self.history.append(AIMessage(content=answer))
        print(answer)
        if self.last_grounding_warnings:
            print(
                "[Grounding Check] ⚠️  Not found in retrieved sources: "
                + ", ".join(self.last_grounding_warnings)
            )

        self._query_count += 1
        self._log_route_stats(question, final_state)

        return answer

    # ---- route/reflection telemetry (agent-mode analog of route_stats.jsonl) ----

    def _log_route_stats(self, question: str, final_state: "OrchestratorState"):
        if not self._enable_route_stats:
            return

        iteration = final_state.get("iteration", 0)
        event = {
            "timestamp": datetime.datetime.now().isoformat(),
            "question": question[:120],
            "route": final_state.get("route"),
            "resolved_identifier": final_state.get("resolved_identifier"),
            "iterations": iteration,
            "critic_verdict": final_state.get("critic_verdict"),
            "hit_max_feedback_iterations": iteration >= self._max_feedback_iterations,
            "cloud_fallbacks": self._cloud_fallback_count,
            "grader_insufficient_count": self._grader_insufficient_count,
            "critic_skipped": self._critic_skipped,
            "grounding_warnings": len(self.last_grounding_warnings),
        }
        try:
            with open(self._route_stats_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass

        if self._route_stats_print_every > 0 and self._query_count % self._route_stats_print_every == 0:
            self._print_route_summary()

    def _print_route_summary(self):
        try:
            with open(self._route_stats_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
        except (OSError, json.JSONDecodeError):
            return
        if not events:
            return

        total = len(events)
        route_counts = Counter(e.get("route") for e in events)
        cloud_fallback_queries = sum(1 for e in events if e.get("cloud_fallbacks", 0) > 0)
        grader_flagged_queries = sum(1 for e in events if e.get("grader_insufficient_count", 0) > 0)
        maxed_out = sum(1 for e in events if e.get("hit_max_feedback_iterations"))

        print(f"\n── Agent Route Stats (last {total} queries) ─────────────")
        print("  Route:                " + "  ".join(f"{k}={v}" for k, v in route_counts.most_common()))
        print(f"  Cloud fallback used : {cloud_fallback_queries} ({100*cloud_fallback_queries//total}%)")
        print(f"  Grader flagged      : {grader_flagged_queries} ({100*grader_flagged_queries//total}%)")
        print(f"  Hit max_feedback_it : {maxed_out} ({100*maxed_out//total}%)")
        print("──────────────────────────────────────────────────\n")

    # ---- API parity with the old AgentRetriever ----

    def set_market_context(self, text: str):
        """Bridge live quotes into DocumentAgent's next prompt (same feature
        query.py's _set_last_quote_docs already relies on)."""
        self.market_agent.set_extra_context(text)
        self.document_agent.set_extra_context(text)

    def clear_history(self):
        self.history = []
        print("[Orchestrator] Conversation history cleared.")

    def set_history(self, messages: list[dict]):
        self.history = [
            AIMessage(content=m["content"]) if m.get("role") == "assistant"
            else HumanMessage(content=m["content"])
            for m in messages
        ]
