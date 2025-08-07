import asyncio
import websockets
import json
from datetime import datetime, timezone

HOST = "localhost"
PORT = 8080

# В новых версиях websockets (>=12.0) убрали аргумент `path`
async def handle_connection(websocket):
    print(f"[+] Клиент подключился: {websocket.remote_address}")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)

                symbol     = data.get("symbol", "N/A")
                timeframe  = data.get("timeframe", "N/A")
                direction  = data.get("direction", "none")
                buffer0    = data.get("buffer0", 0)
                buffer1    = data.get("buffer1", 0)
                bar_time   = data.get("bar_time", 0)
                seconds_to_next = data.get("seconds_to_next", 0)

                bar_time_str = datetime.fromtimestamp(bar_time, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

                print(f"📈 [{symbol} {timeframe}] → {direction} | "
                      f"buffer0: {buffer0} | buffer1: {buffer1} | "
                      f"time: {bar_time_str} | next in {seconds_to_next}s")

            except json.JSONDecodeError:
                print("[!] Ошибка JSON:", message)
    except websockets.exceptions.ConnectionClosed:
        print(f"[-] Клиент отключился: {websocket.remote_address}")

# Новый способ запуска в websockets >=12
async def run_server():
    print(f"🚀 WebSocket-сервер запущен на ws://{HOST}:{PORT}")
    async with websockets.serve(handle_connection, HOST, PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(run_server())

