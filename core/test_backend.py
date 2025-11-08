"""Test-mode backend that simulates intradebar HTTP API responses."""
from __future__ import annotations

import asyncio
import random
import time
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
    profit: float
    percent: int
    win: bool
    option: str
    direction: int
    created_at: float
    expected_close: float


_FAKE_BALANCE: float = 10_000.0
_FAKE_CCY: str = "USD"
_IS_DEMO: bool = True
_RISK: Tuple[float, float] = (0.0, 0.0)
_TRADES: Dict[str, _Trade] = {}
_RECORDED_PERCENTS: Dict[Tuple[str, str, str], int] = {}
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
    option = str(__.get("option", "")).upper()
    minutes = str(__.get("minutes", "1"))
    trade_type = str(__.get("trade_type", "sprint")).lower()
    percent = random.randint(60, 95)
    async with _LOCK:
        _RECORDED_PERCENTS[(option, minutes, trade_type)] = percent
    return percent


def _minutes_to_seconds(value: float | int | str, trade_type: str) -> float:
    """Convert minutes / HH:MM strings into seconds for bookkeeping."""
    if isinstance(value, (int, float)):
        return max(0.0, float(value) * 60.0)

    text = str(value).strip()
    try:
        return max(0.0, float(text.replace(",", ".")) * 60.0)
    except ValueError:
        pass

    if trade_type == "classic" and ":" in text:
        try:
            hours, minutes = text.split(":", 1)
            total_minutes = int(hours) * 60 + int(minutes)
            return max(0.0, float(total_minutes) * 60.0)
        except (TypeError, ValueError):
            return 60.0

    return 60.0


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
    now = time.time()
    trade_kind = str(__.get("trade_type", "sprint")).lower()
    minutes_value = minutes

    async with _LOCK:
        key = (str(option).upper(), str(minutes_value), trade_kind)
        percent = _RECORDED_PERCENTS.pop(key, None)
        if percent is None:
            percent = random.randint(60, 95)

        win = random.random() < 0.5
        profit = investment_value * (percent / 100.0) if win else -investment_value
        duration = _minutes_to_seconds(minutes_value, trade_kind)
        expected_close = now + duration

        _TRADES[trade_id] = _Trade(
            investment=investment_value,
            profit=profit,
            percent=percent,
            win=win,
            option=str(option),
            direction=int(status),
            created_at=now,
            expected_close=expected_close,
        )

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

    remaining = 0.0
    async with _LOCK:
        trade = _TRADES.get(trade_id)
        if trade is not None:
            remaining = max(0.0, trade.expected_close - time.time())

    if remaining > 0:
        await asyncio.sleep(remaining)

    async with _LOCK:
        trade = _TRADES.pop(trade_id, None)
        global _FAKE_BALANCE
        if trade is None:
            return None

        _FAKE_BALANCE = max(0.0, _FAKE_BALANCE + trade.profit)
        return trade.profit


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
