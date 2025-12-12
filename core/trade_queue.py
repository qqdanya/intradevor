"""Очередь для последовательного размещения ставок."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Optional, TypeVar

T = TypeVar("T")


class TradeQueue:
    """Асинхронная очередь выполнения ставок.

    Сервер обрабатывает запросы на сделку последовательно, поэтому одновременные
    запросы от нескольких стратегий приводят к лишним повторным попыткам.
    Очередь гарантирует, что запросы на размещение ставки выполняются по одному,
    сохраняя порядок поступления.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[asyncio.Future[T], Callable[[], Awaitable[T]]]] = (
            asyncio.Queue()
        )
        self._worker_task: Optional[asyncio.Task] = None
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self._worker_task = asyncio.create_task(self._worker())
            self._started = True

    async def stop(self) -> None:
        """Остановить фоновый обработчик очереди."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._started = False

    async def enqueue(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Добавить задачу в очередь и дождаться результата.

        factory вызывается только в одном экземпляре за раз, строго по очереди.
        """
        self._ensure_started()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put((future, factory))
        return await future

    async def _worker(self) -> None:
        while True:
            future, factory = await self._queue.get()
            if future.cancelled():
                self._queue.task_done()
                continue

            try:
                result = await factory()
            except asyncio.CancelledError:
                future.cancel()
                self._queue.task_done()
                raise
            except Exception as exc:  # noqa: BLE001 - важно передать исключение вызывающему
                future.set_exception(exc)
            else:
                future.set_result(result)

            self._queue.task_done()


trade_queue = TradeQueue()
"""Глобальная очередь для всех стратегий."""
