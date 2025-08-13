import asyncio
from datetime import datetime, timezone
from core.ws_client import last_signals
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def wait_for_signal(symbol):
    while True:
        value = last_signals.get(symbol)
        if value is None:
            print("Не найден сигнал на валюту. Ждем 5 секунд")
            await asyncio.sleep(5)
            continue
        direction, next_time, _ = value
        if direction in (1, 2):
            print(f"{symbol}. Прогноз: {direction}")
            return direction
        now_moscow = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow"))
        wait_seconds = (next_time - now_moscow).total_seconds() + 5
        if wait_seconds > 0:
            print("Нет прогноза")
            await asyncio.sleep(wait_seconds)
        else:
            print("Задержка. Ждем 3 секунды")
            await asyncio.sleep(3)
