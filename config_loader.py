from __future__ import annotations

import os
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "financial_rag" / "config.yaml"
LEGACY_PROJECT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
LEGACY_HOME_PROJECTS_CONFIG_PATH = Path.home() / "Projects" / "config.yaml"


def resolve_config_path() -> Path:
    """Resolve config path from env var, then secure/default fallback candidates."""
    env_path = os.getenv("FIN_RAG_CONFIG", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    for candidate in (
        LEGACY_PROJECT_CONFIG_PATH,
        Path.cwd() / "config.yaml",
        DEFAULT_CONFIG_PATH,
        LEGACY_HOME_PROJECTS_CONFIG_PATH,
    ):
        if candidate.exists():
            return candidate.resolve()

    return DEFAULT_CONFIG_PATH.resolve()


def load_config(strict: bool = True) -> dict:
    """Load YAML config.

    strict=True: raise FileNotFoundError if config file does not exist.
    strict=False: return {} when config file is missing.
    """
    config_path = resolve_config_path()
    if not config_path.exists():
        if strict:
            raise FileNotFoundError(
                f"Config file not found: {config_path}. "
                "Set FIN_RAG_CONFIG to your config.yaml path."
            )
        return {}

    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}
