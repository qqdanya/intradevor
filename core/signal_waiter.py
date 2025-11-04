# core/signal_waiter.py
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional, Tuple, Dict, Callable, Awaitable
from datetime import datetime, timezone

# --- helpers ---------------------------------------------------------
def _tf_to_seconds(tf: str) -> Optional[int]:
    if not tf:
        return None
    tf = str(tf).upper()
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

# --- state -----------------------------------------------------------
@dataclass
class _State:
    value: Optional[int] = None  # 1=up, 2=down, None=нет сигнала
    version: int = 0  # монотонная версия (эпоха) для каждой пары
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    last_monotonic: Optional[float] = None  # когда пришёл ПОСЛЕДНИЙ сигнал (loop.time())
    tf_sec: Optional[int] = None  # длительность TF в секундах
    last_indicator: Optional[str] = None  # имя индикатора
    last_symbol: Optional[str] = None
    last_timeframe: Optional[str] = None
    next_timestamp: Optional[datetime] = None  # начало следующей свечи
    timestamp: Optional[datetime] = None  # время текущей свечи сигнала

_states: Dict[tuple[str, str], _State] = defaultdict(_State)

def _key(symbol: str, timeframe: str) -> tuple[str, str]:
    return (str(symbol).upper(), str(timeframe).upper())

ANY_SYMBOL = "*"
ANY_TIMEFRAME = "*"

# --- public api ------------------------------------------------------
def push_signal_if_fresh(
    symbol: str,
    timeframe: str,
    direction: Optional[int],
    indicator: Optional[str] = None,
    next_timestamp: Optional[datetime] = None,
    timestamp: Optional[datetime] = None,
    max_age_sec: float = 5.0
):
    """Пушим сигнал только если он свежий"""
    if timestamp is not None:
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if age > max_age_sec:
            # сигнал слишком старый — пропускаем
            return

    push_signal(symbol, timeframe, direction, indicator, next_timestamp, timestamp)


def push_signal(
    symbol: str,
    timeframe: str,
    direction: Optional[int],
    indicator: Optional[str] = None,
    next_timestamp: Optional[datetime] = None,
    timestamp: Optional[datetime] = None,
) -> None:
    """
    Положить НОВОЕ сообщение сигнала.
    Любой приход (включая None) повышает версию и будит всех ожидающих.
    """
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


async def _maybe_await(func: Callable[[], Awaitable[None] | None]) -> None:
    if func is None:
        return
    res = func()
    if asyncio.iscoroutine(res):
        await res


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
) -> Tuple[int, int] | Tuple[int, int, Dict[str, Optional[str | int | float | datetime]]]:
    st = _states[_key(symbol, timeframe)]
    start = asyncio.get_running_loop().time()

    async def _await_next_change() -> Tuple[Optional[int], int]:
        async with st.cond:
            if (
                st.value in (1, 2)
                and (since_version is None or st.version > since_version)
                and (st.last_monotonic or 0) >= (start - float(max_age_sec))
            ):
                return st.value, st.version
            await st.cond.wait()
            return st.value, st.version

    while True:
        try:
            await _maybe_await(check_pause)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

        if callable(on_delay) and st.last_monotonic is not None and st.tf_sec:
            expected_next = st.last_monotonic + float(st.tf_sec)
            now = asyncio.get_running_loop().time()
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
                elapsed = asyncio.get_running_loop().time() - start
                left = max(0.0, float(timeout) - elapsed)
                direction, ver = await asyncio.wait_for(
                    _await_next_change(), timeout=left
                )
        except asyncio.TimeoutError:
            if raise_on_timeout:
                raise
            continue

        if (
            direction in (1, 2)
            and (since_version is None or ver > since_version)
            and st.last_monotonic is not None
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


async def wait_for_signal(
    symbol: str,
    timeframe: str,
    *,
    check_pause: Optional[Callable[[], Awaitable[None] | None]] = None,
    timeout: Optional[float] = None,
    raise_on_timeout: bool = True,
) -> int:
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


def peek_signal_state(
    symbol: str, timeframe: str
) -> Dict[str, Optional[int | float | str | datetime]]:
    st = _states[_key(symbol, timeframe)]
    return {
        "version": int(st.version),
        "value": (int(st.value) if st.value in (1, 2) else None),
        "indicator": st.last_indicator,
        "tf_sec": st.tf_sec,
        "last_monotonic": st.last_monotonic,
        "next_timestamp": st.next_timestamp,
        "timestamp": st.timestamp,
    }
