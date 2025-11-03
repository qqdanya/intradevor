# core/signal_waiter.py
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional, Tuple, Dict, Callable, Awaitable
from datetime import datetime

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
    tf_sec: Optional[int] = None  # длительность TF в секундах (если удалось распарсить)
    last_indicator: Optional[str] = None  # ИМЯ индикатора источника последнего сигнала
    # для wildcard-ожидателей запоминаем исходный символ/таймфрейм
    last_symbol: Optional[str] = None
    last_timeframe: Optional[str] = None
    next_timestamp: Optional[datetime] = None  # начало следующей свечи

_states: Dict[tuple[str, str], _State] = defaultdict(_State)

def _key(symbol: str, timeframe: str) -> tuple[str, str]:
    return (str(symbol).upper(), str(timeframe).upper())

ANY_SYMBOL = "*"
ANY_TIMEFRAME = "*"

# --- public api ------------------------------------------------------
def push_signal(
    symbol: str,
    timeframe: str,
    direction: Optional[int],
    indicator: Optional[str] = None,
    next_timestamp: Optional[datetime] = None,
) -> None:
    """
    Положить НОВОЕ сообщение сигнала:
      direction: 1 (up), 2 (down), None (или 0) — очистка состояния (none).
      indicator: строка-имя источника сигнала (например, "RSI(14)").
    Любой приход (включая None) повышает версию и будит всех ожидающих.
    Также запоминаем момент прихода (monotonic), длительность TF и имя индикатора.
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
            st.cond.notify_all()

    loop = asyncio.get_running_loop()
    for k in keys:
        loop.create_task(_update_and_notify(_states[k]))


async def _maybe_await(func: Callable[[], Awaitable[None] | None]) -> None:
    """Аккуратно вызвать check_pause: поддерживает sync и async, не роняет цикл без надобности."""
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
    # Если хотите НЕ завершаться на таймауте, а просто продолжить ждать — выставьте:
    raise_on_timeout: bool = True,
    # Детектор задержки следующего прогноза:
    grace_delay_sec: float = 5.0,
    on_delay: Optional[Callable[[float], None]] = None,  # callback(delay_seconds)
    # --- новое: вернуть вместе с direction/version ещё и meta (indicator, tf_sec, symbol, timeframe) ---
    include_meta: bool = False,
    max_age_sec: float = 0.0,
) -> Tuple[int, int] | Tuple[int, int, Dict[str, Optional[str | int | float]]]:
    """
    Ждёт ПЕРВЫЙ up/down с версией > since_version.
    none (очистка) лишь повышает версию, но не возвращается.
    По умолчанию возвращает (direction, version), где direction ∈ {1,2}.
    Если include_meta=True — вернёт (direction, version, meta),
    где meta = {"indicator": str|None, "tf_sec": int|None, "symbol": str|None, "timeframe": str|None}.
    max_age_sec > 0 позволяет использовать сигнал, пришедший не ранее чем
    max_age_sec секунд до вызова функции.
    """
    st = _states[_key(symbol, timeframe)]
    start = asyncio.get_running_loop().time()

    async def _await_next_change() -> Tuple[Optional[int], int]:
        async with st.cond:
            # быстрый путь — только если сигнал пришёл ПОСЛЕ start-max_age_sec
            if (
                st.value in (1, 2)
                and (since_version is None or st.version > since_version)
                and (st.last_monotonic or 0) >= (start - float(max_age_sec))
            ):
                return st.value, st.version
            # иначе ждём новое сообщение
            await st.cond.wait()
            return st.value, st.version

    while True:
        # пауза/стоп
        try:
            await _maybe_await(check_pause)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

        # --- детектор задержки следующего сигнала на базе monotonic и tf_sec ----
        if callable(on_delay) and st.last_monotonic is not None and st.tf_sec:
            expected_next = st.last_monotonic + float(st.tf_sec)
            now = asyncio.get_running_loop().time()
            drift = now - expected_next
            if drift > float(grace_delay_sec):
                try:
                    on_delay(drift)
                except Exception:
                    pass

        # --- ожидание события или таймаут ---------------------------------------
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
            # мягкий режим: крутимся дальше
            continue

        # игнорируем очистки, устаревшие версии и старые сигналы
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
                }
                return int(direction), int(ver), meta
            return int(direction), int(ver)
        # иначе ждём следующего уведомления


# Совместимый шорткат (без версий)
async def wait_for_signal(
    symbol: str,
    timeframe: str,
    *,
    check_pause: Optional[Callable[[], Awaitable[None] | None]] = None,
    timeout: Optional[float] = None,
    raise_on_timeout: bool = True,
) -> int:
    # Всегда ждём следующий сигнал, игнорируя уже полученные ранее.
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
    """
    Неблокирующий доступ к текущему состоянию: возвращает dict с полями:
      version, value (1/2/None), indicator (str|None), tf_sec (int|None), last_monotonic (float|None)
    """
    st = _states[_key(symbol, timeframe)]
    return {
        "version": int(st.version),
        "value": (int(st.value) if st.value in (1, 2) else None),
        "indicator": st.last_indicator,
        "tf_sec": st.tf_sec,
        "last_monotonic": st.last_monotonic,
        "next_timestamp": st.next_timestamp,
    }
