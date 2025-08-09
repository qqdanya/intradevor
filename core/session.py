import sys
import browser_cookie3
import requests
from bs4 import BeautifulSoup
from PyQt6.QtWidgets import QApplication, QMessageBox
from core import config

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

if __name__ == "__main__":
    session = create_session_from_browser_cookies()
    user_id, user_hash = extract_user_credentials(session)
    print("user_id:", user_id)
    print("user_hash:", user_hash)

