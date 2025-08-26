# core/intrade_api_async.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

from bs4 import BeautifulSoup

from core.http_async import HttpClient
from core.policy import DEFAULT_ACCOUNT_CCY, clamp_stake, normalize_sprint
from core.intrade_api import (
    _parse_balance_text,
)  # переиспользуем точный парсер sync-версии

# те же пути, но как относительные — HttpClient добавит base_url сам
PATH_BALANCE = "balance.php"
PATH_TRADE = "ajax5_new.php"
PATH_TRADE_CHECK = "trade_check2.php"
PATH_PERCENT = "ajax_percent.php"
PATH_RISK = "risk_manage.php"
PATH_CCY = "user_currency_edit.php"
PATH_TOGGLE_REAL_DEMO = "user_real_trade.php"
PATH_PROFILE = "profile"


# ---------------- Баланс / валюта ----------------


async def get_balance_info(
    client: HttpClient, user_id: str, user_hash: str
) -> Tuple[float, str, str]:
    """
    Возвращает (amount, currency, display) как sync-версия.
    """
    payload = {"user_id": user_id, "user_hash": user_hash}
    text = await client.post(PATH_BALANCE, data=payload, expect_json=False)
    amount, currency, display = _parse_balance_text(text)
    if not currency:
        currency = DEFAULT_ACCOUNT_CCY
        # подправим display под символ, как в sync-версии
        if currency == "RUB" and "₽" not in display:
            display = f"{display} ₽"
        elif currency == "USD" and "$" not in display:
            display = f"{display} $"
    return amount, currency, display


async def get_balance(client: HttpClient, user_id: str, user_hash: str) -> float:
    amount, _, _ = await get_balance_info(client, user_id, user_hash)
    return amount


async def get_balance_str(client: HttpClient, user_id: str, user_hash: str) -> str:
    _, _, display = await get_balance_info(client, user_id, user_hash)
    return display


async def get_account_currency(client: HttpClient, user_id: str, user_hash: str) -> str:
    _, ccy, _ = await get_balance_info(client, user_id, user_hash)
    return ccy


# ---------------- Процент под ставку ----------------


async def get_current_percent(
    client: HttpClient,
    investment: float | int | str,
    option: str,
    minutes: int | str = 1,
    account_ccy: str = "RUB",
) -> Optional[int]:
    """
    Совместимая версия: отправляет form-data как sync, возвращает int|None.
    """
    payload = {
        "type": "Sprint",
        "time": str(int(minutes)),
        "currency_name": account_ccy,
        "investment": str(investment),
        "percent": "",
        "option": option,
    }
    text = await client.post(PATH_PERCENT, data=payload, expect_json=False)
    try:
        return int(str(text).strip())
    except Exception:
        return None


# ---------------- Сделка ----------------


async def place_trade(
    client: HttpClient,
    user_id: str,
    user_hash: str,
    investment: float | int | str,
    option: str,
    status: int,  # 1/2
    minutes: int | str,  # минуты
    *,
    account_ccy: str = DEFAULT_ACCOUNT_CCY,
    strict: bool = True,
    on_log=None,
) -> Optional[str]:
    """
    Полная проверка, как в sync-версии:
      - нормализация времени спринта
      - привод к лимитам суммы
      - POST и парсинг HTML ответа для data-id
    Возвращает trade_id или None.
    """
    # --- время спринта
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

    # --- сумма
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

    # --- POST сделки и парсинг HTML
    payload = {
        "user_id": user_id,
        "user_hash": user_hash,
        "option": option.replace("/", ""),
        "investment": investment,
        "time": minutes,
        "date": "0",
        "trade_type": "sprint",
        "status": str(status),
    }
    html = await client.post(PATH_TRADE, data=payload, expect_json=False)
    soup = BeautifulSoup(html, "html.parser")
    trade = soup.find("tr", class_="trade_graph_tick")
    if trade and trade.has_attr("data-id"):
        return trade["data-id"]
    return None


async def check_trade_result(
    client: HttpClient,
    user_id: str,
    user_hash: str,
    trade_id: str,
    wait_time: float = 60.0,
    poll_interval: float = 1.0,
    initial_wait: float | None = None,
) -> Optional[float]:
    """
    Периодически опрашивает сервер о результате сделки, возвращая profit
    (result - investment) как float, либо None.

    wait_time     – максимальное время ожидания в секундах.
    poll_interval – интервал между повторными запросами.
    initial_wait  – сколько секунд подождать перед первым запросом
                    (по умолчанию poll_interval).
    """
    deadline = asyncio.get_running_loop().time() + float(wait_time)
    payload = {"user_id": user_id, "user_hash": user_hash, "trade_id": trade_id}

    # пауза перед первым запросом, чтобы не дёргать сервер слишком рано
    if wait_time > 0:
        delay = poll_interval if initial_wait is None else float(initial_wait)
        await asyncio.sleep(min(delay, wait_time))

    while True:
        try:
            text = await client.post(PATH_TRADE_CHECK, data=payload, expect_json=False)
        except Exception:
            text = None
        parts = str(text).strip().split(";") if text is not None else []
        if len(parts) >= 3:
            try:
                rate, result, investment = parts[:3]
                return float(result) - float(investment)
            except Exception:
                pass

        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            break
        await asyncio.sleep(min(poll_interval, left))

    return None


# ---------------- Прочее: риск/валюта аккаунта ----------------


async def change_currency(client: HttpClient, user_id: str, user_hash: str) -> bool:
    payload = {"user_id": user_id, "user_hash": user_hash}
    try:
        await client.post(PATH_CCY, data=payload, expect_json=False)
        return True
    except Exception:
        return False


async def set_risk(
    client: HttpClient,
    user_id: str,
    user_hash: str,
    risk_min: int | float,
    risk_max: int | float,
) -> bool:
    payload = {
        "user_id": user_id,
        "user_hash": user_hash,
        "risk_manage_min": risk_min,
        "risk_manage_max": risk_max,
    }
    try:
        await client.post(PATH_RISK, data=payload, expect_json=False)
        return True
    except Exception:
        return False


async def toggle_real_demo(client: HttpClient, user_id: str, user_hash: str) -> bool:
    """
    Переключить режим Реал/Демо.
    API: POST user_real_trade.php с user_id и user_hash.
    Возвращает True при отсутствии сетевых ошибок (сервер сам переключит режим).
    """
    payload = {"user_id": user_id, "user_hash": user_hash}
    try:
        # Ответ не важен, достаточно, что запрос успешно прошёл
        await client.post(PATH_TOGGLE_REAL_DEMO, data=payload, expect_json=False)
        return True
    except Exception:
        return False


async def is_demo_account(client: HttpClient) -> bool:
    """
    Проверить, активен ли демо-счёт.
    Признак: в /profile есть <input type="hidden" name="demo_update" value="1">.
    """
    try:
        html = await client.get(PATH_PROFILE, expect_json=False)
        soup = BeautifulSoup(html, "html.parser")
        inp = soup.find("input", attrs={"name": "demo_update"})
        return bool(inp and inp.get("value") == "1")
    except Exception:
        # В сомнительных случаях считаем, что НЕ демо, чтобы не вводить в заблуждение
        return False
