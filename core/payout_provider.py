from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from core.intrade_api_async import get_current_percent as _fetch_percent


@dataclass
class _PayoutEntry:
    value: Optional[int] = None
    timestamp: float = 0.0
    in_flight: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_CACHE: Dict[Tuple[str, str, str, str, str], _PayoutEntry] = {}
_DEFAULT_TTL = 1.0


def _build_key(
    investment: float | int | str,
    option: str,
    minutes: int | str,
    account_ccy: str,
    trade_type: str,
) -> Tuple[str, str, str, str, str]:
    return (
        str(option).upper(),
        str(minutes),
        str(account_ccy).upper(),
        str(trade_type).lower(),
        str(investment),
    )


async def _run_fetch(
    key: Tuple[str, str, str, str, str],
    entry: _PayoutEntry,
    client,
    investment: float | int | str,
    option: str,
    minutes: int | str,
    account_ccy: str,
    trade_type: str,
) -> Optional[int]:
    try:
        value = await _fetch_percent(
            client,
            investment=investment,
            option=option,
            minutes=minutes,
            account_ccy=account_ccy,
            trade_type=trade_type,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        value = None

    async with entry.lock:
        entry.value = value
        entry.timestamp = time.monotonic()
        entry.in_flight = None
    return value


async def get_cached_payout(
    client,
    *,
    investment: float | int | str,
    option: str,
    minutes: int | str = 1,
    account_ccy: str = "RUB",
    trade_type: str = "Sprint",
    cache_ttl: float = _DEFAULT_TTL,
) -> Optional[int]:
    """
    Вернуть текущий payout с кешированием между запросами разных ботов.

    Запрос к API выполняется только если данные устарели или отсутствуют.
    Пока никто не запрашивает payout, обращений к серверу нет.
    """

    key = _build_key(investment, option, minutes, account_ccy, trade_type)
    entry = _CACHE.get(key)
    if entry is None:
        entry = _PayoutEntry()
        _CACHE[key] = entry

    async with entry.lock:
        now = time.monotonic()
        if entry.value is not None and (now - entry.timestamp) < max(cache_ttl, 0.0):
            return entry.value

        if entry.in_flight is None:
            entry.in_flight = asyncio.create_task(
                _run_fetch(
                    key,
                    entry,
                    client,
                    investment,
                    option,
                    minutes,
                    account_ccy,
                    trade_type,
                )
            )
        task = entry.in_flight

    return await task
