"""Очередь для проверки результатов сделок."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Optional, TypeVar

from core.http_async import HttpClient
from core.intrade_api_async import check_trade_result

T = TypeVar("T")


class TradeResultQueue:
    """Асинхронная очередь для проверки результатов сделок.

    Задачи проверки результатов передаются в общий диспетчер и исполняются в
    отдельных фоновых задачах, поэтому ожидание ответа по одной сделке не
    блокирует остальные. Возвращает результат проверки обратно в вызывающий
    контекст.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[asyncio.Future[T], Callable[[], Awaitable[T]]]] = (
            asyncio.Queue()
        )
        self._worker_task: Optional[asyncio.Task] = None
        self._active_tasks: set[asyncio.Task] = set()
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self._worker_task = asyncio.create_task(self._worker())
            self._started = True

    async def stop(self) -> None:
        """Остановить обработчик и завершить ожидающие задачи."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        if self._active_tasks:
            for task in list(self._active_tasks):
                task.cancel()
            await asyncio.gather(*list(self._active_tasks), return_exceptions=True)
            self._active_tasks.clear()

        while not self._queue.empty():
            future, _ = self._queue.get_nowait()
            if not future.done():
                future.set_exception(asyncio.CancelledError())
            self._queue.task_done()
        self._started = False
        self._worker_task = None

    async def enqueue(self, factory: Callable[[], Awaitable[T]]) -> T:
        """Добавить задачу в очередь и дождаться результата."""
        self._ensure_started()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put((future, factory))
        return await future

    async def check_result(
        self,
        *,
        client: HttpClient,
        user_id: str,
        user_hash: str,
        trade_id: str,
        wait_time: float = 60.0,
        max_attempts: int = 60,
        initial_poll_delay: float = 1.0,
        backoff_factor: float = 1.5,
        max_poll_delay: float = 10.0,
    ) -> Optional[float]:
        """Поставить запрос проверки сделки в очередь."""

        return await self.enqueue(
            lambda: check_trade_result(
                client,
                user_id=user_id,
                user_hash=user_hash,
                trade_id=trade_id,
                wait_time=wait_time,
                max_attempts=max_attempts,
                initial_poll_delay=initial_poll_delay,
                backoff_factor=backoff_factor,
                max_poll_delay=max_poll_delay,
            )
        )

    async def _run_job(
        self, future: asyncio.Future[T], factory: Callable[[], Awaitable[T]]
    ) -> None:
        try:
            result = await factory()
        except asyncio.CancelledError:
            if not future.cancelled():
                future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - важно передать исключение вызывающему
            if not future.done():
                future.set_exception(exc)
        else:
            if not future.done():
                future.set_result(result)
        finally:
            self._queue.task_done()

    async def _worker(self) -> None:
        while True:
            future, factory = await self._queue.get()
            if future.cancelled():
                self._queue.task_done()
                continue

            task = asyncio.create_task(self._run_job(future, factory))
            self._active_tasks.add(task)
            task.add_done_callback(lambda t: self._active_tasks.discard(t))


trade_result_queue = TradeResultQueue()
"""Глобальная очередь проверки результатов сделок."""
