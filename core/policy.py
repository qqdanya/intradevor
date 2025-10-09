from typing import Tuple, Optional

# Лимиты ставки по валюте счёта
STAKE_LIMITS = {
    "RUB": (100, 50_000),
    "USD": (1, 700),
}
DEFAULT_ACCOUNT_CCY = "RUB"


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
