import json
import os

_CONFIG_FILE = "config.json"

# Значения по умолчанию
domain = "intrade27.bar"
base_url = f"https://{domain}"

def load_config():
    global domain, base_url
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                domain = data.get("domain", domain)
        except Exception as e:
            print(f"[config] Ошибка загрузки config.json: {e}")
    base_url = f"https://{domain}"

def save_config():
    global domain, base_url
    data = {
        "domain": domain,
    }
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[config] Ошибка сохранения config.json: {e}")
    base_url = f"https://{domain}"  # Обновляем base_url после сохранения

# Загружаем конфиг при импорте модуля
load_config()

