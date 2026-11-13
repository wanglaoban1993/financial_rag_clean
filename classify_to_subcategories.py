"""
classify_to_subcategories.py
============================
Extract financial-product labels from CSV files (COMMENT / COMMENT2 / COMMENT3),
classify them into the subcategories.csv taxonomy format with an LLM
(local Ollama or MiniMax API), and append/update sector subcategories.

Dependencies:
    pip install pandas requests

Examples:
    # Use local Ollama (default llama3)
    python classify_to_subcategories.py \
        --csv_in ALLE_POWER.csv \
        --csv_update subcategories.csv \
        --sector Power \
        --backend ollama \
        --model llama3

    # Use MiniMax API
    python classify_to_subcategories.py \
        --csv_in ALLE_ROBOTIC.csv \
        --csv_update subcategories.csv \
        --sector Robotic \
        --backend minimax \
        --minimax-key YOUR_API_KEY

    # Skip LLM and use local rules (fast mode)
    python classify_to_subcategories.py \
        --csv_in ALLE_POWER.csv \
        --csv_update subcategories.csv \
        --sector Power \
        --backend rules
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import pandas as pd
import requests
import yaml
from config_loader import resolve_config_path


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = resolve_config_path()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


CONFIG = load_config()
LLM_CFG = dict(CONFIG.get("llm", {}) or {})
DEFAULT_OLLAMA_MODEL = str(LLM_CFG.get("ollama_model") or "llama3").strip()
DEFAULT_OLLAMA_BASE_URL = str(
    LLM_CFG.get("ollama_base_url") or "http://localhost:11434"
).rstrip("/")
DEFAULT_MINIMAX_API_KEY = str(LLM_CFG.get("minimax_api_key") or "").strip()
DEFAULT_MINIMAX_BASE_URL = str(
    LLM_CFG.get("minimax_base_url") or "https://api.minimaxi.com/v1"
).rstrip("/")
DEFAULT_MINIMAX_MODEL = str(
    LLM_CFG.get("minimax_model") or "MiniMax-M2.7-highspeed"
).strip()
DEFAULT_DEEPSEEK_API_KEY = str(LLM_CFG.get("deepseek_api_key") or "").strip()
DEFAULT_DEEPSEEK_BASE_URL = str(
    LLM_CFG.get("deepseek_base_url") or "https://api.deepseek.com/v1"
).rstrip("/")
DEFAULT_DEEPSEEK_MODEL = str(
    LLM_CFG.get("deepseek_model") or "deepseek-chat"
).strip()


# ──────────────────────────────────────────────
# 1. CSV Reading And Tag Extraction
# ──────────────────────────────────────────────

def load_csv(path: str) -> pd.DataFrame:
    """Read a CSV with several encodings and auto-detect the separator."""
    for enc in ("utf-8", "latin1", "cp1252"):
        try:
            df = pd.read_csv(path, encoding=enc, on_bad_lines="skip",
                             sep=None, engine="python")
            print(f"[CSV] loaded successfully, encoding={enc}, rows={len(df)}")
            return df
        except Exception:
            continue
    raise ValueError(f"Could not read CSV: {path}")


def extract_tags(df: pd.DataFrame) -> list[dict]:
    """
    Extract valid labels from COMMENT / COMMENT2 / COMMENT3.
    Returns: [{"comment": ..., "comment2": ..., "comment3": ..., "name": ...}, ...]
    Ignores pure symbol values (! = ? ---) and NaN.
    """
    noise = re.compile(r"^[!?=\-\s|]+$")

    def clean(v):
        s = str(v).strip() if pd.notna(v) else ""
        return "" if noise.match(s) or s.lower() == "nan" else s

    rows = []
    for _, r in df.iterrows():
        c1 = clean(r.get("COMMENT", ""))
        c2 = clean(r.get("COMMENT2", ""))
        c3 = clean(r.get("COMMENT3", ""))
        name = str(r.get("Name", "")).strip()
        if c1 or c2 or c3:
            rows.append({"comment": c1, "comment2": c2, "comment3": c3, "name": name})

    # Deduplicate while keeping each unique label combination.
    seen = set()
    unique = []
    for row in rows:
        key = (row["comment"], row["comment2"], row["comment3"])
        if key not in seen:
            seen.add(key)
            unique.append(row)

    print(f"[Tags] extracted {len(unique)} unique label combinations")
    return unique


def _clean_tag_value(value: object) -> str:
    noise = re.compile(r"^[!?=\-\s|]+$")
    text = str(value).strip() if pd.notna(value) else ""
    if not text or noise.match(text) or text.lower() == "nan":
        return ""
    return text


def extract_unlabeled_companies(df: pd.DataFrame) -> list[dict]:
    """Return unique companies whose COMMENT/COMMENT2/COMMENT3 are all empty."""
    rows: list[dict] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        c1 = _clean_tag_value(row.get("COMMENT", ""))
        c2 = _clean_tag_value(row.get("COMMENT2", ""))
        c3 = _clean_tag_value(row.get("COMMENT3", ""))
        if c1 or c2 or c3:
            continue

        name = str(row.get("Name", "")).strip()
        if not name or name.lower() in {"nan", "name"}:
            continue

        isin = str(row.get("ISIN", "")).strip()
        key = f"{isin.casefold()}|||{name.casefold()}" if isin else name.casefold()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name,
            "isin": isin,
            "wkn": str(row.get("WKN", "")).strip(),
        })

    print(f"[Empty] found {len(rows)} companies without COMMENT/COMMENT2/COMMENT3")
    return rows


TAXONOMY_STOPWORDS = {
    "and", "the", "for", "with", "including", "companies", "company",
    "providers", "provider", "manufacturers", "manufacturer", "services",
    "service", "systems", "system", "products", "product",
}


def _tokenize_taxonomy_text(text: object) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9&]+", str(text or "").lower()))
    return {token for token in tokens if len(token) >= 3 and token not in TAXONOMY_STOPWORDS}


def _infer_existing_tag_for_company(name: str, comments: list[str], sector_rows: list[dict]) -> str:
    """Return an existing tag if the row already maps cleanly to the taxonomy."""
    if not sector_rows:
        return ""

    known_by_casefold = {
        str(row.get("Tag") or "").strip().casefold(): str(row.get("Tag") or "").strip()
        for row in sector_rows
        if str(row.get("Tag") or "").strip()
    }
    for value in comments:
        for part in re.split(r"[|,;/]+", value):
            tag = known_by_casefold.get(part.strip().casefold())
            if tag:
                return tag

    haystack = " ".join([name, *comments])
    haystack_tokens = _tokenize_taxonomy_text(haystack)
    if not haystack_tokens:
        return ""

    best_tag = ""
    best_score = 0
    best_specific = False
    for row in sector_rows:
        tag = str(row.get("Tag") or "").strip()
        if not tag:
            continue

        tag_tokens = _tokenize_taxonomy_text(tag)
        if tag_tokens and haystack_tokens & tag_tokens:
            return tag

        desc_tokens = _tokenize_taxonomy_text(row.get("Description", ""))
        matching = haystack_tokens & desc_tokens
        score = len(matching)
        specific = any(len(token) >= 5 for token in matching)
        if score > best_score or (score == best_score and specific and not best_specific):
            best_tag = tag
            best_score = score
            best_specific = specific

    if best_score >= 2 or (best_score == 1 and best_specific):
        return best_tag
    return ""


def extract_companies_needing_taxonomy(df: pd.DataFrame, sector_rows: list[dict]) -> list[dict]:
    """Return companies that are empty or cannot be mapped to an existing tag."""
    rows: list[dict] = []
    seen: set[str] = set()
    empty_count = 0
    unmatched_count = 0

    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip()
        if not name or name.lower() in {"nan", "name"}:
            continue

        comments = [
            _clean_tag_value(row.get("COMMENT", "")),
            _clean_tag_value(row.get("COMMENT2", "")),
            _clean_tag_value(row.get("COMMENT3", "")),
        ]
        comments = [value for value in comments if value]
        reason = "empty" if not comments else "unmatched"
        if comments and _infer_existing_tag_for_company(name, comments, sector_rows):
            continue

        isin = str(row.get("ISIN", "")).strip()
        key = f"{isin.casefold()}|||{name.casefold()}" if isin else name.casefold()
        if key in seen:
            continue
        seen.add(key)

        if reason == "empty":
            empty_count += 1
        else:
            unmatched_count += 1

        rows.append({
            "name": name,
            "isin": isin,
            "wkn": str(row.get("WKN", "")).strip(),
            "comment": comments[0] if len(comments) > 0 else "",
            "comment2": comments[1] if len(comments) > 1 else "",
            "comment3": comments[2] if len(comments) > 2 else "",
            "reason": reason,
        })

    print(
        f"[Taxonomy] found {len(rows)} companies needing LLM taxonomy mapping "
        f"({empty_count} empty, {unmatched_count} unmatched)"
    )
    return rows


# ──────────────────────────────────────────────
# 2. LLM Backends
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """You are a financial-product taxonomy expert.
Your task is to organize raw financial-product labels into a concise list of subcategories.

Each input record contains three analyst annotation columns in this format:
  index. COMMENT | COMMENT2 | COMMENT3  (company name)

Column meanings:
- COMMENT: primary classification label, usually the industry/theme, e.g. "Wind" or "Hum Robot".
- COMMENT2: supplementary dimension, such as region or sub-theme, e.g. "CHI", "EU", "Medtech".
- COMMENT3: further refinement or cross-tag that can split a category into more specific groups.

Rules for generating subcategories:
1. All three columns are useful. COMMENT is primary; COMMENT2/COMMENT3 may refine or split tags.
   - Example: COMMENT="Robot" + COMMENT2="CHI" can become "Robot CHI".
   - Example: COMMENT="Solar" + COMMENT2="EU" + COMMENT3="Utility" can become "Solar EU Util".
2. Use moderate granularity. Merge regional/sub-theme variants unless the distinction matters for investment logic.
   - Merge variants such as "Robotik", "Robotic", and "robot" into one subcategory.
   - Merge sparse tags with only 1-2 companies into the nearest parent category.
   - Preserve meaningfully different technology directions, such as Industrial Robot vs Surgical Robot.
   - Preserve regions when they represent a distinct investment thesis, such as Robot CHI vs Robot EU.
3. Ignore purely operational notes such as price targets, buy/sell signals, LÖSCHEN, and separators.
4. Target subcategory count: about 50%-70% of the input record count.
5. Tag format: short English label, at most 3 words, following examples like "Hum Robot" or "Solar CHI".
6. Description: one English sentence describing included companies/technologies.
7. Return a strict JSON array only. No markdown fences and no extra explanation.
8. Each element format: {"tag": "...", "description": "..."}
9. Do not output chain-of-thought, reasoning notes, or <think> blocks.
"""


def call_ollama(tags_text: str, sector: str, model: str, base_url: str) -> list[dict]:
    """Call the local Ollama API."""
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Sector: {sector}\n\nRaw label list:\n{tags_text}"}
        ]
    }
    print(f"[Ollama] requesting {url}, model={model} ...")
    resp = requests.post(url, json=payload, timeout=180)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    return parse_llm_json(content)


def _extract_chat_content(data: dict, label: str) -> str:
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content)
    finish_reason = choice.get("finish_reason") or ""
    if not str(content or "").strip():
        if message.get("reasoning_content"):
            print(f"[{label}] empty final content; response contains reasoning_content only (finish_reason={finish_reason or '-'})")
        else:
            print(f"[{label}] empty final content (finish_reason={finish_reason or '-'})")
    return str(content or "")


def _minimax_chat(
    url: str,
    headers: dict,
    model: str,
    messages: list[dict],
    max_tokens: int = 8000,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    return _extract_chat_content(resp.json(), "MiniMax")


def call_minimax(tags_text: str, sector: str, api_key: str,
                 model: str, base_url: str) -> list[dict]:
    """Call the MiniMax ChatCompletion API."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Sector: {sector}\n\n"
                "Think internally if needed, but the final answer must be only the JSON array. "
                "Do not include markdown or prose outside the array.\n\n"
                f"Raw label list:\n{tags_text}"
            ),
        },
    ]
    print(f"[MiniMax] requesting model={model} ...")
    content = _minimax_chat(url, headers, model, messages)
    try:
        return parse_llm_json(content)
    except ValueError as first_error:
        print("[MiniMax] no JSON array in first response; retrying with stricter JSON-only prompt ...")
        retry_messages = [
            {
                "role": "system",
                "content": (
                    "You return valid JSON only. Your entire response must start with '[' "
                    "and end with ']'. No <think>, no markdown, no prose."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Create the taxonomy for sector {sector}. Return only a JSON array of "
                    "{\"tag\":\"...\",\"description\":\"...\"} objects. Begin with '['.\n\n"
                    f"Raw label list:\n{tags_text}"
                ),
            },
        ]
        retry_content = _minimax_chat(url, headers, model, retry_messages, max_tokens=8000)
        try:
            return parse_llm_json(retry_content)
        except ValueError:
            raise first_error


def call_deepseek(tags_text: str, sector: str, api_key: str,
                  model: str, base_url: str) -> list[dict]:
    """Call the DeepSeek OpenAI-compatible ChatCompletion API."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Sector: {sector}\n\n"
                "Return only the final JSON array. Do not include markdown or prose outside the array.\n\n"
                f"Raw label list:\n{tags_text}"
            ),
        },
    ]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 8000,
    }
    print(f"[DeepSeek] requesting model={model} ...")
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    content = _extract_chat_content(resp.json(), "DeepSeek")
    try:
        return parse_llm_json(content)
    except ValueError as first_error:
        print("[DeepSeek] no JSON array in first response; retrying with stricter JSON-only prompt ...")
        retry_payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You return valid JSON only. Your entire response must start with '[' "
                        "and end with ']'. No <think>, no markdown, no prose."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Create the taxonomy for sector {sector}. Return only a JSON array of "
                        "{\"tag\":\"...\",\"description\":\"...\"} objects. Begin with '['.\n\n"
                        f"Raw label list:\n{tags_text}"
                    ),
                },
            ],
            "temperature": 0.0,
            "max_tokens": 8000,
        }
        retry_resp = requests.post(url, headers=headers, json=retry_payload, timeout=180)
        retry_resp.raise_for_status()
        retry_content = _extract_chat_content(retry_resp.json(), "DeepSeek")
        try:
            return parse_llm_json(retry_content)
        except ValueError:
            raise first_error


def _llm_error_summary(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        body = (exc.response.text or "").strip()
        if len(body) > 300:
            body = body[:300] + "..."
        return f"HTTP {exc.response.status_code}: {body}"
    return str(exc)


def call_minimax_then_deepseek(tags_text: str, sector: str, args: argparse.Namespace) -> list[dict]:
    """Use MiniMax first, then DeepSeek if MiniMax is unavailable or fails."""
    if args.minimax_key:
        try:
            return call_minimax(
                tags_text,
                sector,
                args.minimax_key,
                args.minimax_model,
                args.minimax_base_url,
            )
        except Exception as exc:
            print(f"[MiniMax] failed: {_llm_error_summary(exc)}")
            print("[Fallback] switching to DeepSeek ...")
    else:
        print("[MiniMax] key missing; switching to DeepSeek ...")

    if not args.deepseek_key:
        raise RuntimeError("DeepSeek fallback requires --deepseek-key or config.yaml llm.deepseek_api_key")
    return call_deepseek(
        tags_text,
        sector,
        args.deepseek_key,
        args.deepseek_model,
        args.deepseek_base_url,
    )


def _clean_llm_json_text(content: str) -> str:
    """Remove common wrappers/noise before extracting JSON."""
    content = str(content or "")
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE)
    if re.match(r"^\s*<think\b", content, flags=re.IGNORECASE):
        first_json = min(
            [pos for pos in (content.find("["), content.find("{")) if pos != -1],
            default=-1,
        )
        content = content[first_json:] if first_json >= 0 else ""
    return re.sub(r"```(?:json)?", "", content).strip().rstrip("`")


def parse_llm_json(content: str) -> list[dict]:
    """Extract a JSON array from an LLM response."""
    content = _clean_llm_json_text(content)

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", content):
        try:
            parsed, _ = decoder.raw_decode(content[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            print(f"[LLM] parsed {len(parsed)} subcategories")
            return parsed

    raise ValueError(f"No parseable JSON array found in LLM output:\n{content[:500] or '[empty after cleaning]'}")


# ──────────────────────────────────────────────
# 3. Rule Mode (No LLM)
# ──────────────────────────────────────────────

def parse_llm_json_value(content: str) -> dict | list:
    """Parse a JSON object or array from an LLM response."""
    content = _clean_llm_json_text(content)

    decoder = json.JSONDecoder()
    for marker in ("{", "["):
        for match in re.finditer(re.escape(marker), content):
            try:
                parsed, _ = decoder.raw_decode(content[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, (dict, list)):
                return parsed

    raise ValueError(f"LLM response did not contain JSON:\n{content[:500]}")


def rules_classify(tags: list[dict], sector: str) -> list[dict]:
    """
    Simple keyword-based classification as a fallback to LLM mode.
    Add custom rules here for new sectors if needed.
    """
    print("[Rules] using local rule mode (no LLM call)")

    # Merge all label text for display/reference.
    all_labels = set()
    for t in tags:
        for field in ("comment", "comment2", "comment3"):
            v = t[field].strip()
            if v:
                all_labels.add(v)

    # Generic fallback: each unique COMMENT becomes a candidate tag.
    # Manual sector-specific rules can be added here.
    subcats = []
    seen_tags = set()
    for t in tags:
        label = t["comment"] or t["comment2"] or t["comment3"]
        if label and label not in seen_tags:
            seen_tags.add(label)
            subcats.append({
                "tag": label[:20],
                "description": f"{label} — auto-classified from {sector} CSV tags"
            })

    print(f"[Rules] generated {len(subcats)} candidate subcategories (LLM mode is recommended for better results)")
    return subcats


# ──────────────────────────────────────────────
# 4. Format Label Text For LLM
# ──────────────────────────────────────────────

def format_tags_for_llm(tags: list[dict]) -> str:
    lines = []
    for i, t in enumerate(tags, 1):
        parts = [v for v in [t["comment"], t["comment2"], t["comment3"]] if v]
        company = f" ({t['name']})" if t["name"] else ""
        lines.append(f"{i}. {' | '.join(parts)}{company}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 5. Write CSV
# ──────────────────────────────────────────────

def read_subcategory_rows(csv_path: str) -> list[dict]:
    existing = []
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            existing = [
                {
                    "Sector": row.get("Sector", ""),
                    "Tag": row.get("Tag", ""),
                    "Description": row.get("Description", ""),
                }
                for row in reader
            ]
    except FileNotFoundError:
        pass
    return existing


def clean_subcategory_rows(rows: list[dict]) -> list[dict]:
    """Group rows by sector and remove duplicate tags within each sector."""
    sector_order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    seen: dict[str, dict[str, int]] = {}

    for row in rows:
        sector = str(row.get("Sector") or "").strip()
        tag = str(row.get("Tag") or "").strip()
        description = str(row.get("Description") or "").strip()
        if not sector or not tag:
            continue

        if sector not in grouped:
            sector_order.append(sector)
            grouped[sector] = []
            seen[sector] = {}

        key = tag.casefold()
        clean_row = {"Sector": sector, "Tag": tag, "Description": description}
        if key in seen[sector]:
            existing_row = grouped[sector][seen[sector][key]]
            if len(description) > len(str(existing_row.get("Description") or "")):
                existing_row["Description"] = description
            continue

        seen[sector][key] = len(grouped[sector])
        grouped[sector].append(clean_row)

    cleaned: list[dict] = []
    for sector in sector_order:
        cleaned.extend(grouped[sector])
    return cleaned


def write_subcategory_rows(csv_path: str, rows: list[dict]) -> None:
    rows = clean_subcategory_rows(rows)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Sector", "Tag", "Description"])
        writer.writeheader()
        writer.writerows(rows)


def merge_subcategory_rows(existing: list[dict], sector: str, subcats: list[dict]) -> tuple[list[dict], int, int]:
    """Update/insert subcategories while keeping rows for the same sector together."""
    existing = list(existing)

    sector_indices = [
        i for i, row in enumerate(existing)
        if row.get("Sector", "").strip() == sector
    ]
    tag_to_index = {
        row.get("Tag", "").strip().casefold(): i
        for i, row in enumerate(existing)
        if row.get("Sector", "").strip() == sector and row.get("Tag", "").strip()
    }
    insert_pos = sector_indices[-1] + 1 if sector_indices else len(existing)

    updated = 0
    inserted = 0
    for sub in subcats:
        tag = str(sub.get("tag") or "").strip()
        description = str(sub.get("description") or "").strip()
        if not tag:
            continue

        row = {
            "Sector": sector,
            "Tag": tag,
            "Description": description,
        }
        key = tag.casefold()
        if key in tag_to_index:
            existing[tag_to_index[key]] = row
            updated += 1
        else:
            existing.insert(insert_pos, row)
            tag_to_index[key] = insert_pos
            insert_pos += 1
            inserted += 1

    return existing, updated, inserted


def append_to_csv(csv_path: str, sector: str, subcats: list[dict],
                  output_path: str = None):
    """Update/insert subcategories while keeping rows for the same sector together."""
    if output_path is None:
        output_path = csv_path

    existing = read_subcategory_rows(csv_path)
    existing, updated, inserted = merge_subcategory_rows(existing, sector, subcats)
    cleaned = clean_subcategory_rows(existing)
    write_subcategory_rows(output_path, cleaned)

    print(
        f"[CSV] updated {updated}, inserted {inserted} rows in {output_path} "
        f"(total rows: {len(cleaned)})"
    )
    return output_path


def _sector_rows(rows: list[dict], sector: str) -> list[dict]:
    return [
        row for row in rows
        if row.get("Sector", "").strip() == sector and row.get("Tag", "").strip()
    ]


def _format_company_lines(companies: list[dict]) -> str:
    lines = []
    for item in companies:
        details = []
        if item.get("isin"):
            details.append(f"ISIN {item['isin']}")
        if item.get("wkn"):
            details.append(f"WKN {item['wkn']}")
        raw_labels = [
            item.get("comment", ""),
            item.get("comment2", ""),
            item.get("comment3", ""),
        ]
        raw_labels = [str(value).strip() for value in raw_labels if str(value or "").strip()]
        if raw_labels:
            details.append(f"raw labels: {' | '.join(raw_labels)}")
        if item.get("reason"):
            details.append(f"reason: {item['reason']}")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"- {item['name']}{suffix}")
    return "\n".join(lines)


def _format_existing_tags(rows: list[dict]) -> str:
    if not rows:
        return "(none)"
    return "\n".join(
        f"- {row['Tag']}: {row.get('Description', '')}"
        for row in rows
    )


def _empty_company_prompt(sector: str, companies: list[dict], sector_rows: list[dict]) -> str:
    return (
        "You are updating a financial sector taxonomy.\n"
        f"Sector: {sector}\n\n"
        "Existing tags:\n"
        f"{_format_existing_tags(sector_rows)}\n\n"
        "Companies that still need taxonomy mapping:\n"
        f"{_format_company_lines(companies)}\n\n"
        "For each company, choose the best existing tag when one fits. "
        "Use the raw labels as hints, but translate/normalize them when needed. "
        "Use exact existing tag names. If none fits, create one concise new tag "
        "using the value format \"__NEW__: Tag Name\".\n"
        "Return ONLY a JSON object. Each key must be the exact company name from the list. "
        "Each value must be an object: {\"tag\": \"...\", \"description\": \"...\"}. "
        "For existing tags, description can be empty. For new tags, description must be one "
        "brief English description mentioning the company and business activity."
    )


def _call_taxonomy_repair_provider(
    prompt: str,
    label: str,
    api_key: str,
    model: str,
    base_url: str,
) -> dict | list:
    if not api_key:
        raise RuntimeError(f"{label} API key missing")
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise financial taxonomy assistant. Return valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 4000,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    content = _extract_chat_content(resp.json(), label)
    return parse_llm_json_value(content)


def classify_unlabeled_companies(
    companies: list[dict],
    sector: str,
    sector_rows: list[dict],
    backend: str,
    args: argparse.Namespace,
    batch_size: int,
) -> dict[str, dict]:
    if backend == "rules" or not companies:
        return {}

    results: dict[str, dict] = {}
    for start in range(0, len(companies), batch_size):
        batch = companies[start:start + batch_size]
        prompt = _empty_company_prompt(sector, batch, sector_rows)
        total = -(-len(companies) // batch_size)
        print(f"[Taxonomy] LLM batch {start // batch_size + 1}/{total}: {len(batch)} companies")

        if backend == "ollama":
            url = f"{args.ollama_url.rstrip('/')}/api/chat"
            payload = {
                "model": args.model,
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a precise financial taxonomy assistant. Return valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
            }
            resp = requests.post(url, json=payload, timeout=180)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        elif backend in {"minimax", "deepseek"}:
            if backend == "deepseek":
                providers = [("DeepSeek", args.deepseek_key, args.deepseek_model, args.deepseek_base_url)]
            else:
                providers = [
                    ("MiniMax", args.minimax_key, args.minimax_model, args.minimax_base_url),
                    ("DeepSeek", args.deepseek_key, args.deepseek_model, args.deepseek_base_url),
                ]

            parsed = None
            for label, api_key, model, base_url in providers:
                try:
                    parsed = _call_taxonomy_repair_provider(prompt, label, api_key, model, base_url)
                    break
                except Exception as exc:
                    print(f"[Taxonomy] {label} failed: {_llm_error_summary(exc)}")
                    if backend == "minimax" and label == "MiniMax":
                        print("[Taxonomy] switching to DeepSeek ...")
                    continue
            if parsed is None:
                print("[Taxonomy] no LLM provider succeeded; skipping remaining taxonomy repair")
                return results
        else:
            return results

        if isinstance(parsed, dict):
            for name, value in parsed.items():
                if isinstance(value, dict):
                    tag = str(value.get("tag") or "").strip()
                    desc = str(value.get("description") or "").strip()
                else:
                    tag = str(value or "").strip()
                    desc = ""
                if tag:
                    results[str(name).strip()] = {"tag": tag, "description": desc}

    return results


def _company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _append_holding(description: str, company: str) -> str:
    description = str(description or "").strip()
    if not company:
        return description
    if _company_key(company) and _company_key(company) in _company_key(description):
        return description
    if "Key holdings:" in description:
        return f"{description.rstrip('.')}, {company}."
    if description:
        return f"{description.rstrip('.')}. Key holdings: {company}."
    return f"Key holdings: {company}."


def apply_unlabeled_company_results(
    rows: list[dict],
    sector: str,
    companies: list[dict],
    llm_results: dict[str, dict],
) -> tuple[list[dict], int, int]:
    """Merge empty-company LLM results into subcategory rows."""
    rows = list(rows)
    sector_indices = [i for i, row in enumerate(rows) if row.get("Sector", "").strip() == sector]
    insert_pos = sector_indices[-1] + 1 if sector_indices else len(rows)
    tag_to_index = {
        row.get("Tag", "").strip().casefold(): i
        for i, row in enumerate(rows)
        if row.get("Sector", "").strip() == sector and row.get("Tag", "").strip()
    }
    company_by_key = {_company_key(item["name"]): item["name"] for item in companies}

    updated = 0
    inserted = 0
    for result_name, result in llm_results.items():
        company = company_by_key.get(_company_key(result_name), result_name)
        raw_tag = str(result.get("tag") or "").strip()
        description = str(result.get("description") or "").strip()
        if not raw_tag:
            continue

        is_new = raw_tag.casefold().startswith("__new__:")
        tag = raw_tag.split(":", 1)[1].strip() if is_new and ":" in raw_tag else raw_tag
        if not tag:
            continue

        key = tag.casefold()
        if key in tag_to_index:
            idx = tag_to_index[key]
            rows[idx]["Description"] = _append_holding(rows[idx].get("Description", ""), company)
            updated += 1
        else:
            desc = _append_holding(description, company)
            rows.insert(insert_pos, {"Sector": sector, "Tag": tag, "Description": desc})
            tag_to_index[key] = insert_pos
            insert_pos += 1
            inserted += 1

    return rows, updated, inserted


# ──────────────────────────────────────────────
# 6. Main Flow
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Classify CSV financial-product labels and write them to subcategories.csv"
    )
    parser.add_argument("--csv_in",      required=True, help="Input CSV path")
    parser.add_argument("--csv_update", required=True, help="Path to subcategories.csv")
    parser.add_argument("--sector",   required=True, help="Sector name, e.g. Power / Robotic")
    parser.add_argument("--output",   default=None,  help="Output CSV path")

    parser.add_argument("--backend", choices=["ollama", "minimax", "deepseek", "rules"],
                        default="minimax", help="LLM backend (default: minimax, fallback to deepseek)")

    # Ollama parameters
    parser.add_argument("--model",       default=DEFAULT_OLLAMA_MODEL,           help="Ollama model name")
    parser.add_argument("--ollama-url",  default=DEFAULT_OLLAMA_BASE_URL,
                        help="Ollama service URL")

    # MiniMax parameters
    parser.add_argument("--minimax-key",   default=DEFAULT_MINIMAX_API_KEY, help="MiniMax API key (default from config.yaml)")
    parser.add_argument("--minimax-model",    default=DEFAULT_MINIMAX_MODEL,
                        help="MiniMax model name")
    parser.add_argument("--minimax-base-url", default=DEFAULT_MINIMAX_BASE_URL,
                        help="MiniMax API base URL (default from config.yaml)")
    parser.add_argument("--deepseek-key", default=DEFAULT_DEEPSEEK_API_KEY, help="DeepSeek API key (default from config.yaml)")
    parser.add_argument("--deepseek-model", default=DEFAULT_DEEPSEEK_MODEL, help="DeepSeek model name")
    parser.add_argument("--deepseek-base-url", default=DEFAULT_DEEPSEEK_BASE_URL, help="DeepSeek API base URL")

    # Control parameters
    parser.add_argument("--max-tags", type=int, default=40,
                        help="Maximum number of tag groups sent to the LLM")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print tags and classification results without modifying CSV")

    args = parser.parse_args()

    # Read CSV.
    df = load_csv(args.csv_in)
    tags = extract_tags(df)

    if not tags:
        print("[Error] no valid labels extracted. Check that CSV columns include COMMENT/COMMENT2/COMMENT3.")
        sys.exit(1)

    # Truncate to avoid exceeding the LLM context window.
    if len(tags) > args.max_tags:
        print(f"[Tags] truncated to first {args.max_tags} of {len(tags)} tag groups")
        tags = tags[:args.max_tags]

    tags_text = format_tags_for_llm(tags)
    print("\n-- Tags To Classify --")
    print(tags_text[:1500] + ("..." if len(tags_text) > 1500 else ""))
    print()

    # Call backend.
    if args.backend == "ollama":
        subcats = call_ollama(tags_text, args.sector, args.model, args.ollama_url)

    elif args.backend == "minimax":
        try:
            subcats = call_minimax_then_deepseek(tags_text, args.sector, args)
        except Exception as exc:
            print(f"[Error] MiniMax and DeepSeek failed: {_llm_error_summary(exc)}")
            sys.exit(1)

    elif args.backend == "deepseek":
        if not args.deepseek_key:
            print("[Error] DeepSeek backend requires --deepseek-key")
            sys.exit(1)
        subcats = call_deepseek(tags_text, args.sector,
                                args.deepseek_key,
                                args.deepseek_model,
                                args.deepseek_base_url)

    else:  # rules
        subcats = rules_classify(tags, args.sector)

    # Print results.
    print("\n-- Classification Result --")
    for s in subcats:
        print(f"  [{args.sector}] {s['tag']:22s} | {s['description'][:80]}")

    if args.dry_run:
        print("\n[Dry Run] no files written.")
        return

    # Write CSV.
    out = append_to_csv(args.csv_update, args.sector, subcats, args.output)
    print(f"\nDone. Output file: {out}")


def main_v2():
    parser = argparse.ArgumentParser(
        description="Classify CSV tags and repair empty/unmatched companies in subcategories.csv"
    )
    parser.add_argument("--csv_in", required=True, help="Input CSV path")
    parser.add_argument("--csv_update", required=True, help="subcategories.csv path")
    parser.add_argument("--sector", required=True, help="Sector name, e.g. Power / Robotic / SOFT Com")
    parser.add_argument("--output", default=None, help="Output CSV path; default overwrites --csv_update")
    parser.add_argument(
        "--backend",
        choices=["ollama", "minimax", "deepseek", "rules"],
        default="minimax",
        help="LLM backend (default: minimax, fallback to deepseek)",
    )
    parser.add_argument("--model", default=DEFAULT_OLLAMA_MODEL, help="Ollama model name")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_BASE_URL, help="Ollama base URL")
    parser.add_argument("--minimax-key", default=DEFAULT_MINIMAX_API_KEY, help="MiniMax API key")
    parser.add_argument("--minimax-model", default=DEFAULT_MINIMAX_MODEL, help="MiniMax model name")
    parser.add_argument("--minimax-base-url", default=DEFAULT_MINIMAX_BASE_URL, help="MiniMax API base URL")
    parser.add_argument("--deepseek-key", default=DEFAULT_DEEPSEEK_API_KEY, help="DeepSeek API key")
    parser.add_argument("--deepseek-model", default=DEFAULT_DEEPSEEK_MODEL, help="DeepSeek model name")
    parser.add_argument("--deepseek-base-url", default=DEFAULT_DEEPSEEK_BASE_URL, help="DeepSeek API base URL")
    parser.add_argument("--max-tags", type=int, default=100, help="Maximum labeled tag rows sent to the LLM")
    parser.add_argument("--skip-empty-company-taxonomy", action="store_true",
                        help="Skip LLM taxonomy repair for empty or unmatched companies")
    parser.add_argument("--skip-unmatched-company-taxonomy", action="store_true",
                        dest="skip_empty_company_taxonomy",
                        help="Alias for --skip-empty-company-taxonomy")
    parser.add_argument("--max-empty-companies", type=int, default=200,
                        help="Maximum empty or unmatched companies sent to the LLM")
    parser.add_argument("--max-unmatched-companies", type=int,
                        dest="max_empty_companies",
                        help="Alias for --max-empty-companies")
    parser.add_argument("--empty-batch-size", type=int, default=20,
                        help="LLM batch size for taxonomy repair")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing CSV")
    args = parser.parse_args()

    df = load_csv(args.csv_in)
    tags = extract_tags(df)

    if not tags and "Name" not in df.columns:
        print("[Error] No labeled tags found and the CSV has no Name column.")
        sys.exit(1)

    if len(tags) > args.max_tags:
        print(f"[Tags] truncating to first {args.max_tags} of {len(tags)} tag groups")
        tags = tags[:args.max_tags]

    subcats: list[dict] = []
    if tags:
        tags_text = format_tags_for_llm(tags)
        print("\n--- Labeled Tags To Classify ---")
        print(tags_text[:1500] + ("..." if len(tags_text) > 1500 else ""))
        print()

        if args.backend == "ollama":
            subcats = call_ollama(tags_text, args.sector, args.model, args.ollama_url)
        elif args.backend == "minimax":
            try:
                subcats = call_minimax_then_deepseek(tags_text, args.sector, args)
            except Exception as exc:
                print(f"[Error] MiniMax and DeepSeek failed: {_llm_error_summary(exc)}")
                sys.exit(1)
        elif args.backend == "deepseek":
            if not args.deepseek_key:
                print("[Error] --backend deepseek requires --deepseek-key or config.yaml llm.deepseek_api_key")
                sys.exit(1)
            subcats = call_deepseek(
                tags_text,
                args.sector,
                args.deepseek_key,
                args.deepseek_model,
                args.deepseek_base_url,
            )
        else:
            subcats = rules_classify(tags, args.sector)
    else:
        print("[Tags] no labeled tag groups found; using only empty-company taxonomy")

    print("\n--- Base Taxonomy Result ---")
    for item in subcats:
        print(f"  [{args.sector}] {item['tag']:22s} | {item.get('description', '')[:80]}")

    rows = read_subcategory_rows(args.csv_update)
    rows, updated, inserted = merge_subcategory_rows(rows, args.sector, subcats)
    if subcats:
        print(f"[CSV] base taxonomy: updated {updated}, inserted {inserted} rows (total rows: {len(rows)})")

    sector_rows = _sector_rows(rows, args.sector)
    taxonomy_companies = extract_companies_needing_taxonomy(df, sector_rows)
    if (
        taxonomy_companies
        and not args.skip_empty_company_taxonomy
        and args.backend != "rules"
    ):
        if len(taxonomy_companies) > args.max_empty_companies:
            print(
                f"[Taxonomy] truncating to first {args.max_empty_companies} "
                f"of {len(taxonomy_companies)} companies needing taxonomy mapping"
            )
            taxonomy_companies = taxonomy_companies[:args.max_empty_companies]

        empty_results = classify_unlabeled_companies(
            taxonomy_companies,
            args.sector,
            sector_rows,
            args.backend,
            args,
            max(1, args.empty_batch_size),
        )
        rows, empty_updated, empty_inserted = apply_unlabeled_company_results(
            rows,
            args.sector,
            taxonomy_companies,
            empty_results,
        )
        print(f"[Taxonomy] repair: updated {empty_updated}, inserted {empty_inserted} rows")
    elif taxonomy_companies and args.backend == "rules":
        print("[Taxonomy] skipping repair for --backend rules")

    if args.dry_run:
        print("\n[Dry Run] No files written.")
        return

    out = args.output or args.csv_update
    write_subcategory_rows(out, rows)
    print(f"\nDone. Output file: {out}")


if __name__ == "__main__":
    sys.exit(main_v2())
"""
rebuild_subcategories.py
========================
1. Reads all *_merged.csv files from the csv_update/ folder.
2. Extracts company names per sector.
3. Calls MiniMax (batch) to classify each company into the existing subcategory tags
   and to generate enriched tag descriptions.
4. Rewrites subcategories.csv with updated descriptions.
5. Uploads the updated taxonomy to Google Sheets.

Usage:
    python rebuild_subcategories.py
    python rebuild_subcategories.py --csv-dir csv_update --dry-run

Google Sheets auth:
    First run opens a browser for OAuth consent (token saved to ~/.config/gspread/).
    Subsequent runs use the cached token automatically.
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

# sector ↔ filename-hint mapping (mirrors csv_analyse_watchlist.py)
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
    """Return unique non-empty company names from the Name column."""
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
                    "No explanations, no markdown fences, no extra text outside the JSON. "
                    "Do NOT use chain-of-thought. Do NOT output <think> blocks. "
                    "Output ONLY the JSON object, nothing else."
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
        print(f"    [warn] response truncated (finish_reason=length) — try reducing batch size")
    return content


def _parse_json_from_response(text: str) -> dict | list:
    # strip chain-of-thought blocks
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
    # partial JSON recovery: extract all "key": "value" pairs even if closing brace is missing
    start = text.find("{")
    if start != -1:
        pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', text[start:])
        if pairs:
            return {k: v for k, v in pairs}
    raise ValueError(f"No JSON found in response:\n{text[:400]}")


def _classify_with_ollama(sector: str, companies: list[str],
                           tag_names: list[str]) -> dict[str, str]:
    """Fallback classifier using local Ollama (no token limit issues)."""
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
    # Use a small fast model for fallback — qwen2.5:3b is much quicker than thinking models.
    fallback_model = "qwen2.5:3b"
    url = f"{OLLAMA_BASE_URL}/api/chat"
    payload = {
        "model": fallback_model,
        "stream": False,
        "options": {"num_predict": 512},
        "messages": [
            {
                "role": "system",
                "content": "You are a financial taxonomy assistant. Respond with valid JSON only. No extra text.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(url, json=payload, timeout=180)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    result = _parse_json_from_response(content)
    return result if isinstance(result, dict) else {}


def _classify_batch(sector: str, companies: list[str],
                    tag_names: list[str]) -> dict[str, str]:
    """Classify one batch of companies into tags. Returns {company: tag}."""
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
    """Build enriched descriptions locally from classification results — no LLM needed."""
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
    """
    Classify all companies in batches with automatic retry at batch_size=1 on failure.
    Descriptions are built locally from classification results (no LLM).
    """
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
            # fallback to local Ollama for failed batches
            print(f"    [fallback->ollama] {exc}")
            try:
                partial = _classify_with_ollama(sector, batch, tag_names)
                classifications.update(partial)
                print(f"    [ollama ok] classified {len(partial)} companies")
            except Exception as exc2:
                print(f"    [skip] ollama also failed: {exc2}")

    # group companies by tag
    tag_companies: dict[str, list[str]] = {t: [] for t in tag_names}
    unclassified: list[str] = []
    for company, tag in classifications.items():
        if tag in tag_companies:
            tag_companies[tag].append(company)
        else:
            unclassified.append(company)

    if unclassified:
        print(f"    {len(unclassified)} companies not matched to known tags")

    # build enriched descriptions locally — no additional API calls
    non_empty = {t: cs for t, cs in tag_companies.items() if cs}
    print(f"    building descriptions locally for {len(non_empty)} tags ...")
    updated_tags = _update_descriptions_locally(sector, non_empty, existing_tags)

    return {
        "classifications": classifications,
        "updated_tags": updated_tags,
        "new_tags": [],
    }


# ── subcategories rebuild ─────────────────────────────────────────────────────

def rebuild(csv_dir: Path, dry_run: bool = False,
            only_sector: str | None = None) -> list[dict]:
    existing = load_existing_subcategories()
    # group existing rows by sector
    by_sector: dict[str, list[dict]] = {}
    for row in existing:
        by_sector.setdefault(row["Sector"], []).append(row)

    merged_files = sorted(csv_dir.glob("*_merged.csv"))
    if not merged_files:
        print(f"[error] No *_merged.csv files found in {csv_dir}")
        sys.exit(1)

    # collect companies per sector
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

    # classify per sector via MiniMax
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

        # build lookup: tag -> updated description
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

        # append new tags suggested by LLM
        for new_tag in result.get("new_tags", []):
            t = str(new_tag.get("tag") or "").strip()
            d = str(new_tag.get("description") or "").strip()
            if t and d:
                print(f"  [new tag] {sector} / {t}")
                new_rows.append({"Sector": sector, "Tag": t, "Description": d})

    # sectors not touched by LLM — keep unchanged
    touched = set(sector_companies.keys())
    for sector, rows in by_sector.items():
        if sector not in touched:
            new_rows.extend(rows)

    new_rows = clean_subcategory_rows(new_rows)

    if dry_run:
        print("\n[dry-run] Would write these rows to subcategories.csv:")
        for r in new_rows:
            print(f"  {r['Sector']:10s} | {r['Tag']:25s} | {r['Description'][:70]}")
        return new_rows

    # write local CSV
    with SUBCATEGORIES_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Sector", "Tag", "Description"])
        writer.writeheader()
        writer.writerows(new_rows)
    print(f"\n[OK] Wrote {len(new_rows)} rows to {SUBCATEGORIES_PATH}")

    return new_rows


# ── Google Sheets upload ──────────────────────────────────────────────────────

def _extract_sheet_id(url: str) -> tuple[str, str]:
    """Returns (spreadsheet_id, gid) from a Google Sheets URL."""
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

    print(f"[gsheets] Authenticating (browser will open on first run) ...")
    try:
        gc = gspread.oauth()
    except Exception as exc:
        print(f"[gsheets] OAuth failed: {exc}")
        return

    try:
        sh = gc.open_by_key(spreadsheet_id)
        # find worksheet by gid
        target_ws = None
        for ws in sh.worksheets():
            if str(ws.id) == gid:
                target_ws = ws
                break
        if target_ws is None:
            target_ws = sh.get_worksheet(0)
            print(f"[gsheets] gid={gid} not found, using first sheet: {target_ws.title}")

        # build values matrix
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
        description="Rebuild subcategories.csv using MiniMax and upload to Google Sheets"
    )
    parser.add_argument(
        "--csv-dir", default="csv_update",
        help="Folder containing *_merged.csv files (default: csv_update)"
    )
    parser.add_argument(
        "--sector", default=None,
        help="Process only this sector, e.g. Health, Tech, Power, Robotic"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print results without writing any files"
    )
    parser.add_argument(
        "--no-upload", action="store_true",
        help="Skip Google Sheets upload"
    )
    args = parser.parse_args()

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
