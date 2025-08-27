"""WebSocket client responsible for receiving trading signals."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import websockets
from PyQt6.QtWidgets import QApplication, QMessageBox

from core.signal_waiter import push_signal  # <-- добавили


signal_log_callback = None


# Поддерживаемые таймфреймы. Значения не используются напрямую, важен только ключ.
_TIMEFRAME_MAP = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,
}


def _log(message: str) -> None:
    """Отправить сообщение через колбэк лога, если он установлен."""
    if signal_log_callback:
        signal_log_callback(message)


def show_connection_error(error_text: str) -> None:
    """Показать диалог с ошибкой подключения и завершить приложение."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    QMessageBox.critical(
        None,
        "Ошибка подключения",
        f"Не удалось подключиться к серверу WebSocket:\n{error_text}",
    )
    raise SystemExit(1)


@dataclass
class SignalMessage:
    """Нормализованное сообщение сигнала, полученное через WebSocket."""

    symbol: str
    timeframe: str
    direction: Optional[int]
    indicator: str
    timestamp: datetime


def _parse_direction(raw_dir: Any) -> Optional[int]:
    """Привести поле direction к 1, 2 или None."""
    if isinstance(raw_dir, int):
        return raw_dir if raw_dir in (1, 2) else None
    if isinstance(raw_dir, str):
        d = raw_dir.strip().lower()
        if d in ("1", "up", "buy", "long"):
            return 1
        if d in ("2", "down", "sell", "short"):
            return 2
    return None


def _parse_message(message: str) -> Optional[SignalMessage]:
    """Распарсить JSON-сообщение в структуру SignalMessage."""
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        print("[WS] Ошибка парсинга сигнала.")
        return None

    symbol = (data.get("symbol") or "").upper()
    timeframe = (data.get("timeframe") or "").upper()
    dt_str = data.get("datetime", "")
    indicator = data.get("indicator", "")

    if not (symbol and timeframe and dt_str):
        return None
    if timeframe not in _TIMEFRAME_MAP:
        _log(f"[WS] Неизвестный таймфрейм '{timeframe}' — игнор.")
        return None

    direction = _parse_direction(data.get("direction"))
    timestamp = datetime.fromisoformat(dt_str)
    return SignalMessage(symbol, timeframe, direction, indicator, timestamp)


async def listen_to_signals() -> None:
    """Подключиться к серверу и слушать входящие сигналы."""

    from core.config import ws_url

    retry = 0
    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=10
            ) as websocket:
                retry = 0
                _log("[WS] Подключено к WebSocket-серверу.")
                async for message in websocket:
                    sig = _parse_message(message)
                    if sig is None:
                        continue

                    # ВАЖНО: отправляем в ожидатель ВСЕ сообщения (включая none)
                    push_signal(sig.symbol, sig.timeframe, sig.direction, sig.indicator)

                    dt_naive = sig.timestamp.replace(tzinfo=None)
                    msg_dir = {1: "UP", 2: "DOWN", None: "none"}[sig.direction]
                    if msg_dir != "none":
                        _log(
                            f"[WS] {sig.symbol} / {sig.timeframe}. Прогноз: {msg_dir} от {sig.indicator}. Время свечи: {dt_naive.strftime('%H:%M')}"
                        )
        except Exception as e:
            delay = min(30, 2 ** min(retry, 5))
            _log(f"[WS] Потеря соединения: {e}. Повтор через {delay}c")
            await asyncio.sleep(delay)
            retry += 1
