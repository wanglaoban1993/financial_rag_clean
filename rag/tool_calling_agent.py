# ============================================================
#  rag/tool_calling_agent.py — Reusable single-agent tool-calling loop
#
#  Extracted from the old monolithic AgentRetriever so DocumentAgent
#  and MarketAgent can each be a thin subclass: same hop loop / same
#  MiniMax tool-call parsing, different system prompt + tool subset.
#
#  LangGraph migration note: ToolCallingAgent.query() is written as a
#  pure function of (question, context_messages) -> answer. If this
#  project moves to LangGraph later, this loop becomes the body of a
#  single ReAct-style node (or can be swapped for langgraph.prebuilt
#  create_react_agent) with no change to DocumentAgent/MarketAgent.
# ============================================================

from __future__ import annotations

import json
import re
from typing import Any, Callable

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage

from rag.date_filter import parse_document_date_range
from rag.sources import extract_sources, merge_sources

# User-facing progress line shown while each tool runs (streamed to the dashboard).
_TOOL_STATUS = {
    "search_documents": "Searching indexed documents…",
    "search_watchlist": "Searching your watchlist…",
    "list_watchlists": "Listing your watchlists…",
    "get_watchlist": "Reading a watchlist…",
    "get_market_quote": "Fetching live market prices…",
    "get_instrument_profile": "Looking up the instrument's listing…",
    "get_price_history": "Fetching daily price history…",
    "get_company_news": "Fetching recent news…",
    "get_news_sentiment": "Fetching news sentiment…",
    "read_file": "Reading file…",
    "read_email": "Reading email…",
    "list_indexed_sources": "Checking indexed sources…",
    "list_recent_documents": "Listing your most recent documents…",
}


def build_minimax_llm(cfg: dict, temperature: float = 0.2) -> ChatOpenAI:
    """Construct the shared MiniMax client. Orchestrator builds one instance
    and hands it to both sub-agents instead of each opening its own connection."""
    llm_cfg = cfg["llm"]
    api_key = llm_cfg.get("minimax_api_key", "").strip()
    base_url = llm_cfg.get("minimax_base_url", "https://api.minimaxi.com/v1").strip()
    model = llm_cfg.get("minimax_model", "MiniMax-M2.7-highspeed")

    if not api_key:
        raise ValueError(
            "minimax_api_key not set in config.yaml [llm] section. "
            "ToolCallingAgent requires a cloud LLM."
        )

    # This client is also the last-resort fallback when the local Ollama
    # router/critic times out (see Orchestrator._call_local_then_cloud) —
    # without a timeout here, a stalled MiniMax response has no backstop at
    # all and blocks the whole CLI indefinitely (observed: process alive 1h+
    # with an established-but-idle socket to the MiniMax API).
    timeout = llm_cfg.get("minimax_timeout_seconds", 30)
    max_retries = llm_cfg.get("minimax_max_retries", 1)

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        extra_body={"thinking": {"type": "disabled"}},
        timeout=timeout,
        max_retries=max_retries,
    )


# Appended to every specialist's own system prompt (see ToolCallingAgent.__init__),
# so these two behaviours are defined once instead of copied into each agent.
COMMON_RULES = """

WORKING RULES (apply on top of everything above):
1. Be honest about your functions. Only describe or promise what the functions listed above can
   actually do. If the user asks for something none of them can do, say so plainly in one
   sentence and offer the closest thing you can really do. Never offer a capability right after
   admitting you lack it, and never say "I'll list/read/fetch X" unless one of your functions
   does exactly that.
2. Search before you ask. If a question is vague but has a reasonable reading, make one attempt
   with the most plausible keywords first, answer from what comes back, and state the
   interpretation you used. Ask a clarifying question only if that attempt returned nothing
   useful, and then ask ONE specific question, not a menu of options. (Exception: never guess
   ticker symbols or prices — follow the identifier rules above.)
3. Function results beat your background knowledge. If a function returned data for an
   instrument or document, report that data; never contradict it or dismiss it because of what
   you believe about the company (for example, do not say an instrument "is not listed" when a
   function just returned its price).
4. Report only what the functions returned. Every figure, name, date and claim about a document
   must come from the text you were actually shown. If the text does not contain something the
   user asked about, say "the document does not contain this" instead of filling the gap from
   memory or plausible-sounding detail; never invent index levels, economic data, events or
   technical indicators. When you read a document, state how much you read using the
   chars_returned / total_chars fields. If a result says PARTIAL, either fetch the rest with
   the offset it gives (when the question needs it) or tell the user plainly that only that part
   was read. Never guess where or why a text was cut off."""


_REPAIR_MESSAGE = (
    "Your last message looked like a function call, but it could not be read. Send the call again "
    'as a single JSON object, for example {"tool": "<function name>", "<argument>": "<value>"}, with '
    "nothing else in the message. If you do not need a function, answer the question directly."
)
_HISTORY_NOTE = (
    "The earlier messages in this conversation are context only, not sources. Statements the "
    "assistant made earlier (for example that a file cannot be read, or what a document says) may "
    "be wrong: base every fact on the function results of this turn, and call the functions again "
    "instead of relying on an earlier answer."
)
# Marks an answer the agent could not finish. The Orchestrator keys on it (a partial
# answer is not improved by asking the same specialist again).
PARTIAL_PREFIX = "[Incomplete]"
_PARTIAL_MESSAGE = (
    "The lookup stopped before it was finished. Do not call any function. Using only the function "
    "results above, report what they actually contain that is relevant to the question. Do not "
    "add anything they do not contain, do not guess what any unread part says, and do not claim "
    "the question has been fully answered. If a result is marked PARTIAL, say which part was not read."
)
_STEP_ARG_CHARS = 60
_FALLBACK_EXCERPT_CHARS = 1500


class ToolCallingAgent:
    """One specialist agent: a system prompt, a fixed tool subset, and the
    manual hop loop (kept manual because MiniMax's function-calling via
    bind_tools is unreliable — see the original agent_retriever.py notes)."""

    # Tool-call rounds allowed per question. Reading a long document takes several
    # (list -> read page 1 -> page 2 -> ...), so once a read tool has been used the
    # budget grows to MAX_READ_HOPS. Running out never forces an answer: the user is
    # told it is incomplete and what was read (see _incomplete_answer).
    MAX_TOOL_HOPS = 4
    # How many times the model is asked to resend a function call we could not read.
    MAX_REPAIRS = 2
    MAX_READ_HOPS = 8
    READ_TOOLS = frozenset({"read_file", "read_email"})

    def __init__(
        self,
        name: str,
        system_prompt: str,
        tools: list[Callable],
        llm: ChatOpenAI,
        grader: Callable[[str, str], str] | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt + COMMON_RULES
        self.tool_map = {t.name: t for t in tools}
        self.llm = llm
        # Optional extra context injected as a system message before each
        # query (e.g. "recent market data fetched this session"). Mirrors
        # the old AgentRetriever._last_market_context bridge.
        self._extra_context: str = ""
        # Optional cheap relevance judge, run on search_documents results
        # only (see _execute_tool below). None for agents with no semantic
        # search tool (MarketAgent never passes one).
        self.grader = grader
        # Sources cited by the tool calls of the most recent query() (reset at
        # the start of each query), and an optional progress callback the
        # Orchestrator sets for the duration of one streamed request.
        self.sources: list[dict] = []
        # Raw text the model was shown during the most recent query() -- tool
        # results verbatim (before the grader's note is appended), plus the
        # injected context. This is the evidence the grounding check should
        # compare answers against, rather than another LLM's paraphrase.
        self.evidence: list[str] = []
        self.on_event: Callable[[str], None] | None = None

    def _emit(self, text: str):
        if self.on_event:
            try:
                self.on_event(text)
            except Exception:
                pass  # progress reporting must never break an answer

    def set_extra_context(self, text: str):
        self._extra_context = (text or "").strip()

    # ---- tool-call parsing (unchanged from the original AgentRetriever) ----

    @staticmethod
    def _strip_think(text: str) -> str:
        """Drop any <think>...</think> reasoning MiniMax leaks into content despite
        thinking being disabled, plus an unclosed trailing <think> block."""
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        text = re.sub(r'<think>.*$', '', text, flags=re.DOTALL)
        return text.strip()

    @staticmethod
    def _find_tool_json(text: str) -> dict | None:
        """First JSON object in `text` that has a string "tool" key.

        Scans object by object instead of slicing from the first "{" to the last
        "}", so prose around the call (which may itself contain braces), a code
        fence, or a second call after the first no longer makes the whole reply
        unreadable."""
        decoder = json.JSONDecoder()
        pos = text.find("{")
        while pos != -1:
            try:
                obj, _ = decoder.raw_decode(text, pos)
            except ValueError:
                obj = None
            if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
                return obj
            pos = text.find("{", pos + 1)
        return None

    @staticmethod
    def _parse_tool_call_response(response_text: str) -> tuple[str | None, dict]:
        """Extract a tool call from several MiniMax output formats."""
        tool_name = None
        tool_args: dict = {}

        tag_match = re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', response_text, re.DOTALL)
        if tag_match:
            try:
                tool_call_json = json.loads(tag_match.group(1))
                tool_name = tool_call_json.get("tool")
                tool_args = tool_call_json.get("parameters") or {
                    k: v for k, v in tool_call_json.items() if k != "tool"
                }
                return tool_name, tool_args
            except Exception:
                pass

        tool_call_json = ToolCallingAgent._find_tool_json(response_text)
        if tool_call_json:
            tool_name = tool_call_json["tool"]
            nested = next(
                (tool_call_json[k] for k in ("parameters", "arguments", "args")
                 if isinstance(tool_call_json.get(k), dict) and tool_call_json[k]),
                None,
            )
            tool_args = nested or {
                k: v for k, v in tool_call_json.items() if k not in ("tool", "parameters", "arguments", "args")
            }
            return tool_name, tool_args

        xml_match = re.search(
            r'<minimax:tool_call>\s*<invoke name="([^"]+)"[^>]*>(.*?)</invoke>\s*</minimax:tool_call>',
            response_text, re.DOTALL,
        )
        if not xml_match:
            # Some MiniMax responses use the legacy `[/INVOKE]` terminator
            # and may emit the outer minimax tag without angle brackets.
            xml_match = re.search(
                r'(?:<|`)?minimax:tool_call(?:>|`)?\s*'
                r'<invoke name="([^"]+)"[^>]*>(.*?)</invoke>\s*'
                r'(?:</minimax:tool_call>|\[/INVOKE\])',
                response_text,
                re.DOTALL | re.IGNORECASE,
            )
        if xml_match:
            tool_name = xml_match.group(1)
            invoke_body = xml_match.group(2)
            tool_args = {
                key: value.strip()
                for key, value in re.findall(
                    r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', invoke_body, re.DOTALL
                )
            }
            return tool_name, tool_args

        # `<tool name="search_watchlist" keyword="x"/>` -- MiniMax sometimes
        # emits the arguments as XML attributes instead of <parameter> children.
        attr_match = re.search(r'<tool\s+name\s*=\s*"([^"]+)"([^>]*?)/?>', response_text, re.DOTALL | re.IGNORECASE)
        if attr_match:
            tool_name = attr_match.group(1)
            tool_args = {
                key: value.strip()
                for key, value in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"', attr_match.group(2))
            }
            return tool_name, tool_args

        for pseudo_match in re.finditer(r'\[TOOL_CALL\](.*?)\[/TOOL_CALL\]', response_text, re.DOTALL | re.IGNORECASE):
            block = pseudo_match.group(1)
            name_match = re.search(r'tool\s*=>\s*["\']([^"\']+)["\']', block, re.IGNORECASE)
            if not name_match:
                name_match = re.search(r'tool\s*[:=]\s*["\']?([A-Za-z_][A-Za-z0-9_]*)["\']?', block, re.IGNORECASE)
            if name_match:
                tool_name = name_match.group(1).strip().strip('"\'')

            cli_args = re.findall(r'--([A-Za-z_][A-Za-z0-9_]*)\s+["\']([^"\']+)["\']', block)
            if cli_args and tool_name:
                tool_args = {key: value.strip() for key, value in cli_args}
                return tool_name, tool_args

            kv_args = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*(?:=>|:|=)\s*["\']([^"\']+)["\']', block)
            if kv_args and tool_name:
                tool_args = {key: value.strip() for key, value in kv_args if key.lower() != "tool"}
                if tool_args:
                    return tool_name, tool_args

            quoted = [q.strip() for q in re.findall(r'["\']([^"\']+)["\']', block)]
            if tool_name and len(quoted) == 1:
                tool_args = {"value": quoted[0]}
                return tool_name, tool_args

        return tool_name, tool_args

    _TOOL_MARKUP_RE = re.compile(
        r'minimax:tool_call|<tool_call>|<invoke\b|<tool\s+name|\[TOOL_CALL\]|\{\s*"tool"\s*:',
        re.IGNORECASE,
    )

    @classmethod
    def _looks_like_tool_call(cls, text: str) -> bool:
        """Tool-call markup in a reply that _extract_tool_calls could not parse.
        Returning that to the user as an answer is never right."""
        return bool(cls._TOOL_MARKUP_RE.search(text or ""))

    @classmethod
    def _extract_tool_calls(cls, response_text: str) -> list[tuple[str, dict]]:
        tool_calls: list[tuple[str, dict]] = []
        pseudo_blocks = re.findall(r'\[TOOL_CALL\](.*?)\[/TOOL_CALL\]', response_text, re.DOTALL | re.IGNORECASE)
        if pseudo_blocks:
            for block in pseudo_blocks:
                t_name, t_args = cls._parse_tool_call_response(f"[TOOL_CALL]{block}[/TOOL_CALL]")
                if t_name:
                    tool_calls.append((t_name, t_args))
        else:
            t_name, t_args = cls._parse_tool_call_response(response_text)
            if t_name:
                tool_calls.append((t_name, t_args))
        return tool_calls

    def _execute_tool(self, tool_name: str, tool_args: dict, question: str) -> str:
        tool_fn = self.tool_map.get(tool_name)
        if not tool_fn:
            return f"Unknown tool: {tool_name}"
        if tool_name == "search_documents" and not str(tool_args.get("query") or "").strip():
            # Seen in a real run: a call with only dates and no query fails validation, and the
            # model resent it unchanged until the step budget ran out. The user's own words are
            # a sound query.
            tool_args = {**tool_args, "query": question}
        if tool_name == "search_documents" and not (tool_args.get("date_from") or tool_args.get("date_to")):
            # The model rewrites the user's words into search keywords and usually drops
            # "today"; the date range is read from the original question instead.
            wanted = parse_document_date_range(question)
            if wanted:
                tool_args = {**tool_args, "date_from": wanted.start.isoformat(), "date_to": wanted.end.isoformat()}
        try:
            print(f"\n[{self.name}][Tool] {tool_name}({tool_args})")
            self._emit(_TOOL_STATUS.get(tool_name, f"Running {tool_name}…"))
            result = tool_fn.invoke(tool_args)
            print(f"[{self.name}][Result] {result[:300]}...")
        except Exception as e:
            return f"Tool error: {e}"

        self.evidence.append(result)
        self.sources = merge_sources(self.sources, extract_sources(tool_name, result))

        # Relevance check, search_documents only: the tool-calling model never
        # reliably self-polices on the best_relevance_score already present
        # in its JSON output (the system prompt doesn't tell it to), so a
        # separate cheap judge flags a bad retrieval explicitly instead of
        # hoping the model notices. Fails open to "sufficient" so a stuck/slow
        # judge can never block an otherwise-good tool result.
        # "No document is dated X" is a correct, final answer, not a bad retrieval: grading it
        # would only send the model off to search again without the date.
        if tool_name == "search_documents" and self.grader and "no_documents_in_range" not in result:
            try:
                verdict = self.grader(question, result)
            except Exception as exc:
                print(f"[{self.name}][Grader] error: {exc}")
                verdict = "sufficient"
            if verdict.strip().lower().startswith("insufficient"):
                print(f"[{self.name}][Grader] {verdict.strip()}")
                result += (
                    f"\n\n[Retrieval grader] {verdict.strip()} — try search_documents again "
                    "with different keywords before answering."
                )

        return result

    @staticmethod
    def _describe_step(tool_name: str, tool_args: dict, result: str) -> str:
        """One line for the user: which call was made and, for reads, how much was read."""
        first = next(iter((tool_args or {}).values()), "")
        text = re.sub(r"\s+", " ", str(first)).strip()
        if len(text) > _STEP_ARG_CHARS:
            text = text[:_STEP_ARG_CHARS].rstrip() + "…"
        line = f'{tool_name}("{text}")' if text else f"{tool_name}()"
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return line
        if isinstance(payload, dict) and payload.get("chars_returned"):
            line += f": read characters {payload['chars_returned']}"
            if payload.get("next_offset") is not None:
                line += " (the rest was NOT read)"
        return line

    def _incomplete_answer(self, messages: list[BaseMessage], steps: list[str],
                           retrieved: list[str], stop_reason: str) -> str:
        """Honest wrap-up when the lookup could not finish: a fixed header that says so and
        lists the steps, then a summary limited to what was read. If the model cannot
        produce that summary (timeout, unreadable output), excerpts of the raw results
        take its place, so the user still gets what was retrieved instead of an error."""
        if not steps:
            return (
                f"{PARTIAL_PREFIX} I could not find anything before the lookup stopped "
                f"({stop_reason}). Please try rephrasing the question or asking for something more specific."
            )

        header = (
            f"{PARTIAL_PREFIX} I only found part of this: {stop_reason}, so the lookup is not finished.\n"
            "Steps taken:\n" + "\n".join(f"- {step}" for step in steps)
        )
        summary = ""
        try:
            response = self.llm.invoke(messages + [HumanMessage(content=_PARTIAL_MESSAGE)])
            text = response.content if hasattr(response, "content") else str(response)
            text = self._strip_think(text)
            if text and not self._looks_like_tool_call(text):
                summary = text
        except Exception as exc:
            print(f"[{self.name}] incomplete-answer summary failed: {exc}")

        if not summary:
            excerpts = [r[:_FALLBACK_EXCERPT_CHARS] for r in retrieved[-2:]]
            summary = "Raw excerpts of what was retrieved:\n" + "\n---\n".join(excerpts)
        return f"{header}\n\nWhat I found so far (only from what was actually read):\n{summary}"

    # ---- main entry point ----

    def query(self, question: str, context_messages: list[BaseMessage] | None = None) -> str:
        """Run the hop loop for one question.

        context_messages: prior conversation turns the caller (Orchestrator)
        wants this agent to see. The agent itself is otherwise stateless —
        history lives in the Orchestrator, not per sub-agent, so a later
        document question and an earlier market question stay coherent.
        """
        from datetime import date

        self.sources = []
        today = date.today().isoformat()
        self.evidence = [today]
        messages: list[BaseMessage] = [SystemMessage(content=self.system_prompt)]
        messages.append(SystemMessage(content=f"Today's date is {today}."))
        if self._extra_context:
            messages.append(SystemMessage(content=self._extra_context))
            self.evidence.append(self._extra_context)
        if context_messages:
            messages.append(SystemMessage(content=_HISTORY_NOTE))
        messages.extend(context_messages or [])
        messages.append(HumanMessage(content=question))

        answer = ""
        repairs_left = self.MAX_REPAIRS
        hop_limit = self.MAX_TOOL_HOPS
        steps: list[str] = []      # human-readable log of the calls made, for an incomplete answer
        retrieved: list[str] = []  # raw tool results, for the no-model fallback
        stop_reason = f"it did not finish within {hop_limit} steps"
        try:
            hops_used = 0
            while hops_used < hop_limit:
                hops_used += 1
                response = self.llm.invoke(messages)
                response_text = response.content if hasattr(response, "content") else str(response)
                # Reasoning the model leaks inside <think> must not be mistaken for (or
                # corrupt) a function call; an empty remainder keeps the raw text.
                response_text = self._strip_think(response_text) or response_text

                tool_calls = self._extract_tool_calls(response_text)
                if not tool_calls:
                    if self._looks_like_tool_call(response_text):
                        # The model tried to call a function in a format we can't
                        # read. Ask once for the supported format; if it still
                        # can't, fall through to the forced final answer rather
                        # than showing raw markup to the user as the answer.
                        if repairs_left > 0:
                            repairs_left -= 1
                            print(f"[{self.name}] unparseable tool call, asking the model to resend: {response_text[:200]!r}")
                            messages.append(AIMessage(content=response_text))
                            messages.append(HumanMessage(content=_REPAIR_MESSAGE))
                            continue
                        stop_reason = "the model kept sending function calls that could not be read"
                        break
                    answer = response_text
                    break

                tool_results = []
                for tool_name, tool_args in tool_calls:
                    result = self._execute_tool(tool_name, tool_args, question)
                    tool_results.append(f"Tool '{tool_name}' result:\n{result}")
                    steps.append(self._describe_step(tool_name, tool_args, result))
                    retrieved.append(result)
                    if tool_name in self.READ_TOOLS:
                        hop_limit = max(hop_limit, self.MAX_READ_HOPS)
                        stop_reason = f"it did not finish within {hop_limit} steps"

                messages.append(AIMessage(content=response_text))
                messages.append(HumanMessage(
                    content="\n\n".join(tool_results)
                    + "\n\nIf this already answers the question, respond now with your final "
                    "answer — do not call another function just to double-check or add extra "
                    "detail. Only call another function if the result above failed or did not contain "
                    "what the question needs."
                ))

            if not answer:
                # Out of steps while the model was still asking for tools, or it kept
                # sending unreadable calls. Do not push it into a made-up answer: say
                # the lookup is incomplete and report what was actually read.
                print(f"[{self.name}] No final answer ({stop_reason}); returning an incomplete answer.")
                answer = self._incomplete_answer(messages, steps, retrieved, stop_reason)
        except Exception as exc:
            import traceback
            # Full traceback goes to the server log; the dashboard user only
            # sees a one-line reason (the "[<name> error]" prefix is what the
            # Orchestrator's critic-skip check keys on).
            print(f"[{self.name}] query failed:\n{traceback.format_exc()}")
            reason = "the language model timed out" if "timed out" in str(exc).lower() else str(exc)
            answer = f"[{self.name} error] I could not finish this request: {reason}. Please try again."

        return self._strip_think(answer)
