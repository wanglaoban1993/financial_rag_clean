# ============================================================
#  eval/evaluators/rule_compliance.py — regex/keyword checks against the
#  CRITICAL RULEs baked into rag/agent_retriever.py's system prompt
#  (ISIN/WKN gate, mandatory date citation on prices/passages).
# ============================================================

from __future__ import annotations

import re

from eval.adapters.base import Trace
from eval.evaluators.base import EvalResult

# ISO (2026-07-23), German dotted (23.07.2026), spelled-out month names in
# German or English ("4. August 2026", "August 4, 2026"), a relative
# "N days/Tage ago/her", or an explicit statement that the date is unknown —
# all count as compliant.
_MONTH_NAMES = (
    r"Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
    r"|January|February|March|May|June|July|October|December"
)

_DATE_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\.\d{1,2}\.\d{2,4}"
    rf"|\d{{1,2}}\.?\s*(?:{_MONTH_NAMES})\s+\d{{4}}"
    rf"|(?:{_MONTH_NAMES})\s+\d{{1,2}},?\s+\d{{4}}"
    r"|\d+\s*(days?|Tagen?)\s*(ago|her)"
    r"|\bvor\s+\d+\s*Tagen?\b"
    r"|\bunknown\b|\bunbekannt\b",
    re.IGNORECASE,
)


def evaluate(case: dict, trace: Trace) -> list[EvalResult]:
    expect = case.get("expect", {})
    results: list[EvalResult] = []
    answer = trace.final_answer or ""

    if expect.get("answer_must_contain_date"):
        found = bool(_DATE_PATTERN.search(answer))
        results.append(EvalResult(
            check="answer_must_contain_date",
            passed=found,
            detail=f"date pattern {'found' if found else 'NOT found'} in answer: {answer[:200]!r}",
        ))

    if "answer_keywords_any" in expect:
        keywords = expect["answer_keywords_any"]
        matched = [k for k in keywords if k.lower() in answer.lower()]
        results.append(EvalResult(
            check="answer_keywords_any",
            passed=bool(matched),
            detail=f"expected any of {keywords!r}, matched={matched!r} in answer: {answer[:200]!r}",
        ))

    if "answer_keywords_none" in expect:
        keywords = expect["answer_keywords_none"]
        matched = [k for k in keywords if k.lower() in answer.lower()]
        results.append(EvalResult(
            check="answer_keywords_none",
            passed=not matched,
            detail=f"forbidden keywords {keywords!r}, found={matched!r} in answer: {answer[:200]!r}",
        ))

    return results
