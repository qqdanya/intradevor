import asyncio

class StrategyBase:
    def __init__(self, session, user_id, user_hash, symbol, log_callback, **params):
        self.session = session
        self.user_id = user_id
        self.user_hash = user_hash
        self.symbol = symbol
        self.log = log_callback
        self.params = params
        self._running = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()

    async def run(self):
        raise NotImplementedError("Стратегия не реализована.")

    def stop(self):
        self._running = False
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    async def _pause_point(self):
        await self._pause_event.wait()

