import sys
import json
import asyncio
import websockets
from PyQt6.QtWidgets import QApplication, QMessageBox
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

last_signals = {}
signal_log_callback = None

timeframe_map = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
    "W1": timedelta(weeks=1),
}


def show_connection_error(error_text: str):
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    QMessageBox.critical(
        None,
        "Ошибка подключения",
        f"Не удалось подключиться к серверу WebSocket:\n{error_text}",
    )
    raise SystemExit(1)


async def listen_to_signals():
    uri = "ws://192.168.56.101:8080"
    try:
        async with websockets.connect(uri) as websocket:
            print("[WS] Подключено к WebSocket-серверу.")
            async for message in websocket:
                try:
                    data = json.loads(message)

                    symbol = data.get("symbol", "").upper()
                    direction = data.get("direction", "")
                    timeframe = data.get("timeframe", "")
                    date_time_str = data.get("datetime", "")

                    if (
                        symbol
                        and direction in (0, 1, 2)
                        and timeframe
                        and date_time_str
                    ):
                        date_time = datetime.fromisoformat(date_time_str).replace(
                            tzinfo=ZoneInfo("Europe/Moscow")
                        )
                        next_time = date_time + timeframe_map[timeframe]
                        last_signals[symbol] = (
                            direction,
                            next_time,
                            timeframe_map[timeframe],
                        )
                        msg = f"[WS] {symbol} / Свеча закончилась. Прогноз: {direction}. Следующая проверка {next_time}"
                        if signal_log_callback:
                            signal_log_callback(msg)

                except json.JSONDecodeError:
                    print("[WS] Ошибка парсинга сигнала.")
    except Exception as e:
        show_connection_error(str(e))
