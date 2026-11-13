# ============================================================
#  eval/adapters/base.py — common Trace contract for eval adapters
#
#  Evaluators only ever look at a Trace. Adapters are the only code that
#  knows how to drive a given agent implementation (today: the single
#  tool-calling AgentRetriever; later: a multi-agent orchestrator).
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCallRecord:
    agent: str    # "main" today; sub-agent name once multi-agent RAG exists
    tool: str
    args: dict
    result: str   # raw string result (usually JSON) as returned by the tool


@dataclass
class Trace:
    query: str
    history: list[dict] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    final_answer: str = ""
    error: str | None = None


class Adapter(Protocol):
    def run(self, query: str, history: list[dict] | None = None) -> Trace:
        ...
