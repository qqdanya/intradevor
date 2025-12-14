# core/session.py
from __future__ import annotations

import asyncio
import pickle
import subprocess
import sys
from pathlib import Path
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


def _default_cookies_file() -> Path:
    """Определить путь для cookies.pkl в зависимости от режима запуска."""
    if getattr(sys, "frozen", False):  # PyInstaller onefile/onedir
        return Path(sys.executable).resolve().parent / "cookies.pkl"
    return Path(__file__).resolve().parent.parent / "cookies.pkl"


# путь к cookies.pkl — рядом с exe в собранной версии, либо рядом с исходниками
COOKIES_FILE = _default_cookies_file()


def _cookies_for_domain(domain: str) -> Dict[str, str]:
    """
    Извлекает куки. Если программа запущена как PyInstaller exe (Windows),
    то вызывает внешний скрипт extract_cookies.py.
    """
    from pathlib import Path
    import pickle

    # путь к cookies.pkl (тот же, что у тебя уже используется)
    path = COOKIES_FILE

    # если запущено из PyInstaller exe — использовать внешний скрипт
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        try:
            script_path = Path(sys.executable).resolve().parent / "extract_cookies.py"
            if not script_path.exists():
                print(f"[WARN] extract_cookies.py not found at {script_path}")
                return {}
            print(f"[INFO] Running external extractor: {script_path}")
            subprocess.run(["python", str(script_path), domain], check=True)
            if path.exists():
                print("[INFO] External extractor created cookies.pkl")
                return pickle.load(open(path, "rb"))
        except Exception as e:
            print(f"[ERROR] External cookie extraction failed: {e}")
            return {}

    # иначе — обычное поведение (в разработке на Linux / Python)
    import browser_cookie3 as bc3
    cookies: Dict[str, str] = {}
    try:
        jar = bc3.chrome(domain_name=domain)
        for c in jar:
            if domain in (getattr(c, "domain", "") or ""):
                cookies[c.name] = c.value
    except Exception as e:
        print(f"[WARN] browser_cookie3 failed: {e}")
    return cookies


def _cookies_via_chromedriver(login_url: str) -> Dict[str, str]:
    """
    Открывает ChromeDriver на ``login_url`` и ждёт, пока пользователь
    авторизуется. После появления нужных кук возвращает их в виде
    ``dict``.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    import time

    options = Options()
    driver = webdriver.Chrome(options=options)
    driver.get(login_url)
    print(f"Открыл браузер для авторизации: {login_url}")

    expected_domain = get_domain()
    cookies: Dict[str, str] = {}
    try:
        while True:
            time.sleep(1)
            try:
                cookies_list = driver.get_cookies()
            except Exception:
                break

            cookies = {
                c["name"]: c["value"]
                for c in cookies_list
                if expected_domain in (c.get("domain") or "")
            }

            uid_cookie = next((c for c in cookies_list if c["name"] == "user_id"), None)
            uhash_cookie = next(
                (c for c in cookies_list if c["name"] == "user_hash"), None
            )
            if uid_cookie and uhash_cookie:
                uid_dom = uid_cookie.get("domain") or ""
                uhash_dom = uhash_cookie.get("domain") or ""
                if expected_domain in uid_dom and expected_domain in uhash_dom:
                    break
                raise ValueError(
                    f"Куки user_id/user_hash получены с домена"
                    f" {uid_dom!r}/{uhash_dom!r}, ожидался {expected_domain!r}"
                )
    finally:
        driver.quit()

    return cookies


def save_cookies(cookies: Dict[str, str], path: Path = COOKIES_FILE) -> None:
    """Persist cookies to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(cookies, fh)


def load_cookies(path: Path = COOKIES_FILE) -> Dict[str, str] | None:
    """Load cookies from ``path`` if it exists."""
    try:
        with path.open("rb") as fh:
            return pickle.load(fh)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def clear_saved_cookies(path: Path = COOKIES_FILE) -> None:
    """Remove saved cookies file if present."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---- HttpClient фабрика и утилиты -------------------------------------------


async def create_http_client_from_browser_cookies(
    force_refresh: bool = False,
) -> HttpClient:
    """Создаёт глобальный HttpClient с куками браузера для текущего домена."""
    cfg = HttpConfig(
        base_url=get_base_url(),
        user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0",
    )

    cookies: Dict[str, str] | None = None
    if not force_refresh:
        cookies = load_cookies()

    if cookies:
        client = HttpClient(cfg, cookies=cookies)
        try:
            await client.ensure_session()
            uid, uhash = await extract_user_credentials_from_client(client)
            if uid and uhash:
                return client
        except Exception:
            pass

    cookies = _cookies_for_domain(get_domain())
    if not cookies:
        login_url = f"{get_base_url()}/login"
        try:
            cookies = await asyncio.get_event_loop().run_in_executor(
                None, _cookies_via_chromedriver, login_url
            )
        except Exception as e:
            show_critical_error(f"Не удалось открыть браузер для авторизации: {e}")
        if not cookies:
            show_critical_error("Не удалось получить куки. Авторизация не выполнена.")

    save_cookies(cookies)
    client = HttpClient(cfg, cookies=cookies)
    await client.ensure_session()
    return client


async def refresh_http_client_cookies(client: HttpClient) -> None:
    """Перечитать куки из текущей сессии клиента и сохранить их."""
    session = await client.ensure_session()
    simple = session.cookie_jar.filter_cookies(get_base_url())
    new_cookies = {name: cookie.value for name, cookie in simple.items()}
    await client.clear_cookies()
    await client.update_cookies(new_cookies)
    save_cookies(new_cookies)


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
