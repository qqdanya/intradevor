import sys
import json
import asyncio
import websockets
from PyQt6.QtWidgets import QApplication, QMessageBox
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.signal_waiter import push_signal  # <-- добавили

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
                signal_log_callback("[WS] Подключено к WebSocket-серверу.")
                async for message in websocket:
                    try:
                        data = json.loads(message)

                        symbol = (data.get("symbol") or "").upper()
                        timeframe = (data.get("timeframe") or "").upper()
                        date_time_str = data.get("datetime", "")

                        # direction может быть 0/1/2 или строкой 'none' / 'up' / 'down'
                        raw_dir = data.get("direction", None)
                        indicator = data.get("indicator", "")

                        # нормализуем в {1,2,None}
                        direction = None
                        if isinstance(raw_dir, int):
                            if raw_dir in (1, 2):
                                direction = raw_dir
                            else:
                                direction = None  # 0 или любой другой => none / очистка
                        elif isinstance(raw_dir, str):
                            d = raw_dir.strip().lower()
                            if d in ("1", "up", "buy", "long"):
                                direction = 1
                            elif d in ("2", "down", "sell", "short"):
                                direction = 2
                            else:
                                direction = None  # 'none' и т.п.

                        if symbol and timeframe and date_time_str:
                            td = timeframe_map.get(timeframe)
                            if td is None:
                                if signal_log_callback:
                                    signal_log_callback(
                                        f"[WS] Неизвестный таймфрейм '{timeframe}' — игнор."
                                    )
                                continue

                            # ВАЖНО: отправляем в ожидатель ВСЕ сообщения (включая none)
                            push_signal(symbol, timeframe, direction, indicator)

                            # просто лог (для наглядности)
                            dt = datetime.fromisoformat(date_time_str)
                            dt_naive = dt.replace(tzinfo=None)
                            msg_dir = {1: "UP", 2: "DOWN", None: "none"}[direction]
                            if signal_log_callback:
                                signal_log_callback(
                                    f"[WS] {symbol} / {timeframe}. Прогноз: {msg_dir} от {indicator}. Время свечи: {dt_naive.strftime('%H:%M')}"
                                )

                    except json.JSONDecodeError:
                        print("[WS] Ошибка парсинга сигнала.")
        except Exception as e:
            delay = min(30, 2 ** min(retry, 5))
            if signal_log_callback:
                signal_log_callback(
                    f"[WS] Потеря соединения: {e}. Повтор через {delay}c"
                )
            await asyncio.sleep(delay)
            retry += 1
