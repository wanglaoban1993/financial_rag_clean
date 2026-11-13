# ============================================================
#  rag/doc_inventory.py — "which documents do I have, newest first?"
#
#  The FAISS index stores chunks, not documents, and its metadata is uneven:
#    - email chunks carry `subject`, and `date` (ISO) on only some of them
#    - PDF chunks carry `filename` but no date at all
#  This groups chunks back into documents and dates them as well as the data
#  allows, always saying *what* each date means so the agent can't pass off a
#  file-modified time as a publication date.
# ============================================================

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

MAX_LIMIT = 30
_PREVIEW_CHARS = 300

DATE_BASIS_EMAIL = "email date"
DATE_BASIS_PDF = "file modified time (when the PDF was added; not its publication date)"
DATE_BASIS_PDF_NAME = "date in the PDF's filename (likely its publication date)"

_PATH_SPLIT = re.compile(r"[\\/]")


_NAME_DATE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def date_from_filename(filename: str) -> str:
    """ISO date embedded in a filename as YYYYMMDD or DDMMYYYY (e.g.
    "..._20261008.pdf", "..._03082026.pdf"), or "" if none is a plausible past
    date. Future dates are rejected so a typo'd name is not reported as news."""
    today = datetime.now().date()
    for digits in _NAME_DATE.findall(str(filename or "")):
        for fmt in ("%Y%m%d", "%d%m%Y"):
            try:
                day = datetime.strptime(digits, fmt).date()
            except ValueError:
                continue
            if 2000 <= day.year and day <= today:
                return day.isoformat()
    return ""


def pdf_added_dates(registry: dict[str, str]) -> dict[str, str]:
    """processed_local_pdfs.json maps a PDF path to "<mtime_ns>:<size>".
    Returns {basename: YYYY-MM-DD}; entries that don't parse are skipped.
    Keyed by basename because the registry paths are host paths that differ
    from the (container) paths and from the index's `filename` metadata."""
    dates: dict[str, str] = {}
    for path, stamp in (registry or {}).items():
        try:
            mtime_ns = int(str(stamp).split(":", 1)[0])
            day = datetime.fromtimestamp(mtime_ns / 1e9).date().isoformat()
        except (ValueError, OSError, OverflowError):
            continue
        dates[_PATH_SPLIT.split(str(path))[-1]] = day
    return dates


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS].rstrip() + "…"


def build_recent_documents(
    chunks: Iterable[tuple[dict[str, Any], str]],
    pdf_dates: dict[str, str],
    source: str = "all",
    limit: int = 10,
) -> dict[str, Any]:
    """chunks: (metadata, text) pairs for every indexed chunk, in index order.
    Returns the documents of the requested source, newest first (undated last)."""
    source = (source or "all").strip().lower()
    if source not in {"all", "pdf", "email"}:
        return {"error": f"source must be 'all', 'pdf' or 'email', got {source!r}"}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, MAX_LIMIT))

    # Chunk -> document: PDFs by filename, emails by (subject, date). The first
    # chunk seen supplies the preview (chunks are stored in document order).
    docs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for meta, text in chunks:
        kind = meta.get("source")
        if kind == "pdf":
            title = str(meta.get("filename") or "").strip()
            date = date_from_filename(title)
            basis = DATE_BASIS_PDF_NAME
            if not date:
                date = pdf_dates.get(title, "")
                basis = DATE_BASIS_PDF
        elif kind == "email":
            title = str(meta.get("subject") or "").strip()
            date = str(meta.get("date") or "").strip()
            basis = DATE_BASIS_EMAIL
        else:
            continue
        if not title or (source != "all" and kind != source):
            continue

        key = (kind, title, date)
        entry = docs.get(key)
        if entry is None:
            docs[key] = {
                "type": kind,
                "title": title,
                "date": date or "unknown",
                "date_basis": basis if date else "none available in the index",
                "chunks": 1,
                "preview": _clip(text),
            }
        else:
            entry["chunks"] += 1

    # ISO dates sort lexically; "unknown" sorts after every real date.
    ordered = sorted(
        docs.values(),
        key=lambda d: (d["date"] != "unknown", d["date"]),
        reverse=True,
    )
    undated = sum(1 for d in ordered if d["date"] == "unknown")
    return {
        "total_documents": len(ordered),
        "undated_documents": undated,
        "showing": min(limit, len(ordered)),
        "documents": ordered[:limit],
        "note": (
            "Newest first; undated documents are listed last. Always tell the user what each "
            "date means (see date_basis) and that documents with date 'unknown' cannot be "
            "ranked by recency."
        ),
    }
