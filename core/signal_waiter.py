# core/signal_waiter.py
import asyncio
from datetime import datetime, timezone
from core.ws_client import last_signals
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def wait_for_signal(symbol: str, timeframe: str, *, check_pause=None):
    """Тихое ожидание сигнала 1/2 для конкретного (symbol, timeframe)."""

    def _paused():
        return bool(check_pause and check_pause())

    async def _wait_with_pause(total_sec: float):
        remaining = float(total_sec)
        while remaining > 0:
            if _paused():
                await asyncio.sleep(0.1)
                continue
            step = min(0.2, remaining)
            await asyncio.sleep(step)
            remaining -= step

    while True:
        if _paused():
            await asyncio.sleep(0.1)
            continue

        value = last_signals.get((symbol, timeframe))
        if value is None:
            await _wait_with_pause(5.0)
            continue

        direction, next_time, _tdelta = value
        if direction in (1, 2):
            return direction

        now_moscow = datetime.now(timezone.utc).astimezone(MOSCOW_TZ)
        wait_seconds = max(0.0, (next_time - now_moscow).total_seconds() + 5)
        await _wait_with_pause(wait_seconds)
