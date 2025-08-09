import asyncio

class Bot:
    def __init__(self, strategy_cls, strategy_kwargs, on_log, on_finish):
        self.strategy_cls = strategy_cls
        self.strategy_kwargs = strategy_kwargs
        self.on_log = on_log
        self.on_finish = on_finish
        self._task = None
        self._strategy = None

    def start(self):
        self._strategy = self.strategy_cls(**self.strategy_kwargs)
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            await self._strategy.run()
        finally:
            self.on_finish()

    def stop(self):
        if self._strategy:
            self._strategy.stop()

    def pause(self):
        if self._strategy:
            self._strategy.pause()

    def resume(self):
        if self._strategy:
            self._strategy.resume()

    def is_running(self):
        return self._task and not self._task.done()

