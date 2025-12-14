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
    # remote_address может быть None в некоторых версиях
    try:
        ip, port = ws.remote_address
    except Exception:
        ip, port = "?", "?"

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
                continue

            # === SIGNAL ===
            if "symbol" in data and "direction" in data:
                sym = data.get("symbol", "N/A")
                tf = data.get("timeframe", "N/A")
                ind = data.get("indicator", "N/A")
                dir_code = int(data.get("direction", 0))
                direction = DIRECTIONS.get(dir_code, f"unk({dir_code})")

                # задержка
                bar_time = parse_iso(data.get("close_datetime") or data.get("datetime"))
                detect_time = parse_iso(data.get("detected_time"))
                delay = None
                if bar_time and detect_time:
                    delay = (detect_time - bar_time).total_seconds()
                    if delay < 0:
                        delay = None

                delay_str = f" | задержка={delay:.1f}с" if delay is not None else ""
                log(f"[SIGNAL] {sym} {tf} → {direction} | ind={ind}{delay_str}")

                # Рассылка другим клиентам
                for c in list(connected):
                    if c is ws:
                        continue
                    try:
                        await asyncio.wait_for(c.send(raw), timeout=1.0)
                    except Exception as e:
                        log(f"[!] Ошибка отправки клиенту: {e}")
                        connected.discard(c)

            else:
                log(f"[i] Получен неизвестный пакет: {data}")

    except websockets.exceptions.ConnectionClosed as e:
        log(f"[-] Клиент отключился: {ip}:{port} | code={e.code} reason={e.reason or ''}")
    except Exception as e:
        log(f"[!] Ошибка в обработчике ({ip}:{port}): {type(e).__name__} — {e}")
    finally:
        connected.discard(ws)
        log(f"[i] Клиент отключен ({ip}:{port})")


async def run_server(port: int):
    log(f"WebSocket-сервер запущен на ws://{HOST}:{port}")

    async with websockets.serve(
        handle,
        HOST,
        port,
        compression=None,
        ping_interval=None,
        ping_timeout=10,
        max_size=2**20,
    ):
        # просто "спим" бесконечно, пока процесс жив
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(run_server(PORT))
    except OSError as e:
        # Windows: 10048, Linux: обычно 98 (EADDRINUSE)
        if e.errno in (98, 10048):
            alt = PORT + 1
            log(f"[!] Порт {PORT} занят, пробуем ws://{HOST}:{alt}")
            asyncio.run(run_server(alt))
        else:
            raise
