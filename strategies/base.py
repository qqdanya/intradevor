# strategies/base.py
from __future__ import annotations
import asyncio
from typing import Any, Awaitable, Optional


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
