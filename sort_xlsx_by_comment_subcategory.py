"""Sort an XLSX watchlist by the subcategory inside the COMMENT column.

The script removes fully blank rows, extracts the text after "|" from the
COMMENT column, and groups rows with the same subcategory together.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "csv_update" / "ALLE Data Center_merged.xlsx"


def is_blank_value(value: object) -> bool:
    """Return True for NaN, None, or strings that only contain whitespace."""
    if pd.isna(value):
        return True
    return isinstance(value, str) and not value.strip()


def drop_blank_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Remove rows where every cell is blank."""
    blank_mask = df.map(is_blank_value).all(axis=1)
    return df.loc[~blank_mask].copy()


def find_comment_column(df: pd.DataFrame) -> str:
    """Find COMMENT/comment column without requiring exact capitalization."""
    for column in df.columns:
        if str(column).strip().lower() == "comment":
            return column
    raise ValueError("No COMMENT column found in the input file.")


def extract_subcategory(comment: object) -> str:
    """Extract subcategory text from COMMENT values like 'US | Industrial REIT'."""
    if pd.isna(comment):
        return ""

    text = str(comment).strip()
    if "|" not in text:
        return text.casefold()

    return text.split("|", 1)[1].strip().casefold()


def sort_by_comment_subcategory(input_path: Path, output_path: Path) -> None:
    df = pd.read_excel(input_path)
    df = drop_blank_rows(df)

    comment_column = find_comment_column(df)
    df["_comment_subcategory_sort"] = df[comment_column].map(extract_subcategory)
    df["_original_row_order"] = range(len(df))

    df = df.sort_values(
        by=["_comment_subcategory_sort", comment_column, "_original_row_order"],
        kind="mergesort",
        na_position="last",
    ).drop(columns=["_comment_subcategory_sort", "_original_row_order"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(output_path, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove blank rows from an XLSX file and group rows by the "
            "subcategory in the COMMENT column."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Input XLSX file. Default: {DEFAULT_INPUT}",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output XLSX file. Default: input filename with '_sorted' suffix.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file instead of writing a new sorted file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve()

    if args.in_place:
        output_path = input_path
    elif args.output:
        output_path = args.output.resolve()
    else:
        output_path = input_path.with_name(f"{input_path.stem}_sorted{input_path.suffix}")

    sort_by_comment_subcategory(input_path, output_path)
    print(f"Sorted file written to: {output_path}")


if __name__ == "__main__":
    main()
