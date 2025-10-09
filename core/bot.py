"""Асинхронный контейнер для работы стратегии бота."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Optional, Type


class Bot:
    """Запускает стратегию в отдельной асинхронной задаче."""

    def __init__(
        self,
        strategy_cls: Type,
        strategy_kwargs: dict[str, Any],
        on_log: Callable[[str], None],
        on_finish: Callable[[], None],
    ) -> None:
        self.strategy_cls = strategy_cls
        self.strategy_kwargs = strategy_kwargs
        self.on_log = on_log
        self.on_finish = on_finish
        self._task: Optional[asyncio.Task] = None
        self._strategy: Optional[Any] = None
        self._started = False

    def start(self) -> None:
        """Создать экземпляр стратегии и запустить её."""
        if self._started:
            return
        self._strategy = self.strategy_cls(**self.strategy_kwargs)

        # Если у стратегии есть мягкая инициализация старта — дергаем
        init_start = getattr(self._strategy, "start", None)
        if callable(init_start):
            try:
                init_start()
            except Exception:
                pass

        self._task = asyncio.create_task(self._run())
        self._started = True

    async def _run(self) -> None:
        """Внутренний цикл исполнения стратегии."""
        try:
            await self._strategy.run()
        except asyncio.CancelledError:
            self._call_strategy_method("stop")
            raise
        finally:
            # аккуратно закрываем per-bot HttpClient, если он есть
            await self._close_http_client()

            self._started = False
            self.on_finish()

    def stop(self) -> None:
        """Остановить стратегию и отменить асинхронную задачу."""
        self._call_strategy_method("stop")
        if self._task and not self._task.done():
            self._task.cancel()

    def pause(self) -> None:
        """Поставить стратегию на паузу, если она поддерживает паузу."""
        self._call_strategy_method("pause")

    def resume(self) -> None:
        """Возобновить стратегию после паузы."""
        self._call_strategy_method("resume")

    def is_running(self) -> bool:
        """Проверить, выполняется ли задача стратегии."""
        return bool(self._task and not self._task.done())

    def has_started(self) -> bool:
        """Стартовал ли бот ранее."""
        return self._started

    @property
    def strategy(self) -> Optional[Any]:
        """Возвращает инстанс стратегии (если запущен)."""
        return self._strategy

    def _call_strategy_method(self, name: str) -> None:
        """Вызвать метод стратегии, если он существует и вызываем."""
        if not self._strategy:
            return
        method = getattr(self._strategy, name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass

    async def _close_http_client(self) -> None:
        """Закрыть HttpClient стратегии, если он задан."""
        if not self._strategy:
            return
        client = getattr(self._strategy, "http_client", None)
        if client is None:
            return

        close_callable = getattr(client, "aclose", None)
        if close_callable is None:
            close_callable = getattr(client, "close", None)

        if not callable(close_callable):
            return

        try:
            result = close_callable()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
