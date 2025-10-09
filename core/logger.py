# core/logger.py
from datetime import datetime


def ts(text: str) -> str:
    """
    Добавляет префикс времени к сообщению.
    Формат: [дд.мм.гггг | чч:мм:сс] текст
    """
    prefix = datetime.now().strftime("[%d.%m.%Y  %H:%M:%S]")
    return f"{prefix} {text}"
