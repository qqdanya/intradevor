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
        self._running = False

        # Пауза/стоп
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # не на паузе по умолчанию
        self._stop_event = asyncio.Event()

    async def run(self):
        raise NotImplementedError("Стратегия не реализована.")

    # --- lifecycle ---
    def stop(self):
        # выставляем флаги и будим всех ждущих
        self._running = False
        self._stop_event.set()
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    async def _pause_point(self):
        # ждём конца паузы, но прервёмся мгновенно, если пришёл stop()
        done, _ = await asyncio.wait(
            {
                asyncio.create_task(self._pause_event.wait()),
                asyncio.create_task(self._stop_event.wait()),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._stop_event.is_set():
            raise asyncio.CancelledError

    # --- helpers: отменяемый сон и ожидание ---
    async def sleep(self, seconds: float) -> None:
        """Сон, который мгновенно прервётся по stop()."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            # если проснулись от stop_event — отменяемся
            raise asyncio.CancelledError
        except asyncio.TimeoutError:
            return  # нормальный таймаут сна

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
                # общий таймаут ожидания
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.TimeoutError

            if stop_task in done:
                # пришёл стоп — отменяем рабочую задачу
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.CancelledError

            # иначе завершился task
            return await task
        finally:
            for t in (task, stop_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(task, stop_task, return_exceptions=True)

    # --- live settings ---
    def update_params(self, **params):
        self.params.update(params)
        if hasattr(self, "log") and self.log:
            self.log(f"[{self.symbol}] ⚙ Параметры обновлены: {params}")

    def get_param(self, key, default=None):
        return self.params.get(key, default)
