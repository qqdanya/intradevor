import asyncio
import re

from bs4 import BeautifulSoup

from core import config
from core.money import format_amount
from core.policy import (
    DEFAULT_ACCOUNT_CCY,
    clamp_stake,
    normalize_sprint,
)

_SPACE_RE = re.compile(r"\s+")
_NUMERIC_RE = re.compile(r"[^0-9,\.\-]")

_CURRENCY_DEFINITIONS = {
    "RUB": {
        "symbols": ("₽",),
        "keywords": ("руб", "rub", "rur"),
    },
    "USD": {
        "symbols": ("$",),
        "keywords": ("usd", "дол", "dollar"),
    },
}

BALANCE_URL = f"{config.base_url}/balance.php"
TRADE_URL = f"{config.base_url}/ajax5_new.php"
TRADE_CHECK_URL = f"{config.base_url}/trade_check2.php"
PERCENT_URL = f"{config.base_url}/ajax_percent.php"
RISK_URL = f"{config.base_url}/risk_manage.php"
CURRENCY_URL = f"{config.base_url}/user_currency_edit.php"


def change_currency(session, user_id, user_hash):
    payload = {"user_id": user_id, "user_hash": user_hash}
    r = session.post(CURRENCY_URL, data=payload)
    return r.ok


def set_risk(session, user_id, user_hash, risk_min, risk_max):
    payload = {
        "user_id": user_id,
        "user_hash": user_hash,
        "risk_manage_min": risk_min,
        "risk_manage_max": risk_max,
    }
    r = session.post(RISK_URL, data=payload)
    return r.ok


def _normalise_whitespace(text: str) -> str:
    cleaned = (
        text.replace("\xa0", " ")
        .replace("\u202f", " ")
        .replace("\u2212", "-")
    )
    return _SPACE_RE.sub(" ", cleaned).strip()


def _detect_currency(raw: str, lower: str) -> tuple[str | None, str]:
    for code, meta in _CURRENCY_DEFINITIONS.items():
        symbols = meta["symbols"]
        keywords = meta["keywords"]
        if any(sym and sym in raw for sym in symbols):
            return code, symbols[0]
        if any(keyword in lower for keyword in keywords):
            return code, symbols[0]
    return None, ""


def _normalise_numeric(raw: str) -> str:
    compact = raw.replace(" ", "")
    numeric = _NUMERIC_RE.sub("", compact)
    if not numeric:
        return "0"

    has_comma = "," in numeric
    has_dot = "." in numeric

    if has_comma and not has_dot:
        return numeric.replace(",", ".")
    if has_comma and has_dot:
        last_comma = numeric.rfind(",")
        last_dot = numeric.rfind(".")
        if last_comma > last_dot:
            # comma is decimal separator -> remove dots (thousands)
            return numeric.replace(".", "").replace(",", ".")
        return numeric.replace(",", "")
    return numeric


def _format_display(amount: float, symbol: str) -> str:
    display_amount = f"{amount:,.2f}".replace(",", " ")
    return f"{display_amount} {symbol}".rstrip() if symbol else display_amount


def _parse_balance_text(text: str):
    """
    Возвращает (amount: float, currency: str|None, display: str)
    Пример входа: "12 345,67 ₽" или "$1234.56" или "12 345.67 руб."
    """

    raw_norm = _normalise_whitespace(text or "")
    lower = raw_norm.lower()
    currency, symbol = _detect_currency(raw_norm, lower)

    numeric = _normalise_numeric(raw_norm)
    try:
        amount = float(numeric)
    except ValueError:
        amount = 0.0

    display = _format_display(amount, symbol)
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


def get_current_percent(
    session, investment, option, minutes=1, account_ccy="RUB", trade_type="sprint"
):
    """
    Получить текущий процент под заданную ставку/время/символ.
    minutes/ccy необязательны — оставлены для совместимости.
    """
    trade_type_str = str(trade_type).strip()
    trade_type_lower = trade_type_str.lower()
    t = "Classic" if trade_type_lower == "classic" else "sprint"
    payload = {
        "type": t,
        "currency_name": account_ccy,  # раньше было жестко "RUB"
        "investment": str(investment),
        "percent": "",
        "option": option,
    }
    if trade_type_lower != "classic":
        payload["time"] = str(int(minutes))
    r = session.post(PERCENT_URL, data=payload)
    try:
        return int(r.text.strip())
    except Exception:
        return None


def place_trade(
    session,
    user_id,
    user_hash,
    investment,
    option,  # символ, напр. "EURUSD" / "BTCUSDT"
    status,  # 1/2
    minutes,  # строка (HH:MM) или int
    *,
    account_ccy: str = DEFAULT_ACCOUNT_CCY,
    strict: bool = True,
    on_log=None,
    trade_type: str = "sprint",
    date: str = "0",
):
    """
    strict=True  -> при нарушении правил возвращает None (не отправляет запрос).
    strict=False -> ставка будет поджата к лимитам, но недопустимое время спринта всё равно запрещаем.
    """

    trade_type_str = str(trade_type).strip()
    trade_type_lower = trade_type_str.lower()

    if trade_type_lower == "classic":
        time_value = str(minutes)
    else:
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
        time_value = str(norm_m)

    # ---- Валидация/приведение суммы ставки
    try:
        inv = float(investment)
    except Exception:
        if on_log:
            on_log(f"[{option}] ❌ Некорректная сумма ставки: {investment}")
        return None

    clamped = clamp_stake(account_ccy, inv)
    if clamped != inv:
        msg = (
            f"[{option}] ℹ️ Ставка приведена к лимитам {account_ccy}: "
            f"{format_amount(clamped)}"
        )
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
        "time": time_value,
        "date": date,
        "trade_type": trade_type_str,
        "status": str(status),
    }
    r = session.post(TRADE_URL, data=payload)
    soup = BeautifulSoup(r.text, "html.parser")
    trade = soup.find("tr", class_="trade_graph_tick")
    if trade and trade.has_attr("data-id"):
        return trade["data-id"]
    return None


async def check_trade_result(session, user_id, user_hash, trade_id, wait_time=60.0):
    """Запрашивает результат сделки и, при необходимости, опрашивает сервер.

    После ожидания ``wait_time`` секунд отправляется запрос на получение
    результата сделки. Если ответ не содержит нужных данных, выполняется
    повторная проверка каждые 0.5 секунды до появления результата. Отмена
    корутины приводит к немедленному выходу из функции.
    """

    await asyncio.sleep(wait_time)
    payload = {"user_id": user_id, "user_hash": user_hash, "trade_id": trade_id}

    while True:
        try:
            r = session.post(TRADE_CHECK_URL, data=payload)
            parts = r.text.strip().split(";")
            if len(parts) >= 3:
                rate, result, investment = parts[:3]
                return float(result) - float(investment)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

        await asyncio.sleep(0.5)
