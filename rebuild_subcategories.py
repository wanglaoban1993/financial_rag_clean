"""
rebuild_subcategories.py
========================
Two modes:

1. --from-xlsx  (new)
   Reads COMMENT column from completed xlsx files, extracts subcategory tags
   (format "XX | Tag"), groups companies per tag, and rewrites subcategories.csv.

2. Default MiniMax mode
   Reads *_merged.csv files, classifies companies via MiniMax API,
   updates subcategories.csv descriptions, optionally uploads to Google Sheets.

Usage:
    python rebuild_subcategories.py --from-xlsx --xlsx-dir "C:/Users/.../Downloads"
    python rebuild_subcategories.py --sector Health --no-upload
"""

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd
import requests
import yaml
from config_loader import resolve_config_path

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = resolve_config_path()
SUBCATEGORIES_PATH = SCRIPT_DIR / "subcategories.csv"

# ── config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


CONFIG = load_config()
LLM_CFG = dict(CONFIG.get("llm", {}) or {})
MARKET_CFG = dict(CONFIG.get("market", {}) or {})

MINIMAX_API_KEY   = str(LLM_CFG.get("minimax_api_key") or "").strip()
MINIMAX_BASE_URL  = str(LLM_CFG.get("minimax_base_url") or "https://api.minimaxi.com/v1").rstrip("/")
MINIMAX_MODEL     = str(LLM_CFG.get("minimax_model") or "MiniMax-M2.7-highspeed").strip()
GSHEETS_URL       = str(MARKET_CFG.get("subcategories_csv_url") or "").strip()
OLLAMA_BASE_URL   = str(LLM_CFG.get("ollama_base_url") or "http://localhost:11434").rstrip("/")
OLLAMA_MODEL      = str(LLM_CFG.get("ollama_model") or "qwen2.5:3b").strip()

SECTOR_FILE_HINTS = {
    "Health":  ("health",),
    "Tech":    ("tech",),
    "Power":   ("power",),
    "Robotic": ("robotic", "robot"),
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _sector_from_filename(name: str) -> str:
    lower = name.lower()
    for sector, hints in SECTOR_FILE_HINTS.items():
        if any(h in lower for h in hints):
            return sector
    return ""


def load_existing_subcategories() -> list[dict]:
    if not SUBCATEGORIES_PATH.exists():
        return []
    with SUBCATEGORIES_PATH.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return [row for row in reader if row.get("Sector") and row.get("Tag")]


def read_company_names(csv_path: Path) -> list[str]:
    for enc in ("utf-8", "latin1", "cp1252"):
        try:
            df = pd.read_csv(csv_path, encoding=enc, on_bad_lines="skip",
                             sep=None, engine="python")
            break
        except Exception:
            continue
    else:
        print(f"  [warn] could not read {csv_path.name}")
        return []

    if "Name" not in df.columns:
        return []

    names = (
        df["Name"]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
        .tolist()
    )
    return [n for n in names if n and n.lower() not in ("nan", "name")]


# ── from-xlsx mode ────────────────────────────────────────────────────────────

def _parse_tag_from_comment(comment: str) -> str:
    """Extract tag from 'XX | Tag Name' format. Returns empty string if unparseable."""
    if not comment or str(comment).strip().lower() in ("nan", ""):
        return ""
    s = str(comment).strip()
    if "|" in s:
        tag = s.split("|", 1)[1].strip()
    else:
        tag = s
    return tag


def rebuild_from_xlsx(xlsx_dir: Path, only_sector: str | None = None,
                      dry_run: bool = False) -> None:
    """
    Read COMMENT column from completed xlsx files, extract subcategory tags,
    and rewrite subcategories.csv preserving the existing Sector/Tag/Description format.
    """
    xlsx_files = sorted(xlsx_dir.glob("*.xlsx"))
    if not xlsx_files:
        print(f"[error] No xlsx files found in {xlsx_dir}")
        sys.exit(1)

    # collect sector -> tag -> [company names]
    sector_tag_companies: dict[str, dict[str, list[str]]] = {}

    for fpath in xlsx_files:
        sector = _sector_from_filename(fpath.stem)
        if not sector:
            print(f"  [skip] {fpath.name} — unknown sector")
            continue
        if only_sector and sector != only_sector:
            continue

        try:
            df = pd.read_excel(fpath)
        except Exception as exc:
            print(f"  [warn] could not read {fpath.name}: {exc}")
            continue

        if "COMMENT" not in df.columns or "Name" not in df.columns:
            print(f"  [skip] {fpath.name} — missing Name or COMMENT column")
            continue

        tag_companies: dict[str, list[str]] = sector_tag_companies.setdefault(sector, {})
        added = 0
        for _, row in df.iterrows():
            name = str(row.get("Name") or "").strip()
            tag = _parse_tag_from_comment(row.get("COMMENT"))
            if not name or not tag or name.lower() == "nan":
                continue
            if name not in tag_companies.setdefault(tag, []):
                tag_companies[tag].append(name)
                added += 1

        print(f"  [read] {fpath.name} -> {sector}: "
              f"{len(tag_companies)} tags, {added} company entries")

    if not sector_tag_companies:
        print("[error] No data extracted from xlsx files.")
        sys.exit(1)

    # load existing subcategories to preserve descriptions and other sectors
    existing = load_existing_subcategories()
    existing_desc: dict[tuple[str, str], str] = {
        (r["Sector"], r["Tag"]): r.get("Description", "")
        for r in existing
    }
    existing_keys: set[tuple[str, str]] = set(existing_desc.keys())

    # build new rows
    new_rows: list[dict] = []
    touched_sectors = set(sector_tag_companies.keys())

    # sectors not touched — keep unchanged
    for row in existing:
        if row["Sector"] not in touched_sectors:
            new_rows.append(row)

    # rebuild touched sectors
    for sector, tag_companies in sector_tag_companies.items():
        # collect all tags: existing ones first (preserve order), then new ones
        existing_tags_for_sector = [r["Tag"] for r in existing if r["Sector"] == sector]
        all_tags = list(existing_tags_for_sector)
        for tag in tag_companies:
            if tag not in all_tags:
                all_tags.append(tag)
                print(f"  [new tag] {sector} / {tag}")

        for tag in all_tags:
            companies = tag_companies.get(tag, [])
            base_desc = existing_desc.get((sector, tag), "").rstrip(".")
            if companies:
                sample = ", ".join(companies[:10])
                desc = f"{base_desc}. Key holdings: {sample}." if base_desc else f"Key holdings: {sample}."
            else:
                desc = base_desc + "." if base_desc and not base_desc.endswith(".") else base_desc
            new_rows.append({"Sector": sector, "Tag": tag, "Description": desc})

    if dry_run:
        print("\n[dry-run] Would write these rows:")
        for r in new_rows:
            print(f"  {r['Sector']:10s} | {r['Tag']:30s} | {r['Description'][:60]}")
        return

    with SUBCATEGORIES_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Sector", "Tag", "Description"])
        writer.writeheader()
        writer.writerows(new_rows)
    print(f"\n[OK] Wrote {len(new_rows)} rows to {SUBCATEGORIES_PATH}")


# ── MiniMax API ───────────────────────────────────────────────────────────────

def _minimax_call(prompt: str, max_tokens: int = 8000) -> str:
    url = f"{MINIMAX_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MINIMAX_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise financial taxonomy assistant. "
                    "Always respond with valid JSON only. "
                    "No explanations, no markdown fences, no extra text outside the JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    finish = data["choices"][0].get("finish_reason", "")
    if finish == "length":
        print(f"    [warn] response truncated (finish_reason=length)")
    return content


def _parse_json_from_response(text: str) -> dict | list:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`")
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        try:
            return json.loads(text[s:e+1])
        except json.JSONDecodeError:
            pass
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1:
        return json.loads(text[s:e+1])
    start = text.find("{")
    if start != -1:
        pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', text[start:])
        if pairs:
            return {k: v for k, v in pairs}
    raise ValueError(f"No JSON found in response:\n{text[:400]}")


def _classify_with_ollama(sector: str, companies: list[str],
                           tag_names: list[str]) -> dict[str, str]:
    tag_list = "\n".join(f"- {t}" for t in tag_names)
    company_lines = "\n".join(f"- {c}" for c in companies)
    prompt = (
        f"Sector: {sector}\n"
        f"Available tags:\n{tag_list}\n\n"
        f"Classify each company into the single best matching tag.\n"
        f"Return ONLY a JSON object: {{\"<company>\": \"<tag>\", ...}}\n"
        f"Use exact tag names. No markdown.\n\n"
        f"Companies:\n{company_lines}"
    )
    url = f"{OLLAMA_BASE_URL}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "You are a financial taxonomy assistant. Respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(url, json=payload, timeout=120)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    result = _parse_json_from_response(content)
    return result if isinstance(result, dict) else {}


def _classify_batch(sector: str, companies: list[str],
                    tag_names: list[str]) -> dict[str, str]:
    tag_list = "\n".join(f"- {t}" for t in tag_names)
    company_lines = "\n".join(f"- {c}" for c in companies)
    prompt = (
        f"Sector: {sector}\n"
        f"Available tags:\n{tag_list}\n\n"
        f"Classify each company into the single best matching tag.\n"
        f"Return ONLY a JSON object: {{\"<company>\": \"<tag>\", ...}}\n"
        f"Use exact tag names. No markdown.\n\n"
        f"Companies:\n{company_lines}"
    )
    raw = _minimax_call(prompt, max_tokens=4000)
    result = _parse_json_from_response(raw)
    return result if isinstance(result, dict) else {}


def _update_descriptions_locally(sector: str, tag_companies: dict[str, list[str]],
                                  existing_tags: list[dict]) -> list[dict]:
    existing_desc = {r["Tag"]: r.get("Description", "") for r in existing_tags}
    results = []
    for tag, companies in tag_companies.items():
        if not companies:
            continue
        base = existing_desc.get(tag, "").rstrip(".")
        sample = ", ".join(companies[:10])
        desc = f"{base}. Key holdings: {sample}." if base else f"Key holdings: {sample}."
        results.append({"tag": tag, "description": desc})
    return results


def classify_sector(sector: str, companies: list[str],
                    existing_tags: list[dict]) -> dict:
    tag_names = [r["Tag"] for r in existing_tags]
    classifications: dict[str, str] = {}
    batch_size = 3

    for i in range(0, len(companies), batch_size):
        batch = companies[i:i + batch_size]
        total = -(-len(companies) // batch_size)
        print(f"    batch {i//batch_size + 1}/{total}: {len(batch)} companies ...")
        try:
            partial = _classify_batch(sector, batch, tag_names)
            classifications.update(partial)
        except Exception as exc:
            print(f"    [fallback->ollama] {exc}")
            try:
                partial = _classify_with_ollama(sector, batch, tag_names)
                classifications.update(partial)
                print(f"    [ollama ok] classified {len(partial)} companies")
            except Exception as exc2:
                print(f"    [skip] ollama also failed: {exc2}")

    tag_companies: dict[str, list[str]] = {t: [] for t in tag_names}
    unclassified: list[str] = []
    for company, tag in classifications.items():
        if tag in tag_companies:
            tag_companies[tag].append(company)
        else:
            unclassified.append(company)

    if unclassified:
        print(f"    {len(unclassified)} companies not matched to known tags")

    non_empty = {t: cs for t, cs in tag_companies.items() if cs}
    print(f"    building descriptions locally for {len(non_empty)} tags ...")
    updated_tags = _update_descriptions_locally(sector, non_empty, existing_tags)

    return {
        "classifications": classifications,
        "updated_tags": updated_tags,
        "new_tags": [],
    }


# ── MiniMax rebuild mode ──────────────────────────────────────────────────────

def rebuild(csv_dir: Path, dry_run: bool = False,
            only_sector: str | None = None) -> list[dict]:
    existing = load_existing_subcategories()
    by_sector: dict[str, list[dict]] = {}
    for row in existing:
        by_sector.setdefault(row["Sector"], []).append(row)

    merged_files = sorted(csv_dir.glob("*_merged.csv"))
    if not merged_files:
        print(f"[error] No *_merged.csv files found in {csv_dir}")
        sys.exit(1)

    sector_companies: dict[str, list[str]] = {}
    for fpath in merged_files:
        sector = _sector_from_filename(fpath.stem)
        if not sector:
            print(f"  [skip] {fpath.name} — unknown sector")
            continue
        if only_sector and sector != only_sector:
            continue
        names = read_company_names(fpath)
        sector_companies.setdefault(sector, [])
        for n in names:
            if n not in sector_companies[sector]:
                sector_companies[sector].append(n)
        print(f"  [read] {fpath.name} -> {sector}: {len(names)} companies")

    if not sector_companies:
        print("[error] No companies extracted.")
        sys.exit(1)

    new_rows: list[dict] = []
    for sector, companies in sector_companies.items():
        tags_for_sector = by_sector.get(sector, [])
        if not tags_for_sector:
            print(f"  [warn] No existing tags for sector '{sector}', skipping")
            continue

        print(f"\n  [MiniMax] {sector}: {len(companies)} companies, {len(tags_for_sector)} tags")
        try:
            result = classify_sector(sector, companies, tags_for_sector)
        except Exception as exc:
            print(f"  [error] MiniMax failed for {sector}: {exc}")
            new_rows.extend(tags_for_sector)
            continue

        updated = {
            item["tag"]: item["description"]
            for item in result.get("updated_tags", [])
            if item.get("tag") and item.get("description")
        }

        for row in tags_for_sector:
            tag = row["Tag"]
            new_rows.append({
                "Sector": sector,
                "Tag": tag,
                "Description": updated.get(tag, row.get("Description", "")),
            })

        for new_tag in result.get("new_tags", []):
            t = str(new_tag.get("tag") or "").strip()
            d = str(new_tag.get("description") or "").strip()
            if t and d:
                print(f"  [new tag] {sector} / {t}")
                new_rows.append({"Sector": sector, "Tag": t, "Description": d})

    touched = set(sector_companies.keys())
    for sector, rows in by_sector.items():
        if sector not in touched:
            new_rows.extend(rows)

    if dry_run:
        print("\n[dry-run] Would write these rows to subcategories.csv:")
        for r in new_rows:
            print(f"  {r['Sector']:10s} | {r['Tag']:25s} | {r['Description'][:70]}")
        return new_rows

    with SUBCATEGORIES_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Sector", "Tag", "Description"])
        writer.writeheader()
        writer.writerows(new_rows)
    print(f"\n[OK] Wrote {len(new_rows)} rows to {SUBCATEGORIES_PATH}")

    return new_rows


# ── Google Sheets upload ──────────────────────────────────────────────────────

def _extract_sheet_id(url: str) -> tuple[str, str]:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError(f"Cannot extract sheet ID from URL: {url}")
    sheet_id = m.group(1)
    gid_m = re.search(r"[?#&]gid=(\d+)", url)
    gid = gid_m.group(1) if gid_m else "0"
    return sheet_id, gid


def upload_to_gsheets(rows: list[dict]) -> None:
    try:
        import gspread
    except ImportError:
        print("[gsheets] gspread not installed — skipping upload (pip install gspread)")
        return

    if not GSHEETS_URL:
        print("[gsheets] No subcategories_csv_url in config — skipping upload")
        return

    spreadsheet_id, gid = _extract_sheet_id(GSHEETS_URL)

    print(f"[gsheets] Authenticating ...")
    try:
        gc = gspread.oauth()
    except Exception as exc:
        print(f"[gsheets] OAuth failed: {exc}")
        return

    try:
        sh = gc.open_by_key(spreadsheet_id)
        target_ws = None
        for ws in sh.worksheets():
            if str(ws.id) == gid:
                target_ws = ws
                break
        if target_ws is None:
            target_ws = sh.get_worksheet(0)

        header = ["Sector", "Tag", "Description"]
        values = [header] + [[r["Sector"], r["Tag"], r["Description"]] for r in rows]
        target_ws.clear()
        target_ws.update(values, "A1")
        print(f"[gsheets] Uploaded {len(rows)} rows to '{target_ws.title}'")
    except Exception as exc:
        print(f"[gsheets] Upload failed: {exc}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Rebuild subcategories.csv from xlsx files or via MiniMax API"
    )
    parser.add_argument("--from-xlsx", action="store_true",
                        help="Rebuild from completed xlsx files (reads COMMENT column)")
    parser.add_argument("--xlsx-dir", default=None,
                        help="Folder containing completed xlsx files (required with --from-xlsx)")
    parser.add_argument("--csv-dir", default="csv_update",
                        help="Folder with *_merged.csv files (MiniMax mode, default: csv_update)")
    parser.add_argument("--sector", default=None,
                        help="Process only this sector: Health / Tech / Power / Robotic")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing any files")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip Google Sheets upload")
    args = parser.parse_args()

    if args.from_xlsx:
        if not args.xlsx_dir:
            print("[error] --xlsx-dir is required when using --from-xlsx")
            sys.exit(1)
        xlsx_dir = Path(args.xlsx_dir)
        if not xlsx_dir.is_dir():
            print(f"[error] xlsx directory not found: {xlsx_dir}")
            sys.exit(1)
        label = f"sector={args.sector}" if args.sector else "all sectors"
        print(f"=== Rebuilding subcategories from xlsx ({label}) ===\n")
        rebuild_from_xlsx(xlsx_dir, only_sector=args.sector, dry_run=args.dry_run)
        print("\nDone.")
        return

    # MiniMax mode
    if not MINIMAX_API_KEY:
        print("[error] minimax_api_key not set in config.yaml llm section")
        sys.exit(1)

    csv_dir = SCRIPT_DIR / args.csv_dir
    if not csv_dir.is_dir():
        print(f"[error] CSV directory not found: {csv_dir}")
        sys.exit(1)

    label = f"sector={args.sector}" if args.sector else "all sectors"
    print(f"=== Rebuilding subcategories from {csv_dir} ({label}) ===\n")
    rows = rebuild(csv_dir, dry_run=args.dry_run, only_sector=args.sector)

    if not args.dry_run and not args.no_upload:
        print()
        upload_to_gsheets(rows)

    print("\nDone.")


if __name__ == "__main__":
    main()
