# ============================================================
#  rag/local_llm.py — constrained-output client for the small local Ollama
#  model used by the Orchestrator's router / critic / retrieval grader.
#
#  Those three only make a cheap decision ("document or market?", "is this
#  enough?"), but asking qwen3:4b to "reply with one word" does not work: it
#  writes pages of reasoning instead, burns the whole token budget at ~13
#  tokens/s on CPU (~23s per call), and never emits the label. Ollama's
#  `format` option takes a JSON schema and constrains decoding to it, so the
#  model can only produce a valid answer and stops as soon as the object is
#  complete (~1s). `think: false` additionally skips qwen3's reasoning block.
# ============================================================

from __future__ import annotations

import json
from typing import Any, Callable

import requests

DEFAULT_BASE_URL = "http://localhost:11434"


def parse_json_object(text: str) -> dict | None:
    """First JSON object in `text` (models sometimes wrap it in prose or
    fences), or None if there isn't a parseable one."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def enum_validator(schema: dict) -> Callable[[dict], bool]:
    """True when every enum-typed property of `schema` holds an allowed value.
    Constrained decoding guarantees this locally; the cloud fallback has no
    such guarantee, so callers check it on both paths."""
    enums = {
        name: spec["enum"]
        for name, spec in schema.get("properties", {}).items()
        if "enum" in spec
    }
    return lambda obj: all(obj.get(name) in allowed for name, allowed in enums.items())


class OllamaJSONClient:
    def __init__(self, model: str, base_url: str = DEFAULT_BASE_URL, keep_alive: str | int | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        # None leaves Ollama's default (unload after 5 idle minutes).
        self.keep_alive = keep_alive

    def generate(self, prompt: str, schema: dict, num_predict: int, timeout_seconds: float) -> dict | None:
        """One schema-constrained completion. Returns the parsed object, or
        None on timeout / connection error / unparseable output so the caller
        can fall back (it never raises)."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": 0, "num_predict": num_predict},
        }
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive

        response = self._post(body, timeout_seconds)
        if response is None:
            return None
        try:
            content = response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError):
            return None
        return parse_json_object(content)

    def _post(self, body: dict, timeout_seconds: float):
        url = f"{self.base_url}/api/chat"
        try:
            response = requests.post(url, json=body, timeout=timeout_seconds)
            # A model without thinking support rejects the `think` flag outright.
            if response.status_code == 400 and "think" in response.text.lower():
                body = {k: v for k, v in body.items() if k != "think"}
                response = requests.post(url, json=body, timeout=timeout_seconds)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            print(f"[LocalLLM] {self.model} call failed: {exc}")
            return None
