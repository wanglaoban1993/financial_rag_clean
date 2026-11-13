# ============================================================
#  eval/evaluators/tool_selection.py — did the agent call the right tool(s)?
# ============================================================

from __future__ import annotations

from eval.adapters.base import Trace
from eval.evaluators.base import EvalResult


def evaluate(case: dict, trace: Trace) -> list[EvalResult]:
    expect = case.get("expect", {})
    if "tools_called" not in expect:
        return []

    expected = expect["tools_called"]
    actual = [c.tool for c in trace.tool_calls]
    passed = actual == expected
    return [EvalResult(
        check="tools_called",
        passed=passed,
        detail=f"expected={expected!r} actual={actual!r}",
    )]
