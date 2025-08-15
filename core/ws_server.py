import asyncio
import websockets
import json
from datetime import datetime

HOST = "0.0.0.0"
PORT = 8080

connected_clients = set()


async def handle_connection(websocket):
    # Регистрируем клиента
    connected_clients.add(websocket)
    print(f"[+] Клиент подключился: {websocket.remote_address}")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)

                symbol = data.get("symbol", "N/A")
                timeframe = data.get("timeframe", "N/A")
                direction_code = data.get("direction", 0)
                datetime_str = data.get("datetime", "")

                # Преобразуем строку времени в объект datetime для вывода (если валидно)
                try:
                    bar_time = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
                    bar_time_str = bar_time.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    bar_time_str = datetime_str

                # Преобразуем направление в строку для читаемости
                direction_map = {0: "none", 1: "up", 2: "down", 3: "both"}
                direction = direction_map.get(direction_code, "unknown")

                print(f"📈 [{symbol} {timeframe}] → {direction} | time: {bar_time_str}")

                # Рассылаем сообщение всем остальным клиентам
                disconnected = set()
                for client in connected_clients:
                    if client != websocket:
                        try:
                            await client.send(message)
                        except Exception as e:
                            print(
                                f"[!] Ошибка при отправке клиенту {client.remote_address}: {e}"
                            )
                            disconnected.add(client)

                for dc in disconnected:
                    connected_clients.remove(dc)

            except json.JSONDecodeError:
                print("[!] Ошибка JSON:", message)

    except websockets.exceptions.ConnectionClosed:
        print(f"[-] Клиент отключился: {websocket.remote_address}")

    finally:
        # Убираем клиента при отключении
        connected_clients.discard(websocket)


async def run_server():
    print(f"🚀 WebSocket-сервер запущен на ws://{HOST}:{PORT}")
    async with websockets.serve(handle_connection, HOST, PORT):
        await asyncio.Future()  # Ожидание навсегда


if __name__ == "__main__":
    asyncio.run(run_server())
