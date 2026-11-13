# ============================================================
#  eval/evaluators/retrieval.py — did search_documents/search_watchlist
#  actually return something relevant, not just get called?
# ============================================================

from __future__ import annotations

from eval.adapters.base import Trace
from eval.evaluators.base import EvalResult

_RETRIEVAL_TOOLS = {"search_documents", "search_watchlist"}


def evaluate(case: dict, trace: Trace) -> list[EvalResult]:
    expect = case.get("expect", {})
    if "passages_keywords_any" not in expect:
        return []

    keywords = expect["passages_keywords_any"]
    combined = "\n".join(
        c.result for c in trace.tool_calls if c.tool in _RETRIEVAL_TOOLS
    )
    matched = [k for k in keywords if k.lower() in combined.lower()]

    if not combined:
        detail = "no search_documents/search_watchlist tool call found in trace"
    else:
        detail = f"expected any of {keywords!r}, matched={matched!r} in retrieved results"

    return [EvalResult(
        check="passages_keywords_any",
        passed=bool(matched),
        detail=detail,
    )]
