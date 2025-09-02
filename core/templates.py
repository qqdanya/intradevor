import json
from pathlib import Path
from typing import List, Dict

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
LAST_USED_FILE = TEMPLATES_DIR / "last_used.json"


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


def load_last_template(strategy_key: str) -> str | None:
    """Return name of last used template for strategy or None."""
    if not LAST_USED_FILE.exists():
        return None
    try:
        with open(LAST_USED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get(strategy_key)
    except Exception:
        return None
    return None


def save_last_template(strategy_key: str, name: str) -> None:
    """Persist last used template name for strategy."""
    data: Dict[str, str] = {}
    if LAST_USED_FILE.exists():
        try:
            with open(LAST_USED_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                data = obj
        except Exception:
            pass
    data[strategy_key] = name
    LAST_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LAST_USED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
