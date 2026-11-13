from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from server_app.db import connect, ensure_schema


ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin1")


def read_csv(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ENCODINGS:
        try:
            return pd.read_csv(path, sep=None, engine="python", encoding=encoding, dtype=str).fillna("")
        except Exception as exc:  # pragma: no cover - keeps import tolerant for mixed exports
            last_error = exc
    raise RuntimeError(f"Could not read {path}: {last_error}")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_decimal(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    normalized = (
        text.replace("%", "")
        .replace("EUR", "")
        .replace("USD", "")
        .replace(".", "")
        .replace(",", ".")
        .strip()
    )
    try:
        number = float(normalized)
    except ValueError:
        return None
    if math.isnan(number):
        return None
    return number


def json_ready(row: dict[str, Any]) -> str:
    cleaned = {key: clean_text(value) for key, value in row.items()}
    return json.dumps(cleaned, ensure_ascii=False)


def watchlist_name(path: Path) -> str:
    name = path.stem
    if name.endswith("_merged"):
        name = name[: -len("_merged")]
    return name


def asset_group(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return relative.parts[0] if len(relative.parts) > 1 else None


def compute_source_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root.parent if root.name else root))


def import_file(path: Path, root: Path) -> int:
    df = read_csv(path)
    source_path = compute_source_path(path, root)
    imported_at = datetime.now(timezone.utc)
    rows: list[tuple[Any, ...]] = []
    group = asset_group(path, root)
    name = watchlist_name(path)
    source_mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)

    for index, row in df.iterrows():
        row_dict = row.to_dict()
        if not any(clean_text(value) for value in row_dict.values()):
            continue
        rows.append(
            (
                source_path,
                name,
                group,
                int(index) + 1,
                clean_text(row_dict.get("WKN")),
                clean_text(row_dict.get("ISIN")),
                clean_text(row_dict.get("Name")),
                clean_text(row_dict.get("STOCK_ID")),
                clean_text(row_dict.get("COMMENT")),
                clean_text(row_dict.get("COMMENT2")),  # stored in the `tmp` column
                clean_text(row_dict.get("COMMENT3")),
                clean_text(row_dict.get("Bid")),
                clean_text(row_dict.get("Ask")),
                clean_text(row_dict.get("Ask Time")),
                parse_decimal(row_dict.get("% Differenz ein Jahr")),
                parse_decimal(row_dict.get("% Differenz ein Monat")),
                parse_decimal(row_dict.get("% Differenz zum Vortageskurs")),
                None,
                json_ready(row_dict),
                imported_at,
            )
        )

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.watchlist_imports WHERE source_path = %s", (source_path,))
            cur.execute(
                """
                INSERT INTO app.watchlist_imports
                    (source_path, watchlist_name, asset_group, source_mtime, row_count, imported_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (source_path, name, group, source_mtime, len(rows), imported_at),
            )
            import_id = cur.fetchone()["id"]
            cur.executemany(
                """
                INSERT INTO app.watchlist_rows (
                    import_id, source_path, watchlist_name, asset_group, row_number,
                    wkn, isin, name, stock_id, comment, tmp, comment3,
                    bid_text, ask_text, ask_time, return_1y, return_1m, return_1d,
                    price_date, raw, imported_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s::json, %s
                )
                """,
                [(import_id, *row) for row in rows],
            )
    return len(rows)


def _is_importable(path: Path) -> bool:
    if path.name.endswith("_top.csv"):
        return False
    # Internal working dirs created by scripts/run_watchlist*.ps1 (log output,
    # temp per-job input copies) — not real watchlists, just implementation
    # details of those scripts. Skip anything under an underscore-prefixed dir.
    if any(part.startswith("_") for part in path.parts):
        return False
    # TOP_LIST keeps timestamped historical snapshots alongside the live file;
    # only the latest snapshot should be imported, or every run duplicates the
    # same rows under a new watchlist_name.
    if path.parent.name == "TOP_LIST" and path.name != "top_liste_latest.csv":
        return False
    return True


def prune_stale_imports(root: Path, current_source_paths: set[str]) -> int:
    """Delete watchlist_imports (cascades to watchlist_rows) for files under
    `root` that no longer exist on disk — e.g. renamed or moved CSVs from a
    past folder reorganization. Without this, every rename leaves a permanent
    zombie entry showing up alongside the real one."""
    prefix = compute_source_path(root, root) + "/"  # e.g. "csv_update/"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM app.watchlist_imports
                WHERE source_path LIKE %s
                  AND NOT (source_path = ANY(%s))
                """,
                [f"{prefix}%", list(current_source_paths)],
            )
            return cur.rowcount


def import_tree(root: Path) -> tuple[int, int]:
    ensure_schema()
    csv_files = sorted(path for path in root.rglob("*.csv") if _is_importable(path))
    imported_files = 0
    imported_rows = 0
    source_paths: set[str] = set()
    for path in csv_files:
        count = import_file(path, root)
        imported_files += 1
        imported_rows += count
        source_paths.add(compute_source_path(path, root))
        print(f"Imported {count:5d} rows from {path}")
    pruned = prune_stale_imports(root, source_paths)
    if pruned:
        print(f"Pruned {pruned} stale import(s) for files no longer on disk")
    return imported_files, imported_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Import generated watchlist CSV files into PostgreSQL.")
    parser.add_argument("--root", default=os.getenv("WATCHLIST_CSV_ROOT", "csv_update"))
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        raise SystemExit(f"CSV root does not exist: {root}")

    files, rows = import_tree(root)
    print(f"Done. Imported {rows} rows from {files} files.")


if __name__ == "__main__":
    main()
