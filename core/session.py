# core/session.py
from __future__ import annotations

import sys
from typing import Dict, Tuple

from PyQt6.QtWidgets import QApplication, QMessageBox

from core.config import get_base_url, get_domain
from core.http_async import HttpClient, HttpConfig


def show_critical_error(text: str):
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    QMessageBox.critical(None, "Ошибка", text)
    raise SystemExit(1)


# ---- cookies helpers ---------------------------------------------------------


def _cookies_for_domain(domain: str) -> Dict[str, str]:
    """
    Безопасно собираем (name -> value) из поддерживаемых браузеров,
    НЕ вызывая browser_cookie3.load() (который пытается дергать все, включая Arc и т.п.).
    """
    import browser_cookie3 as bc3

    cookies: Dict[str, str] = {}

    loaders = [
        ("chrome", bc3.chrome),
        ("chromium", bc3.chromium),
        ("brave", bc3.brave),
        ("vivaldi", bc3.vivaldi),
        ("edge", bc3.edge),
        ("opera", bc3.opera),
        ("firefox", bc3.firefox),
    ]

    for name, loader in loaders:
        try:
            jar = loader(domain_name=domain)  # таргетировано по домену
        except Exception:
            # тихо игнорируем браузеры, где нет профиля/ключей/путей и т.д.
            continue

        # объединяем куки из этого браузера
        for c in jar:
            c_dom = getattr(c, "domain", "") or ""
            if domain in c_dom:
                cookies[c.name] = c.value

    return cookies


# ---- HttpClient фабрика и утилиты -------------------------------------------


async def create_http_client_from_browser_cookies() -> HttpClient:
    """
    Создаёт глобальный HttpClient с куками браузера для текущего домена.
    """
    cfg = HttpConfig(base_url=get_base_url(), user_agent="Intradevor/1.0")
    client = HttpClient(cfg, cookies=_cookies_for_domain(get_domain()))
    await client.ensure_session()
    return client


async def refresh_http_client_cookies(client: HttpClient) -> None:
    """
    Перечитать куки из браузера и заменить их у клиента (для переключений ДЕМО/РЕАЛ и т.п.).
    ⚠ Не трогает пер-ботовые форки: обновляй только глобальный клиент.
    """
    new_cookies = _cookies_for_domain(get_domain())
    # очистить и залить заново
    await client.clear_cookies()
    await client.update_cookies(new_cookies)


async def extract_user_credentials_from_client(
    client: HttpClient,
) -> Tuple[str | None, str | None]:
    """
    Достать user_id и user_hash из cookie_jar клиента.
    """
    session = await client.ensure_session()
    simple = session.cookie_jar.filter_cookies(get_base_url())
    user_id = simple.get("user_id").value if "user_id" in simple else None
    user_hash = simple.get("user_hash").value if "user_hash" in simple else None
    return user_id, user_hash
