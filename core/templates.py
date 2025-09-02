import json
from pathlib import Path
from typing import List, Dict

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def load_templates(strategy_key: str) -> List[Dict]:
    """Load list of templates for a given strategy."""
    path = TEMPLATES_DIR / f"{strategy_key}.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_templates(strategy_key: str, templates: List[Dict]) -> None:
    """Persist templates for a strategy."""
    path = TEMPLATES_DIR / f"{strategy_key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=2)
