import asyncio
import json
import websockets
from datetime import datetime

HOST = "0.0.0.0"
PORT = 8080
connected = set()

DIRECTIONS = {0: "none", 1: "up", 2: "down", 3: "both"}


def log(msg: str) -> None:
    """Печатает сообщение с меткой времени."""
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] {msg}")


def parse_iso(s: str):
    """Безопасный парсер ISO-времени."""
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


async def handle(ws):
    """Обрабатывает подключение одного клиента."""
    ip, port = ws.remote_address
    connected.add(ws)
    log(f"[+] Клиент подключился: {ip}:{port}")

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception as e:
                log(f"[!] Ошибка JSON: {e} | raw={raw[:100]}")
                continue

            # === HELLO ===
            if data.get("type") == "hello":
                acc = data.get("account", "?")
                log(f"[HELLO] account={acc}")
                await ws.send('{"type":"pong"}')
                continue

            # === PING ===
            if data.get("type") == "ping":
                await ws.send('{"type":"pong"}')
                log(f"[PONG→] {ip}:{port} (ответ на ping)")
                continue

            # === SIGNAL ===
            if "symbol" in data and "direction" in data:
                sym = data.get("symbol", "N/A")
                tf = data.get("timeframe", "N/A")
                ind = data.get("indicator", "N/A")
                dir_code = int(data.get("direction", 0))
                direction = DIRECTIONS.get(dir_code, f"unk({dir_code})")

                # задержка
                bar_time = parse_iso(data.get("datetime"))
                detect_time = parse_iso(data.get("detected_time"))
                delay = None
                if bar_time and detect_time:
                    delay = (detect_time - bar_time).total_seconds()

                delay_str = f" | задержка={delay:.1f}с" if delay else ""
                log(f"[SIGNAL] {sym} {tf} → {direction} | ind={ind}{delay_str}")

                # Рассылка другим клиентам
                for c in list(connected):
                    if c is ws:
                        continue
                    try:
                        await asyncio.wait_for(c.send(raw), timeout=1.0)
                    except Exception:
                        connected.discard(c)

    except websockets.exceptions.ConnectionClosed as e:
        log(f"[-] Клиент отключился: {ip}:{port} | code={e.code} reason={e.reason or ''}")
    except Exception as e:
        log(f"[!] Ошибка в обработчике ({ip}:{port}): {type(e).__name__} — {e}")
    finally:
        connected.discard(ws)
        log(f"[i] Клиент отключен ({ip}:{port})")


async def main():
    log(f"WebSocket-сервер запущен на ws://{HOST}:{PORT}")

    async with websockets.serve(
        handle,
        HOST,
        PORT,
        compression=None,
        ping_interval=None,  # отключаем авто-ping
        ping_timeout=None,
        max_size=2**20,
    ):
        # держим сервер активным и чистим старые подключения
        while True:
            for c in list(connected):
                # Новая проверка для websockets 13+
                if c.state.name != "OPEN":
                    connected.discard(c)
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except OSError as e:
        if e.errno == 10048:
            alt = PORT + 1
            log(f"[!] Порт {PORT} занят, пробуем ws://{HOST}:{alt}")
            asyncio.run(websockets.serve(handle, HOST, alt))
        else:
            raise
