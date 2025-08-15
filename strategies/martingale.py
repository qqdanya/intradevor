# strategies/martingale.py
from __future__ import annotations

import asyncio
from typing import Optional

from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance_info,
    get_current_percent,
    place_trade,
    check_trade_result,
)
from core.signal_waiter import wait_for_signal
from strategies.base import StrategyBase


DEFAULTS = {
    "base_investment": 100,  # базовая ставка
    "max_steps": 5,  # максимум колен
    "repeat_count": 10,  # сколько серий подряд пытаться
    "min_balance": 100,  # ниже не торгуем
    "coefficient": 2.0,  # множитель мартингейла
    "min_percent": 70,  # минимальный % выплаты
    "wait_on_low_percent": 1,  # пауза если % низкий (сек)
    "signal_timeout_sec": 3600,  # общий таймаут ожидания сигнала (сек)
    "account_currency": "RUB",  # валюта счёта
    "result_wait_s": 60.0,  # сколько ждать результата сделки (сек)
}


def _minutes_from_timeframe(tf: str) -> int:
    # "M1","M5","M15","M30","H1","H4","D1","W1" -> минуты для sprint
    if not tf:
        return 1
    unit = tf[0].upper()
    try:
        n = int(tf[1:])
    except Exception:
        return 1
    if unit == "M":
        return n
    if unit == "H":
        return n * 60
    if unit == "D":
        return n * 60 * 24
    if unit == "W":
        return n * 60 * 24 * 7
    return 1


class MartingaleStrategy(StrategyBase):
    """
    Совместима с Bot/MainWindow:
      MartingaleStrategy(http_client, user_id, user_hash, symbol, log_callback=None,
                         timeframe="M1", params=None, **_)
    Все сетевые операции — async, через core.intrade_api_async.
    """

    def __init__(
        self,
        http_client: HttpClient,
        user_id: str,
        user_hash: str,
        symbol: str,
        log_callback=None,
        *,
        timeframe: str = "M1",
        params: Optional[dict] = None,
        **_,
    ):
        # Объединим дефолты с пришедшими параметрами
        p = dict(DEFAULTS)
        if params:
            p.update(params)

        # StrategyBase хранит params и базовые эвенты паузы/стопа
        super().__init__(
            session=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            **p,
        )

        # для Bot.cleanup() — он ищет атрибут http_client и закрывает его
        self.http_client = http_client

        # таймфрейм сигнала (может отличаться от минут спринта)
        self.timeframe = timeframe or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe  # чтобы было видно в GUI

        # вычислим минуты для сделки: если пользователь явно не задал, берём из TF
        self._trade_minutes = int(
            self.params.get("minutes", _minutes_from_timeframe(self.timeframe))
        )

        # внутренняя метка
        self._running = False

    # ---- основной сценарий ----
    async def run(self):
        self._running = True
        log = self.log or (lambda s: None)

        # sanity: покажем баланс и валюту
        try:
            amount, ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(
                f"[{self.symbol}] Баланс: {display} ({amount:.2f}), валюта счёта: {ccy}"
            )
            # если GUI ещё не знает валюту — положим её в параметры для окна управления
            self.params.setdefault(
                "account_currency", ccy or self.params.get("account_currency")
            )
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось получить баланс при старте: {e}")

        series_left = int(self.params.get("repeat_count", DEFAULTS["repeat_count"]))

        while self._running and series_left > 0:
            # пауза/стоп-поинт
            await self._pause_point()

            # проверка лимита баланса
            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
            except Exception:
                bal = 0.0
            if bal < float(self.params.get("min_balance", DEFAULTS["min_balance"])):
                log(f"[{self.symbol}] ⛔ Баланс ниже минимума. Ожидание...")
                await self.sleep(2.0)
                continue

            # ждём сигнал 1/2 (up/down) для (symbol,timeframe)
            log(f"[{self.symbol}] ⏳ Ожидание сигнала на {self.timeframe}...")
            try:
                direction = await self.wait_signal(
                    timeout=float(
                        self.params.get(
                            "signal_timeout_sec", DEFAULTS["signal_timeout_sec"]
                        )
                    )
                )
            except asyncio.TimeoutError:
                log(f"[{self.symbol}] ⌛ Таймаут ожидания сигнала — новая попытка.")
                continue

            status = 1 if direction == 1 else 2  # 1=up/buy, 2=down/sell
            stake = float(
                self.params.get("base_investment", DEFAULTS["base_investment"])
            )
            coeff = float(self.params.get("coefficient", DEFAULTS["coefficient"]))
            max_steps = int(self.params.get("max_steps", DEFAULTS["max_steps"]))
            min_pct = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
            wait_low = float(
                self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"])
            )
            account_ccy = str(
                self.params.get("account_currency", DEFAULTS["account_currency"])
            )
            result_wait_s = float(
                self.params.get("result_wait_s", DEFAULTS["result_wait_s"])
            )

            # серия мартингейла до WIN или исчерпания шагов
            step = 0
            while self._running and step < max_steps:
                await self._pause_point()

                # текущий % выплаты
                pct = await get_current_percent(
                    self.http_client,
                    investment=stake,
                    option=self.symbol,
                    minutes=self._trade_minutes,
                    account_ccy=account_ccy,
                )
                if pct is None:
                    log(f"[{self.symbol}] ⚠ Не получили % выплаты. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue
                if pct < min_pct:
                    log(
                        f"[{self.symbol}] ℹ Низкий payout {pct}% < {min_pct}% — ждём {wait_low}s"
                    )
                    await self.sleep(wait_low)
                    continue

                log(
                    f"[{self.symbol}] step={step} stake={stake:.2f} min={self._trade_minutes} side={'UP' if status == 1 else 'DOWN'} payout={pct}%"
                )

                # сделка
                trade_id = await place_trade(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    investment=stake,
                    option=self.symbol,
                    status=status,
                    minutes=self._trade_minutes,
                    account_ccy=account_ccy,
                    strict=True,
                    on_log=log,
                )
                if not trade_id:
                    log(f"[{self.symbol}] ❌ Сделка не размещена. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue

                # ждём результат
                profit = await check_trade_result(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    trade_id=trade_id,
                    wait_time=result_wait_s,
                )

                if profit is None:
                    log(f"[{self.symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                    step += 1
                    stake *= coeff
                elif profit >= 0:
                    log(
                        f"[{self.symbol}] ✅ WIN: profit={profit:.2f}. Серия завершена, откат к базе."
                    )
                    break
                else:
                    log(
                        f"[{self.symbol}] ❌ LOSS: profit={profit:.2f}. Увеличиваем ставку."
                    )
                    step += 1
                    stake *= coeff

                await self.sleep(0.2)

            if step >= max_steps:
                log(
                    f"[{self.symbol}] 🛑 Достигнут лимит шагов ({max_steps}). Переход к новой серии."
                )
            series_left -= 1

        self._running = False
        (self.log or (lambda s: None))(f"[{self.symbol}] Завершение стратегии.")

    async def wait_signal(self, *, timeout: float) -> int:
        """
        Обёртка над core.signal_waiter.wait_for_signal с отменяемым ожиданием и таймаутом.
        Возвращает 1 (up) или 2 (down).
        """
        coro = wait_for_signal(self.symbol, self.timeframe, check_pause=self.is_paused)
        return int(await self.wait_cancellable(coro, timeout=timeout))

    # --- переопределим обновление параметров (тонкая настройка на лету) ---
    def update_params(self, **params):
        super().update_params(**params)
        # Если минуту задали явно — обновим кэш
        if "minutes" in params:
            try:
                self._trade_minutes = int(params["minutes"])
            except Exception:
                pass
        if "timeframe" in params:
            self.timeframe = str(params["timeframe"]) or self.timeframe
            # если minutes не задан — подстроим из TF
            if "minutes" not in params:
                self._trade_minutes = _minutes_from_timeframe(self.timeframe)
