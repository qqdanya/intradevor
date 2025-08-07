import browser_cookie3
import requests
from bs4 import BeautifulSoup

def create_session_from_browser_cookies():
    try:
        cookies = browser_cookie3.firefox(domain_name="intrade27.bar")
        session = requests.Session()
        session.cookies.update(cookies)
        return session
    except Exception as e:
        print("[-] Ошибка загрузки cookies:", e)
        return None

def extract_user_credentials(session):
    for cookie in session.cookies:
        if cookie.name == "user_id":
            user_id = cookie.value
        if cookie.name == "user_hash":
            user_hash = cookie.value
    if user_id and user_hash:
        return user_id, user_hash
    return None, None


if __name__ == "__main__":
    extract_user_credentials(create_session_from_browser_cookies())
