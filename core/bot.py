# core/bot.py
import asyncio


class Bot:
    def __init__(self, strategy_cls, strategy_kwargs, on_log, on_finish):
        self.strategy_cls = strategy_cls
        self.strategy_kwargs = strategy_kwargs
        self.on_log = on_log
        self.on_finish = on_finish
        self._task = None
        self._strategy = None
        self._started = False

    def start(self):
        if self._started:
            return
        self._strategy = self.strategy_cls(**self.strategy_kwargs)
        self._task = asyncio.create_task(self._run())
        self._started = True

    async def _run(self):
        try:
            await self._strategy.run()
        except asyncio.CancelledError:
            try:
                self._strategy.stop()
            except Exception:
                pass
            raise
        finally:
            # NEW: аккуратно закрываем per-bot HttpClient, если он есть
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

            self.on_finish()
            self._started = False

    def stop(self):
        if self._strategy:
            self._strategy.stop()
        if self._task and not self._task.done():
            self._task.cancel()

    def pause(self):
        if self._strategy:
            self._strategy.pause()

    def resume(self):
        if self._strategy:
            self._strategy.resume()

    def is_running(self):
        return bool(self._task and not self._task.done())

    def has_started(self):
        return self._started

    @property
    def strategy(self):
        return self._strategy
