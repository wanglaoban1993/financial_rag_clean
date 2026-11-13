# ============================================================
#  rag/date_filter.py — "today's email", "the PDF from 2026-10-08"
#
#  A question that names a document and a date ("what does today's newsletter
#  say about the DAX?") used to be answered by text similarity alone, so
#  yesterday's near-identical newsletter ranked as high as today's. Chunks do
#  carry a date: emails have `date` (ISO), PDFs have none, but most file names
#  end in one (rag.doc_inventory.date_from_filename). This module
#    - finds such a date range in the question (parse_document_date_range), and
#    - dates a chunk from its metadata (effective_date) so the retriever can
#      restrict the search to that range *before* ranking.
#
#  The range is only applied when the question mentions BOTH a document word
#  and a date. "What's the news on SpaceX today?" is a market question, not a
#  request for documents dated today, and must not be filtered.
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from rag.doc_inventory import date_from_filename

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DOTTED = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_CHINESE = re.compile(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")

_MONTHS = {
    "january": 1, "jan": 1, "januar": 1, "february": 2, "feb": 2, "februar": 2,
    "march": 3, "mar": 3, "märz": 3, "maerz": 3, "april": 4, "apr": 4, "may": 5, "mai": 5,
    "june": 6, "jun": 6, "juni": 6, "july": 7, "jul": 7, "juli": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "oktober": 10, "okt": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12, "dezember": 12, "dez": 12,
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY_FIRST = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\.?\s+(?:of\s+)?({_MONTH_NAMES})\b\.?(?:,?\s*(\d{{4}}))?", re.I)
_MONTH_FIRST = re.compile(rf"\b({_MONTH_NAMES})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?", re.I)

_TODAY = re.compile(r"\btoday(?:'s)?\b|\bheute\b|今天|今日", re.I)
_YESTERDAY = re.compile(r"\byesterday(?:'s)?\b|\bgestern\b|昨天|昨日", re.I)
_DAY_BEFORE = re.compile(r"day before yesterday|\bvorgestern\b|前天", re.I)
_THIS_WEEK = re.compile(r"\bthis week(?:'s)?\b|\bdiese woche\b|本周|这周|这一周", re.I)

# Words that say the question is about stored documents, not about market data.
_DOCUMENT_WORDS = re.compile(
    r"\be-?mails?\b|\bmails?\b|\bnewsletters?\b|\bpdfs?\b|\breports?\b|\bdocuments?\b|\bfiles?\b"
    r"|\bbriefings?\b|\bbericht(?:e)?\b|\bdokument(?:e)?\b|\bausgabe\b|邮件|报告|文档|文件|研报|简报",
    re.I,
)


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def label(self) -> str:
        return self.start.isoformat() if self.start == self.end else f"{self.start.isoformat()} to {self.end.isoformat()}"


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _without_year(month: int, day: int, today: date) -> date | None:
    """A date written without a year means the most recent one that is not in the future."""
    found = _safe_date(today.year, month, day)
    if found and found > today:
        found = _safe_date(today.year - 1, month, day)
    return found


def _explicit_dates(text: str, today: date) -> list[date]:
    found: list[date | None] = []
    for y, m, d in _ISO.findall(text):
        found.append(_safe_date(int(y), int(m), int(d)))
    for d, m, y in _DOTTED.findall(text):
        found.append(_safe_date(int(y), int(m), int(d)))
    for y, m, d in _CHINESE.findall(text):
        found.append(_safe_date(int(y), int(m), int(d)) if y else _without_year(int(m), int(d), today))
    for d, month, y in _DAY_FIRST.findall(text):
        mon = _MONTHS[month.lower()]
        found.append(_safe_date(int(y), mon, int(d)) if y else _without_year(mon, int(d), today))
    for month, d, y in _MONTH_FIRST.findall(text):
        mon = _MONTHS[month.lower()]
        found.append(_safe_date(int(y), mon, int(d)) if y else _without_year(mon, int(d), today))
    return sorted({f for f in found if f})


def parse_document_date_range(question: str, today: date | None = None) -> DateRange | None:
    """The date range a question asks for, or None when the question does not ask for
    *documents* from a date. Explicit dates win over relative words; two or more explicit
    dates give the range between the earliest and the latest."""
    text = question or ""
    if not _DOCUMENT_WORDS.search(text):
        return None
    today = today or date.today()

    explicit = _explicit_dates(text, today)
    if explicit:
        return DateRange(explicit[0], explicit[-1])
    if _DAY_BEFORE.search(text):
        day = today - timedelta(days=2)
        return DateRange(day, day)
    if _YESTERDAY.search(text):
        day = today - timedelta(days=1)
        return DateRange(day, day)
    if _TODAY.search(text):
        return DateRange(today, today)
    if _THIS_WEEK.search(text):
        return DateRange(today - timedelta(days=today.weekday()), today)
    return None


def effective_date(metadata: dict | None) -> str:
    """ISO date of a chunk: the email date, else a date in the PDF's file name, else ''."""
    md = metadata or {}
    raw = str(md.get("date") or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return date_from_filename(str(md.get("filename") or ""))


def in_range(metadata: dict | None, start: str, end: str) -> bool:
    """Is the chunk's date within [start, end] (ISO strings compare correctly as text)?
    A chunk with no date at all is not in any range."""
    day = effective_date(metadata)
    return bool(day) and start <= day <= end
