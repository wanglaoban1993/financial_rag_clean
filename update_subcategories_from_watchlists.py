# from __future__ import annotations

# import argparse
# import csv
# import json
# import os
# import re
# import shutil
# import urllib.error
# import urllib.request
# from collections import defaultdict
# from dataclasses import dataclass, field
# from datetime import datetime
# from pathlib import Path


# SCRIPT_DIR = Path(__file__).resolve().parent
# DEFAULT_SUBCATEGORIES = SCRIPT_DIR / "subcategories.csv"
# DEFAULT_SOURCES = (
#     SCRIPT_DIR / "csv" / "ALLE POWER.csv",
#     SCRIPT_DIR / "csv" / "ALLE ROBOTIC.csv",
# )
# DEFAULT_COMMENT_COLUMNS = ("COMMENT",)
# DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# TAG_SPLIT_RE = re.compile(r"\|\||>>>|--+|/{2,}|\s+->\s+|\s+=\s+")
# SPACE_RE = re.compile(r"\s+")
# NOISE_CHARS_RE = re.compile(r"^[\s!?:,;*+\-_=\[\]{}<>|]+|[\s!?:,;*+\-_=\[\]{}<>|]+$")
# PRICE_OR_WEIGHT_RE = re.compile(
#     r"^(?:ab\s+)?\d+(?:[,.]\d+)?\s*(?:%|\$|usd|eur|€|gbp|chf)?$",
#     re.IGNORECASE,
# )
# TARGET_PRICE_RE = re.compile(r"^(?:zi|ziel|target|steps?|buy\s*in|rb|up|k\d+|t-)\b", re.IGNORECASE)
# BORING_NOTE_RE = re.compile(
#     r"^(?:p\s*&\s*r|pr|us|usd|cad|etf|etr|otc|usa|stu|trg|buy\s*in|steps?|delete|loeschen|loschen|löschen)$",
#     re.IGNORECASE,
# )
# TAG_ALIASES = {
#     "asolar": "Solar",
#     "a solar": "Solar",
#     "us solar": "Solar",
#     "wind": "Wind",
#     "utilities": "Utilities",
#     "utlities": "Utilities",
#     "medtec": "MedTec",
#     "lidar sensoren": "LiDAR Sensoren",
# }


# @dataclass
# class TagInfo:
#     sector: str
#     tag: str
#     source_files: set[str] = field(default_factory=set)
#     examples: list[str] = field(default_factory=list)


# def default_ollama_model() -> str:
#     env_model = os.getenv("OLLAMA_MODEL", "").strip()
#     if env_model:
#         return env_model

#     config_path = SCRIPT_DIR / "config.yaml"
#     if config_path.exists():
#         match = re.search(r"^\s*ollama_model:\s*[\"']?([^\"'\n#]+)", config_path.read_text(encoding="utf-8"), re.MULTILINE)
#         if match:
#             return match.group(1).strip()

#     return "qwen3.5:latest"


# def read_csv_rows(path: Path) -> list[dict[str, str]]:
#     encodings = ("utf-8-sig", "cp1252", "latin1")
#     last_error: Exception | None = None

#     for encoding in encodings:
#         try:
#             with path.open("r", encoding=encoding, newline="") as handle:
#                 sample = handle.read(4096)
#                 handle.seek(0)
#                 dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
#                 reader = csv.DictReader(handle, dialect=dialect)
#                 return [{str(k or "").strip(): str(v or "").strip() for k, v in row.items()} for row in reader]
#         except (UnicodeDecodeError, csv.Error) as error:
#             last_error = error

#     raise ValueError(f"Could not parse {path}") from last_error


# def read_subcategories(path: Path) -> list[dict[str, str]]:
#     if not path.exists():
#         return []
#     with path.open("r", encoding="utf-8-sig", newline="") as handle:
#         reader = csv.reader(handle)
#         header = next(reader, None)
#         if not header:
#             return []

#         rows: list[dict[str, str]] = []
#         for raw_row in reader:
#             if len(raw_row) < 2:
#                 continue
#             sector = str(raw_row[0] or "").strip()
#             tag = str(raw_row[1] or "").strip()
#             description = ", ".join(str(value or "").strip() for value in raw_row[2:]).strip()
#             if sector and tag:
#                 rows.append({"Sector": sector, "Tag": tag, "Description": description})
#         return rows


# def sector_from_source(path: Path) -> str:
#     name = path.stem.lower()
#     if "power" in name:
#         return "Power"
#     if "robot" in name:
#         return "Robotic"

#     cleaned = re.sub(r"^alle\s+", "", path.stem, flags=re.IGNORECASE)
#     return SPACE_RE.sub(" ", cleaned.replace("_", " ").replace("-", " ")).strip().title()


# def clean_tag(raw_value: str) -> str:
#     value = str(raw_value or "").replace("\ufeff", " ")
#     value = value.replace("�", " ")
#     value = value.replace("&", " & ")
#     value = SPACE_RE.sub(" ", value).strip()
#     value = NOISE_CHARS_RE.sub("", value).strip()
#     if value.startswith("(") and value.endswith(")"):
#         value = value[1:-1].strip()
#     value = re.sub(r"^(?:a\s+)+", "", value, flags=re.IGNORECASE).strip()
#     value = SPACE_RE.sub(" ", value)
#     alias = TAG_ALIASES.get(value.lower())
#     if alias:
#         return alias

#     if not value:
#         return ""
#     if len(value) < 3 or len(value) > 48:
#         return ""
#     if PRICE_OR_WEIGHT_RE.match(value) or TARGET_PRICE_RE.match(value) or BORING_NOTE_RE.match(value):
#         return ""
#     if not re.search(r"[A-Za-zÄÖÜäöüß]", value):
#         return ""

#     words = []
#     for word in value.split(" "):
#         if word.isupper() and len(word) <= 5:
#             words.append(word)
#         elif any(char.isupper() for char in word[1:]):
#             words.append(word)
#         else:
#             words.append(word[:1].upper() + word[1:].lower())
#     return " ".join(words)


# def iter_tag_candidates(row: dict[str, str], comment_columns: tuple[str, ...]) -> list[str]:
#     candidates: list[str] = []
#     for column in comment_columns:
#         raw_value = row.get(column, "")
#         for part in TAG_SPLIT_RE.split(raw_value):
#             tag = clean_tag(part)
#             if tag:
#                 candidates.append(tag)
#     return candidates


# def build_description(info: TagInfo) -> str:
#     sources = ", ".join(sorted(info.source_files))
#     if info.examples:
#         examples = ", ".join(info.examples[:3])
#         return f"Imported from {sources}; examples: {examples}"
#     return f"Imported from {sources}"


# def merge_tag_info(target: TagInfo, source: TagInfo) -> None:
#     target.source_files.update(source.source_files)
#     for example in source.examples:
#         if example and example not in target.examples and len(target.examples) < 3:
#             target.examples.append(example)


# def collect_tags(source_paths: list[Path], comment_columns: tuple[str, ...]) -> dict[tuple[str, str], TagInfo]:
#     collected: dict[tuple[str, str], TagInfo] = {}

#     for source_path in source_paths:
#         sector = sector_from_source(source_path)
#         for row in read_csv_rows(source_path):
#             name = SPACE_RE.sub(" ", row.get("Name", "")).strip()
#             for tag in iter_tag_candidates(row, comment_columns):
#                 key = (sector.lower(), tag.lower())
#                 if key not in collected:
#                     collected[key] = TagInfo(sector=sector, tag=tag)
#                 info = collected[key]
#                 info.source_files.add(source_path.name)
#                 if name and name not in info.examples and len(info.examples) < 3:
#                     info.examples.append(name)

#     return collected


# def _extract_json_payload(raw_text: str) -> object:
#     text = str(raw_text or "").strip()
#     # Strip thinking-model reasoning blocks (Qwen3, DeepSeek-R1, etc.)
#     text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
#     # Strip markdown code fences
#     text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
#     try:
#         return json.loads(text)
#     except json.JSONDecodeError:
#         pass

#     start_candidates = [idx for idx in (text.find("["), text.find("{")) if idx >= 0]
#     if not start_candidates:
#         preview = text[:300] if text else "<empty>"
#         raise ValueError(f"AI response did not contain JSON. Raw response preview:\n{preview}")
#     start = min(start_candidates)
#     end = max(text.rfind("]"), text.rfind("}"))
#     if end < start:
#         raise ValueError("AI response JSON was incomplete.")
#     return json.loads(text[start : end + 1])


# def call_ollama_json(prompt: str, model: str, base_url: str, timeout_seconds: int) -> object:
#     url = base_url.rstrip("/") + "/api/generate"
#     payload = {
#         "model": model,
#         "stream": False,
#         # No "format": "json" — thinking models (Qwen3, DeepSeek-R1) leave
#         # response empty when that constraint is set; regex extraction handles JSON.
#         "prompt": (
#             "You normalize investment watchlist taxonomy tags. "
#             "Return strict JSON only. Do not include markdown.\n\n"
#             f"{prompt}"
#         ),
#         "options": {"temperature": 0.1},
#     }
#     request = urllib.request.Request(
#         url,
#         data=json.dumps(payload).encode("utf-8"),
#         headers={"Content-Type": "application/json"},
#         method="POST",
#     )
#     try:
#         with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
#             response_payload = json.loads(response.read().decode("utf-8"))
#     except urllib.error.URLError as error:
#         raise RuntimeError(f"Could not reach Ollama at {url}: {error}") from error

#     # Thinking models may return content in "thinking" when "response" is empty.
#     content = str(response_payload.get("response") or response_payload.get("thinking") or "")
#     return _extract_json_payload(content)


# def build_ai_prompt(sector: str, infos: list[TagInfo]) -> str:
#     candidates = [
#         {
#             "tag": info.tag,
#             "examples": info.examples[:3],
#             "sources": sorted(info.source_files),
#         }
#         for info in sorted(infos, key=lambda item: item.tag.lower())
#     ]
#     return json.dumps(
#         {
#             "task": (
#                 "Normalize these watchlist category tags for one Sector. Merge obvious duplicates and spelling variants, "
#                 "drop values that are only trading notes, prices, exchange names, countries, or generic flags, and create concise English descriptions."
#             ),
#             "sector": sector,
#             "rules": [
#                 "Keep tags short: ideally 2-4 words, maximum 32 characters.",
#                 "Use domain taxonomy labels, not full sentences.",
#                 "Do not invent categories unrelated to the input examples.",
#                 "Return one JSON object with key 'rows'.",
#                 "Each row must have: tag, description, aliases.",
#                 "aliases must list the input tags that were merged into this output tag.",
#             ],
#             "input_tags": candidates,
#             "output_schema": {"rows": [{"tag": "string", "description": "string", "aliases": ["string"]}]},
#         },
#         ensure_ascii=False,
#     )


# def normalize_with_ollama(
#     collected: dict[tuple[str, str], TagInfo],
#     model: str,
#     base_url: str,
#     timeout_seconds: int,
# ) -> dict[tuple[str, str], TagInfo]:
#     by_sector: dict[str, list[TagInfo]] = defaultdict(list)
#     for info in collected.values():
#         by_sector[info.sector].append(info)

#     normalized: dict[tuple[str, str], TagInfo] = {}
#     for sector, infos in sorted(by_sector.items()):
#         print(f"[ai] asking Ollama to normalize {len(infos)} tags for {sector} ...")
#         ai_payload = call_ollama_json(build_ai_prompt(sector, infos), model, base_url, timeout_seconds)
#         rows = ai_payload.get("rows", []) if isinstance(ai_payload, dict) else ai_payload
#         if not isinstance(rows, list):
#             raise ValueError(f"AI response for {sector} did not contain a rows list.")

#         source_by_lower = {info.tag.lower(): info for info in infos}
#         for row in rows:
#             if not isinstance(row, dict):
#                 continue
#             tag = clean_tag(str(row.get("tag") or ""))
#             description = SPACE_RE.sub(" ", str(row.get("description") or "")).strip()
#             aliases = [str(value or "").strip() for value in row.get("aliases", []) if str(value or "").strip()]
#             if not tag:
#                 continue

#             key = (sector.lower(), tag.lower())
#             target = normalized.setdefault(key, TagInfo(sector=sector, tag=tag))
#             if description:
#                 target.examples = target.examples[:3]
#                 target.source_files.add(f"AI: {description}")
#             for alias in aliases or [tag]:
#                 source = source_by_lower.get(alias.lower())
#                 if source:
#                     merge_tag_info(target, source)

#         known_aliases = {
#             alias.lower()
#             for row in rows
#             if isinstance(row, dict)
#             for alias in row.get("aliases", [])
#             if str(alias or "").strip()
#         }
#         for info in infos:
#             if info.tag.lower() in known_aliases:
#                 continue
#             key = (info.sector.lower(), info.tag.lower())
#             if key not in normalized:
#                 normalized[key] = info

#     return normalized


# def merge_subcategories(
#     subcategory_path: Path,
#     source_paths: list[Path],
#     comment_columns: tuple[str, ...],
#     use_ai: bool,
#     ollama_model: str,
#     ollama_url: str,
#     ollama_timeout: int,
# ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
#     existing_rows = read_subcategories(subcategory_path)
#     existing_keys = {(row["Sector"].lower(), row["Tag"].lower()) for row in existing_rows}
#     collected = collect_tags(source_paths, comment_columns)
#     if use_ai and collected:
#         collected = normalize_with_ollama(collected, ollama_model, ollama_url, ollama_timeout)

#     new_rows: list[dict[str, str]] = []
#     for key in sorted(collected, key=lambda item: (collected[item].sector.lower(), collected[item].tag.lower())):
#         if key in existing_keys:
#             continue
#         info = collected[key]
#         ai_descriptions = [source.removeprefix("AI: ").strip() for source in info.source_files if source.startswith("AI: ")]
#         source_files = {source for source in info.source_files if not source.startswith("AI: ")}
#         row_info = TagInfo(sector=info.sector, tag=info.tag, source_files=source_files, examples=info.examples)
#         description = ai_descriptions[0] if ai_descriptions else build_description(row_info)
#         new_rows.append({"Sector": info.sector, "Tag": info.tag, "Description": description})

#     return existing_rows + new_rows, new_rows


# def write_subcategories(path: Path, rows: list[dict[str, str]]) -> None:
#     path.parent.mkdir(parents=True, exist_ok=True)
#     with path.open("w", encoding="utf-8", newline="") as handle:
#         writer = csv.DictWriter(handle, fieldnames=["Sector", "Tag", "Description"])
#         writer.writeheader()
#         writer.writerows(rows)


# def parse_args() -> argparse.Namespace:
#     parser = argparse.ArgumentParser(
#         description="Update subcategories.csv with tags found in ALLE POWER.csv and ALLE ROBOTIC.csv."
#     )
#     parser.add_argument(
#         "--subcategories",
#         type=Path,
#         default=DEFAULT_SUBCATEGORIES,
#         help="Path to subcategories.csv.",
#     )
#     parser.add_argument(
#         "--source",
#         type=Path,
#         action="append",
#         dest="sources",
#         help="Source watchlist CSV. Can be passed more than once.",
#     )
#     parser.add_argument(
#         "--comment-column",
#         action="append",
#         dest="comment_columns",
#         help="Column to mine for tags. Defaults to COMMENT. Can be passed more than once.",
#     )
#     parser.add_argument("--dry-run", action="store_true", help="Print planned additions without writing files.")
#     parser.add_argument("--no-backup", action="store_true", help="Do not create a .bak file before writing.")
#     parser.add_argument("--ai", action="store_true", help="Use local Ollama to merge tags and write better descriptions.")
#     parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama base URL.")
#     parser.add_argument("--ollama-model", default=default_ollama_model(), help="Local Ollama model name.")
#     parser.add_argument("--ollama-timeout", type=int, default=120, help="Seconds to wait for each Ollama request.")
#     return parser.parse_args()


# def main() -> None:
#     args = parse_args()
#     source_paths = [path.resolve() for path in (args.sources or DEFAULT_SOURCES)]
#     comment_columns = tuple(args.comment_columns or DEFAULT_COMMENT_COLUMNS)
#     missing = [str(path) for path in source_paths if not path.exists()]
#     if missing:
#         raise FileNotFoundError(f"Missing source CSV: {', '.join(missing)}")

#     subcategory_path = args.subcategories.resolve()
#     if args.ai:
#         print(f"[ai] using local Ollama model: {args.ollama_model} ({args.ollama_url})")
#     merged_rows, new_rows = merge_subcategories(
#         subcategory_path,
#         source_paths,
#         comment_columns,
#         args.ai,
#         args.ollama_model,
#         args.ollama_url,
#         args.ollama_timeout,
#     )

#     print(f"Found {len(new_rows)} new subcategory rows.")
#     grouped: dict[str, list[str]] = defaultdict(list)
#     for row in new_rows:
#         grouped[row["Sector"]].append(row["Tag"])
#     for sector in sorted(grouped):
#         print(f"\n[{sector}]")
#         for tag in grouped[sector]:
#             print(f"  - {tag}")

#     if args.dry_run:
#         return

#     if subcategory_path.exists() and not args.no_backup:
#         timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
#         backup_path = subcategory_path.with_suffix(f".csv.{timestamp}.bak")
#         shutil.copy2(subcategory_path, backup_path)
#         print(f"\nBackup written: {backup_path}")

#     write_subcategories(subcategory_path, merged_rows)
#     print(f"Updated: {subcategory_path}")


# if __name__ == "__main__":
#     main()
"""
classify_with_ollama.py
=======================
用本地 Ollama 模型读取 CSV 中的金融产品，自动生成 Sector / Tag / Description，
并将新分类追加到 subcategories.xlsx（保留原有格式和内容，只在末尾新增行）。

依赖安装：
    pip install pandas openpyxl requests

使用方式：
    python classify_with_ollama.py \
        --csv   ALLE_POWER.csv \
        --xlsx  subcategories.xlsx \
        --out   subcategories_updated.xlsx \
        --model llama3          # 换成你本地 ollama 里已有的模型名
        --comment-col COMMENT   # CSV 中代表产品类别的列名
        --name-col    Name      # CSV 中代表产品名称的列名

可选参数：
    --ollama-url  http://localhost:11434   （Ollama 默认地址，一般不用改）
    --batch-size  20                       （每次送给 LLM 多少行，太大会超 context）
    --csv-sep     ;                        （CSV 分隔符，默认分号）
    --csv-enc     latin1                   （CSV 编码）
    --dry-run                              （只打印会写入什么，不实际保存）
"""

import argparse
import json
import re
import sys
import requests
import pandas as pd
from copy import copy
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


# ──────────────────────────────────────────────────────────────────────────────
# 1.  Ollama 调用
# ──────────────────────────────────────────────────────────────────────────────

def ask_ollama(prompt: str, model: str, base_url: str, timeout: int = 300) -> str:
    """向本地 Ollama 发送请求，返回模型的文本回复。"""
    url = base_url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        # 思考模型（Qwen3、DeepSeek-R1）response 为空时回退到 thinking 字段
        content = data.get("response") or data.get("thinking") or ""
        # 去掉 <think>…</think> 推理块，只保留最终输出
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content
    except requests.exceptions.ConnectionError:
        sys.exit(
            f"[ERROR] 无法连接到 Ollama（{url}）。\n"
            "请确认 Ollama 正在运行：  ollama serve"
        )
    except Exception as e:
        sys.exit(f"[ERROR] Ollama 请求失败：{e}")


# ──────────────────────────────────────────────────────────────────────────────
# 2.  构建分类 Prompt
# ──────────────────────────────────────────────────────────────────────────────

def build_prompt(items: list[dict], existing_cats: list[dict]) -> str:
    """
    items:        [{"name": "VESTAS WIND ADR", "comment": "WIND"}, ...]
    existing_cats:[{"sector": "Tech", "tag": "Cloud", "description": "..."}, ...]
    返回一个让模型输出 JSON 列表的 prompt。
    """
    existing_json = json.dumps(existing_cats, ensure_ascii=False, indent=2)
    items_json    = json.dumps(items,         ensure_ascii=False, indent=2)

    return f"""You are a financial product classifier.

## Existing categories in the spreadsheet (DO NOT duplicate these):
{existing_json}

## Products to classify:
{items_json}

Each product has a "name" and a "comment". The "comment" is the analyst's own
category hint written directly in the watchlist CSV — treat it as the PRIMARY
signal for choosing the Tag. Follow these rules:

1. If the comment matches an existing Tag (case-insensitive), reuse that exact
   Sector + Tag — do not create a duplicate.
2. If the comment is a meaningful label but not yet in the spreadsheet, use it
   (cleaned to ≤12 chars) as the new Tag under an appropriate Sector.
3. Only if the comment is empty, a price, a ticker, or otherwise uninformative,
   fall back to inferring the Tag from the product name.
4. Sector must be one of: Health, Tech, Energy, Commodities, Finance, Industry,
   Portfolio — or a new one only if none of these fits.
5. Description: one sentence, max 80 chars, English, summarising what the tag covers.

## Output format  ← IMPORTANT
Reply with ONLY a JSON array, no markdown fences, no extra text:
[
  {{"name": "...", "sector": "...", "tag": "...", "description": "..."}},
  ...
]
"""


# ──────────────────────────────────────────────────────────────────────────────
# 3.  解析模型回复
# ──────────────────────────────────────────────────────────────────────────────

def parse_json_response(text: str) -> list[dict]:
    """从模型回复中提取 JSON 数组，容忍一些常见噪音。"""
    # 去掉 markdown 代码块标记
    text = re.sub(r"```(?:json)?", "", text).strip()
    # 找到第一个 [ ... ] 块
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"模型回复中没有找到 JSON 数组:\n{text[:400]}")
    return json.loads(match.group())


# ──────────────────────────────────────────────────────────────────────────────
# 4.  读取 xlsx：现有分类 + 格式信息
# ──────────────────────────────────────────────────────────────────────────────

def read_existing_categories(ws) -> tuple[list[dict], set]:
    """
    返回:
      existing_cats  – 现有行的列表 [{"sector":..,"tag":..,"description":..}]
      existing_tags  – 已存在的 tag 名称集合（用于去重）
    """
    existing_cats = []
    existing_tags = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        sector, tag, *rest = (row + (None, None, None))[:5]
        if tag:
            desc = " | ".join(str(c) for c in rest if c) if rest else ""
            existing_cats.append({
                "sector":      str(sector or "").strip(),
                "tag":         str(tag).strip(),
                "description": desc.strip(),
            })
            existing_tags.add(str(tag).strip())
    return existing_cats, existing_tags


def collect_sector_style_rows(ws) -> dict[str, int]:
    """返回每个 Sector 最后一次出现的行号，用于复制样式。"""
    sector_row: dict[str, int] = {}
    for row in ws.iter_rows(min_row=2):
        sector_val = str(row[0].value or "").strip()
        if sector_val:
            sector_row[sector_val.lower()] = row[0].row
    return sector_row


def detect_row_style(ws) -> int:
    """返回最后一个数据行的行号，作为样式回退模板。"""
    return ws.max_row if ws.max_row >= 2 else 2


def _copy_cell_style(src_cell, dst_cell) -> None:
    """精确复制单元格的字体、填充、对齐、边框样式。"""
    from copy import copy
    if src_cell.has_style:
        dst_cell.font      = copy(src_cell.font)
        dst_cell.fill      = copy(src_cell.fill)
        dst_cell.alignment = copy(src_cell.alignment)
        dst_cell.border    = copy(src_cell.border)
        dst_cell.number_format = src_cell.number_format


# ──────────────────────────────────────────────────────────────────────────────
# 5.  将新分类写回 xlsx
# ──────────────────────────────────────────────────────────────────────────────

def append_new_rows(ws, new_entries: list[dict], existing_tags: set, row_style: int, dry_run: bool):
    """
    new_entries: [{"sector":..,"tag":..,"description":..}, ...]  ← 去重后的新分类
    精确复制同 Sector 已有行的样式追加到末尾；找不到同 Sector 则用最后一行样式。
    """
    sector_style_rows = collect_sector_style_rows(ws)
    fallback_row = row_style  # 最后一个数据行行号

    added = []
    for entry in new_entries:
        tag = entry.get("tag", "").strip()
        if not tag or tag in existing_tags:
            continue

        sector = entry.get("sector", "").strip()
        desc   = entry.get("description", "").strip()

        if dry_run:
            print(f"  [DRY-RUN] 会追加: Sector={sector!r:14} Tag={tag!r:16} Desc={desc[:50]!r}")
            existing_tags.add(tag)
            added.append(entry)
            continue

        ws.append([sector, tag, desc])
        new_row_idx = ws.max_row
        # 找同 Sector 的样式源行，否则用 fallback
        style_src_row = sector_style_rows.get(sector.lower(), fallback_row)

        for col in range(1, 4):
            src  = ws.cell(row=style_src_row, column=col)
            dst  = ws.cell(row=new_row_idx,   column=col)
            _copy_cell_style(src, dst)

        # 更新 sector_style_rows，让后续同 Sector 的新行也能找到样式
        sector_style_rows[sector.lower()] = new_row_idx
        fallback_row = new_row_idx

        existing_tags.add(tag)
        added.append(entry)

    return added


# ──────────────────────────────────────────────────────────────────────────────
# 6.  主流程
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="用 Ollama 对 CSV 产品分类并更新 subcategories.xlsx")
    parser.add_argument("--csv",          required=True,  help="输入 CSV 文件路径")
    parser.add_argument("--xlsx",         required=True,  help="subcategories.xlsx 路径")
    parser.add_argument("--out",          required=True,  help="输出 xlsx 路径（可与 --xlsx 相同以覆盖原文件）")
    parser.add_argument("--model",        default="llama3", help="Ollama 模型名（默认 llama3）")
    parser.add_argument("--ollama-url",   default="http://localhost:11434", help="Ollama 服务地址")
    parser.add_argument("--comment-col",  default="COMMENT", help="CSV 中代表产品类别/备注的列名")
    parser.add_argument("--name-col",     default="Name",    help="CSV 中代表产品名称的列名")
    parser.add_argument("--batch-size",   type=int, default=20, help="每批送给 LLM 的产品数量")
    parser.add_argument("--csv-sep",      default=";",    help="CSV 分隔符（默认 ;）")
    parser.add_argument("--csv-enc",      default="latin1", help="CSV 编码（默认 latin1）")
    parser.add_argument("--sheet",        default="subcategories", help="xlsx 中分类表的工作表名")
    parser.add_argument("--timeout",      type=int, default=300,   help="Ollama 请求超时秒数（默认 300，思考模型建议 600）")
    parser.add_argument("--dry-run",      action="store_true", help="只打印，不写文件")
    args = parser.parse_args()

    # ── 6a. 读取 CSV ──────────────────────────────────────────────────────────
    print(f"[1/5] 读取 CSV: {args.csv}")
    df = pd.read_csv(args.csv, sep=args.csv_sep, encoding=args.csv_enc)
    if args.comment_col not in df.columns:
        sys.exit(f"[ERROR] CSV 中没有列 '{args.comment_col}'。可用列：{df.columns.tolist()}")
    if args.name_col not in df.columns:
        sys.exit(f"[ERROR] CSV 中没有列 '{args.name_col}'。可用列：{df.columns.tolist()}")

    df[args.comment_col] = df[args.comment_col].fillna("").astype(str).str.strip()
    df[args.name_col]    = df[args.name_col].fillna("").astype(str).str.strip()

    # 收集需要分类的唯一 (name, comment) 组合，过滤掉空行
    unique_items = (
        df[[args.name_col, args.comment_col]]
        .drop_duplicates()
        .rename(columns={args.name_col: "name", args.comment_col: "comment"})
        .to_dict("records")
    )
    unique_items = [i for i in unique_items if i["comment"] or i["name"]]
    print(f"      找到 {len(unique_items)} 个唯一产品条目")

    # ── 6b. 读取 xlsx ──────────────────────────────────────────────────────────
    print(f"[2/5] 读取 xlsx: {args.xlsx}")
    wb = load_workbook(args.xlsx)
    if args.sheet not in wb.sheetnames:
        sys.exit(f"[ERROR] xlsx 中没有工作表 '{args.sheet}'。可用：{wb.sheetnames}")
    ws = wb[args.sheet]
    existing_cats, existing_tags = read_existing_categories(ws)
    row_style = detect_row_style(ws)
    print(f"      现有分类行数：{len(existing_cats)}")

    # ── 6c. 批量调用 Ollama ────────────────────────────────────────────────────
    print(f"[3/5] 调用 Ollama 模型 '{args.model}'（批次大小={args.batch_size}）")
    all_results: list[dict] = []
    batches = [unique_items[i:i+args.batch_size] for i in range(0, len(unique_items), args.batch_size)]

    for idx, batch in enumerate(batches, 1):
        print(f"      批次 {idx}/{len(batches)}（{len(batch)} 条）…", end=" ", flush=True)
        prompt   = build_prompt(batch, existing_cats)
        response = ask_ollama(prompt, args.model, args.ollama_url, args.timeout)
        try:
            parsed = parse_json_response(response)
            all_results.extend(parsed)
            print(f"OK，得到 {len(parsed)} 条分类")
        except Exception as e:
            print(f"解析失败（{e}），跳过该批次")
            print(f"      模型回复片段：{response[:200]!r}")

    # ── 6d. 去重、过滤、整理 ──────────────────────────────────────────────────
    print(f"[4/5] 整理新分类条目")
    seen_tags_this_run: set = set()
    new_entries: list[dict] = []
    for r in all_results:
        tag = str(r.get("tag", "")).strip()
        if not tag:
            continue
        if tag in existing_tags:
            continue                         # 已在 xlsx 里
        if tag in seen_tags_this_run:
            continue                         # 本次批次重复
        seen_tags_this_run.add(tag)
        new_entries.append({
            "sector":      str(r.get("sector", "Other")).strip(),
            "tag":         tag,
            "description": str(r.get("description", "")).strip()[:80],
        })

    # 按 Sector 字母序排列，保持整洁
    new_entries.sort(key=lambda x: (x["sector"], x["tag"]))
    print(f"      共 {len(new_entries)} 个新 Tag 需要写入")

    # ── 6e. 写回 xlsx ──────────────────────────────────────────────────────────
    print(f"[5/5] {'[DRY-RUN] 模拟写入' if args.dry_run else f'写入 {args.out}'}")
    added = append_new_rows(ws, new_entries, existing_tags, row_style, args.dry_run)

    if not args.dry_run:
        wb.save(args.out)
        print(f"\n✅ 完成！新增 {len(added)} 行，已保存到 {args.out}")
    else:
        print(f"\n✅ Dry-run 完成，共会新增 {len(added)} 行。去掉 --dry-run 后正式执行。")

    # ── 打印摘要 ───────────────────────────────────────────────────────────────
    if added:
        print("\n新增分类一览：")
        print(f"{'Sector':<16} {'Tag':<16} {'Description'}")
        print("-" * 70)
        for e in added:
            print(f"{e['sector']:<16} {e['tag']:<16} {e['description'][:50]}")


if __name__ == "__main__":
    main()