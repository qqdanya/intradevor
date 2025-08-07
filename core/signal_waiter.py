import asyncio
from ws_client import last_signals

async def wait_for_signal(symbol):
    while True:
        direction, seconds_to_next = last_signals.get(symbol, ("none", 5))

        if direction == "none":
            print(f"[~] Нет сигнала для {symbol}, ждём {seconds_to_next}s")
            await asyncio.sleep(seconds_to_next)
        else:
            print(f"[→] Сигнал для {symbol}: {direction}")
            last_signals[symbol] = ("none", 0)
            return "1" if direction == "up" else "2"

        await asyncio.sleep(0.5)

