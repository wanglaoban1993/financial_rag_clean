# ============================================================
#  rag/grounding.py — Shared post-generation grounding heuristic.
#
#  Extracted from rag/retriever.py so both Pipeline mode (RAGRetriever)
#  and Agent mode (Orchestrator._synthesize_with_llm) can flag numbers/
#  dates an LLM stated that don't appear anywhere in the evidence it was
#  given, instead of each mode keeping its own copy of the same regex.
#
#  Heuristic only -- token/string matching, not semantic verification.
#  Numbers match exactly or at the answer's stated rounding (see
#  _grounded_by_rounding), but it still can't tell a computed average from
#  an invented one, and flags legitimate numbers rephrased beyond
#  recognition (units like "1.2 Mrd", words like "double"). Treat its output
#  as "worth a second look", not a proof of hallucination.
# ============================================================

from __future__ import annotations

import bisect
import re

# Numeric/date token pattern. Alternatives are tried in this order at each
# position and matched non-overlappingly, so e.g. "2024-01-15" is consumed
# whole as a date instead of also being re-matched piecemeal as a decimal/
# year fragment. Each alternative requires a decimal point, thousands
# separator, '%' sign, or date-like shape -- bare small integers ("top 5",
# "3 companies") are deliberately not matched, since they're rarely the kind
# of fabricated figure this check is meant to catch and would otherwise be
# the dominant source of false positives.
_TOKEN_RE = re.compile(
    r"(?P<date>\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b)"
    r"|(?P<percent>\d+(?:[.,]\d+)?\s?%)"
    r"|(?P<thousands>\b\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?\b)"
    r"|(?P<decimal>\b\d+[.,]\d+\b)"
    r"|(?P<year>\b(?:19|20)\d{2}\b)"
)


def _normalize_numeric_variants(raw: str) -> set[str]:
    """Return the possible normalized float values for a numeric token,
    covering both '1,234.56' (US) and '1.234,56' (European) conventions
    since we don't know which one the model/evidence used."""
    has_percent = "%" in raw
    core = raw.replace("%", "").strip().replace(" ", "")
    candidates = {core, core.replace(",", "")}  # comma = thousands sep
    candidates.add(core.replace(".", "").replace(",", "."))  # comma = decimal sep

    variants: set[str] = set()
    for cand in candidates:
        try:
            val = float(cand)
        except ValueError:
            continue
        variants.add(f"{val:.4f}".rstrip("0").rstrip("."))
    if not variants:
        variants.add(core)
    return {f"num:{v}{'%' if has_percent else ''}" for v in variants}


def _normalize_date_token(raw: str) -> str:
    # Canonicalize by sorted digits so "2024-01-15" and "15.01.2024"
    # (same date, different field order/separators) still match.
    digits = re.sub(r"\D", "", raw)
    return f"date:{''.join(sorted(digits))}"


def _tokenize_numeric(text: str) -> list[tuple[str, str]]:
    """Non-overlapping (raw_text, group_name) numeric/date tokens."""
    return [(m.group(), m.lastgroup) for m in _TOKEN_RE.finditer(text or "")]


def _normalize_token(raw: str, group: str) -> set[str]:
    if group == "date":
        return {_normalize_date_token(raw)}
    if group == "year":
        return {f"num:{raw}"}
    return _normalize_numeric_variants(raw)


def _extract_numeric_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw, group in _tokenize_numeric(text):
        tokens |= _normalize_token(raw, group)
    return tokens


# ---- rounding-tolerant fallback --------------------------------------
# Exact token matching flags legitimate rewrites: evidence "165.543" vs an
# answer that says "165.54", evidence JSON `"change_pct": 3.7` (no '%' sign) vs
# an answer that says "3.7%", or evidence "2026-10-08" vs a bare "2026". So a
# token that fails the exact match gets a second chance: it is grounded if some
# number in the evidence rounds to it at the precision the answer states.
# Dates and years stay exact -- a rounded date is not a date.

_EVIDENCE_NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")


def _float_candidates(raw: str) -> list[tuple[float, int]]:
    """(value, decimals) for each plausible reading of a numeric token --
    comma as thousands separator, or comma as decimal separator."""
    core = raw.replace("%", "").strip().replace(" ", "")
    readings = {core, core.replace(",", ""), core.replace(".", "").replace(",", ".")}
    out: list[tuple[float, int]] = []
    for reading in readings:
        try:
            value = float(reading)
        except ValueError:
            continue
        decimals = len(reading.split(".")[1]) if "." in reading else 0
        out.append((value, decimals))
    return out


def _evidence_values(evidence: str) -> list[float]:
    values: set[float] = set()
    for match in _EVIDENCE_NUMBER_RE.finditer(evidence or ""):
        for value, _ in _float_candidates(match.group()):
            values.add(value)
            values.add(-value)  # evidence may carry a sign the answer states as words
    return sorted(values)


def _within_rounding(value: float, decimals: int, sorted_values: list[float]) -> bool:
    tolerance = 0.5 * 10 ** -decimals + 1e-9
    i = bisect.bisect_left(sorted_values, value - tolerance)
    return i < len(sorted_values) and sorted_values[i] <= value + tolerance


def _grounded_by_rounding(raw: str, group: str, sorted_values: list[float]) -> bool:
    if group == "date":
        return False
    if group == "year":
        # exact only, but any evidence number counts -- e.g. the 2026 in "2026-10-08"
        i = bisect.bisect_left(sorted_values, float(raw))
        return i < len(sorted_values) and sorted_values[i] == float(raw)
    return any(_within_rounding(v, d, sorted_values) for v, d in _float_candidates(raw))


# PDF text extraction often leaves a space or line break after the thousands
# separator ("25. 164", "26.\n000"), which would be read as two numbers.
_BROKEN_THOUSANDS_RE = re.compile(r"(?<=\d)([.,])\s+(?=\d{3}(?!\d))")


def _with_repaired_numbers(evidence: str) -> str:
    """Evidence plus a copy with broken thousands separators joined. Both are kept
    because joining can be wrong ("in 2025. 300 people"), and extra text can only
    add matches, never remove one."""
    repaired = _BROKEN_THOUSANDS_RE.sub(r"\1", evidence or "")
    return evidence if repaired == evidence else f"{evidence}\n{repaired}"


def check_numeric_grounding(answer: str, evidence: str) -> list[str]:
    """Flag numbers/percentages/dates in `answer` that don't appear (in any
    format, or at the answer's stated rounding) anywhere in `evidence`.
    Heuristic only -- see module docstring."""
    evidence = _with_repaired_numbers(evidence)
    evidence_tokens = _extract_numeric_tokens(evidence)
    evidence_values: list[float] | None = None  # built lazily, only if needed

    unverified: list[str] = []
    seen_raw: set[str] = set()
    for raw, group in _tokenize_numeric(answer):
        if raw in seen_raw:
            continue
        seen_raw.add(raw)
        token_set = _normalize_token(raw, group)
        if token_set & evidence_tokens:
            continue
        if evidence_values is None:
            evidence_values = _evidence_values(evidence)
        if _grounded_by_rounding(raw, group, evidence_values):
            continue
        unverified.append(raw)
    return unverified


# Shared correction prompt: appended to the original prompt for a single
# regeneration attempt when check_numeric_grounding flags something. Kept
# here (not duplicated in retriever.py/orchestrator.py) so both call sites
# ask the model to correct itself the same way.
RETRY_SUFFIX_TEMPLATE = """

## Correction needed
Your previous draft stated these numbers/dates, but they do not appear anywhere in the evidence
above: {unverified}. Rewrite your answer. Only state numbers/dates that appear in the evidence
above -- if you cannot support a figure from the evidence, say plainly that it is not available
instead of restating it.
"""
