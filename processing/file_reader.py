"""Locate and read a specific file the user referenced by name/path in a question.

Used when a query mentions a filename or path directly (PDF/CSV/XLSX/TXT) —
reads the file fresh from disk instead of relying on whatever got embedded
into FAISS, so the answer reflects that file's actual current content.

Scope: only files that already exist under the project's known data folders
(data/pdfs, csv_update/**) — this is a "find and read a specific file the user
named" feature, not a general filesystem browser.
"""

import re
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOME_ROOT = Path.home()
_SEARCH_ROOTS = [
    _HOME_ROOT / "Projects" / "pdfs",
    _PROJECT_ROOT / "data" / "pdfs",
    _PROJECT_ROOT / "csv_update",
]
_SUPPORTED_EXTENSIONS = (".pdf", ".csv", ".xlsx", ".txt")

# Absolute Windows path pasted directly (e.g. "D:\Dropbox\...\CHINA.csv").
_ABS_PATH_PATTERN = re.compile(
    r'[A-Za-z]:[\\/][^"\'\r\n]+?\.(?:pdf|csv|xlsx|txt)', re.IGNORECASE
)


def find_referenced_file(question: str) -> Optional[Path]:
    """Find a file referenced in the question.

    1. If an absolute path is pasted directly and it exists, use it as-is.
    2. Otherwise, check whether any real filename under the known data
       folders appears verbatim (case-insensitive) in the question text —
       avoids fragile "extract a filename from free text" regex guessing.
    """
    q = (question or "").strip()
    if not q:
        return None

    abs_match = _ABS_PATH_PATTERN.search(q)
    if abs_match:
        candidate = Path(abs_match.group(0).strip().strip("\"'"))
        if candidate.is_file():
            return candidate

    q_lower = q.lower()
    for root in _SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                continue
            if candidate.name.lower() in q_lower:
                return candidate
    return None


def read_file_for_context(path: Path, max_chars: int = 8000) -> str:
    """Read a file's content as plain text, truncated for prompt injection."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            from processing.pdf_parser import parse_pdf
            text = parse_pdf(str(path)).text
        elif suffix in (".csv", ".xlsx"):
            import pandas as pd
            df = (
                pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
                if suffix == ".csv"
                else pd.read_excel(path)
            )
            text = df.to_string(index=False)
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return f"[Could not read {path.name}: {exc}]"

    text = text or ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[truncated, {len(text)} chars total]"
    return text
