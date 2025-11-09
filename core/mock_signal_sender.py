"""Utility to emulate trading signals and send them to the local WebSocket server.

This module allows developers to test the desktop client without MetaTrader 4
running.  The original flow is: ``ArrowScanner.mq4`` → ``core/ws_server.py`` →
``core/ws_client.py``.  When the market is closed the first stage is silent, so
this script connects to the server and periodically generates pseudo signals
that imitate the payloads produced by the MQL4 expert advisor.

Run ``python core/mock_signal_sender.py --help`` to see the available options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import websockets

MOSCOW_TZ = timezone(timedelta(hours=3))

DEFAULT_SYMBOLS: tuple[str, ...] = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCAD",
    "AUDUSD",
    "BTCUSD",
)

DEFAULT_TIMEFRAMES: tuple[str, ...] = (
    "M1",
    "M5",
    "M15",
    "M30",
    "H1",
)

DEFAULT_INDICATORS: tuple[str, ...] = (
    "ConnorsRSI",
    "SuperArrows",
    "Randomchik",
)
@dataclass
class Signal:
    symbol: str
    timeframe: str
    direction: int
    indicator: str
    candle_open: str
    detected_time: str
    trade_type: str = "sprint"
    next_candle_open: str | None = None
    next_expire_dt: str | None = None

    def to_json(self) -> str:
        payload = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "indicator": self.indicator,
            "datetime": self.candle_open,
            "detected_time": self.detected_time,
        }
        if self.next_candle_open:
            payload["next_datetime"] = self.next_candle_open
        trade_type = self.trade_type.lower()
        if trade_type in {"classic", "sprint"}:
            payload["trade_type"] = trade_type
        if trade_type == "classic" and self.next_expire_dt:
            payload["_next_expire_dt"] = self.next_expire_dt
        return json.dumps(payload)


def _choice(seq: Sequence[str]) -> str:
    return random.choice(tuple(seq))


def build_signal(
    symbols: Sequence[str],
    timeframes: Sequence[str],
    indicators: Sequence[str],
    *,
    direction_bias: float = 0.5,
    candle_age: int = 0,
    trade_type: str = "sprint",
) -> Signal:
    """Generate a pseudo random signal.

    Parameters
    ----------
    direction_bias:
        Probability of generating direction ``1`` (up).  ``0.0`` is always down,
        ``1.0`` is always up.
    candle_age:
        Offset in minutes from the current candle open time.  ``0`` means the
        candle opened exactly now.  Positive values emulate signals that were
        detected ``candle_age`` minutes after the candle started forming.
    """

    symbol = _choice(symbols)
    timeframe = _choice(timeframes)
    indicator = _choice(indicators)

    direction = 1 if random.random() < direction_bias else 2

    detected_dt = datetime.now(MOSCOW_TZ).replace(microsecond=0)
    candle_dt = detected_dt - timedelta(minutes=candle_age)

    # Calculate the next candle open; classic mode always includes it
    tf_minutes = _timeframe_to_minutes(timeframe)
    next_dt = candle_dt + timedelta(minutes=tf_minutes)
    trade_type_norm = str(trade_type).lower()
    include_next = tf_minutes >= 5 or trade_type_norm == "classic"
    next_candle = next_dt.isoformat() if include_next else None
    next_expire = next_dt.isoformat() if trade_type_norm == "classic" else None

    return Signal(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        indicator=indicator,
        candle_open=candle_dt.isoformat(),
        detected_time=detected_dt.isoformat(),
        trade_type=trade_type_norm,
        next_candle_open=next_candle,
        next_expire_dt=next_expire,
    )


def _timeframe_to_minutes(tf: str) -> int:
    mapping = {
        "M1": 1,
        "M5": 5,
        "M15": 15,
        "M30": 30,
        "H1": 60,
        "H4": 240,
        "D1": 1440,
        "W1": 7 * 1440,
    }
    return mapping.get(tf.upper(), 1)


async def _send_hello(ws, account: str) -> None:
    await ws.send(json.dumps({"type": "hello", "account": account}))


async def signal_loop(
    ws_url: str,
    *,
    account: str,
    interval: float,
    symbols: Sequence[str],
    timeframes: Sequence[str],
    indicators: Sequence[str],
    direction_bias: float,
    candle_age: int,
    trade_type: str,
    once: bool = False,
) -> None:
    """Connect to the WS server and repeatedly send test signals."""

    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=None, ping_timeout=None
            ) as ws:
                print(f"[mock] connected to {ws_url}")
                await _send_hello(ws, account)

                while True:
                    sig = build_signal(
                        symbols,
                        timeframes,
                        indicators,
                        direction_bias=direction_bias,
                        candle_age=candle_age,
                        trade_type=trade_type,
                    )
                    await ws.send(sig.to_json())
                    print(
                        "[mock] sent",
                        sig.symbol,
                        sig.timeframe,
                        "UP" if sig.direction == 1 else "DOWN",
                        sig.indicator,
                        f"[{sig.trade_type}]",
                    )
                    if once:
                        return
                    await asyncio.sleep(interval)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[mock] connection error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(3.0)


def _comma_split(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Отправляет тестовые сигналы на WebSocket-сервер, эмулируя работу "
            "ArrowScanner.mq4."
        )
    )
    parser.add_argument(
        "--ws-url",
        default="ws://127.0.0.1:8080",
        help="Адрес WebSocket-сервера (по умолчанию ws://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--account",
        default="1000000",
        help="Номер аккаунта, который будет отправлен в hello-пакете.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Интервал между сигналами в секундах.",
    )
    parser.add_argument(
        "--symbols",
        type=_comma_split,
        default=list(DEFAULT_SYMBOLS),
        help="Список символов через запятую (по умолчанию набор популярных пар).",
    )
    parser.add_argument(
        "--timeframes",
        type=_comma_split,
        default=list(DEFAULT_TIMEFRAMES),
        help="Список таймфреймов через запятую (по умолчанию M1,M5,M15,M30,H1).",
    )
    parser.add_argument(
        "--indicators",
        type=_comma_split,
        default=list(DEFAULT_INDICATORS),
        help="Список индикаторов через запятую (по умолчанию ConnorsRSI, SuperArrows, Randomchik).",
    )
    parser.add_argument(
        "--direction-bias",
        type=float,
        default=0.5,
        help="Вероятность отправки сигнала на покупку (0..1).",
    )
    parser.add_argument(
        "--candle-age",
        type=int,
        default=0,
        help="Задержка между открытием свечи и сигналом в минутах.",
    )
    parser.add_argument(
        "--trade-type",
        choices=["sprint", "classic"],
        default="sprint",
        help="Тип торговли, который нужно эмулировать (sprint или classic).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Отправить только один сигнал и завершиться.",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(argv)

    asyncio.run(
        signal_loop(
            args.ws_url,
            account=args.account,
            interval=max(args.interval, 0.1),
            symbols=args.symbols or list(DEFAULT_SYMBOLS),
            timeframes=args.timeframes or list(DEFAULT_TIMEFRAMES),
            indicators=args.indicators or list(DEFAULT_INDICATORS),
            direction_bias=min(max(args.direction_bias, 0.0), 1.0),
            candle_age=max(args.candle_age, 0),
            trade_type=args.trade_type,
            once=args.once,
        )
    )


if __name__ == "__main__":
    main()
