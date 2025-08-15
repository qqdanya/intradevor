import sys
import os
import pickle
import browser_cookie3
import requests
from bs4 import BeautifulSoup
from PyQt6.QtWidgets import QApplication, QMessageBox
from core import config
from typing import Dict
from core.http_async import HttpClient
from core.config import get_base_url, get_domain


SESSION_FILE = "session.pkl"


def save_session(session: requests.Session):
    with open(SESSION_FILE, "wb") as f:
        pickle.dump(session.cookies, f)


def load_session() -> requests.Session | None:
    if not os.path.exists(SESSION_FILE):
        return None
    s = requests.Session()
    with open(SESSION_FILE, "rb") as f:
        s.cookies.update(pickle.load(f))
    return s


def show_critical_error(text: str):
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    QMessageBox.critical(None, "Ошибка", text)
    sys.exit(1)


def create_session_from_browser_cookies():
    try:
        cookies = browser_cookie3.firefox(domain_name=config.domain)
        session = requests.Session()
        session.cookies.update(cookies)
        return session
    except Exception as e:
        show_critical_error(f"Не удалось загрузить cookies: {e}")


def extract_user_credentials(session):
    user_id = user_hash = None
    for cookie in session.cookies:
        if cookie.name == "user_id":
            user_id = cookie.value
        if cookie.name == "user_hash":
            user_hash = cookie.value

    if not user_id or not user_hash:
        show_critical_error("Не удалось извлечь user_id и user_hash из cookies.")

    return user_id, user_hash


def _cookies_for_domain(domain: str) -> Dict[str, str]:
    jar = browser_cookie3.load()  # можно ограничить конкретным браузером, если нужно
    cookies: Dict[str, str] = {}
    for c in jar:
        # фильтруем по домену
        if domain in (c.domain or ""):
            cookies[c.name] = c.value
    return cookies


async def create_http_client_from_browser_cookies() -> HttpClient:
    base_url = get_base_url()  # например, "https://example.com/"
    domain = get_domain()  # например, "example.com"
    cookies = _cookies_for_domain(domain)
    return HttpClient(
        base_url, cookies=cookies, headers={"User-Agent": "Intradevor/1.0"}
    )


if __name__ == "__main__":
    session = create_session_from_browser_cookies()
    user_id, user_hash = extract_user_credentials(session)
    print("user_id:", user_id)
    print("user_hash:", user_hash)
