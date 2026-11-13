# ============================================================
#  eval/evaluators/base.py — shared EvalResult type
# ============================================================

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EvalResult:
    check: str      # which expect-key this result is for, e.g. "tools_called"
    passed: bool
    detail: str
