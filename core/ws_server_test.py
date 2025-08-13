import asyncio
import json
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import websockets

HOST = "0.0.0.0"  # чтобы клиенты в сети могли подключаться
PORT = 8080  # совпадает с core/ws_client.py
SYMBOL = "BTCUSDT"
TIMEFRAME = "M1"

# Вероятности направления: 0=нет стрелки, 1=up, 2=down
P_NONE, P_UP, P_DOWN = 0.70, 0.15, 0.15

MOSCOW = ZoneInfo("Europe/Moscow")
clients = set()


def minute_floor(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def next_minute_boundary(dt: datetime) -> datetime:
    floored = minute_floor(dt)
    return floored + timedelta(minutes=1)


def sample_direction() -> int:
    r = random.random()
    if r < P_NONE:
        return 0
    elif r < P_NONE + P_UP:
        return 1
    else:
        return 2


def build_message(now_ms: datetime) -> str:
    """
    Формат ПОД ws_client.py:
      {
        "symbol": "EURUSD",
        "direction": 0|1|2,
        "timeframe": "M1",
        "datetime": "YYYY-MM-DD HH:MM:SS"   # ws_client делает fromisoformat(...)
      }
    Время — конец только что закрывшейся свечи по Москве.
    """
    candle_close_time = minute_floor(now_ms)  # только что закрытая минута
    payload = {
        "symbol": SYMBOL,
        "direction": sample_direction(),
        "timeframe": TIMEFRAME,
        "datetime": candle_close_time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return json.dumps(payload, ensure_ascii=False)


async def handler(ws):
    clients.add(ws)
    print(
        f"[+] Клиент подключился: {getattr(ws, 'remote_address', None)} (всего {len(clients)})"
    )
    try:
        # Эхо не требуется, просто держим соединение открытым
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)
        print(
            f"[-] Клиент отключился: {getattr(ws, 'remote_address', None)} (всего {len(clients)})"
        )


async def broadcaster():
    # Дождёмся ближайшего старта минуты по московскому времени
    now_ms = datetime.now(MOSCOW)
    target = next_minute_boundary(now_ms)
    print(f"[i] Ждём до начала минуты: {target.strftime('%H:%M:%S')} (MSK)")
    while True:
        # Спим «мелкими шагами», чтобы ловить Ctrl+C
        now_ms = datetime.now(MOSCOW)
        dt = (target - now_ms).total_seconds()
        if dt <= 0:
            break
        await asyncio.sleep(min(0.5, dt))

    while True:
        now_ms = datetime.now(MOSCOW)
        msg = build_message(now_ms)
        # Разошлём всем подключённым клиентам
        if clients:
            print(
                f"[→] {SYMBOL} {TIMEFRAME} @ {minute_floor(now_ms).strftime('%Y-%m-%d %H:%M:%S')} MSK → broadcast {len(clients)} клиентам"
            )
            await asyncio.gather(
                *(c.send(msg) for c in list(clients)), return_exceptions=True
            )
        else:
            print("[→] Нет клиентов. Сигнал сгенерирован, но некому отправлять.")
        # Следующая граница минуты
        target = next_minute_boundary(datetime.now(MOSCOW))
        # Спим до неё
        while True:
            now_ms = datetime.now(MOSCOW)
            dt = (target - now_ms).total_seconds()
            if dt <= 0:
                break
            await asyncio.sleep(min(0.5, dt))


async def main():
    async with websockets.serve(handler, HOST, PORT):
        print(f"🚀 Тестовый WebSocket-сервер сигналов на ws://{HOST}:{PORT}")
        await broadcaster()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Остановка по Ctrl+C")
