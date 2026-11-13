# ============================================================
#  rag/agent_retriever.py — Agentic RAG using function calling
#
#  Architecture:
#    - LLM: MiniMax via OpenAI-compatible API (langchain-openai ChatOpenAI)
#    - Tools: search_documents, get_market_quote, list_indexed_sources
#    - Memory: manual message history (last N turns)
#    - Approach: Manual function calling loop (more reliable than bind_tools)
# ============================================================

from __future__ import annotations

import json
import re
from typing import Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from rag.agent_tools import (
    init_tools,
    search_documents,
    get_market_quote,
    read_file,
    search_watchlist,
    list_indexed_sources,
)


_SYSTEM_PROMPT = """You are a helpful financial analysis assistant.

You have access to the following functions:
1. search_documents(query: str) - Searches the local document archive (PDFs/emails)
2. get_market_quote(symbols: str) - Fetches real-time market prices
3. read_file(filename: str) - Reads one specific file by name/path fresh from disk (a PDF under data/pdfs, or a CSV/XLSX/TXT under csv_update)
4. search_watchlist(keyword: str) - Searches the user's local watchlist (csv_update/EQUITY, ETF, FONDS, active_trader, and the finanzen.net TOP_LIST snapshot) by country/sector/theme keyword, e.g. "China", "Health", "Defense"
5. list_indexed_sources() - Shows which documents are indexed

Use search_watchlist (not search_documents) when the user asks what stocks/ETFs/
funds they already track for a given country, sector, or theme — e.g. "what
China ETFs do I have", "show my Health sector holdings". search_documents only
covers PDFs/emails, not the watchlist CSVs.

Use read_file whenever the user names or pastes a path to one specific file
(e.g. "CHINA.csv", "what's in Equity Preferences List_de_1656262.pdf") — this
reads that exact file fresh from disk, which is more reliable than
search_documents/search_watchlist for a single named file.

CRITICAL RULE — instrument identification: if the user mentions an equity,
ETF, or fund by name (e.g. "SAP", "Alibaba", "the China ETF") rather than by
ISIN or WKN, do NOT call get_market_quote or search_documents yet. Instead,
your FIRST step is always to call search_watchlist(keyword=<name>) to check
whether that instrument is already in the user's watchlist and resolve its
ISIN/WKN from there.
- If search_watchlist finds a matching instrument, use its ISIN/WKN to
  proceed with get_market_quote (or search_documents) in the same turn —
  do not make the user repeat the ISIN/WKN you just found.
- If search_watchlist finds no match (or errors), THEN ask the user for the
  ISIN or WKN of that instrument. Only proceed with get_market_quote or
  search_documents once the user has given it (or has explicitly told you
  they don't have one, in which case you may fall back to the bare
  name/ticker).
This does not apply to indices, commodities, or currency pairs (e.g. DAX,
Gold, EUR/USD), which have no ISIN/WKN and can be queried directly.

CRITICAL RULE — dates on fetched data: get_market_quote results include a
"timestamp" field, and market-sourced search_documents passages include a
"Time" field — this is when the data was actually retrieved, not necessarily
the exchange's last close date. Whenever you report a price or figure that
came from one of these tools, always state that date/time in your answer
(e.g. "SAP: 165.54 EUR, as of 2026-07-23 15:05"), so the user knows exactly
which day/time the number is from. Never state a price without its date.

CRITICAL RULE — dates on documents: every search_documents passage includes
a "date" field (the email's send date, or "unknown" if it couldn't be
determined). Always state this date when summarizing or quoting a passage
(e.g. "According to the 2026-07-15 UBS newsletter..."). If the date is
"unknown", say so explicitly instead of guessing or omitting it — never
imply a passage is recent when its date is unknown. You are also told
today's date below; use it to judge how old a passage is (e.g. "this is
from 8 days ago").

IMPORTANT: Use these functions as JSON requests:
- If you need search_documents: respond with JSON {"tool": "search_documents", "query": "..."}
- If you need get_market_quote: respond with JSON {"tool": "get_market_quote", "symbols": "..."}
- If you need read_file: respond with JSON {"tool": "read_file", "filename": "..."}
- If you need search_watchlist: respond with JSON {"tool": "search_watchlist", "keyword": "..."}
- If you need list_indexed_sources: respond with JSON {"tool": "list_indexed_sources"}

After receiving tool results, respond to the results in English.

Respond in English, precisely and in a well-structured way."""


def _get_tool_schema() -> list[dict]:
    """Define tool schemas for function calling."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_documents",
                "description": "Search the local financial document index for information relevant to the query",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_market_quote",
                "description": "Get live market prices for stocks, ETFs, indices or commodities",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {"type": "string", "description": "Comma-separated symbols (e.g. 'SAP.DE, AAPL, Gold')"}
                    },
                    "required": ["symbols"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read one specific file by name/path fresh from disk (PDF under data/pdfs, or CSV/XLSX/TXT under csv_update)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "description": "Filename or path, e.g. 'CHINA.csv'"}
                    },
                    "required": ["filename"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_watchlist",
                "description": "Search the user's local watchlist (csv_update/EQUITY, ETF, FONDS, active_trader, TOP_LIST) by country/sector/theme keyword",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "Keyword to match against instrument name and sector tag, e.g. 'China', 'Health'"}
                    },
                    "required": ["keyword"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_indexed_sources",
                "description": "List the types and counts of documents currently in the local index",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }
    ]


class AgentRetriever:
    def __init__(self, embedder, store, market_fetcher, cfg: dict):
        self.cfg = cfg
        self.history: list = []
        # Last market quote context (bridged from query.py after a price lookup)
        self._last_market_context: str = ""

        # Inject shared instances into tool module
        top_k = cfg.get("rag", {}).get("top_k", 5)
        init_tools(store, market_fetcher, top_k)

        # LLM: MiniMax via OpenAI-compatible endpoint
        llm_cfg = cfg["llm"]
        minimax_api_key = llm_cfg.get("minimax_api_key", "").strip()
        minimax_base_url = llm_cfg.get("minimax_base_url", "https://api.minimaxi.com/v1").strip()
        minimax_model = llm_cfg.get("minimax_model", "MiniMax-M2.7-highspeed")

        if not minimax_api_key:
            raise ValueError(
                "minimax_api_key not set in config.yaml [llm] section. "
                "AgentRetriever requires a cloud LLM."
            )

        self.llm = ChatOpenAI(
            model=minimax_model,
            api_key=minimax_api_key,
            base_url=minimax_base_url,
            temperature=0.2,
            extra_body={"thinking": {"type": "disabled"}},
        )
        
        self.tool_schemas = _get_tool_schema()
        self.tool_map = {
            "search_documents": search_documents,
            "get_market_quote": get_market_quote,
            "read_file": read_file,
            "search_watchlist": search_watchlist,
            "list_indexed_sources": list_indexed_sources,
        }

    def set_market_context(self, text: str):
        """Inject live market quote text so it appears in the next agent prompt."""
        self._last_market_context = (text or "").strip()

    @staticmethod
    def _strip_think(text: str) -> str:
        """Drop any <think>...</think> reasoning MiniMax leaks into content despite
        thinking being disabled, plus an unclosed trailing <think> block."""
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        text = re.sub(r'<think>.*$', '', text, flags=re.DOTALL)
        return text.strip()

    @staticmethod
    def _parse_tool_call_response(response_text: str) -> tuple[str | None, dict]:
        """Extract a tool call from several MiniMax output formats."""
        tool_name = None
        tool_args: dict = {}

        # Format 1: MiniMax <tool_call>{"tool": "...", "parameters": {...}}</tool_call>
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

        # Format 2: bare JSON tool call {"tool": "...", "query": "..."}
        try:
            json_match = re.search(r'\{.*"tool".*\}', response_text, re.DOTALL)
            if json_match:
                tool_call_json = json.loads(json_match.group())
                tool_name = tool_call_json.get("tool")
                tool_args = tool_call_json.get("parameters") or {
                    k: v for k, v in tool_call_json.items() if k != "tool"
                }
                return tool_name, tool_args
        except Exception:
            pass

        # Format 3: MiniMax XML-like format <minimax:tool_call><invoke name="..."></invoke></minimax:tool_call>
        xml_match = re.search(r'<minimax:tool_call>\s*<invoke name="([^"]+)"[^>]*>\s*</invoke>\s*</minimax:tool_call>', response_text, re.DOTALL)
        if xml_match:
            return xml_match.group(1), {}

        # Format 4: Pseudo tool call blocks like:
        # [TOOL_CALL]
        # {tool => "search_watchlist", args => { --keyword "ETF" }}
        # [/TOOL_CALL]
        for pseudo_match in re.finditer(r'\[TOOL_CALL\](.*?)\[/TOOL_CALL\]', response_text, re.DOTALL | re.IGNORECASE):
            block = pseudo_match.group(1)
            name_match = re.search(r'tool\s*=>\s*["\']([^"\']+)["\']', block, re.IGNORECASE)
            if not name_match:
                name_match = re.search(r'tool\s*[:=]\s*["\']?([A-Za-z_][A-Za-z0-9_]*)["\']?', block, re.IGNORECASE)
            if name_match:
                tool_name = name_match.group(1).strip().strip('"\'')

            # Parse generic CLI-like arguments: --query "market analysis", --filename "Biotech.csv".
            cli_args = re.findall(r'--([A-Za-z_][A-Za-z0-9_]*)\s+["\']([^"\']+)["\']', block)
            if cli_args and tool_name:
                tool_args = {key: value.strip() for key, value in cli_args}
                return tool_name, tool_args

            # Parse generic pseudo key/value shapes: keyword => "ETF", filename: "Biotech.csv".
            kv_args = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*(?:=>|:|=)\s*["\']([^"\']+)["\']', block)
            if kv_args and tool_name:
                tool_args = {key: value.strip() for key, value in kv_args if key.lower() != "tool"}
                if tool_args:
                    return tool_name, tool_args

            # Fallback: if the block only contains one quoted string, use it as the most likely value.
            quoted = [q.strip() for q in re.findall(r'["\']([^"\']+)["\']', block)]
            if tool_name and len(quoted) == 1:
                if tool_name == "read_file":
                    tool_args = {"filename": quoted[0]}
                elif tool_name == "search_documents":
                    tool_args = {"query": quoted[0]}
                elif tool_name == "search_watchlist":
                    tool_args = {"keyword": quoted[0]}
                elif tool_name == "get_market_quote":
                    tool_args = {"symbols": quoted[0]}
                else:
                    tool_args = {"value": quoted[0]}
                return tool_name, tool_args

        return tool_name, tool_args

    @classmethod
    def _extract_tool_calls(cls, response_text: str) -> list[tuple[str, dict]]:
        """Extract one or more tool calls from a single LLM response."""
        tool_calls: list[tuple[str, dict]] = []

        # If MiniMax emits multiple pseudo blocks, execute each one in order.
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

    def _execute_tool(self, tool_name: str, tool_args: dict) -> str:
        """Execute a single tool and return result as string."""
        tool_fn = self.tool_map.get(tool_name)
        if not tool_fn:
            return f"Unknown tool: {tool_name}"
        
        try:
            print(f"\n[Tool] {tool_name}({tool_args})")
            result = tool_fn.invoke(tool_args)
            print(f"[Result] {result[:300]}...")
            return result
        except Exception as e:
            return f"Tool error: {e}"

    MAX_TOOL_HOPS = 4

    def query(self, question: str, stream: bool = False) -> str:
        """Run agent with function calling loop.

        Loops up to MAX_TOOL_HOPS times: each hop may itself request one or
        more tool calls (e.g. search_watchlist fails to resolve an ISIN, so
        the model tries get_market_quote with a bare ticker next) instead of
        assuming a single tool-call round trip always ends in a final answer.

        Args:
            question: User's query
            stream: Ignored (kept for API compatibility)
        """
        from datetime import date

        recent_history = self.history[-6:]
        print(f"\n[Agent] Processing: {question}\n")

        try:
            # Build messages
            messages = [SystemMessage(content=_SYSTEM_PROMPT)]
            messages.append(SystemMessage(content=f"Today's date is {date.today().isoformat()}."))
            if self._last_market_context:
                messages.append(SystemMessage(
                    content=f"Recent market data fetched this session:\n{self._last_market_context}"
                ))
            messages.extend(recent_history)
            messages.append(HumanMessage(content=question))

            answer = ""
            for hop in range(self.MAX_TOOL_HOPS):
                response = self.llm.invoke(messages)
                response_text = response.content if hasattr(response, "content") else str(response)

                tool_calls = self._extract_tool_calls(response_text)
                if not tool_calls:
                    answer = response_text
                    break

                tool_results: list[str] = []
                for tool_name, tool_args in tool_calls:
                    result = self._execute_tool(tool_name, tool_args)
                    tool_results.append(f"Tool '{tool_name}' result:\n{result}")

                messages.append(AIMessage(content=response_text))
                messages.append(HumanMessage(
                    content="\n\n".join(tool_results)
                    + "\n\nIf this already answers the question, respond now with your final "
                    "answer — do not call another function just to double-check or add extra "
                    "detail. Only call another function if the result above clearly failed "
                    "(e.g. an error, empty result, or null price) and you still need the "
                    "information to answer."
                ))
                answer = response_text
            else:
                print(f"[Agent] Hit MAX_TOOL_HOPS={self.MAX_TOOL_HOPS} without a final answer; "
                      "returning the last response as-is.")

            answer = self._strip_think(answer)

        except Exception as exc:
            import traceback
            answer = f"[Agent error] {exc}\n{traceback.format_exc()}"

        print(answer)

        # Save to history
        self.history.append(HumanMessage(content=question))
        self.history.append(AIMessage(content=answer))

        return answer

    def clear_history(self):
        """Reset conversation history."""
        self.history = []
        print("[Agent] Conversation history cleared.")

    def set_history(self, messages: list[dict]):
        """Replace conversation history from plain {"role", "content"} dicts.

        Used by the stateless HTTP /chat endpoint: the caller (dashboard)
        owns the transcript in its own session state and resends it on every
        request, since a single AgentRetriever instance is shared across
        concurrent browser sessions.
        """
        self.history = [
            AIMessage(content=m["content"]) if m.get("role") == "assistant"
            else HumanMessage(content=m["content"])
            for m in messages
        ]
