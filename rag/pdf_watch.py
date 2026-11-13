from __future__ import annotations

from pathlib import Path
from typing import Optional


def resolve_pdf_scan_root(cfg: dict) -> Path:
    root = (cfg.get("rag", {}) or {}).get("pdf_scan_root")
    if not root:
        root = "~/Projects/pdfs"
    return Path(root).expanduser().resolve()


def find_new_pdf_files(root: Path, processed_paths: set[str]) -> list[Path]:
    if not root.exists():
        return []
    pdf_files = sorted(root.rglob("*.pdf"))
    return [pdf for pdf in pdf_files if str(pdf.resolve()) not in processed_paths]
