# core/config.py
from __future__ import annotations

import json
import os
from typing import Any, Dict

_CONFIG_FILE = "config.json"

# ===== Значения по умолчанию (могут быть переопределены окружением) =====
APP_NAME: str = os.getenv("APP_NAME", "Intradevor")
APP_VERSION: str = os.getenv("APP_VERSION", "0.0.0")

domain: str = os.getenv("DOMAIN", "intrade27.bar")
base_url: str = f"https://{domain}"

ws_url: str = os.getenv("WS_URL", "ws://192.168.56.101:8080")


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
    global APP_NAME, APP_VERSION, domain, base_url, ws_url

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


# ===== Опционально: апдейтеры из кода =====
def set_app_name(name: str) -> None:
    global APP_NAME
    APP_NAME = (name or "").strip() or APP_NAME


def set_app_version(ver: str) -> None:
    global APP_VERSION
    APP_VERSION = (ver or "").strip() or APP_VERSION
