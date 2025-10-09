"""Persistence helpers for strategy templates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Template = dict[str, Any]

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
LAST_USED_FILE = TEMPLATES_DIR / "last_used.json"


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None


def load_templates(strategy_key: str) -> list[Template]:
    """Load list of templates for a given strategy."""
    data = _read_json(TEMPLATES_DIR / f"{strategy_key}.json")
    if isinstance(data, list):
        return [tmpl for tmpl in data if isinstance(tmpl, dict)]
    return []


def save_templates(strategy_key: str, templates: list[Template]) -> None:
    """Persist templates for a strategy."""
    path = TEMPLATES_DIR / f"{strategy_key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(templates, fp, ensure_ascii=False, indent=2)


def load_last_template(strategy_key: str) -> str | None:
    """Return name of last used template for strategy or None."""
    data = _read_json(LAST_USED_FILE)
    if isinstance(data, dict):
        value = data.get(strategy_key)
        return str(value) if isinstance(value, str) else None
    return None


def save_last_template(strategy_key: str, name: str) -> None:
    """Persist last used template name for strategy."""
    data: dict[str, str] = {}
    existing = _read_json(LAST_USED_FILE)
    if isinstance(existing, dict):
        data.update({k: str(v) for k, v in existing.items() if isinstance(v, str)})

    data[strategy_key] = name
    LAST_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LAST_USED_FILE.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
