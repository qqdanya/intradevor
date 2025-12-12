# trade_queue.py
"""Очередь для последовательного размещения ставок.

Важно:
- В очередь кладём только "быстрые" операции (обычно place_trade).
- Ожидание результата сделки (polling/check_trade_result) НЕ кладём в очередь.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Optional, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class TradeQueue:
    """Асинхронная очередь выполнения задач строго по одной."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[
            tuple[asyncio.Future[T], Callable[[], Awaitable[T]], float | None, str | None]
        ] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self._worker_task = asyncio.create_task(self._worker(), name="TradeQueueWorker")
            self._started = True

    async def stop(self) -> None:
        """Остановить обработчик очереди (жёстко)."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._started = False

    def size(self) -> int:
        return self._queue.qsize()

    async def enqueue(
        self,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
        name: str | None = None,
    ) -> T:
        """Добавить задачу в очередь и дождаться результата.

        timeout — ограничение ожидания результата (включая ожидание очереди).
        """
        self._ensure_started()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put((future, factory, timeout, name))

        if timeout is None:
            return await future

        # shield: если wait_for истёк, мы НЕ отменяем future внутри очереди
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)

    async def _worker(self) -> None:
        while True:
            future, factory, timeout, name = await self._queue.get()
            try:
                if future.cancelled():
                    continue

                # timeout здесь ограничивает именно выполнение factory()
                if timeout is None:
                    result = await factory()
                else:
                    result = await asyncio.wait_for(factory(), timeout=timeout)

                if not future.done():
                    future.set_result(result)

            except asyncio.TimeoutError as exc:
                if not future.done():
                    future.set_exception(exc)
                if name:
                    log.warning("TradeQueue task timed out: %s (timeout=%.2fs)", name, timeout)

            except asyncio.CancelledError:
                # Воркер отменили — пробрасываем дальше, future отменяем
                if not future.done():
                    future.cancel()
                raise

            except Exception as exc:  # noqa: BLE001
                if not future.done():
                    future.set_exception(exc)
                if name:
                    log.exception("TradeQueue task failed: %s", name)

            finally:
                self._queue.task_done()


trade_queue = TradeQueue()
"""Глобальная очередь для всех стратегий."""
