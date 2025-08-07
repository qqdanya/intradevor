import json
import websockets

last_signals = {}

async def listen_to_signals():
    uri = "ws://localhost:8080"
    async with websockets.connect(uri) as websocket:
        print("[WS] Подключено к WebSocket-серверу.")
        async for message in websocket:
            try:
                data = json.loads(message)
                symbol = data.get("symbol", "").upper()
                direction = data.get("direction", "none")
                seconds_to_next = data.get("seconds_to_next", 60)
                if symbol:
                    last_signals[symbol] = (direction, seconds_to_next)
                    print(f"[WS] Новый сигнал: {symbol} → {direction} ({seconds_to_next}s до новой свечи)")
            except json.JSONDecodeError:
                print("[WS] Ошибка парсинга сигнала.")

