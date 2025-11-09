import asyncio
from typing import Tuple, Optional

# Лимиты ставки по валюте счёта
STAKE_LIMITS = {
    "RUB": (100, 50_000),
    "USD": (1, 700),
}
DEFAULT_ACCOUNT_CCY = "RUB"

# Максимальное количество открытых сделок
MAX_OPEN_TRADES = 5

# Текущее количество открытых сделок (общая квота для всех стратегий)
_CURRENT_OPEN_TRADES = 0
_OPEN_TRADES_LOCK: Optional[asyncio.Lock] = None


def stake_range(account_ccy: str) -> Tuple[float, float]:
    lo, hi = STAKE_LIMITS.get(account_ccy, STAKE_LIMITS[DEFAULT_ACCOUNT_CCY])
    return float(lo), float(hi)


def clamp_stake(account_ccy: str, amount: float) -> float:
    lo, hi = stake_range(account_ccy)
    return max(lo, min(hi, float(amount)))


def is_sprint_allowed(symbol: str, minutes: int) -> bool:
    m = int(minutes)
    if symbol == "BTCUSDT":
        return 5 <= m <= 500
    return m == 1 or (3 <= m <= 500)


def normalize_sprint(symbol: str, minutes: int) -> Optional[int]:
    """Вернёт минуту, если допустима; иначе None."""
    m = max(1, min(500, int(minutes)))
    return m if is_sprint_allowed(symbol, m) else None


def _clamp_open_trades(value: int) -> int:
    """Вспомогательная функция для защиты от отрицательных значений."""
    return max(0, int(value))


def _get_open_trades_lock() -> asyncio.Lock:
    """Возвращает (или создаёт) глобальную блокировку для подсчёта сделок."""

    global _OPEN_TRADES_LOCK

    lock = _OPEN_TRADES_LOCK
    if lock is None:
        lock = asyncio.Lock()
        _OPEN_TRADES_LOCK = lock
    return lock


def get_current_open_trades() -> int:
    """Возвращает текущее количество открытых сделок во всей программе."""
    return _CURRENT_OPEN_TRADES


def can_open_new_trade(current_open_trades: Optional[int] = None) -> bool:
    """Проверяет, можно ли открыть новую сделку исходя из общего лимита."""
    if current_open_trades is None:
        current_open_trades = get_current_open_trades()
    return int(current_open_trades) < MAX_OPEN_TRADES


async def try_acquire_trade_slot() -> bool:
    """Пробует зарезервировать слот под новую сделку."""
    global _CURRENT_OPEN_TRADES
    async with _get_open_trades_lock():
        if _CURRENT_OPEN_TRADES >= MAX_OPEN_TRADES:
            return False
        _CURRENT_OPEN_TRADES += 1
        return True


async def release_trade_slot() -> None:
    """Освобождает ранее зарезервированный слот сделки."""
    global _CURRENT_OPEN_TRADES
    async with _get_open_trades_lock():
        _CURRENT_OPEN_TRADES = _clamp_open_trades(_CURRENT_OPEN_TRADES - 1)


def get_max_open_trades() -> int:
    """Возвращает максимальное разрешённое количество открытых сделок."""
    return MAX_OPEN_TRADES
