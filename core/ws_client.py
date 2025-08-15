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
    from core.config import ws_url

    retry = 0
    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=10
            ) as websocket:
                retry = 0
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
                            td = timeframe_map.get(timeframe)
                            if td is None:
                                if signal_log_callback:
                                    signal_log_callback(
                                        f"[WS] Неизвестный таймфрейм '{timeframe}' — игнор."
                                    )
                                continue
                            date_time = datetime.fromisoformat(date_time_str)
                            next_time = date_time + td
                            last_signals[(symbol, timeframe)] = (
                                direction,
                                next_time,
                                td,
                            )
                            msg = f"[WS] {symbol} / {timeframe}. Прогноз: {direction}. Следующая проверка {next_time}"
                            if signal_log_callback:
                                signal_log_callback(msg)

                    except json.JSONDecodeError:
                        print("[WS] Ошибка парсинга сигнала.")
        except Exception as e:
            delay = min(30, 2 ** min(retry, 5))
            if signal_log_callback:
                signal_log_callback(
                    f"[WS] Потеря соединения: {e}. Повтор через {delay}s"
                )
            await asyncio.sleep(delay)
            retry += 1
