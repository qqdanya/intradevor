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

from core.signal_waiter import push_signal  # без изменения сигнатуры

signal_log_callback = None

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
    if signal_log_callback:
        signal_log_callback(message)


def show_connection_error(error_text: str) -> None:
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
    timestamp: datetime  # время свечи с сигналом (закрытая)
    next_timestamp: Optional[datetime] = None  # начало следующей ПОСЛЕ текущей
    next2_timestamp: Optional[datetime] = None  # ещё через одну


def _parse_direction(raw_dir: Any) -> Optional[int]:
    if isinstance(raw_dir, int):
        return raw_dir if raw_dir in (1, 2) else None
    if isinstance(raw_dir, str):
        d = raw_dir.strip().lower()
        if d in ("1", "up", "buy", "long"):
            return 1
        if d in ("2", "down", "sell", "short"):
            return 2
    return None


def _parse_dt_opt(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def _parse_message(message: str) -> Optional[SignalMessage]:
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

    next_ts = _parse_dt_opt(data.get("next_datetime"))
    next2_ts = _parse_dt_opt(data.get("next2_datetime"))

    return SignalMessage(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        indicator=indicator,
        timestamp=timestamp,
        next_timestamp=next_ts,
        next2_timestamp=next2_ts,
    )


async def listen_to_signals() -> None:
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

                    # Отправляем в ожидатель — сигнатура без изменений
                    push_signal(sig.symbol, sig.timeframe, sig.direction, sig.indicator)

                    # Лог: время без таймзоны (локально-наивное)
                    dt_naive = sig.timestamp.replace(tzinfo=None)
                    msg_dir = {1: "UP", 2: "DOWN", None: "none"}[sig.direction]
                    tail = ""
                    if sig.next_timestamp and sig.next2_timestamp:
                        n1 = sig.next_timestamp.replace(tzinfo=None).strftime("%H:%M")
                        n2 = sig.next2_timestamp.replace(tzinfo=None).strftime("%H:%M")
                        tail = f" | next: {n1}, next2: {n2}"
                    _log(
                        f"[WS] {sig.symbol} / {sig.timeframe}. Прогноз: {msg_dir} от {sig.indicator}. "
                        f"Время свечи: {dt_naive.strftime('%H:%M')}{tail}"
                    )
        except Exception as e:
            delay = min(30, 2 ** min(retry, 5))
            _log(f"[WS] Потеря соединения: {e}. Повтор через {delay}c")
            await asyncio.sleep(delay)
            retry += 1
