# core/signal_waiter.py
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional, Tuple, Dict, Callable, Awaitable


# --- helpers ---------------------------------------------------------


def _tf_to_seconds(tf: str) -> Optional[int]:
    """'M1','M5','H1','D1','W1' -> длительность таймфрейма в секундах (если распознали)."""
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
    last_monotonic: Optional[float] = (
        None  # когда пришёл ПОСЛЕДНИЙ сигнал (loop.time())
    )
    tf_sec: Optional[int] = None  # длительность TF в секундах (если удалось распарсить)


_states: Dict[tuple[str, str], _State] = defaultdict(_State)


def _key(symbol: str, timeframe: str) -> tuple[str, str]:
    return (str(symbol).upper(), str(timeframe).upper())


# --- public api ------------------------------------------------------


def push_signal(symbol: str, timeframe: str, direction: Optional[int]) -> None:
    """
    Положить НОВОЕ сообщение сигнала:
      direction: 1 (up), 2 (down), None (или 0) — очистка состояния (none).
    Любой приход (включая None) повышает версию и будит всех ожидающих.
    Также запоминаем момент прихода (monotonic) и длительность TF (если распознали).
    """
    st = _states[_key(symbol, timeframe)]

    async def _update_and_notify():
        async with st.cond:
            st.version += 1
            st.value = direction if direction in (1, 2) else None
            st.last_monotonic = asyncio.get_running_loop().time()
            # запомним tf_sec один раз (или обновим, если распознали)
            sec = _tf_to_seconds(timeframe)
            if sec:
                st.tf_sec = sec
            st.cond.notify_all()

    asyncio.get_running_loop().create_task(_update_and_notify())


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
) -> Tuple[int, int]:
    """
    Ждёт ПЕРВЫЙ up/down с версией > since_version.
    none (очистка) лишь повышает версию, но не возвращается.
    Возвращает (direction, version), где direction ∈ {1,2}.
    Дополнительно: если следующее сообщение «запаздывает» дольше grace_delay_sec,
    вызываем on_delay(delay_seconds).
    """
    st = _states[_key(symbol, timeframe)]

    async def _await_next_change() -> Tuple[Optional[int], int]:
        async with st.cond:
            # быстрый путь
            if st.value in (1, 2) and (
                since_version is None or st.version > since_version
            ):
                return st.value, st.version
            # иначе ждём новое сообщение
            await st.cond.wait()
            return st.value, st.version

    start = asyncio.get_running_loop().time()

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

        # игнорируем очистки (None) и устаревшие версии
        if direction in (1, 2) and (since_version is None or ver > since_version):
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
    direction, _ = await wait_for_signal_versioned(
        symbol,
        timeframe,
        since_version=None,  # NB: стратегия, желающая избегать повторов, должна хранить версию сама
        check_pause=check_pause,
        timeout=timeout,
        raise_on_timeout=raise_on_timeout,
    )
    return int(direction)
