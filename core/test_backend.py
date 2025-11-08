"""Test-mode backend that simulates intradebar HTTP API responses."""
from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

__all__ = [
    "TEST_USER_ID",
    "TEST_USER_HASH",
    "initial_cookies",
    "get_balance_info",
    "get_current_percent",
    "place_trade",
    "check_trade_result",
    "change_currency",
    "set_risk",
    "toggle_real_demo",
    "is_demo_account",
]

TEST_USER_ID = "test_user"
TEST_USER_HASH = "test_hash"

_SYMBOLS = {
    "RUB": "₽",
    "USD": "$",
}

@dataclass
class _Trade:
    investment: float
    win: bool
    payout: float


_FAKE_BALANCE: float = 10_000.0
_FAKE_CCY: str = "USD"
_IS_DEMO: bool = True
_RISK: Tuple[float, float] = (0.0, 0.0)
_TRADES: Dict[str, _Trade] = {}
_LOCK = asyncio.Lock()


def initial_cookies() -> Dict[str, str]:
    """Return a minimal cookie set recognised by the app."""
    return {"user_id": TEST_USER_ID, "user_hash": TEST_USER_HASH}


def _format_display(amount: float, currency: str) -> str:
    display = f"{amount:,.2f}".replace(",", " ")
    symbol = _SYMBOLS.get(currency.upper(), currency.upper())
    return f"{display} {symbol}".strip()


async def get_balance_info(*_: object, **__: object) -> Tuple[float, str, str]:
    """Return the simulated balance, currency and display string."""
    async with _LOCK:
        return _FAKE_BALANCE, _FAKE_CCY, _format_display(_FAKE_BALANCE, _FAKE_CCY)


async def get_current_percent(*_: object, **__: object) -> Optional[int]:
    """Return a random payout percent between 60 and 95."""
    return random.randint(60, 95)


async def place_trade(
    *_: object,
    investment: float | int | str,
    option: str,
    status: int,
    minutes: float | int | str,
    on_log=None,
    **__: object,
) -> Optional[str]:
    """Register a simulated trade and return its ID."""
    try:
        investment_value = float(investment)
    except (TypeError, ValueError):
        if on_log:
            on_log(f"[{option}] ❌ Некорректная сумма ставки: {investment}")
        return None

    trade_id = str(uuid.uuid4())
    win = random.random() < 0.5
    payout = random.uniform(0.65, 0.9)

    async with _LOCK:
        _TRADES[trade_id] = _Trade(investment=investment_value, win=win, payout=payout)

    if on_log:
        on_log(
            f"[{option}] 🧪 Тестовая сделка создана (статус={status}, время={minutes}). "
            f"ID: {trade_id}"
        )

    return trade_id


async def check_trade_result(
    *_: object,
    trade_id: str,
    wait_time: float = 60.0,
    **__: object,
) -> Optional[float]:
    """Return the simulated profit/loss for the given trade."""
    await asyncio.sleep(wait_time)

    async with _LOCK:
        trade = _TRADES.pop(trade_id, None)
        global _FAKE_BALANCE
        if trade is None:
            return None

        profit = trade.investment * trade.payout if trade.win else -trade.investment
        _FAKE_BALANCE = max(0.0, _FAKE_BALANCE + profit)
        return profit


async def change_currency(*_: object, **__: object) -> bool:
    """Toggle the simulated account currency between RUB and USD."""
    async with _LOCK:
        global _FAKE_CCY
        _FAKE_CCY = "RUB" if _FAKE_CCY == "USD" else "USD"
    return True


async def set_risk(*_: object, risk_manage_min: float, risk_manage_max: float, **__: object) -> bool:
    """Store risk settings for completeness."""
    async with _LOCK:
        global _RISK
        _RISK = (float(risk_manage_min), float(risk_manage_max))
    return True


async def toggle_real_demo(*_: object, **__: object) -> bool:
    """Invert demo/real flag."""
    async with _LOCK:
        global _IS_DEMO
        _IS_DEMO = not _IS_DEMO
    return True


async def is_demo_account(*_: object, **__: object) -> bool:
    """Return whether the simulated account is in demo mode."""
    async with _LOCK:
        return _IS_DEMO
