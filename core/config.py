# core/config.py
from __future__ import annotations

import json
import os
from typing import Any, Dict

_CONFIG_FILE = "config.json"

# ===== Значения по умолчанию (могут быть переопределены окружением) =====
APP_NAME: str = os.getenv("APP_NAME", "Intradevor")
APP_VERSION: str = os.getenv("APP_VERSION", "1.1.0")

domain: str = os.getenv("DOMAIN", "intrade27.bar")
base_url: str = f"https://{domain}"

ws_url: str = os.getenv("WS_URL", "ws://localhost:8080")

# Параметры шрифта приложения
FONT_FAMILY: str | None = os.getenv("FONT_FAMILY")
try:
    FONT_SIZE: int | None = (
        int(os.getenv("FONT_SIZE")) if os.getenv("FONT_SIZE") else None
    )
except ValueError:
    FONT_SIZE = None

# Тема оформления приложения: "light", "dark" или "system"
THEME: str = os.getenv("THEME", "system")


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"[config] Ошибка чтения {path}: {e}")
        return {}


def _write_json(path: str, data: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[config] Ошибка сохранения {path}: {e}")


def load_config() -> None:
    """
    Загружает config.json, обновляя глобальные переменные.
    Порядок приоритетов:
      1) Переменные окружения (если заданы)
      2) config.json
      3) Значения по умолчанию
    Если файла нет — он будет создан с текущими значениями (дефолты/окружение).
    """
    global APP_NAME, APP_VERSION, domain, base_url, ws_url, FONT_FAMILY, FONT_SIZE, THEME

    if not os.path.exists(_CONFIG_FILE):
        # создаём файл с текущими (дефолт/окружение) значениями
        save_config()
        return

    data = _read_json(_CONFIG_FILE)

    # Имя/версия — применяем из файла ТОЛЬКО если не заданы окружением
    if "APP_NAME" not in os.environ:
        APP_NAME = data.get("app_name", APP_NAME)
    if "APP_VERSION" not in os.environ:
        APP_VERSION = data.get("app_version", APP_VERSION)

    # Домен/WS — применяем из файла ТОЛЬКО если не заданы окружением
    if "DOMAIN" not in os.environ:
        # поддерживаем старое поле "domain"
        domain = data.get("domain", domain)
    if "WS_URL" not in os.environ:
        ws_url = data.get("ws_url", ws_url)

    if "FONT_FAMILY" not in os.environ:
        FONT_FAMILY = data.get("font_family", FONT_FAMILY)
    if "FONT_SIZE" not in os.environ:
        try:
            FONT_SIZE = int(data.get("font_size", FONT_SIZE)) if data.get("font_size") is not None else FONT_SIZE
        except (TypeError, ValueError):
            pass

    if "THEME" not in os.environ:
        THEME = data.get("theme", THEME)

    base_url = f"https://{domain}"


def save_config() -> None:
    """
    Сохраняет актуальные настройки в config.json.
    (Если файл отсутствует — создастся.)
    """
    data: Dict[str, Any] = _read_json(_CONFIG_FILE)
    data.update(
        {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "domain": domain,
            "ws_url": ws_url,
            "font_family": FONT_FAMILY,
            "font_size": FONT_SIZE,
            "theme": THEME,
        }
    )
    _write_json(_CONFIG_FILE, data)

    global base_url
    base_url = f"https://{domain}"


# Загружаем конфиг при импорте модуля
load_config()


# ===== Удобные геттеры =====
def get_base_url() -> str:
    return base_url


def get_domain() -> str:
    return domain


def get_ws_url() -> str:
    return ws_url


def get_app_name() -> str:
    return APP_NAME


def get_app_version() -> str:
    return APP_VERSION


def get_font_family() -> str | None:
    return FONT_FAMILY


def get_font_size() -> int | None:
    return FONT_SIZE


def get_theme() -> str:
    return THEME


# ===== Опционально: апдейтеры из кода =====
def set_app_name(name: str) -> None:
    global APP_NAME
    APP_NAME = (name or "").strip() or APP_NAME


def set_app_version(ver: str) -> None:
    global APP_VERSION
    APP_VERSION = (ver or "").strip() or APP_VERSION


def set_font_family(name: str | None) -> None:
    global FONT_FAMILY
    FONT_FAMILY = name


def set_font_size(size: int | None) -> None:
    global FONT_SIZE
    FONT_SIZE = size


def set_theme(theme: str) -> None:
    global THEME
    THEME = theme
