from strategies.base import StrategyBase
from core.intrade_api import (
    get_current_percent,
    get_balance,
    place_trade,
    check_trade_result,
)
from core.signal_waiter import wait_for_signal
import asyncio

class MartingaleStrategy(StrategyBase):
    async def run(self):
        self._running = True
        await self._pause_point()

        base_investment = self.params.get("base_investment", 100)
        max_steps = self.params.get("max_steps", 5)
        repeat_count = self.params.get("repeat_count", 10)
        min_balance = self.params.get("min_balance", 100)
        coefficient = self.params.get("coefficient", 2.0)
        min_percent = self.params.get("min_percent", 70)
        wait_on_low_percent = self.params.get("wait_on_low_percent", 1)

        for repeat in range(repeat_count):
            if not self._running:
                self.log(f"[{self.symbol}] ⛔ Стратегия остановлена.")
                return

            self.log(f"\n🔁 [{self.symbol}] Запуск серии #{repeat + 1}/{repeat_count}")
            investment = base_investment
            step = 0

            while step < max_steps and self._running:
                await self._pause_point()
                balance = get_balance(self.session, self.user_id, self.user_hash)
                if balance is None:
                    self.log(f"[{self.symbol}] ⚠️ Не удалось получить баланс.")
                    return
                if balance < min_balance:
                    self.log(f"[{self.symbol}] 🛑 Баланс {balance:.2f} ниже минимального ({min_balance}).")
                    return

                percent = get_current_percent(self.session, investment, self.symbol)
                if percent is None:
                    self.log(f"[{self.symbol}] ⚠️ Не удалось получить процент.")
                    await asyncio.sleep(5)
                    continue

                while percent < min_percent and self._running:
                    await self._pause_point()
                    self.log(f"[{self.symbol}] ⏳ Процент {percent}% < {min_percent}%. Ждём {wait_on_low_percent} сек...")
                    await asyncio.sleep(wait_on_low_percent)
                    percent = get_current_percent(self.session, investment, self.symbol)
                    if percent is None:
                        self.log(f"[{self.symbol}] ⚠️ Не удалось получить процент при повторной проверке.")
                        await asyncio.sleep(5)

                if not self._running:
                    self.log(f"[{self.symbol}] ⛔ Остановлено до сигнала.")
                    return

                await self._pause_point()
                signal = await wait_for_signal(self.symbol)
                if signal in (1, 2):
                    status_code = signal

                trade_id = place_trade(self.session, self.user_id, self.user_hash, investment, self.symbol, status_code)
                if not trade_id:
                    self.log(f"[{self.symbol}] ❌ Не удалось открыть сделку.")
                    continue

                profit = await check_trade_result(self.session, self.user_id, self.user_hash, trade_id)
                if profit is None:
                    self.log(f"[{self.symbol}] ⚠️ Не удалось получить результат.")
                    continue

                if profit > 0:
                    self.log(f"[{self.symbol}] ✅ Профит: {profit:.2f}. Сброс ставки.")
                    investment = base_investment
                    step = 0
                    break
                else:
                    self.log(f"[{self.symbol}] ❌ Убыток: {profit:.2f}. Увеличиваем ставку.")
                    investment = min(investment * coefficient, 50000)
                    step += 1

            else:
                self.log(f"[{self.symbol}] 🚫 Достигнут лимит шагов ({max_steps}). Переход к следующей серии.")

