from pathlib import Path
import argparse

import pandas as pd


INVALID_FILENAME_CHARS = '\\/:*?"<>|'


def safe_sheet_name(sheet_name: object) -> str:
    safe_name = "".join(
        "_" if c in INVALID_FILENAME_CHARS else c
        for c in str(sheet_name)
    ).strip()
    return safe_name or "sheet"


def export_all_sheets(xlsx_file: Path, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_sheets = pd.read_excel(xlsx_file, sheet_name=None)

    name_map: dict[str, str] = {}
    for sheet_name, df_sheet in all_sheets.items():
        csv_name = f"{safe_sheet_name(sheet_name)}.csv"
        csv_path = output_dir / csv_name
        df_sheet.to_csv(csv_path, index=False, encoding="utf-8-sig")
        name_map[sheet_name] = csv_name

    return name_map


def main() -> None:
    parser = argparse.ArgumentParser(description="Export XLSX sheets to CSV.")
    parser.add_argument("input_xlsx", type=Path, help="Path to the input .xlsx file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for CSV files. Defaults to the XLSX folder.",
    )
    parser.add_argument(
        "--all-sheets",
        action="store_true",
        help="Export every worksheet as a separate CSV.",
    )
    args = parser.parse_args()

    xlsx_file = args.input_xlsx
    if args.all_sheets:
        output_dir = args.output_dir or xlsx_file.parent
        name_map = export_all_sheets(xlsx_file, output_dir)
        print(f"Exported {len(name_map)} sheets to: {output_dir.resolve()}")
        for sheet_name, csv_name in name_map.items():
            print(f"- {sheet_name} -> {csv_name}")
        return

    csv_file = (
        args.output_dir / xlsx_file.with_suffix(".csv").name
        if args.output_dir
        else xlsx_file.with_suffix(".csv")
    )
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(xlsx_file)
    df.to_csv(csv_file, index=False, encoding="utf-8-sig")
    print(f"Converted: {xlsx_file} -> {csv_file}")


if __name__ == "__main__":
    main()
