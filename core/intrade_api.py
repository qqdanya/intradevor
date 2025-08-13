import asyncio
import re
import requests
from bs4 import BeautifulSoup
from PyQt6.QtCore import qDebug
from core import config
from core.policy import (
    DEFAULT_ACCOUNT_CCY,
    clamp_stake,
    normalize_sprint,
)

BALANCE_URL = f"{config.base_url}/balance.php"
TRADE_URL = f"{config.base_url}/ajax5_new.php"
TRADE_CHECK_URL = f"{config.base_url}/trade_check2.php"
PERCENT_URL = f"{config.base_url}/ajax_percent.php"
RISK_URL = f"{config.base_url}/risk_manage.php"


def set_risk(session, user_id, user_hash, risk_min, risk_max):
    payload = {
        "user_id": user_id,
        "user_hash": user_hash,
        "risk_manage_min": risk_min,
        "risk_manage_max": risk_max,
    }
    r = session.post(RISK_URL, data=payload)
    return r.ok


def _parse_balance_text(text: str):
    """
    Возвращает (amount: float, currency: str|None, display: str)
    Пример входа: "12 345,67 ₽" или "$1234.56" или "12 345.67 руб."
    """
    raw = (text or "").strip()
    # Однотипные пробелы
    raw_norm = re.sub(r"\s+", " ", raw)

    # Определяем валюту
    lower = raw_norm.lower()
    currency = None
    symbol = ""
    if "₽" in raw_norm or "руб" in lower or "rub" in lower:
        currency = "RUB"
        symbol = "₽"
    elif "$" in raw_norm or "usd" in lower or "дол" in lower:
        currency = "USD"
        symbol = "$"

    # Достаём числовую часть (оставляем цифры и , . -)
    # Сначала уберём пробелы/nbsp, чтобы не мешали
    compact = raw_norm.replace("\xa0", " ").replace(" ", "")
    numeric = re.sub(r"[^0-9,.\-]", "", compact)

    # Приводим к стандартному float:
    # - если есть только запятая -> считаем её десятичной
    # - если есть и точка и запятая -> считаем, что последний из них — десятичный,
    #   второй — разделитель тысяч (убираем)
    num_std = numeric
    if "," in numeric and "." not in numeric:
        num_std = numeric.replace(",", ".")
    elif "," in numeric and "." in numeric:
        last_comma = numeric.rfind(",")
        last_dot = numeric.rfind(".")
        if last_comma > last_dot:
            # десятичная запятая
            num_std = numeric.replace(".", "").replace(",", ".")
        else:
            # десятичная точка
            num_std = numeric.replace(",", "")

    try:
        amount = float(num_std)
    except Exception:
        amount = 0.0

    # Красивое строковое представление: пробелы как разделитель тысяч + 2 знака
    display_amount = f"{amount:,.2f}".replace(",", " ")
    display = f"{display_amount} {symbol}".rstrip() if symbol else display_amount

    return amount, currency, display


def get_balance_info(session, user_id, user_hash):
    """
    Делает 1 запрос и возвращает (amount: float, currency: str, display: str).
    currency — 'RUB'/'USD' (если не распознали, вернётся DEFAULT_ACCOUNT_CCY).
    display — готовая строка для UI, например: '12 345.67 ₽'.
    """
    payload = {"user_id": user_id, "user_hash": user_hash}
    r = session.post(BALANCE_URL, data=payload)
    if not r.ok:
        return 0.0, DEFAULT_ACCOUNT_CCY, "ошибка"

    amount, currency, display = _parse_balance_text(r.text)
    if not currency:
        currency = DEFAULT_ACCOUNT_CCY
        # если нужно, можно добавить значок к display
        if currency == "RUB" and "₽" not in display:
            display = f"{display} ₽"
        elif currency == "USD" and "$" not in display:
            display = f"{display} $"
    return amount, currency, display


# ---- Тонкие обёртки для совместимости/удобства ----


def get_balance(session, user_id, user_hash) -> float:
    """Оставляем старый интерфейс, но без дублирования запроса."""
    amount, _, _ = get_balance_info(session, user_id, user_hash)
    return amount


def get_balance_str(session, user_id, user_hash) -> str:
    """Готовая строка для UI, например '12 345.67 ₽' или 'ошибка'."""
    _, _, display = get_balance_info(session, user_id, user_hash)
    return display


def get_account_currency(session, user_id, user_hash) -> str:
    """'RUB' или 'USD' (при нераспознавании — DEFAULT_ACCOUNT_CCY)."""
    _, currency, _ = get_balance_info(session, user_id, user_hash)
    return currency


def get_current_percent(session, investment, option, minutes=1, account_ccy="RUB"):
    """
    Получить текущий процент под заданную ставку/время/символ.
    minutes/ccy необязательны — оставлены для совместимости.
    """
    payload = {
        "type": "Sprint",
        "time": str(int(minutes)),
        "currency_name": account_ccy,  # раньше было жестко "RUB"
        "investment": str(investment),
        "percent": "",
        "option": option,
    }
    r = session.post(PERCENT_URL, data=payload)
    try:
        return int(r.text.strip())
    except:
        return None


def place_trade(
    session,
    user_id,
    user_hash,
    investment,
    option,  # символ, напр. "EURUSD" / "BTCUSDT"
    status,  # 1/2
    minutes,  # строка или int
    *,
    account_ccy: str = DEFAULT_ACCOUNT_CCY,
    strict: bool = True,
    on_log=None,
):
    """
    strict=True  -> при нарушении правил возвращает None (не отправляет запрос).
    strict=False -> ставка будет поджата к лимитам, но недопустимое время спринта всё равно запрещаем.
    """

    # ---- Валидация времени спринта
    try:
        m = int(minutes)
    except Exception:
        if on_log:
            on_log(f"[{option}] ❌ Некорректное значение минут: {minutes}")
        return None

    norm_m = normalize_sprint(option, m)
    if norm_m is None:
        if on_log:
            if option == "BTCUSDT":
                on_log(
                    f"[{option}] 🚫 Недопустимое время спринта: {m} мин. Разрешено 5–500."
                )
            else:
                on_log(
                    f"[{option}] 🚫 Недопустимое время спринта: {m} мин. Разрешено 1 или 3–500."
                )
        return None
    minutes = str(norm_m)

    # ---- Валидация/приведение суммы ставки
    try:
        inv = float(investment)
    except Exception:
        if on_log:
            on_log(f"[{option}] ❌ Некорректная сумма ставки: {investment}")
        return None

    clamped = clamp_stake(account_ccy, inv)
    if clamped != inv:
        msg = f"[{option}] ℹ️ Ставка приведена к лимитам {account_ccy}: {clamped:.2f}"
        if strict:
            if on_log:
                on_log(f"[{option}] 🚫 Ставка вне лимитов {account_ccy}. {msg}")
            return None
        else:
            if on_log:
                on_log(msg)
    investment = str(clamped)

    # ---- Отправка сделки
    payload = {
        "user_id": user_id,
        "user_hash": user_hash,
        "option": option,
        "investment": investment,
        "time": minutes,
        "date": "0",
        "trade_type": "sprint",
        "status": str(status),
    }
    r = session.post(TRADE_URL, data=payload)
    soup = BeautifulSoup(r.text, "html.parser")
    trade = soup.find("tr", class_="trade_graph_tick")
    if trade and trade.has_attr("data-id"):
        return trade["data-id"]
    return None


async def check_trade_result(session, user_id, user_hash, trade_id, wait_time):
    await asyncio.sleep(wait_time)
    payload = {"user_id": user_id, "user_hash": user_hash, "trade_id": trade_id}
    r = session.post(TRADE_CHECK_URL, data=payload)
    parts = r.text.strip().split(";")
    if len(parts) >= 3:
        try:
            rate, result, investment = parts
            return float(result) - float(investment)
        except:
            return None
    return None
