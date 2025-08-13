# strategies/martingale.py
import asyncio
from strategies.base import StrategyBase
from core.intrade_api import (
    get_current_percent,
    get_balance,
    place_trade,
    check_trade_result,
    get_account_currency,
)
from core.signal_waiter import wait_for_signal
from core.ws_client import last_signals


class MartingaleStrategy(StrategyBase):
    """
    Мартингейл для бинарных/опционных трейдов.
    Особенности:
      - Пауза/стоп мгновенно прерывают ожидания (через StrategyBase._stop_event).
      - Все длинные ожидания обёрнуты в cancellable sleep/wait.
      - Параметры читаются «вживую» на каждой итерации, можно менять на лету через update_params().
    Ожидаемые параметры (с дефолтами):
      base_investment: int/float = 100
      max_steps: int = 5
      min_balance: int/float = 100
      coefficient: float = 2.0
      min_percent: int = 60
      wait_on_low_percent: float = 1.0
      repeat_count: int = 10
      signal_timeout_sec: float = 3600  # максимум ожидания сигнала (1 час)
    """

    async def run(self):
        self._running = True
        account_ccy = self.get_param("account_currency") or get_account_currency(
            self.session, self.user_id, self.user_hash
        )
        self.params["account_currency"] = (
            account_ccy  # чтобы видеть/переиспользовать дальше
        )

        await self._pause_point()  # уважаем старт/паузу перед началом

        repeat = 0
        repeat_count = int(self.get_param("repeat_count", 10))

        while self._running and repeat < repeat_count:
            repeat += 1

            # читаем параметры «вживую» каждый цикл серии
            base_investment = self.get_param("base_investment", 100)
            max_steps = int(self.get_param("max_steps", 5))
            min_balance = self.get_param("min_balance", 100)
            coefficient = float(self.get_param("coefficient", 2.0))
            min_percent = int(self.get_param("min_percent", 60))
            wait_on_low_percent = float(self.get_param("wait_on_low_percent", 1.0))
            signal_timeout_sec = float(self.get_param("signal_timeout_sec", 3600))

            if not self._running:
                break

            self.log(f"\n🔁 [{self.symbol}] Запуск серии #{repeat}/{repeat_count}")
            investment = float(base_investment)
            step = 0

            while self._running and step < max_steps:
                await self._pause_point()

                # Баланс
                balance = get_balance(self.session, self.user_id, self.user_hash)
                if balance is None:
                    self.log(f"[{self.symbol}] ⚠️ Не удалось получить баланс.")
                    return
                if balance < min_balance:
                    self.log(
                        f"[{self.symbol}] 🛑 Баланс {balance:.2f} ниже минимума ({min_balance})."
                    )
                    return

                # Текущий процент
                percent = get_current_percent(self.session, investment, self.symbol)
                if percent is None:
                    self.log(f"[{self.symbol}] ⚠️ Не удалось получить процент.")
                    await self.sleep(5)
                    continue

                # Ждём, пока процент не станет приемлемым
                while self._running and percent < min_percent:
                    await self._pause_point()
                    # можно крутить параметры на лету
                    min_percent = int(self.get_param("min_percent", min_percent))
                    wait_on_low_percent = float(
                        self.get_param("wait_on_low_percent", wait_on_low_percent)
                    )
                    self.log(
                        f"[{self.symbol}] ⏳ Процент {percent}% < {min_percent}%. Ждём {wait_on_low_percent} сек..."
                    )
                    await self.sleep(wait_on_low_percent)
                    percent = get_current_percent(self.session, investment, self.symbol)
                    if percent is None:
                        self.log(
                            f"[{self.symbol}] ⚠️ Не удалось получить процент при повторной проверке."
                        )
                        await self.sleep(5)
                        # попробуем ещё раз на следующей итерации while
                        percent = get_current_percent(
                            self.session, investment, self.symbol
                        )
                        if percent is None:
                            continue

                if not self._running:
                    self.log(f"[{self.symbol}] ⛔ Остановлено до получения сигнала.")
                    return

                await self._pause_point()

                # Ожидаем сигнал (UP/DOWN = 1/2) с таймаутом
                try:
                    signal = await self.wait_cancellable(
                        wait_for_signal(self.symbol), timeout=signal_timeout_sec
                    )
                except asyncio.TimeoutError:
                    self.log(f"[{self.symbol}] ⏳ Таймаут ожидания сигнала.")
                    continue  # новая итерация того же шага

                if signal not in (1, 2):
                    self.log(
                        f"[{self.symbol}] ⚠️ Некорректный сигнал: {signal}. Пропуск."
                    )
                    continue

                # Берём длительность таймфрейма из last_signals
                value = last_signals.get(self.symbol)
                if value is None:
                    self.log(f"[{self.symbol}] ⚠️ Нет данных по длительности сигнала.")
                    continue
                _, _, time_delta = value  # ожидается timedelta

                # Открываем сделку
                trade_id = place_trade(
                    self.session,
                    self.user_id,
                    self.user_hash,
                    investment,
                    self.symbol,
                    signal,  # 1 или 2
                    str(int(time_delta.total_seconds() // 60)),  # минуты строкой
                    account_ccy=account_ccy,
                    strict=True,
                    on_log=self.log,
                )
                if not trade_id:
                    self.log(f"[{self.symbol}] ❌ Не удалось открыть сделку.")
                    continue

                # Ждём результат сделки не дольше её длительности + небольшая дельта
                try:
                    profit = await self.wait_cancellable(
                        check_trade_result(
                            self.session,
                            self.user_id,
                            self.user_hash,
                            trade_id,
                            time_delta.total_seconds(),
                        ),
                        timeout=time_delta.total_seconds() + 5,
                    )
                except asyncio.TimeoutError:
                    self.log(f"[{self.symbol}] ⚠️ Таймаут получения результата.")
                    continue

                if profit is None:
                    self.log(f"[{self.symbol}] ⚠️ Не удалось получить результат.")
                    continue

                if profit > 0:
                    self.log(f"[{self.symbol}] ✅ Профит: {profit:.2f}. Сброс ставки.")
                    # читаем текущее базовое значение на момент сброса
                    investment = float(
                        self.get_param("base_investment", base_investment)
                    )
                    step = 0
                    break  # новая серия
                else:
                    self.log(
                        f"[{self.symbol}] ❌ Убыток: {profit:.2f}. Увеличиваем ставку."
                    )
                    coefficient = float(self.get_param("coefficient", coefficient))
                    # ограничим рост «на всякий случай»
                    investment = min(investment * coefficient, 50_000)
                    step += 1

            else:
                # while завершился без break (достигнут лимит шагов)
                self.log(
                    f"[{self.symbol}] 🚫 Достигнут лимит шагов ({max_steps}). Переход к следующей серии."
                )

        self._running = False
        self.log(f"[{self.symbol}] ✅ Завершение работы стратегии Мартингейл.")
