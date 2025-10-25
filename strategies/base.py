# strategies/base.py
from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Optional


class StrategyBase:
    def __init__(self, session, user_id, user_hash, symbol, log_callback, **params):
        self.session = session
        self.user_id = user_id
        self.user_hash = user_hash
        self.symbol = symbol
        self.log = log_callback
        self.params = params  # живой dict
        self.timeframe = self.params.get("timeframe", "M1")
        self._running = False

        # Пауза/стоп
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # не на паузе по умолчанию
        self._stop_event = asyncio.Event()

        # Задача стратегии (для статусов)
        self._task: Optional[asyncio.Task] = None

        # Очередь сигналов и обслуживающая задача
        self._signal_queue: Optional[asyncio.Queue] = None
        self._signal_listener_task: Optional[asyncio.Task] = None
        self._signal_listener_fetcher: Optional[
            Callable[[Optional[int]], Awaitable[tuple[int, int, Any]]]
        ] = None
        self._signal_listener_func_key: Optional[Any] = None
        self._signal_listener_config: Optional[Any] = None
        self._signal_queue_since_version: Optional[int] = None

    async def run(self):
        raise NotImplementedError("Стратегия не реализована.")

    # --- lifecycle ---
    def start(self):
        """Помечает стратегию как запущенную (сброс флагов), до реального run()."""
        self._running = True
        # сбрасываем флаги
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()

    def has_started(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_running(self) -> bool:
        return self._running and (self._task is None or not self._task.done())

    def _emit_status(self, text: str):
        cb = self.params.get("on_status")
        if callable(cb):
            try:
                cb(text)
            except Exception:
                pass

    def stop(self):
        # выставляем флаги и будим всех ждущих
        self._running = False
        self._stop_event.set()
        self._pause_event.set()
        self._emit_status("завершен")
        if self._signal_listener_task and not self._signal_listener_task.done():
            self._signal_listener_task.cancel()
        # отменяем задачу, если мы её создавали
        if self._task and not self._task.done():
            self._task.cancel()

    def pause(self):
        self._pause_event.clear()
        self._emit_status("пауза")

    def resume(self):
        self._pause_event.set()
        self._emit_status("ожидание сигнала")

    async def _pause_point(self):
        wait_pause = asyncio.create_task(self._pause_event.wait())
        wait_stop = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            {wait_pause, wait_stop},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if self._stop_event.is_set():
            raise asyncio.CancelledError

    # --- helpers: отменяемый сон и ожидание ---
    async def sleep(self, seconds: float) -> None:
        """Сон, который мгновенно прервётся по stop()."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            raise asyncio.CancelledError
        except asyncio.TimeoutError:
            return

    async def wait_cancellable(
        self, aw: Awaitable[Any], timeout: Optional[float] = None
    ) -> Any:
        """
        Ожидает aw с таймаутом и мгновенным прерыванием по stop().
        Под капотом: гонка (aw) vs (_stop_event.wait()).
        """
        task = asyncio.create_task(aw)
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {task, stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.TimeoutError

            if stop_task in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.CancelledError

            return await task
        finally:
            for t in (task, stop_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(task, stop_task, return_exceptions=True)

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def is_stopped(self) -> bool:
        return not self._running

    # --- live settings ---
    def update_params(self, **params):
        self.params.update(params)
        if hasattr(self, "log") and self.log:
            pretty = {
                k: (round(v, 8) if isinstance(v, float) else v)
                for k, v in params.items()
            }
            self.log(f"[{self.symbol}] ⚙ Параметры обновлены: {pretty}")

    def get_param(self, key, default=None):
        return self.params.get(key, default)

    # --- signal queue helpers -------------------------------------------------

    def _signal_queue_capacity(self) -> int:
        """Максимальный размер очереди сигналов."""
        return 32

    def _handle_signal_listener_error(self, exc: Exception) -> None:
        cb = getattr(self, "log", None)
        if callable(cb):
            try:
                cb(f"[{self.symbol}] ⚠ Ошибка очереди сигналов: {exc}")
            except Exception:
                pass

    async def _cancel_signal_listener(self) -> None:
        if self._signal_listener_task:
            self._signal_listener_task.cancel()
            try:
                await asyncio.gather(
                    self._signal_listener_task, return_exceptions=True
                )
            except Exception:
                pass
        self._signal_listener_task = None
        self._signal_listener_fetcher = None
        self._signal_listener_func_key = None
        self._signal_listener_config = None
        self._signal_queue_since_version = None
        if self._signal_queue is not None:
            try:
                while True:
                    self._signal_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._signal_queue = None

    async def _signal_listener_loop(self) -> None:
        queue = self._signal_queue
        fetcher = self._signal_listener_fetcher
        since_version = self._signal_queue_since_version
        while True:
            if self._stop_event.is_set():
                break
            if fetcher is None or queue is None:
                break
            try:
                payload = await fetcher(since_version)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._handle_signal_listener_error(exc)
                await asyncio.sleep(0.5)
                continue

            if payload is None:
                continue

            direction, ver, meta = payload
            since_version = ver
            self._signal_queue_since_version = ver

            try:
                await queue.put(payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._handle_signal_listener_error(exc)
                await asyncio.sleep(0.1)

    async def _restart_signal_listener(
        self,
        fetcher: Callable[[Optional[int]], Awaitable[tuple[int, int, Any]]],
        *,
        func_key: Any,
        config_key: Any,
    ) -> None:
        if self._signal_listener_task and not self._signal_listener_task.done():
            self._signal_listener_task.cancel()
            try:
                await asyncio.gather(
                    self._signal_listener_task, return_exceptions=True
                )
            except Exception:
                pass

        capacity = max(0, int(self._signal_queue_capacity()))
        self._signal_queue = asyncio.Queue(maxsize=capacity)
        self._signal_listener_fetcher = fetcher
        self._signal_listener_func_key = func_key
        self._signal_listener_config = config_key
        self._signal_queue_since_version = getattr(self, "_last_signal_ver", None)
        self._signal_listener_task = asyncio.create_task(self._signal_listener_loop())

    async def _ensure_signal_listener(
        self,
        fetcher: Callable[[Optional[int]], Awaitable[tuple[int, int, Any]]],
        *,
        config_key: Any = None,
    ) -> None:
        func_key = getattr(fetcher, "__func__", fetcher)
        if (
            self._signal_listener_task
            and not self._signal_listener_task.done()
            and self._signal_listener_func_key is func_key
            and self._signal_listener_config == config_key
        ):
            return

        await self._restart_signal_listener(
            fetcher, func_key=func_key, config_key=config_key
        )

    async def _next_signal_from_queue(
        self, *, timeout: Optional[float] = None
    ) -> tuple[int, int, Any]:
        if self._signal_queue is None:
            raise RuntimeError("Signal queue is not initialized")
        coro = self._signal_queue.get()
        return await self.wait_cancellable(coro, timeout=timeout)
