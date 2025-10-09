import asyncio
import json
import websockets
from datetime import datetime

HOST = "0.0.0.0"
PORT = 8080

connected = set()

DIRECTIONS = {
    0: "none",
    1: "up",
    2: "down",
    3: "both",
}


def log(message: str) -> None:
    """Печать с текущей датой и временем."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    print(f"[{now}] {message}")


def parse_time(s: str):
    """Безопасный парсер ISO-времени."""
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


async def handle(ws):
    # Очистим старые мертвые соединения (без использования .closed)
    for c in list(connected):
        try:
            if not c.open:
                connected.discard(c)
        except Exception:
            connected.discard(c)

    connected.add(ws)
    ip, port = ws.remote_address
    log(f"[+] Клиент подключился: {ip}:{port}")

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception as e:
                log(f"[!] Ошибка JSON: {e} | raw={raw}")
                continue

            # === HELLO от эксперта ===
            if data.get("type") == "hello":
                acc = data.get("account", "?")
                log(f"[HELLO] account={acc}")
                await ws.send('{"type":"pong"}')
                continue

            # === Ответ на пинг от эксперта ===
            if data.get("type") == "ping":
                await ws.send('{"type":"pong"}')
                continue

            # === Сигнал от эксперта ===
            if "symbol" in data and "direction" in data:
                sym = data.get("symbol", "N/A")
                tf = data.get("timeframe", "N/A")
                ind = data.get("indicator", "N/A")
                dir_code = data.get("direction", 0)
                direction = DIRECTIONS.get(dir_code, f"unk({dir_code})")

                log(f"[SIGNAL] {sym} {tf} → {direction} | ind={ind}")

            # === Рассылаем сигнал всем остальным клиентам ===
            for c in list(connected):
                if c is ws:
                    continue
                try:
                    await asyncio.wait_for(c.send(raw), timeout=1.0)
                except Exception as e:
                    log(f"[!] Ошибка отправки клиенту {c.remote_address}: {e}")
                    connected.discard(c)

    except websockets.exceptions.ConnectionClosed as e:
        ip, port = ws.remote_address
        log(f"[-] Клиент отключился: {ip}:{port} | code={e.code} reason={e.reason}")
    except Exception as e:
        log(f"[!] Ошибка в обработчике: {type(e).__name__} — {e}")
    finally:
        connected.discard(ws)
        log(f"[i] Клиент отключен ({ip}:{port})")


async def main():
    log(f"WebSocket-сервер запущен на ws://{HOST}:{PORT}")
    try:
        async with websockets.serve(
            handle,
            HOST,
            PORT,
            compression=None,    # отключаем permessage-deflate
            ping_interval=None,  # MT4 сам шлёт heartbeats
            ping_timeout=None,
        ):
            await asyncio.Future()  # сервер работает вечно
    except OSError as e:
        # Порт занят
        if e.errno == 10048:
            alt_port = PORT + 1
            log(f"[!] Порт {PORT} занят, пробуем ws://{HOST}:{alt_port}")
            async with websockets.serve(
                handle,
                HOST,
                alt_port,
                compression=None,
                ping_interval=None,
                ping_timeout=None,
            ):
                await asyncio.Future()
        else:
            raise


if __name__ == "__main__":
    asyncio.run(main())
