"""Асинхронный контейнер для работы стратегии бота."""

from __future__ import annotations

import asyncio
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
            try:
                self._strategy.stop()
            except Exception:
                pass
            raise
        finally:
            # аккуратно закрываем per-bot HttpClient, если он есть
            try:
                client = getattr(self._strategy, "http_client", None)
                if client is not None:
                    close_coro = getattr(client, "aclose", None) or getattr(
                        client, "close", None
                    )
                    if callable(close_coro):
                        await close_coro()
            except Exception:
                pass

            self._started = False
            self.on_finish()

    def stop(self) -> None:
        """Остановить стратегию и отменить асинхронную задачу."""
        if self._strategy:
            self._strategy.stop()
        if self._task and not self._task.done():
            self._task.cancel()

    def pause(self) -> None:
        """Поставить стратегию на паузу, если она поддерживает паузу."""
        if self._strategy:
            self._strategy.pause()

    def resume(self) -> None:
        """Возобновить стратегию после паузы."""
        if self._strategy:
            self._strategy.resume()

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
