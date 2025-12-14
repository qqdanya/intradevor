# extract_cookies.py
"""
Отдельный скрипт, который извлекает куки браузеров с помощью browser_cookie3
и сохраняет их в cookies.json (рядом с собой). JSON используется вместо
pickle, чтобы предотвратить выполнение произвольного кода при загрузке
файла.
"""

import browser_cookie3 as bc3
import json
import sys
from pathlib import Path

def extract_cookies(domain: str) -> dict:
    cookies = {}
    try:
        jar = bc3.chrome(domain_name=domain)
        for c in jar:
            if domain in (getattr(c, "domain", "") or ""):
                cookies[c.name] = c.value
        print(f"[OK] Cookies extracted for domain: {domain} ({len(cookies)} шт.)")
    except Exception as e:
        print(f"[ERROR] Failed to read cookies via browser_cookie3: {e}")
    return cookies

def main():
    domain = sys.argv[1] if len(sys.argv) > 1 else "example.com"
    cookies = extract_cookies(domain)
    path = Path(__file__).resolve().parent / "cookies.json"
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Saved to {path}")

if __name__ == "__main__":
    main()

