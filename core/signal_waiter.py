# core/signal_waiter.py
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional, Tuple, Dict, Callable, Awaitable
from datetime import datetime

# ======================================================================
# helpers
# ======================================================================

def _tf_to_seconds(tf: str) -> Optional[int]:
    """Преобразует строку таймфрейма (M1, H1, D1...) в количество секунд."""
    if not tf:
        return None
    tf = str(tf).upper().strip()
    unit = tf[0]
    try:
        n = int(tf[1:])
    except Exception:
        return None
    if n <= 0:
        return None

    if unit == "M":
        return n * 60
    if unit == "H":
        return n * 3600
    if unit == "D":
        return n * 86400
    if unit == "W":
        return n * 604800
    return None


# ======================================================================
# state
# ======================================================================

@dataclass
class _State:
    value: Optional[int] = None  # 1=up, 2=down, None=нет сигнала
    version: int = 0             # монотонная версия (эпоха) для каждой пары
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    last_monotonic: Optional[float] = None  # момент последнего сигнала
    tf_sec: Optional[int] = None            # длительность TF в секундах
    last_indicator: Optional[str] = None    # имя источника последнего сигнала
    last_symbol: Optional[str] = None       # последний символ
    last_timeframe: Optional[str] = None    # последний TF
    next_timestamp: Optional[datetime] = None  # начало следующей свечи
    timestamp: Optional[datetime] = None       # время текущей свечи сигнала


_states: Dict[tuple[str, str], _State] = defaultdict(_State)

def _key(symbol: str, timeframe: str) -> tuple[str, str]:
    return (str(symbol).upper(), str(timeframe).upper())


ANY_SYMBOL = "*"
ANY_TIMEFRAME = "*"


# ======================================================================
# push_signal
# ======================================================================

def push_signal(
    symbol: str,
    timeframe: str,
    direction: Optional[int],
    indicator: Optional[str] = None,
    next_timestamp: Optional[datetime] = None,
    timestamp: Optional[datetime] = None,
) -> None:
    """
    Положить новое сообщение сигнала:
      direction: 1 (up), 2 (down), None — очистка состояния.
      indicator: имя источника сигнала.
      timestamp: время текущей свечи.
      next_timestamp: начало следующей свечи.
    Любой приход (включая None) повышает версию и будит всех ожидающих.
    """
    symbol = str(symbol).upper()
    timeframe = str(timeframe).upper()

    keys = {
        _key(symbol, timeframe),
        _key(ANY_SYMBOL, timeframe),
        _key(symbol, ANY_TIMEFRAME),
        _key(ANY_SYMBOL, ANY_TIMEFRAME),
    }

    async def _update_and_notify(st: _State):
        async with st.cond:
            st.version += 1
            st.value = direction if direction in (1, 2) else None
            st.last_monotonic = asyncio.get_running_loop().time()
            sec = _tf_to_seconds(timeframe)
            if sec:
                st.tf_sec = sec
            if indicator is not None:
                st.last_indicator = indicator
            st.last_symbol = symbol
            st.last_timeframe = timeframe
            st.next_timestamp = next_timestamp
            st.timestamp = timestamp
            st.cond.notify_all()

    loop = asyncio.get_running_loop()
    for k in keys:
        loop.create_task(_update_and_notify(_states[k]))


# ======================================================================
# helpers
# ======================================================================

async def _maybe_await(func: Optional[Callable[[], Awaitable[None] | None]]) -> None:
    """Безопасно вызывает функцию check_pause (sync или async)."""
    if func is None:
        return
    res = func()
    if asyncio.iscoroutine(res):
        await res


# ======================================================================
# wait_for_signal_versioned
# ======================================================================

async def wait_for_signal_versioned(
    symbol: str,
    timeframe: str,
    *,
    since_version: Optional[int] = None,
    check_pause: Optional[Callable[[], Awaitable[None] | None]] = None,
    timeout: Optional[float] = None,
    raise_on_timeout: bool = True,
    grace_delay_sec: float = 5.0,
    on_delay: Optional[Callable[[float], None]] = None,
    include_meta: bool = False,
    max_age_sec: float = 0.0,
) -> (
    Tuple[int, int]
    | Tuple[int, int, Dict[str, Optional[str | int | float | datetime]]]
):
    """
    Ждёт сигнал (up/down) с версией > since_version.
    По умолчанию возвращает (direction, version).
    Если include_meta=True — вернёт (direction, version, meta).
    """
    st = _states[_key(symbol, timeframe)]
    loop = asyncio.get_running_loop()
    start = loop.time()

    async def _await_next_change() -> Tuple[Optional[int], int]:
        async with st.cond:
            # быстрый путь: сигнал уже есть и свежий
            if (
                st.value in (1, 2)
                and (since_version is None or st.version > since_version)
                and (st.last_monotonic or 0) >= (start - float(max_age_sec))
            ):
                return st.value, st.version
            await st.cond.wait()
            return st.value, st.version

    while True:
        # обработка паузы
        try:
            await _maybe_await(check_pause)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

        # проверка задержки
        if callable(on_delay) and st.last_monotonic and st.tf_sec:
            expected_next = st.last_monotonic + float(st.tf_sec)
            now = loop.time()
            drift = now - expected_next
            if drift > float(grace_delay_sec):
                try:
                    on_delay(drift)
                except Exception:
                    pass

        try:
            if timeout is None:
                direction, ver = await _await_next_change()
            else:
                elapsed = loop.time() - start
                left = max(0.0, float(timeout) - elapsed)
                direction, ver = await asyncio.wait_for(_await_next_change(), timeout=left)
        except asyncio.TimeoutError:
            if raise_on_timeout:
                raise
            continue  # мягкий режим ожидания

        if (
            direction in (1, 2)
            and (since_version is None or ver > since_version)
            and st.last_monotonic
            and st.last_monotonic >= (start - float(max_age_sec))
        ):
            if include_meta:
                meta = {
                    "indicator": st.last_indicator,
                    "tf_sec": st.tf_sec,
                    "symbol": st.last_symbol,
                    "timeframe": st.last_timeframe,
                    "next_timestamp": st.next_timestamp,
                    "timestamp": st.timestamp,
                }
                return int(direction), int(ver), meta
            return int(direction), int(ver)
        # иначе ждём дальше


# ======================================================================
# wait_for_signal
# ======================================================================

async def wait_for_signal(
    symbol: str,
    timeframe: str,
    *,
    check_pause: Optional[Callable[[], Awaitable[None] | None]] = None,
    timeout: Optional[float] = None,
    raise_on_timeout: bool = True,
) -> int:
    """Упрощённая версия — ждёт следующий сигнал, игнорируя старые."""
    st = _states[_key(symbol, timeframe)]
    direction, _ = await wait_for_signal_versioned(
        symbol,
        timeframe,
        since_version=st.version,
        check_pause=check_pause,
        timeout=timeout,
        raise_on_timeout=raise_on_timeout,
    )
    return int(direction)


# ======================================================================
# peek_signal_state
# ======================================================================

def peek_signal_state(
    symbol: str,
    timeframe: str,
) -> Dict[str, Optional[int | float | str | datetime]]:
    """Неблокирующий доступ к текущему состоянию."""
    st = _states[_key(symbol, timeframe)]
    return {
        "version": int(st.version),
        "value": int(st.value) if st.value in (1, 2) else None,
        "indicator": st.last_indicator,
        "tf_sec": st.tf_sec,
        "last_monotonic": st.last_monotonic,
        "next_timestamp": st.next_timestamp,
        "timestamp": st.timestamp,
    }
