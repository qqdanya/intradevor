"""WebSocket client responsible for receiving trading signals."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import websockets
from PyQt6.QtWidgets import QApplication, QMessageBox

from core.signal_waiter import push_signal  # без изменения сигнатуры

signal_log_callback = None
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

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
    """Лог с callback."""
    if signal_log_callback:
        signal_log_callback(message)
    else:
        print(message)


@dataclass
class SignalMessage:
    """Нормализованное сообщение сигнала, полученное через WebSocket."""

    symbol: str
    timeframe: str
    direction: Optional[int]
    indicator: str
    timestamp: datetime
    next_timestamp: Optional[datetime] = None


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
    except Exception as e:
        _log(f"[WS] Ошибка парсинга JSON: {e}")
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
    if direction not in (1, 2):
        _log(f"[WS] direction={data.get('direction')} — не поддерживается, пропуск.")
        return None

    try:
        timestamp = datetime.fromisoformat(dt_str).astimezone(MOSCOW_TZ)
    except Exception as e:
        _log(f"[WS] Ошибка парсинга времени: {e}")
        return None

    next_ts = _parse_dt_opt(data.get("next_datetime"))
    if next_ts:
        next_ts = next_ts.astimezone(MOSCOW_TZ)

    return SignalMessage(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        indicator=indicator,
        timestamp=timestamp,
        next_timestamp=next_ts,
    )


async def listen_to_signals() -> None:
    from core.config import get_ws_auth_token, ws_url

    waiting_logged = False
    while True:
        headers = {}
        token = get_ws_auth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with websockets.connect(
                ws_url, ping_interval=None, ping_timeout=None, extra_headers=headers
            ) as websocket:
                waiting_logged = False
                _log("[WS] Подключено к WebSocket-серверу.")
                async for message in websocket:
                    sig = _parse_message(message)
                    if sig is None:
                        continue

                    # Отправляем в ожидатель
                    push_signal(
                        sig.symbol,
                        sig.timeframe,
                        sig.direction,
                        sig.indicator,
                        sig.next_timestamp,
                        sig.timestamp,  # Передаем timestamp
                    )

                    # Лог: время без таймзоны
                    dt_naive = sig.timestamp.astimezone(MOSCOW_TZ).replace(tzinfo=None)
                    msg_dir = {1: "UP", 2: "DOWN", None: "none"}[sig.direction]
                    tail = ""
                    if sig.next_timestamp:
                        n1 = sig.next_timestamp.astimezone(MOSCOW_TZ).strftime("%H:%M")
                        tail = f" | Следующая: {n1}"
                    _log(
                        f"[WS] {sig.symbol} / {sig.timeframe}. Прогноз: {msg_dir} от {sig.indicator}. "
                        f"Свеча: {dt_naive.strftime('%H:%M')}{tail}"
                    )

        except websockets.exceptions.ConnectionClosed as e:
            _log(f"[WS] Соединение закрыто сервером: code={e.code}, reason={e.reason}")
            await asyncio.sleep(3)
        except Exception as e:
            if not isinstance(e, (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError)):
                _log(f"[WS] Ошибка соединения: {type(e).__name__} — {e}")
            if not waiting_logged:
                _log("Ожидание подключения к WebSocket-серверу...")
                waiting_logged = True
            await asyncio.sleep(3)
